"""ADK Runner 실행 → SSE 이벤트 변환.

`/chat/stream` 엔드포인트의 심장. `root_agent`를 ADK Runner로 돌리며 흘러나오는
이벤트(function call/response, partial/final text)를 프론트 계약(status/source/
delta/done/error)의 SSE 프레임으로 번역해 yield한다.

인용 환각 차단은 여기서 마무리된다: 스트림이 끝나면 이번 턴에 관측한 출처와
답변 본문의 `[n]` 마커를 대조(validate_citations)하고, 검증된 결과만 done
이벤트에 담는다.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.genai import types
from google.genai.errors import APIError

from yes24_agent.adk_stream import iter_adk_events
from yes24_agent.agent import (
    root_agent,
)
from yes24_agent.config import get_settings
from yes24_agent.event_translate import (
    _reconcile_sources,
    _sources_from_response,
    _status_for_call,
    _status_for_error,
    _status_for_result,
    project_public_source,
    project_source_ref,
)
from yes24_agent.postprocess import (
    build_done_payload,
    validate_citations,
)
from yes24_agent.rbti.persona import is_valid_code
from yes24_agent.session_service import (
    _POC_USER_ID,
    _get_session_lock,
    _get_session_service,
    _resolve_session,
)
from yes24_agent.sources import (
    get_sources,
    merge_source_records,
)
from yes24_agent.sse import (
    STREAM_ERROR_MESSAGE,
    sse_delta,
    sse_done,
    sse_error,
    sse_reset,
    sse_source,
    sse_status,
)
from yes24_agent.thought_translation import translate_thought_label

logger = logging.getLogger(__name__)

# Gemini 과부하/일시장애로 판정하는 HTTP 상태코드(반응형 폴백 트리거). 429=RESOURCE_EXHAUSTED
# (레이트리밋·쿼터), 503=UNAVAILABLE(과부하), 500/502/504=일시 서버 오류, 529=Overloaded.
# 400(bad request)·403·404 등 영구 오류는 제외 ─ 재시도해도 소용없어 정직 안내로 보낸다.
_OVERLOAD_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})


_MD_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MD_PIPE_RE = re.compile(r"^\s*\|.*\|\s*$")


def _markdown_block_is_open(text: str) -> bool:
    """마크다운 구조(코드펜스·표)가 아직 닫히지 않았을 **수 있는지** 판정한다.

    이 지점에서 텍스트를 임의 분할하면 열린 구조가 깨질 수 있다는 보수적 신호다.
    판정 술어는 `yes24_agent/static/lib/md.js`의 `canSplitAfter`와 같고(펜스 개수 홀짝, 마지막 내용
    줄이 표 행인지 — 동치는 tests/test_md_split_equivalence.py가 고정), 문구·키워드가
    아니라 라인 구조만 본다. 단 파이프 표는 GFM 문법상 빈 줄에서만 끝나므로 "마지막 줄이
    표 행"은 열림/닫힘을 확정하지 못한다 — 도구 경계의 예고 확정 가부는 held 내용까지 보는
    `_held_continues_open_block`이 판정한다.
    """
    if _fence_is_open(text):
        return True
    for line in reversed(text.split("\n")):
        if line.strip():
            return bool(_MD_PIPE_RE.match(line))
    return False


def _fence_is_open(text: str) -> bool:
    """코드펜스가 홀수 개라 아직 닫히지 않았는지 판정한다(라인 구조만 본다)."""
    return bool(sum(1 for line in text.split("\n") if _MD_FENCE_RE.match(line)) % 2)


def _held_continues_open_block(body: str, held_text: str) -> bool:
    """도구 경계의 held 텍스트가 **열린 마크다운 구조의 내용물**인지 판정한다.

    펜스가 열려 있으면 held는 코드 내용이다 — 예고로 빼면 코드가 훼손되므로 본문이다.
    파이프 표는 "마지막 줄이 표 행"만으로 열림/닫힘을 구분할 수 없으므로(GFM 표는 빈
    줄에서만 끝난다) held **자신이 표 행("|"로 시작)인지**로 가른다: 행이면 표의 연속이라
    본문으로 흘려야 하고, 산문이면 표는 그 앞에서 사실상 끝난 것이라 예고 확정(status행)이
    안전하다. 산문을 본문으로 흘리면 완결된 표의 마지막 행에 무개행 접합돼 답변 전체가
    표 행으로 빨려 들어간다(적대 검증 2026-07-23 실측 — 시퀀스 E).
    """
    if _fence_is_open(body):
        return True
    return _markdown_block_is_open(body) and held_text.lstrip().startswith("|")


def _event_text(event) -> str:
    """ADK 이벤트 content에서 **본문** 텍스트 파트만 손실 없이 이어붙인다(사고 파트 제외).

    `include_thoughts=True`면 ADK가 사고 요약을 `thought=True` 파트로 같은 content에 실어
    보낸다(streaming_utils.py에서 `_thought_text`를 별도 버퍼링한 뒤 집계 응답의 parts에
    본문과 나란히 담는다). 본문 소유권은 thought가 아닌 파트에만 있으므로 여기서 거른다 —
    이 함수가 partial delta 경로와 aggregated final 경로의 **공통 관문**이라, 이 한 줄이
    "사고 발화의 본문 혼입 금지"(원칙 4b)의 방어선 전체다. ADK 내부도 같은 구조 플래그로
    거른다(contents.py의 `if part.text and not part.thought`).
    """
    if not event.content or not event.content.parts:
        return ""
    return "".join(part.text or "" for part in event.content.parts if not part.thought)


def _event_thought_text(event) -> str:
    """ADK 이벤트에서 **사고 요약** 파트만 이어붙인다(_event_text의 여집합).

    include_thoughts=True면 벤더 사고 구간(첫 3~5초, 본문 파트 0)에 사고 요약이 먼저
    도착한다. 이 텍스트는 본문이 아니라 진행 타임라인(stage=thinking) 몫이다 — 첫 응답
    체감 침묵을 LLM 실생성 텍스트로 채운다(정적 라벨 금지 원칙과 양립하는 유일한 재료).
    """
    if not event.content or not event.content.parts:
        return ""
    return "".join(part.text or "" for part in event.content.parts if part.thought)


def _thought_status_labels(buffer: list[str], chunk: str, max_chars: int) -> list[str]:
    """사고 요약 청크를 buffer에 누적하고, 닫힌 문단의 **헤드라인만** 진행 라벨로 뽑는다.

    Gemini 사고 요약은 "**주제 헤드라인**\\n독백 본문…" 구조로 스트림된다. 독백 본문까지
    라벨로 내보내면 "답변이 모두 만족스러우며…" 같은 내부 독백이 사용자에게 그대로 노출돼
    소음이 된다(2026-07-23 사용자 지적) — 퍼플렉시티·Claude Code처럼 짧은 단계 제목만
    표시한다. 판정은 마크다운 구조(문단 첫 줄이 볼드로 감싸였는가)뿐, 문구 매칭 없음.
    헤드라인 없는 문단은 버린다(미리보기 채널 — 유실이 아니라 절제).
    """
    buffer.append(chunk)
    joined = "".join(buffer)
    *closed, tail = joined.split("\n\n")
    buffer[:] = [tail]
    labels = []
    for paragraph in closed:
        first_line = paragraph.strip().split("\n")[0].strip()
        if not (first_line.startswith("**") and first_line.endswith("**") and len(first_line) > 4):
            continue  # 헤드라인이 아닌 독백 본문 문단은 표시하지 않는다
        label = " ".join(first_line.strip("*").split())
        if len(label) > max_chars:
            # 단어 중간 절단("translatio")을 피해 상한 안의 마지막 공백에서 자른다.
            cut = label.rfind(" ", 0, max_chars)
            label = label[: cut if cut > 0 else max_chars] + "…"
        if label:
            labels.append(label)
    return labels


# 최후 방어 문구: 어떤 경로로도 done.text가 비면(모델 빈 응답·홀드 flush 누락 등) 빈 응답을
# 그대로 내보내지 않고 이 안내로 대체한다("빈 성공 위장 금지"의 사용자 노출 버전).
_EMPTY_RESPONSE_FALLBACK = (
    "죄송해요, 방금 답변을 제대로 만들지 못했어요. 질문을 한 번 더 보내주시겠어요?"
)

def _is_overloaded_error(exc: BaseException) -> bool:
    """Gemini API 과부하/일시장애(반응형 재시도 대상)인지 판정한다."""
    return isinstance(exc, APIError) and getattr(exc, "code", None) in _OVERLOAD_STATUS_CODES


def _finalize_answer(
    text: str,
    sources: list[dict],
    session_id: str,
) -> tuple[object, dict]:
    """평상·예외·타임아웃이 공통으로 거치는 최종 인용 조립기.

    유효 인용이 0건이어도 **확보된 본문을 폐기하지 않는다.** 과거 require_evidence 분기가
    본문을 정형 문구로 갈아끼웠는데, 2026-07-22 실측에서 캐치 0 · 오탐 14/14였다(40턴 중
    14턴에서 접지된 정답이 죽었고 창작은 0건). 정상 경로에서 그 근거로 삭제했으면서
    에러·타임아웃 경로에만 남겨두면 같은 결함이 드문 경로에서 계속 재발한다.
    """
    citation = validate_citations(text or "", sources)
    payload = build_done_payload(
        sources=sources,
        used_source_ids=citation.used_source_ids,
        session_id=session_id,
        supports=citation.supports,
    )
    payload["text"] = citation.text
    return citation, payload


def _citable_sources(turn_sources: list[dict], prior_sources: list[dict]) -> list[dict]:
    """인용 검증이 쓸 유효 id 집합 = **이번 턴 관측본 ∪ 세션 레지스트리**.

    모델이 참조하는 id는 `sources.py`의 세션 레지스트리(멀티턴 영속) 것인데, 검증에는
    이번 턴 스트림 관측본만 넘기고 있었다. 도구를 다시 부르지 않는 연속성 턴은 관측본이
    비어 유효 집합이 공집합이 되고, **정상 인용이 통째로 폐기됐다**(실측 재현 3/5,
    `removed=True sources=0`). 그 턴의 수치는 자기 턴1의 접지 수치와 바이트 단위로 같았고
    라이브 대조에서 전부 사실이었다 — 창작이 아니라 대화 연속성이다.

    이 합집합은 마커를 **더 지우는 게 아니라 덜 지운다**: 삭제 집합이 기존의 진부분집합이라
    파괴 경로가 구조적으로 없다. 같은 id는 이번 턴 관측이 이긴다(레지스트리의 과거 상세·가격이
    새 관측을 덮지 못하게 — `_reconcile_sources` 주석의 규율을 그대로 유지).

    원칙 4는 불변이다. 여기서 넓히는 것은 **검증 집합**일 뿐이고, 공개 `source`·
    `done.sources`는 `build_done_payload`가 `used_source_ids`로 걸러 **이번 턴 최종 본문이
    실제로 인용한 것만** 싣는다 — 인용되지 않은 레지스트리 출처는 카드로 새지 않는다.
    """
    by_id: dict[int, dict] = {}
    for source in (*prior_sources, *turn_sources):
        source_id = source.get("id")
        if source_id is not None:
            by_id[source_id] = source
    return [by_id[key] for key in sorted(by_id)]


def _best_effort_text(pending: list[str], final_text: str) -> str:
    """실패 시점까지 확보한 본문을 정상 경로와 같은 규약으로 조립한다.

    스트림 후처리(재조립·인용 검증) 어디서 터져도, 모델이 이미 만들어 낸
    본문을 버리지 않는다("비파괴"를 특정 구간이 아니라 파이프라인 전체의 계약으로). aggregate
    final response를 우선하고, 없을 때만 대기 버퍼를 조립한다. 확보된 본문이 없으면 빈 문자열을
    반환해 호출부의 최후 방어가 채우게 둔다.
    """
    # `pending`은 도구 경계에서 예고 세그먼트가 제거된 뒤의 본문이다(예고는 status로 갔다).
    # 그래서 이 값이 곧 화면에 흐른 본문이고 done.text와 같아야 한다(원칙 4b).
    # partial이 하나도 없었던 경우에만 aggregate final로 폴백한다.
    return "".join(pending) or final_text


def _ensure_substantive_text(payload: dict) -> bool:
    """빈 본문을 최후 방어 안내로 대체한다(대체했으면 True).

    정상·타임아웃·예외 **세 경로가 같은 판정을 쓴다**. 정상 경로에만 두면 드문 실패
    경로에서 같은 결함이 계속 재발한다(require_evidence가 그랬듯이).

    옛 _preface_only 가드(마지막 도구 경계 이후 텍스트 없음 → 예고뿐 판정)는 삭제했다 —
    held 설계 이후 예고는 확정 시 pending에 들어가지 않아, 발동 가능한 유일한 케이스가
    경계 **앞**에 흘린 실본문을 폴백으로 갈아끼우는 파괴 경로뿐이었다(전수 리뷰 2026-07-23).

    남는 구멍 하나는 정직하게 적는다: 예고 partial과 도구 호출 이벤트 **사이**에서 스트림이
    죽으면(call 이벤트 미도착) closeout이 held를 본문으로 방류해 "…볼게요."만 담긴 done이
    나갈 수 있다. call 이벤트가 없어 그 텍스트가 예고인지 짧은 실본문인지 **구조적으로 판별
    불가능**하므로, 비파괴 원칙상 보존이 폐기보다 낫다(옛 가드도 preface_mark>0 조건 탓에
    이 케이스를 못 잡았다 — 적대 검증 2026-07-23 결함 3).
    """
    if not (payload.get("text") or "").strip():
        payload["text"] = _EMPTY_RESPONSE_FALLBACK
        return True
    return False


def _emit_final_body(streamed_text: str, payload: dict) -> list[str]:
    """이미 흘려보낸 본문과 정본을 대조해 마감 프레임을 만든다.

    정상·타임아웃·예외 **세 경로가 같은 판정을 쓴다**. v2 초안은 오류 경로 두 곳에서
    가드 없이 본문을 다시 보내 화면에 같은 답이 두 번 그려졌고(프론트 finalize는
    errored면 조기 반환해 자가치유도 못 한다) "delta 합계 == done.text"(원칙 4b)가
    깨졌다 — 판정을 세 벌 두면 한 벌만 고치는 실수가 반복되므로 한 곳으로 모은다.
    """
    remaining = payload.get("text") if isinstance(payload.get("text"), str) else ""
    if not streamed_text:
        return [sse_delta(remaining)] if remaining else []
    if streamed_text == remaining:
        return []
    # 인용 검증이 본문을 바꿨다. 이어붙이기로는 합계를 맞출 수 없으므로(짧아졌을 수도 있다)
    # 흘린 것을 무르고 정본을 다시 보낸다.
    return [sse_reset()] + ([sse_delta(remaining)] if remaining else [])


def _closeout_error_frames(
    error_text: str,
    *,
    pending: list[str],
    held: list[str] | None,
    final_text: str,
    observed_sources: list[dict],
    prior_sources: list[dict],
    active_model: str,
    session_id: str,
) -> list[str]:
    """타임아웃·예외 두 실패 경로가 공유하는 마감 시퀀스를 프레임 목록으로 조립한다.

    순서는 기존 두 경로와 동일: sse_error → held flush(delta) → source* → 본문 → done.
    held 조각은 pending에 append(뮤테이션 — 모든 append 옆에 delta 프레임이 있어
    "pending == 흘려보낸 조각" 불변식 유지)하고, held=None 재바인딩은 호출부가 수행한다.
    출처는 정상 경로와 동일하게 _reconcile_sources를 거친다 — 과거 두 실패 경로만 raw
    observed_sources를 넘겨 병렬 도구 유실 보정이 빠지는 드리프트가 있었다(판정을 세 벌
    두면 한 벌만 고치는 실수가 반복된다는 _emit_final_body docstring의 실례).
    """
    frames = [sse_error(error_text)]
    if held:
        tail = "".join(held)
        if tail:
            pending.append(tail)
            frames.append(sse_delta(tail))
    best_effort = _best_effort_text(pending, final_text)
    _, error_done = _finalize_answer(
        best_effort,
        _citable_sources(_reconcile_sources(observed_sources), prior_sources),
        session_id,
    )
    error_done["model"] = active_model
    if _ensure_substantive_text(error_done):
        logger.warning(
            "실패 경로의 본문이 비어 최후 방어 안내로 대체합니다(session_id=%s).", session_id
        )
    for source in error_done.get("sources", []):
        frames.append(sse_source(source))
    frames.extend(_emit_final_body("".join(pending), error_done))
    frames.append(sse_done(error_done))
    return frames


async def run_agent_stream(
    message: str, session_id: str | None, rbti: str | None = None
) -> AsyncIterator[str]:
    """사용자 메시지 1건을 처리하며 SSE 프레임 문자열을 순서대로 yield한다.

    이벤트 순서 계약: status·delta가 도착 순서대로 섞여 흐르고, 마지막에
    source → (reset →) delta → done으로 마감한다. 도구 응답 시점의 status는
    `refs`(마커 렌더용 id·url 힌트)를 실어 본문 [n]보다 먼저 도착하고, 검증을 통과한
    **최종 인용 출처만** source·done.sources로 나간다(원칙 4). 어떤 예외가 나도
    제너레이터가 예외로 죽지 않고 error+done을 흘려보낸 뒤 정상 종료한다.
    """
    settings = get_settings()
    # 같은 session_id 동시 요청을 순차화한다(입력 id 기준). 신규 세션(None)은 create_session이
    # 고유 id를 부여하므로 충돌하지 않아 락이 불필요하다.
    lock = _get_session_lock(session_id) if session_id else None
    if lock is not None:
        await lock.acquire()
    try:
        # 세션 서비스 생성(디렉토리 실패 등)과 세션 조회/생성 실패는 스트림을 시작조차
        # 못 하는 상황 — error+done으로 알리고 종료한다. DB 락(OperationalError)·디렉토리
        # 오류(OSError) 등 어떤 예외가 나도 "done 정확히 1회" 불변식을 지킨다.
        try:
            service = _get_session_service()
            session = await _resolve_session(service, session_id)
            # 현재 UI 요청을 RBTI state의 정본으로 본다. 유효 코드는 저장하고 None·무효 코드는
            # 기존 값을 명시적으로 지워, 이전 요청의 페르소나가 다음 기본 채팅에 남지 않게 한다.
            code = rbti if is_valid_code(rbti) else None
            if session.state.get("rbti") != code:
                await service.append_event(
                    session,
                    Event(author="system", actions=EventActions(state_delta={"rbti": code})),
                )
        except Exception as exc:  # noqa: BLE001 — 스트림 시작 전 방어선(done 1회 불변식 보장)
            logger.exception("세션 준비 실패: %s", exc)
            error_text = "대화 세션을 준비하지 못했어요. 잠시 후 다시 시도해 주세요."
            yield sse_error(error_text)
            _, error_done = _finalize_answer(
                error_text,
                [],
                session_id or "",
            )
            error_done["model"] = None
            yield sse_delta(error_done["text"])
            yield sse_done(error_done)
            return

        resolved_session_id = session.id
        # 이전 턴들이 남긴 출처 레지스트리(멀티턴 영속). 턴 시작 시점에 고정해 두고 인용
        # **검증 집합**에만 합친다(_citable_sources). 공개 채널은 원칙 4대로 인용분만.
        prior_sources = get_sources(session.state)


        # 스트림에서 관찰한 출처를 누적한다. 병렬 도구 실행 시 세션 state가 유실될 수
        # 있어(_reconcile_sources 참고), done 조립의 유실 방지용 완전한 사본으로 쓴다.
        observed_sources: list[dict] = []
        # 이번 턴에 도구가 돈 횟수. 최후 방어 실패 로그(tools=%d)의 진단값으로만 쓴다.
        tool_call_count = 0
        # 화면에 흘린 본문 조각. **모든 append 옆에 sse_delta가 있다** — 이 불변식이
        # "pending == 흘려보낸 조각"을 만들고, 마감의 4b 대조(_emit_final_body)가 그 위에 선다.
        pending: list[str] = []
        # 예고 후보 버퍼. 각 도구 경계에서 열리고(모델은 매 도구 호출 직전에 예고 한 문단을
        # 쓴다), 도구 호출이 오면 예고로 확정돼 status로 간다. 문단이 닫힌 뒤에도 텍스트가
        # 오거나 예고 길이를 넘으면 본문으로 확정돼 **순서대로** 흘리고 None(홀드 종료)이 된다.
        # 전량 홀드는 금지 — 토큰 스트리밍이 통째로 죽는다(실측: 1,779자가 12.4초 무출력).
        held: list[str] | None = []
        # 사고 요약 누적 버퍼(_thought_status_labels). 닫힌 문단만 타임라인으로 나가고
        # 미완 꼬리는 여기 남는다 — 미리보기 채널이라 스트림 종료 시 꼬리는 버려도 된다.
        thought_buf: list[str] = []
        # 진행 중인 사고 라벨 번역 task들. 본류를 막지 않도록 병행시키고, 완료분은 다음
        # 이벤트 대기와 **병합**해 즉시 흘린다. 스트림이 끝나면 미완분은 취소한다 —
        # 본문이 이미 흐른 뒤 도착하는 진행 라벨은 표시 순서를 어지럽힐 뿐이다.
        thought_tasks: set[asyncio.Task] = set()
        event_task: asyncio.Task | None = None

        final_text = ""
        emitted_output = False
        retried_overload = False

        run_config = RunConfig(
            streaming_mode=StreamingMode.SSE,
            max_llm_calls=settings.max_llm_calls,
        )
        # 단일 pro 경로: flash/pro 하이브리드 라우팅도 사전 질의분류기도 폐기했다. 강한 pro
        # 단일 루프가 질문 자체를 보고 도구를 스스로 당기므로 사용자 원문을 그대로 넘기고,
        # 난도별 추론량 조절은 thinking_budget(Gemini 동적 추론)에 위임한다.
        active_model = str(root_agent.model)

        runner = Runner(
            agent=root_agent,
            app_name=settings.app_name,
            session_service=service,
        )
        new_message = types.Content(role="user", parts=[types.Part(text=message)])

        # 이벤트 간격에 sse_timeout_s 상한을 건다. ADK 스트림은 하나의 고정 task가 소비해
        # 여러 yield에 걸친 OpenTelemetry context의 소유권을 보존한다.
        event_stream = runner.run_async(
            user_id=_POC_USER_ID,
            session_id=resolved_session_id,
            new_message=new_message,
            run_config=run_config,
        )
        timed_event_stream = iter_adk_events(event_stream, timeout_s=settings.sse_timeout_s)

        try:
            while True:
                while True:
                    try:
                        if thought_tasks:
                            # 번역 완료분을 본류 이벤트와 병합해 흘린다. 번역이 본류를
                            # 기다리게 하지도(무차단), 본류가 번역을 기다리게 하지도
                            # 않는다 — 어느 쪽이든 먼저 끝난 것부터 나간다.
                            event_task = asyncio.ensure_future(
                                timed_event_stream.__anext__()
                            )
                            while thought_tasks and not event_task.done():
                                completed, _ = await asyncio.wait(
                                    {event_task, *thought_tasks},
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                for task in list(thought_tasks):
                                    if task in completed:
                                        thought_tasks.discard(task)
                                        # 번역 실패는 translate_thought_label 내부에서
                                        # 원문 폴백으로 흡수된다(비파괴).
                                        yield sse_status("thinking", task.result())
                            event = await event_task
                            event_task = None
                        else:
                            event = await timed_event_stream.__anext__()
                    except StopAsyncIteration:
                        break
                    except APIError as exc:
                        if (
                            settings.error_fallback
                            and not retried_overload
                            and not emitted_output
                            and _is_overloaded_error(exc)
                        ):
                            logger.warning(
                                "Gemini 과부하/일시장애(code=%s) 감지 → 같은 메시지로 재시도"
                                "(session_id=%s).",
                                getattr(exc, "code", "?"),
                                resolved_session_id,
                            )
                            await timed_event_stream.aclose()
                            await event_stream.aclose()
                            retried_overload = True
                            pending = []
                            # 죽은 스트림이 남긴 예고 후보도 버린다 — 새 스트림의 홀드에
                            # 이전 시도의 조각이 섞이면 안 된다(emitted_output=False 조건이
                            # 보장하듯 화면에 나간 적 없는 텍스트라 유실이 아니다).
                            held = []
                            thought_buf = []
                            for task in thought_tasks:
                                task.cancel()
                            thought_tasks.clear()
                            final_text = ""
                            retry_runner = Runner(
                                agent=root_agent,
                                app_name=settings.app_name,
                                session_service=service,
                            )
                            event_stream = retry_runner.run_async(
                                user_id=_POC_USER_ID,
                                session_id=resolved_session_id,
                                new_message=new_message,
                                run_config=run_config,
                            )
                            timed_event_stream = iter_adk_events(
                                event_stream, timeout_s=settings.sse_timeout_s
                            )
                            continue
                        raise

                    if not event.partial and event.get_function_calls():
                        if held:
                            if _held_continues_open_block("".join(pending), "".join(held)):
                                # held가 열린 블록의 내용물(코드·표 행)이다. status로 빼면
                                # 블록이 깨지고, 버리면 유실이다(전수 리뷰 2026-07-23) —
                                # 문서 순서대로 본문에 흘리는 것만이 비파괴다.
                                tail = "".join(held)
                                if tail:
                                    pending.append(tail)
                                    emitted_output = True
                                    yield sse_delta(tail)
                            else:
                                # 도구 호출이 왔다 = 앞 텍스트는 예고로 확정.
                                # 화면에 안 나갔으므로 **되돌릴 reset이 필요 없다** —
                                # 과거 전량 홀드는 최종 본문까지 잡아 토큰 스트리밍을
                                # 죽였는데(1,779자 12.4초 무출력), 여기서는 도구 호출 직전
                                # 한 문단만 잡고 그 뒤로는 즉시 흘린다.
                                label = " ".join("".join(held).split())
                                if label:
                                    emitted_output = True
                                    yield sse_status(
                                        "preface", label[: settings.status_detail_max_chars]
                                    )
                        # 홀드를 다시 연다. 예고는 **매 도구 호출 직전**에 오므로(실측: 모델은
                        # 첫 라운드엔 텍스트 없이 도구를 부르고, 라운드 2+에서 예고를 쓴다)
                        # 첫 경계에서 홀드를 닫으면 그 뒤 예고가 전부 본문으로 샌다.
                        held = []
                        for call in event.get_function_calls():
                            status = _status_for_call(call)
                            if status is None:
                                continue  # 알릴 진행이 없는 도구는 조용히 지나간다
                            emitted_output = True
                            yield sse_status(*status)
                        continue

                    responses = event.get_function_responses()
                    if responses:
                        for resp in responses:
                            payload = resp.response or {}
                            tool_call_count += 1
                            if payload.get("status") == "error":
                                stage, detail = _status_for_error(payload)
                                emitted_output = True
                                yield sse_status(stage, detail)
                                continue
                            count = payload.get("result_count")
                            result = _status_for_result(count) if isinstance(count, int) else None
                            if result is not None:
                                emitted_output = True
                                yield sse_status(*result)
                            refs: list[dict] = []
                            for source in _sources_from_response(payload):
                                source_id = source.get("source_id")
                                source_event = project_public_source(source)
                                for index, observed in enumerate(observed_sources):
                                    if observed.get("id") == source_id:
                                        observed_sources[index] = merge_source_records(
                                            observed, source_event
                                        )
                                        break
                                else:
                                    observed_sources.append(source_event)
                                refs.append(project_source_ref(source_event))
                            # 마커 렌더용 id 힌트를 **본문보다 먼저** 흘린다. 여기서 안 보내면
                            # 프론트는 [n]이 실재 인용인지 몰라 done 직전까지 생 대괄호로 둔다.
                            # detail이 비어 있어 진행 단계 행은 만들지 않는다(보이지 않는 프레임).
                            # emitted_output도 올리지 않는다 — 사용자에게 나간 출력이 아니다.
                            if refs:
                                yield sse_status("refs", "", refs=refs)
                        continue

                    if event.partial:
                        # 사고 요약은 본문보다 먼저 도착한다(벤더 사고 구간) — 닫힌 문단마다
                        # 진행 타임라인으로 흘려 첫 응답 침묵을 채운다. emitted_output은
                        # 올리지 않는다: 미리보기 채널이라 과부하 재시도 가능성을 보존한다
                        # (재시도하면 새 스트림의 사고가 이어 붙는 것이 자연스럽다).
                        thought_chunk = _event_thought_text(event)
                        if thought_chunk:
                            for label in _thought_status_labels(
                                thought_buf, thought_chunk, settings.status_detail_max_chars
                            ):
                                # 번역을 본류와 병행시킨다(위 병합 대기가 완료분을 흘림).
                                thought_tasks.add(
                                    asyncio.create_task(translate_thought_label(label))
                                )
                        chunk = _event_text(event)
                        if chunk:
                            if held is None:
                                pending.append(chunk)
                                emitted_output = True
                                yield sse_delta(chunk)
                            else:
                                # 예고 후보 구간: 아직 흘리지 않는다. 도구 호출이 오면
                                # 예고로 확정돼 status로 가고 본문에서 빠지며, 도구 없이
                                # 끝나면 최종 본문이므로 마감에서 한 번에 나간다.
                                # **화면에 안 나갔으므로 되돌릴 reset도 필요 없다.**
                                held.append(chunk)
                                # 예고는 **한 문단**이고, 그 뒤엔 도구 호출이 온다(텍스트가
                                # 아니라). 그러므로 문단이 닫힌 뒤에도 텍스트가 계속 오면
                                # 그건 예고가 아니라 본문이다 — 홀드분을 **순서대로 먼저**
                                # 흘리고 홀드를 푼다. 순서를 뒤집으면 4b가 깨진다(실측).
                                # 전량을 잡으면 토큰 스트리밍이 죽는다(2,388자 delta 1개).
                                buffered = "".join(held)
                                # 선행 개행은 문단 "닫힘"이 아니라 라운드 시작의 여백이다 —
                                # 이를 닫힘으로 읽으면 cut=0·후속 텍스트 존재로 즉시 본문
                                # 확정돼 예고 분리가 통째로 무력화된다(적대 검증 C').
                                content_at = len(buffered) - len(buffered.lstrip("\n"))
                                cut = buffered.find("\n\n", content_at)
                                # 문단 뒤 "텍스트"는 실질 내용만 친다 — 공백·개행만 담긴
                                # 후행 청크로 예고가 본문으로 새지 않도록(diff 리뷰 지적).
                                if (cut >= 0 and buffered[cut + 2 :].strip()) or (
                                    len(buffered) > settings.status_detail_max_chars
                                ):
                                    # 문단이 닫힌 **뒤에도** 텍스트가 왔거나, 예고 길이
                                    # 상한을 넘었다 = 이 라운드는 예고가 아니라 본문이다.
                                    # 순서대로 전량 흘리고 홀드를 푼다.
                                    # ① 첫 문단만 계속 잡으면 종료 시 tail flush가 그
                                    #    문단을 말미에 붙여 문서 순서가 파괴되고(전수 리뷰
                                    #    2026-07-23 — "## 결론" 서두가 답변 맨 뒤로 감),
                                    # ② 닫힌 문단을 길이 무제한으로 잡으면 장문 실질
                                    #    문단이 예고로 확정돼 120자 절단·본문 소실된다
                                    #    (적대 검증 결함 2 — 상한은 닫힘 여부와 무관하게
                                    #    대칭이어야 청크 경계 비결정 파괴가 없다).
                                    held = None
                                    rest = buffered
                                elif cut >= 0:
                                    # 상한 이내의 한 문단이 닫힌 상태 = 예고 후보로 계속
                                    # 잡아 둔다. 다음 이벤트가 도구 호출이면 예고로 확정된다.
                                    held[:] = [buffered]
                                    rest = ""
                                else:
                                    rest = ""
                                if rest:
                                    pending.append(rest)
                                    emitted_output = True
                                    yield sse_delta(rest)
                        continue

                    if event.is_final_response():
                        text = _event_text(event)
                        if text:
                            final_text = text

                if held:
                    # 도구 호출 없이 끝났다 = 홀드분은 예고가 아니라 본문이다.
                    # 아직 화면에 안 나갔으므로 delta로도 함께 내보낸다(4b).
                    tail = "".join(held)
                    held = None
                    if tail:
                        pending.append(tail)
                        emitted_output = True
                        yield sse_delta(tail)
                answer_text = _best_effort_text(pending, final_text)
                # 근거 스냅샷은 세션 state가 아니라 **스트림 관찰본**으로 만든다. ADK의
                # deep_merge_dicts가 병렬 도구의 state_delta를 리스트 키에서 last-wins로
                # 덮어써 한 도구의 출처가 통째로 유실될 수 있기 때문이다(_reconcile_sources
                # 주석 참조). 이 유실은 상류 미수정이라 여기서 계속 우회한다.
                sources = _citable_sources(_reconcile_sources(observed_sources), prior_sources)

                citation, done_payload = _finalize_answer(
                    answer_text,
                    sources,
                    resolved_session_id,
                )
                # 유효 인용 0건이어도 **본문을 폐기하지 않는다**. 무접지 백스톱은 2026-07-22
                # 실측에서 캐치 0 · 오탐 14/14였다 — 40턴 중 14턴에서 접지된 정답을 정형
                # 거절문으로 덮었고, 14건 전량 육안 판독 결과 창작은 0건이었다(가격·쪽수·정책
                # 조항 전부 라이브 재확인 일치).
                #
                # 오탐이 우연이 아니라 구조적 100%인 경로가 둘 있었다:
                #   1) 코드블록 전용 출력 → _code_span_ranges가 코드 스팬 내 [n]을 검증에서
                #      제외 → 유효 인용 0이 확정된다("JSON으로 줘"가 곧 답변 파괴).
                #   2) 마커 선두 표기("*   [2] …") → _build_support의 세그먼트가 불릿뿐이라
                #      support_is_meaningful이 false → 마커 전량 제거.
                # 둘 다 답변 **형식**의 문제이지 접지의 문제가 아니다.
                #
                # 2026-07-15에 같은 근거(캐치 0·오탐 9/9)로 게이트 스택을 삭제하고
                # validate_citations만 코어로 남겼다. 이 백스톱은 그때 살아남은 같은 계열이며
                # 같은 실패를 반복했다. 무효 마커 제거(validate_citations)는 유지하되,
                # **정상 본문을 거절문으로 갈아끼우는 파괴 경로만 제거한다.**
                # 재도입하려면 "창작을 실제로 잡은" 관측을 먼저 가져올 것.
                if not citation.meaningful_support_count and (
                    citation.removed_markers or observed_sources
                ):
                    logger.warning(
                        "유효 인용 0건(removed=%s sources=%d) — 본문은 유지하고 기록만 남긴다"
                        "(session_id=%s).",
                        bool(citation.removed_markers),
                        len(observed_sources),
                        resolved_session_id,
                    )

                done_payload["model"] = active_model
                if citation.removed_markers:
                    logger.warning(
                        "무효 인용 마커 %d개를 본문에서 제거했습니다: %s (유효 출처 id=%s)",
                        len(citation.removed_markers),
                        citation.removed_markers,
                        sorted(s["id"] for s in sources),
                    )

                final_done = done_payload
                if _ensure_substantive_text(final_done):
                    logger.warning(
                        "본문이 비어 최후 방어 안내로 대체합니다"
                        "(session_id=%s tools=%d sources=%d rbti=%s).",
                        resolved_session_id,
                        tool_call_count,
                        len(sources),
                        bool(rbti),
                    )
                for source in final_done.get("sources", []):
                    yield sse_source(source)
                for frame in _emit_final_body("".join(pending), final_done):
                    yield frame
                yield sse_done(final_done)
                break

        except asyncio.TimeoutError:
            logger.error(
                "LLM 응답이 %s초 내 오지 않아 스트림을 종료합니다(session_id=%s).",
                settings.sse_timeout_s,
                resolved_session_id,
            )
            frames = _closeout_error_frames(
                "응답이 너무 지연되고 있어요. 잠시 후 다시 시도해 주세요.",
                pending=pending,
                held=held,
                final_text=final_text,
                observed_sources=observed_sources,
                prior_sources=prior_sources,
                active_model=active_model,
                session_id=resolved_session_id,
            )
            held = None
            for frame in frames:
                yield frame
        except Exception as exc:  # noqa: BLE001 — SSE 스트림 최상위 방어선(마지막 수단)
            # 어떤 예외든 제너레이터를 예외로 종료시키지 않고 사용자에게 error를 알린 뒤 done으로
            # 스트림을 정상 마감한다. **빈 done으로 마감하지 않는다**: 모델이 완주한 뒤 재조립·세션
            # 재조립·인용 검증 구간에서 예외가 나면 이미 만들어 둔 답이 통째로 사라진다(실측:
            # 사용자에게 나간 본문 0자). 비파괴 원칙을 파이프라인 전체로 올려,
            # 그 시점까지 확보한 최선의 본문·출처로 마감한다. 스택트레이스는 반드시 로그에 남긴다.
            logger.exception("스트림 처리 중 예외 발생: %s", exc)
            frames = _closeout_error_frames(
                STREAM_ERROR_MESSAGE,
                pending=pending,
                held=held,
                final_text=final_text,
                observed_sources=observed_sources,
                prior_sources=prior_sources,
                active_model=active_model,
                session_id=resolved_session_id,
            )
            held = None
            for frame in frames:
                yield frame
        finally:
            # 타임아웃·클라이언트 중단 시 미소진 제너레이터의 자원을 정리한다.
            # 미완 번역·병합 대기 task도 함께 취소한다(부가 채널이라 유실이 아니다).
            for task in thought_tasks:
                task.cancel()
            if event_task is not None and not event_task.done():
                event_task.cancel()
            await timed_event_stream.aclose()
            await event_stream.aclose()
    finally:
        if lock is not None:
            lock.release()
