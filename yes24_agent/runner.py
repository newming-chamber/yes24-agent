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
from collections import deque
from collections.abc import AsyncIterator

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.genai import types
from google.genai.errors import APIError

from yes24_agent.adk_stream import iter_adk_events
from yes24_agent.agent import (
    get_agent,
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
    StreamRenumberer,
    build_done_payload,
    renumber_for_display,
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
from yes24_agent.tools.web_search import start_web_prefetch

logger = logging.getLogger(__name__)

# Gemini 과부하/일시장애로 판정하는 HTTP 상태코드(반응형 폴백 트리거). 429=RESOURCE_EXHAUSTED
# (레이트리밋·쿼터), 503=UNAVAILABLE(과부하), 500/502/504=일시 서버 오류, 529=Overloaded.
# 400(bad request)·403·404 등 영구 오류는 제외 ─ 재시도해도 소용없어 정직 안내로 보낸다.
_OVERLOAD_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})


# 예고 홀드 기계(_markdown_block_is_open·_fence_is_open·_held_continues_open_block·held 버퍼)는
# 2026-07-29 Claude Code식 내레이션 전환으로 통째로 삭제했다 — 도구 직전 예고를 본문에서 빼
# status로 승격하던 구조 자체가 사라졌고, 이제 조사 경과 서술은 응답 본문의 일부로 흐른다
# (원칙 4b 개정 — 사용자 승인). delta 합계 == done.text 불변식은 그대로다.

# 조사 라운드 경계에 넣는 문단 구분자. 라이브 화면은 도구 스텝 프레임이 블록을 갈라 멀쩡해
# 보이지만, 그 순서 정보가 없는 소비자(새로고침 복원·복사·매트릭스 셀·API)에게는 라운드의
# 문장들이 그대로 붙어 나갔다("…찾아볼게요.베스트셀러 목록을…" 실측). 라운드 경계는 실제
# 흐름의 구조이므로 이 표기는 4b("본문은 흐른 그대로")의 위반이 아니라 충실한 표기다.
_ROUND_SEPARATOR = "\n\n"


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


def _round_boundary_prefix(raw_body: list[str], chunk: str) -> str:
    """도구를 건너뛴 두 텍스트 사이에 넣을 문단 구분자(넣지 않으면 빈 문자열).

    **경계에 공백이 전혀 없을 때만** 넣는다. 이미 어느 쪽이든 개행·공백으로 끝나거나
    시작하면 접착이 아니므로 손대지 않는다 — 이 한 줄이 중복 삽입 방지와 열린
    마크다운 블록 보호를 동시에 해낸다: 표는 행마다 개행으로 끊기므로 표 도중의 경계는
    항상 개행 위에 놓이고, 그 자리에 빈 줄을 넣으면 표가 그 지점에서 종료돼 뒤따르는
    행이 생 파이프 평문으로 남는다(2026-07-22 실측, 프론트에서 복구 불가). 펜스 홀짝을
    세는 블록 파서를 되살리지 않고 구조만으로 같은 선을 지킨다.
    """
    if not raw_body or raw_body[-1][-1:].isspace() or chunk[:1].isspace():
        return ""
    return _ROUND_SEPARATOR


def _display_frames(
    renumberer: StreamRenumberer,
    raw_body: list[str],
    known_sources: dict[int, dict],
    streamed: list[str],
    *,
    final: bool = False,
) -> list[str]:
    """원시 본문(세션 누적 id)을 **표시 번호 본문**으로 바꿔 흘릴 프레임을 만든다.

    표시 번호를 스트리밍 시점에 배정하므로, 사용자는 첫 글자부터 출처 카드와 같은
    `[1][2]`를 본다(예전엔 `[49, 59, 78]`이 흐르다 마감 reset에서 통째로 다시 그려졌다).

    새로 배정된 번호의 `refs`를 **그 번호가 처음 실린 delta보다 먼저** 낸다 — 프론트가
    마커를 링크로 승격할 힌트(id→url)가 마커보다 늦게 오면 그 사이 생 대괄호가 보인다.
    refs를 표시 번호로 내보내는 덕에 프론트의 힌트 맵·출처 카드·본문 마커가 **한 번호
    공간**을 쓴다(예전엔 refs만 내부 id라, 같은 턴에서 `[2]`가 refs와 카드에서 서로 다른
    상품을 가리켰다 — 실측 40턴 중 32턴).

    `streamed`에는 실제로 흘린 조각만 쌓는다(원칙 4b의 "delta 합계 == done.text" 대조본).
    """
    chunk, assigned = renumberer.feed(
        "".join(raw_body), known_sources.keys(), final=final
    )
    frames: list[str] = []
    if assigned:
        frames.append(
            sse_status(
                "refs",
                "",
                refs=[
                    project_source_ref({**known_sources[old], "id": new})
                    for old, new in assigned.items()
                ],
            )
        )
    if chunk:
        streamed.append(chunk)
        frames.append(sse_delta(chunk))
    return frames


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
    # 검증이 끝난 **뒤에만** 공개 번호를 1..n으로 다시 매긴다(renumber_for_display docstring).
    citation, sources = renumber_for_display(citation, sources)
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


def _best_effort_text(raw_body: list[str], final_text: str) -> str:
    """실패 시점까지 확보한 본문을 정상 경로와 같은 규약으로 조립한다.

    스트림 후처리(재조립·인용 검증) 어디서 터져도, 모델이 이미 만들어 낸
    본문을 버리지 않는다("비파괴"를 특정 구간이 아니라 파이프라인 전체의 계약으로). aggregate
    final response를 우선하고, 없을 때만 대기 버퍼를 조립한다. 확보된 본문이 없으면 빈 문자열을
    반환해 호출부의 최후 방어가 채우게 둔다.
    """
    # `raw_body`는 모델이 쓴 본문 전체다(내레이션 포함 — 홀드 없음). 표시 번호로 바꾸기
    # **전**의 원시 id를 그대로 담아 인용 검증의 입력이 된다. partial이 하나도 없었던
    # 경우에만 aggregate final로 폴백한다.
    return "".join(raw_body) or final_text


def _ensure_substantive_text(payload: dict) -> bool:
    """빈 본문을 최후 방어 안내로 대체한다(대체했으면 True).

    정상·타임아웃·예외 **세 경로가 같은 판정을 쓴다**. 정상 경로에만 두면 드문 실패
    경로에서 같은 결함이 계속 재발한다(require_evidence가 그랬듯이).

    내레이션 전환(2026-07-29) 이후 "…볼게요"류 경과 서술은 응답의 정상 구성 요소다 —
    실패 시에도 화면에 흐른 서술은 비파괴 원칙대로 보존되고, 실패 안내는 error 프레임이
    담당한다. 이 가드는 문자 그대로 **빈 본문**만 잡는다(문구 판정 없음).
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
    renumberer: StreamRenumberer,
    raw_body: list[str],
    streamed: list[str],
    known_sources: dict[int, dict],
    final_text: str,
    observed_sources: list[dict],
    prior_sources: list[dict],
    active_model: str,
    session_id: str,
) -> list[str]:
    """타임아웃·예외 두 실패 경로가 공유하는 마감 시퀀스를 프레임 목록으로 조립한다.

    순서: sse_error → source* → 본문 → done. 내레이션 전환으로 본문은 이미 전량 흘렀으므로
    (홀드 없음) 여기서 붙일 잔여 조각은 없다 — 화면에 흐른 경과 서술("~찾아볼게요")은
    비파괴 원칙대로 done.text에 남고, 사용자는 error 프레임으로 실패를 안내받는다.
    출처는 정상 경로와 동일하게 _reconcile_sources를 거친다 — 과거 두 실패 경로만 raw
    observed_sources를 넘겨 병렬 도구 유실 보정이 빠지는 드리프트가 있었다(판정을 세 벌
    두면 한 벌만 고치는 실수가 반복된다는 _emit_final_body docstring의 실례).
    """
    frames = [sse_error(error_text)]
    # 이월해 둔 표시 번호 꼬리를 먼저 방류한다 — 실패 경로에서도 화면에 흐른 본문과
    # done.text의 대조(원칙 4b)가 같은 규약 위에서 이뤄져야 한다.
    frames.extend(_display_frames(renumberer, raw_body, known_sources, streamed, final=True))
    best_effort = _best_effort_text(raw_body, final_text)
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
    frames.extend(_emit_final_body("".join(streamed), error_done))
    frames.append(sse_done(error_done))
    return frames


async def run_agent_stream(
    message: str,
    session_id: str | None,
    rbti: str | None = None,
    model: str | None = None,
    session_service: BaseSessionService | None = None,
) -> AsyncIterator[str]:
    """사용자 메시지 1건을 처리하며 SSE 프레임 문자열을 순서대로 yield한다.

    이벤트 순서 계약: status·delta가 도착 순서대로 섞여 흐르고, 마지막에
    source → (reset →) delta → done으로 마감한다. 도구 응답 시점의 status는
    `refs`(마커 렌더용 id·url 힌트)를 실어 본문 [n]보다 먼저 도착하고, 검증을 통과한
    **최종 인용 출처만** source·done.sources로 나간다(원칙 4). 어떤 예외가 나도
    제너레이터가 예외로 죽지 않고 error+done을 흘려보낸 뒤 정상 종료한다.

    session_service를 주입하면 sqlite 싱글턴 대신 그 서비스를 쓴다 — 매트릭스가
    1회성 셀 세션을 InMemorySessionService로 돌려 sqlite 동시 쓰기 경합
    (`database is locked`, 2026-08-04 상용 실측)을 피하는 경로다. 채팅(None)은 종전대로.
    """
    settings = get_settings()
    # 웹 선제 실행: 모델 1라운드 사고와 그라운딩 서브콜을 병렬화하는 순수 지연 최적화.
    # 힌트 오판·실패·미소비 전부 정상 경로 폴백이라 답 내용·도구 선택에 영향이 없다
    # (계약·격리·매트릭스 공유는 web_search.py의 프리페치 절 참조).
    start_web_prefetch(message)
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
            service = session_service if session_service is not None else _get_session_service()
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
        # 모델이 쓴 **원시** 본문 조각(세션 누적 id 그대로). 인용 검증의 입력이다.
        raw_body: list[str] = []
        # 화면에 흘린 **표시 번호** 본문 조각. 마감의 4b 대조(_emit_final_body)가 이 값 위에
        # 선다 — 모든 append 옆에 sse_delta가 있다(_display_frames가 그 불변식을 소유).
        streamed: list[str] = []
        # 원시 id → 표시 번호 증분 치환기. 배정 규칙은 출구(renumber_for_display)와 공유한다.
        renumberer = StreamRenumberer()
        # 이번 턴에 인용 가능한 출처(원시 id → 공개 DTO). 이전 턴 레지스트리에서 출발해
        # 도구 응답마다 자란다 — 표시 번호 배정의 유효 id 집합이자 refs url의 출처다.
        known_sources: dict[int, dict] = {
            source["id"]: project_public_source(source)
            for source in prior_sources
            if source.get("id") is not None
        }
        # 마지막 본문 청크 이후 도구가 돌았는가 = 다음 텍스트가 **새 조사 라운드**의 시작인가.
        # 이 신호로만 라운드 경계 구분자를 넣는다(_round_boundary_prefix).
        tool_ran_since_text = False
        # 사고 요약 누적 버퍼(_thought_status_labels). 닫힌 문단만 타임라인으로 나가고
        # 미완 꼬리는 여기 남는다 — 미리보기 채널이라 스트림 종료 시 꼬리는 버려도 된다.
        thought_buf: list[str] = []
        # 진행 중인 사고 라벨 번역 task들 — **발생 순서 deque**. 번역은 본류와 병행하되,
        # 방류는 머리(head)가 끝났을 때만 순서대로 한다: 완료순 방류는 번역 지연 편차만큼
        # 사고 순서를 뒤섞어 타임라인이 실제 진행과 어긋난다(2026-07-29 사용자 실측:
        # "시장 데이터 수집 중"이 말미에). 스트림이 끝나면 미완분은 취소한다.
        thought_tasks: deque[asyncio.Task] = deque()
        event_task: asyncio.Task | None = None

        final_text = ""
        emitted_output = False
        retried_overload = False

        run_config = RunConfig(
            streaming_mode=StreamingMode.SSE,
            max_llm_calls=settings.max_llm_calls,
        )
        # 사용자가 UI에서 고른 모델의 에이전트(무효·미지정이면 config 기본 모델). 자동 라우팅이
        # 아니라 명시 선택이라 단일 루프 원칙은 유지된다 — 프롬프트·도구·thinking 구성은
        # 모델과 무관하게 동일하고, done.model이 실제 사용 모델을 싣는다.
        agent = get_agent(model)
        active_model = str(agent.model)

        runner = Runner(
            agent=agent,
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
                            # 번역 완료분을 본류 이벤트와 병합해 흘리되, **발생 순서(FIFO)**
                            # 로만 방류한다 — 머리가 아직이면 뒤의 완료분도 기다린다(순서가
                            # 체감보다 우선, 번역은 ~1초라 지연 미미). 번역 실패는
                            # translate_thought_label 내부의 원문 폴백으로 흡수된다.
                            event_task = asyncio.ensure_future(
                                timed_event_stream.__anext__()
                            )
                            while thought_tasks and not event_task.done():
                                await asyncio.wait(
                                    {event_task, thought_tasks[0]},
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                while thought_tasks and thought_tasks[0].done():
                                    yield sse_status(
                                        "thinking", thought_tasks.popleft().result()
                                    )
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
                            # 재시도 스트림에도 프리페치를 다시 연다 — 첫 시도가 이미
                            # 소비했어도 캐시가 완료 task를 돌려주므로 추가 비용이 없다.
                            start_web_prefetch(message)
                            raw_body = []
                            streamed = []
                            renumberer = StreamRenumberer()
                            tool_ran_since_text = False
                            thought_buf = []
                            for task in thought_tasks:
                                task.cancel()
                            thought_tasks.clear()
                            final_text = ""
                            retry_runner = Runner(
                                agent=agent,
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
                        # 도구 직전 예고·경과 서술은 이미 본문 delta로 흘렀다(내레이션 전환).
                        # 여기서는 도구 호출 자체의 진행 칩만 낸다.
                        tool_ran_since_text = True
                        for call in event.get_function_calls():
                            status = _status_for_call(call)
                            if status is None:
                                continue  # 알릴 진행이 없는 도구는 조용히 지나간다
                            emitted_output = True
                            yield sse_status(*status)
                        continue

                    responses = event.get_function_responses()
                    if responses:
                        tool_ran_since_text = True
                        for resp in responses:
                            payload = resp.response or {}
                            tool_call_count += 1
                            if payload.get("status") == "error":
                                status = _status_for_error(payload)
                                if status is not None:
                                    emitted_output = True
                                    yield sse_status(*status)
                                continue
                            count = payload.get("result_count")
                            result = _status_for_result(count) if isinstance(count, int) else None
                            if result is not None:
                                emitted_output = True
                                yield sse_status(*result)
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
                                # 인용 후보 풀만 넓힌다. 마커 렌더용 refs는 여기서 내지 않는다 —
                                # 표시 번호가 배정되는 시점(_display_frames)에 나가야 프론트의
                                # 힌트 맵이 본문 마커와 같은 번호 공간에 놓인다.
                                known_sources[source_id] = merge_source_records(
                                    known_sources.get(source_id, {}), source_event
                                )
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
                                thought_tasks.append(
                                    asyncio.create_task(translate_thought_label(label))
                                )
                        chunk = _event_text(event)
                        if chunk:
                            # 내레이션 전환: 본문은 항상 즉시 흐른다(홀드 없음). 도구 직전
                            # 예고·경과 서술도 응답의 일부다 — 4b의 "delta 합계 == done.text"
                            # 불변식은 이 무조건 방류로 오히려 단순하게 성립한다.
                            # 라운드 경계 구분자도 **여기 한 지점에서** 청크에 붙여, 스트림과
                            # 정본 누적이 갈릴 수 없게 한다(불변식이 자동 충족되는 구조).
                            if tool_ran_since_text:
                                chunk = _round_boundary_prefix(raw_body, chunk) + chunk
                                tool_ran_since_text = False
                            raw_body.append(chunk)
                            for frame in _display_frames(
                                renumberer, raw_body, known_sources, streamed
                            ):
                                emitted_output = True
                                yield frame
                        continue

                    if event.is_final_response():
                        text = _event_text(event)
                        if text:
                            final_text = text

                # 이월해 둔 표시 번호 꼬리(미완성 마커·열린 인라인 코드)를 방류한다.
                for frame in _display_frames(
                    renumberer, raw_body, known_sources, streamed, final=True
                ):
                    yield frame

                answer_text = _best_effort_text(raw_body, final_text)
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
                # (2)의 마커 제거는 2026-08-04에 삭제됐다 — 같은 오탐이 책 제목 끝의 `!`·`?`
                # 에서도 100% 재현돼(재생 47턴, 캐치 0) 본문 삭제 부분만 걷어냈다. 이제 이
                # 카운트가 0이어도 마커는 본문에 남고, 로그만 남는다.
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
                for frame in _emit_final_body("".join(streamed), final_done):
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
                renumberer=renumberer,
                raw_body=raw_body,
                streamed=streamed,
                known_sources=known_sources,
                final_text=final_text,
                observed_sources=observed_sources,
                prior_sources=prior_sources,
                active_model=active_model,
                session_id=resolved_session_id,
            )
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
                renumberer=renumberer,
                raw_body=raw_body,
                streamed=streamed,
                known_sources=known_sources,
                final_text=final_text,
                observed_sources=observed_sources,
                prior_sources=prior_sources,
                active_model=active_model,
                session_id=resolved_session_id,
            )
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
