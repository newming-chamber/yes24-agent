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
    TURN_START_STATUS,
    _reconcile_sources,
    _sources_from_response,
    _status_for_call,
    _status_for_error,
    _status_for_result,
    build_source_event,
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
    merge_turn_source_records,
)
from yes24_agent.sse import (
    sse_delta,
    sse_done,
    sse_error,
    sse_reset,
    sse_source,
    sse_status,
)

logger = logging.getLogger(__name__)

# Gemini 과부하/일시장애로 판정하는 HTTP 상태코드(반응형 폴백 트리거). 429=RESOURCE_EXHAUSTED
# (레이트리밋·쿼터), 503=UNAVAILABLE(과부하), 500/502/504=일시 서버 오류, 529=Overloaded.
# 400(bad request)·403·404 등 영구 오류는 제외 ─ 재시도해도 소용없어 정직 안내로 보낸다.
_OVERLOAD_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})


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


def _best_effort_text(pending: list[str], final_text: str) -> str:
    """실패 시점까지 확보한 본문을 정상 경로와 같은 규약으로 조립한다.

    스트림 후처리(재조립·인용 검증) 어디서 터져도, 모델이 이미 만들어 낸
    본문을 버리지 않는다("비파괴"를 특정 구간이 아니라 파이프라인 전체의 계약으로). aggregate
    final response를 우선하고, 없을 때만 대기 버퍼를 조립한다. 확보된 본문이 없으면 빈 문자열을
    반환해 호출부의 최후 방어가 채우게 둔다.
    """
    # 흘려보낸 조각의 합이 곧 본문이다. ADK의 aggregate final은 **마지막 모델 턴만** 담아
    # 도구 호출 앞의 예고를 빠뜨리는데, 예고도 본문으로 내보내기로 했으므로(사용자 방향)
    # 그걸 정본으로 삼으면 delta 합계와 done.text가 어긋난다(원칙 4b). partial이 하나도
    # 없었던 경우에만 aggregate로 폴백한다.
    return "".join(pending) or final_text


def _final_body_delta(payload: dict) -> str:
    """최종 본문 문자열을 꺼낸다(없으면 빈 문자열)."""
    return payload.get("text") if isinstance(payload.get("text"), str) else ""


def _emit_final_body(streamed_text: str, payload: dict) -> list[str]:
    """이미 흘려보낸 본문과 정본을 대조해 마감 프레임을 만든다.

    정상·타임아웃·예외 **세 경로가 같은 판정을 쓴다**. v2 초안은 오류 경로 두 곳에서
    가드 없이 본문을 다시 보내 화면에 같은 답이 두 번 그려졌고(프론트 finalize는
    errored면 조기 반환해 자가치유도 못 한다) "delta 합계 == done.text"(원칙 4b)가
    깨졌다 — 판정을 세 벌 두면 한 벌만 고치는 실수가 반복되므로 한 곳으로 모은다.
    """
    remaining = _final_body_delta(payload)
    if not streamed_text:
        return [sse_delta(remaining)] if remaining else []
    if streamed_text == remaining:
        return []
    # 인용 검증이 본문을 바꿨다. 이어붙이기로는 합계를 맞출 수 없으므로(짧아졌을 수도 있다)
    # 흘린 것을 무르고 정본을 다시 보낸다.
    return [sse_reset()] + ([sse_delta(remaining)] if remaining else [])


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

        # 첫 프레임을 곧바로 흘려 스트림이 살아 있음을 알린다(문구는 event_translate 단일 출처).
        yield sse_status(*TURN_START_STATUS)


        # 스트림에서 관찰한 출처를 누적한다. 병렬 도구 실행 시 세션 state가 유실될 수
        # 있어(_reconcile_sources 참고), done 조립의 유실 방지용 완전한 사본으로 쓴다.
        observed_sources: list[dict] = []
        # 이번 턴에 도구가 돌았는지만 센다. 과거엔 tool_name·result_count·needs_followup·status를
        # 담았으나 충분성 게이트 삭제 후 **어떤 소비자도 필드를 읽지 않고 개수만 본다**
        # (로그 카운트). 필드를 되살리려면 읽는 쪽을 먼저 만들 것.
        observed_tool_calls: list[bool] = []
        # partial 본문은 **즉시 흘리고 동시에 모은다**. 도구 호출 직전의 예고("~를 찾아볼게요")도
        # 본문의 일부로 남긴다 — ChatGPT류가 하는 방식이고, 사용자가 명시적으로 택한 방향이다
        # (2026-07-22: "생각쪽 말고 응답에도 중간 응답이 나가도 괜찮다").
        # 예고를 본문에서 빼내려면 확정 전까지 홀드해야 하고, 그러면 토큰 스트리밍이 통째로
        # 죽는다(실측: 1,779자 답변이 12.4초 무출력). 남겨두면 홀드도 reset도 필요 없다.
        pending: list[str] = []
        # 직전 도구 경계까지 예고로 소비한 조각 수. 예고는 **경계 이후 새로 온 부분만** 실어야
        # 한다 — 버퍼 전체를 쓰면 두 번째 예고에 첫 예고가 그대로 따라붙는다(실측).
        preface_mark = 0
        # 이미 delta로 흘려보낸 본문. 예외 핸들러도 참조하므로 try 진입 전에 바인딩한다.
        streamed_text = ""
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
                            preface_mark = 0
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
                        # 도구 호출 앞의 텍스트(예고)는 **본문에 그대로 둔다**. status로
                        # 복사하지도, 마감에서 잘라내지도 않는다 — 프론트가 진행 단계와
                        # 본문 조각을 도착 순서대로 한 열에 쌓으므로, 예고들은 답변 머리에
                        # 적층되는 게 아니라 각자 자기 도구 단계 앞에 놓여 서사가 된다.
                        # (과거의 "마지막 예고만 남기기"는 이 서사를 done에서 지우면서
                        # 매 다도구 턴마다 reset+전체 재delta를 유발했다.)
                        segment = "".join(pending[preface_mark:])
                        preface_mark = len(pending)
                        if segment.strip() and not segment.endswith("\n\n"):
                            # 예고와 답변이 한 문장처럼 붙지 않게 문단을 띄운다. 이 문단
                            # 경계가 프론트의 블록 분할 지점이기도 하다(표·코드블록이 블록
                            # 경계에 걸리지 않음을 구조로 보장).
                            gap = "\n\n"
                            pending.append(gap)
                            preface_mark = len(pending)
                            emitted_output = True
                            yield sse_delta(gap)
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
                            observed_tool_calls.append(True)
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
                                source_event = build_source_event(source)
                                for index, observed in enumerate(observed_sources):
                                    if observed.get("id") == source_id:
                                        observed_sources[index] = merge_turn_source_records(
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
                        chunk = _event_text(event)
                        if chunk:
                            pending.append(chunk)
                            emitted_output = True
                            yield sse_delta(chunk)
                        continue

                    if event.is_final_response():
                        text = _event_text(event)
                        if text:
                            final_text = text

                # 실제로 화면에 흘려보낸 본문. 마감에서 정본과 대조해 달라졌으면(인용 검증이
                # 무효 마커를 지운 경우) reset 후 정본을 다시 그린다.
                streamed_text = "".join(pending)
                answer_text = _best_effort_text(pending, final_text)
                # 예고 뒤에 실제 답변이 하나도 오지 않았는가. preface_mark는 예고로 소비한
                # 조각 수이므로, 그게 곧 전체라면 본문은 예고뿐이다("찾아볼게요.\n\n"만 남아
                # 최후 방어의 strip() 검사를 통과해 버린다). 문구가 아니라 카운터로 판정한다.
                answer_is_preface_only = preface_mark >= len(pending) and preface_mark > 0
                pending = []
                # 근거 스냅샷은 세션 state가 아니라 **스트림 관찰본**으로 만든다. ADK의
                # deep_merge_dicts가 병렬 도구의 state_delta를 리스트 키에서 last-wins로
                # 덮어써 한 도구의 출처가 통째로 유실될 수 있기 때문이다(_reconcile_sources
                # 주석 참조). 이 유실은 상류 미수정이라 여기서 계속 우회한다.
                sources = _reconcile_sources(observed_sources)

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
                if answer_is_preface_only or not (final_done.get("text") or "").strip():
                    logger.warning(
                        "done.text가 비어 최후 방어 안내로 대체합니다"
                        "(session_id=%s tools=%d sources=%d rbti=%s).",
                        resolved_session_id,
                        len(observed_tool_calls),
                        len(sources),
                        bool(rbti),
                    )
                    final_done["text"] = _EMPTY_RESPONSE_FALLBACK
                for source in final_done.get("sources", []):
                    yield sse_source(source)
                for frame in _emit_final_body(streamed_text, final_done):
                    yield frame
                yield sse_done(final_done)
                break

        except asyncio.TimeoutError:
            logger.error(
                "LLM 응답이 %s초 내 오지 않아 스트림을 종료합니다(session_id=%s).",
                settings.sse_timeout_s,
                resolved_session_id,
            )
            error_text = "응답이 너무 지연되고 있어요. 잠시 후 다시 시도해 주세요."
            yield sse_error(error_text)
            best_effort = _best_effort_text(pending, final_text)
            _, error_done = _finalize_answer(
                best_effort,
                observed_sources,
                resolved_session_id,
            )
            error_done["model"] = active_model
            for source in error_done.get("sources", []):
                yield sse_source(source)
            # pending은 "흘려보낸 조각"과 동일하다(모든 append 옆에 sse_delta가 있다).
            # 정상 경로가 이미 비웠다면 그때 저장해 둔 streamed_text를 쓴다.
            for frame in _emit_final_body("".join(pending) or streamed_text, error_done):
                yield frame
            yield sse_done(error_done)
        except Exception as exc:  # noqa: BLE001 — SSE 스트림 최상위 방어선(마지막 수단)
            # 어떤 예외든 제너레이터를 예외로 종료시키지 않고 사용자에게 error를 알린 뒤 done으로
            # 스트림을 정상 마감한다. **빈 done으로 마감하지 않는다**: 모델이 완주한 뒤 재조립·세션
            # 재조립·인용 검증 구간에서 예외가 나면 이미 만들어 둔 답이 통째로 사라진다(실측:
            # 사용자에게 나간 본문 0자). 비파괴 원칙을 파이프라인 전체로 올려,
            # 그 시점까지 확보한 최선의 본문·출처로 마감한다. 스택트레이스는 반드시 로그에 남긴다.
            logger.exception("스트림 처리 중 예외 발생: %s", exc)
            error_text = "일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요."
            yield sse_error(error_text)
            best_effort = _best_effort_text(pending, final_text)
            _, error_done = _finalize_answer(
                best_effort,
                observed_sources,
                resolved_session_id,
            )
            error_done["model"] = active_model
            for source in error_done.get("sources", []):
                yield sse_source(source)
            # pending은 "흘려보낸 조각"과 동일하다(모든 append 옆에 sse_delta가 있다).
            # 정상 경로가 이미 비웠다면 그때 저장해 둔 streamed_text를 쓴다.
            for frame in _emit_final_body("".join(pending) or streamed_text, error_done):
                yield frame
            yield sse_done(error_done)
        finally:
            # 타임아웃·클라이언트 중단 시 미소진 제너레이터의 자원을 정리한다.
            await timed_event_stream.aclose()
            await event_stream.aclose()
    finally:
        if lock is not None:
            lock.release()
