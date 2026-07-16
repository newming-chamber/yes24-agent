"""ADK Runner 실행 → SSE 이벤트 변환.

`/chat/stream` 엔드포인트의 심장. `root_agent`를 ADK Runner로 돌리며 흘러나오는
이벤트(function call/response, partial/final text)를 프론트 계약(status/source/
delta/done/error)의 SSE 프레임으로 번역해 yield한다.

인용 환각 차단은 여기서 마무리된다: 스트림이 끝나면 세션 state를 다시 조회해
누적된 출처와 답변 본문의 `[n]` 마커를 대조(validate_citations)하고, 검증된
결과만 done 이벤트에 담는다.
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
    _reconcile_sources,
    _sources_from_response,
    _status_for_call,
    _status_for_error,
    build_source_event,
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
    merge_turn_source_records,
)
from yes24_agent.sse import sse_delta, sse_done, sse_error, sse_source, sse_status
from yes24_agent.turn_assembly import _event_text

logger = logging.getLogger(__name__)

# Gemini 과부하/일시장애로 판정하는 HTTP 상태코드(반응형 폴백 트리거). 429=RESOURCE_EXHAUSTED
# (레이트리밋·쿼터), 503=UNAVAILABLE(과부하), 500/502/504=일시 서버 오류, 529=Overloaded.
# 400(bad request)·403·404 등 영구 오류는 제외 ─ 폴백해도 소용없어 정직 안내로 보낸다.
_OVERLOAD_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})

# 최후 방어 문구: 어떤 경로로도 done.text가 비면(모델 빈 응답·홀드 flush 누락 등) 빈 응답을
# 그대로 내보내지 않고 이 안내로 대체한다("빈 성공 위장 금지"의 사용자 노출 버전).
_EMPTY_RESPONSE_FALLBACK = (
    "죄송해요, 방금 답변을 제대로 만들지 못했어요. 질문을 한 번 더 보내주시겠어요?"
)


def _is_overloaded_error(exc: BaseException) -> bool:
    """Gemini API 과부하/일시장애(반응형 폴백 대상)인지 결정론적으로 판정한다.

    google.genai는 HTTP 4xx/5xx를 APIError(ClientError/ServerError)로 감싸 .code에 상태코드를
    싣고, ADK는 429를 다시 _ResourceExhaustedError(ClientError 하위)로 감싸는데 이 역시
    APIError·code=429를 보존한다. 재시도로 나아질 수 있는 과부하/일시장애 코드만 True로 보고
    (그 외 영구 오류·비-API 예외는 False), 그래야 400 같은 영구 오류를 헛되이 폴백하지 않는다.
    """
    return isinstance(exc, APIError) and getattr(exc, "code", None) in _OVERLOAD_STATUS_CODES


def _finalize_answer(
    text: str,
    sources: list[dict],
    session_id: str,
    *,
    require_evidence: bool = False,
    fallback_text: str = "",
) -> tuple[object, dict]:
    """평상·예외·타임아웃이 공통으로 거치는 최종 인용 조립기."""
    citation = validate_citations(text or "", sources)
    if require_evidence and not citation.meaningful_support_count:
        citation = validate_citations(fallback_text, sources)
    payload = build_done_payload(
        sources=sources,
        used_source_ids=citation.used_source_ids,
        session_id=session_id,
        supports=citation.supports,
    )
    payload["text"] = citation.text
    return citation, payload


def _best_effort_text(
    turn_texts: list[str],
    current_turn: list[str],
    final_text: str,
) -> str:
    """실패 시점까지 확보한 본문을 정상 경로와 같은 규약으로 조립한다.

    스트림 후처리(재조립·세션 재조회·인용 검증·게이트) 어디서 터져도, 모델이 이미 만들어 낸
    본문을 버리지 않는다("비파괴"를 게이트 안이 아니라 파이프라인 전체의 계약으로). aggregate
    final response를 우선하고, 없을 때만 partial 조각을 조립한다. 확보된 본문이 없으면 빈 문자열을
    반환해 호출부의 최후 방어가 채우게 둔다.
    """
    pieces = [*turn_texts, "".join(current_turn)] if current_turn else list(turn_texts)
    return final_text or "".join(pieces)


def _final_body_delta(payload: dict) -> str:
    """홀드가 끝난 최종 본문을 단일 delta로 반환한다."""
    return payload.get("text") if isinstance(payload.get("text"), str) else ""


async def run_agent_stream(
    message: str, session_id: str | None, rbti: str | None = None
) -> AsyncIterator[str]:
    """사용자 메시지 1건을 처리하며 SSE 프레임 문자열을 순서대로 yield한다.

    이벤트 순서 계약: status → source → delta → done. function call/response가
    텍스트보다 먼저 흐르므로 자연스럽게 이 순서가 유지된다. 어떤 예외가 나도
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

        # 제출 즉시 체감 반응(<200ms 목표) — 첫 status를 곧바로 흘려보낸다.
        yield sse_status("thinking", "질문을 확인하고 있어요")

        # 스트림에서 관찰한 출처를 누적한다. 병렬 도구 실행 시 세션 state가 유실될 수
        # 있어(_reconcile_sources 참고), done 조립의 유실 방지용 완전한 사본으로 쓴다.
        observed_sources: list[dict] = []
        # 이번 턴 검색성 도구 호출의 충분성 힌트를 누적한다(tool_name·result_count·
        # needs_followup·status). 충분성 게이트가 "마지막 검색이 얕았는지(결과 0건)"를 판정해
        # 재검색을 트리거하는 데 쓴다 — 지금까진 도구가 반환만 하고 아무도 읽지 않던 힌트다.
        observed_tool_calls: list[dict] = []
        # done.text 조립용 턴별 누적. 도구 호출 전 텍스트는 턴 경계에서 버리고 최종 모델 턴만
        # 조립한다. current_turn은 진행 중 턴의 조각이다.
        turn_texts: list[str] = []
        current_turn: list[str] = []
        final_text = ""
        requires_grounding = False
        # 모든 턴의 본문은 최종 인용 검증까지 홀드한다. 분류가 외부 근거 필요성을 오판해도
        # 도구 사용 가능 에이전트가 질문 자체를 보고 검색할 수 있어야 한다.
        # 반응형 폴백의 안전 조건: 사용자에게 보이는 프레임(delta·source·도구/에러 status)을
        # 하나라도 흘렸는지. 오버로드가 첫 LLM 호출 전에 나면(아직 아무것도 안 보임) flash로
        # 조용히 재시도해도 중복 노출·모순이 없다. 반대로 이미 뭔가 흘렸으면 재시도가 본문·
        # 출처를 중복시키므로 폴백하지 않고 정직 안내로 간다. (열기 thinking status는 제외.)
        emitted_output = False

        run_config = RunConfig(
            streaming_mode=StreamingMode.SSE,
            max_llm_calls=settings.max_llm_calls,
        )
        # 사전 질의분류기(query_understanding)는 삭제했다 — 라우팅·게이트·typed dispatch가
        # W1~W3에서 사라지며 분류 출력의 소비처가 없어졌다. 강한 pro 단일 루프가 질문 자체를
        # 보고 도구를 스스로 당기므로 사용자 원문을 그대로 검색어로 넘긴다(원래도 pass-through).
        search_query = message
        # 단일 pro 경로: flash/pro 하이브리드 라우팅을 폐기하고 모든 질의를 pro로 처리한다.
        # 난도별 추론량 조절은 thinking_budget(Gemini 동적 추론)에 위임한다.
        main_agent = root_agent
        active_model = str(main_agent.model)

        runner = Runner(
            agent=main_agent,
            app_name=settings.app_name,
            session_service=service,
        )
        new_message = types.Content(role="user", parts=[types.Part(text=search_query)])

        # 반응형 재시도 상태: 과부하 재시도를 이미 1회 썼는지. 재시도는 같은 pro로
        # 딱 1회로 고정한다(무한 재시도 금지).
        retried_overload = False

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
                try:
                    event = await timed_event_stream.__anext__()
                except StopAsyncIteration:
                    break
                except APIError as exc:
                    # 반응형 재시도: pro가 Gemini 과부하/일시장애로, 아직 사용자에게 아무것도
                    # 안 흘린 상태에서 실패하면 같은 pro로 딱 1회 조용히 재시도한다. 그 외(이미
                    # 뭔가 흘림·과부하 아님·재시도 소진·기능 off)는 재-raise해 아래 정직
                    # 안내(error+done) 방어선으로 넘긴다.
                    if (
                        settings.error_fallback
                        and not retried_overload
                        and not emitted_output
                        and _is_overloaded_error(exc)
                    ):
                        logger.warning(
                            "Gemini 과부하/일시장애(code=%s) 감지 → pro로 재시도"
                            "(session_id=%s).",
                            getattr(exc, "code", "?"),
                            resolved_session_id,
                        )
                        await timed_event_stream.aclose()
                        await event_stream.aclose()
                        retried_overload = True
                        fallback_agent = root_agent
                        active_model = str(fallback_agent.model)
                        # 폐기되는 pro 시도가 홀드한 버퍼를 리셋한다.
                        turn_texts = []
                        current_turn = []
                        final_text = ""
                        # 같은 세션·같은 메시지를 같은 pro로 재실행한다. 이전 시도가 이미 이 user
                        # 메시지를 세션에 append했으므로 히스토리에 user 턴이 한 번 더 붙지만
                        # (ADK가 매 run_async 시작에 append), 아무 응답도 못 낸 실패라 중복
                        # 노출·본문 모순은 없다 ─ 드문 과부하 시의 경미한 히스토리 중복이다.
                        fallback_runner = Runner(
                            agent=fallback_agent,
                            app_name=settings.app_name,
                            session_service=service,
                        )
                        event_stream = fallback_runner.run_async(
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

                # 1) function call(집계본) → 도구별 진행 status. partial 조각은 args가
                #    불완전하므로 무시하고 non-partial 집계 이벤트에서만 args를 읽는다.
                if not event.partial and event.get_function_calls():
                    # 도구 호출 전 텍스트는 진행 발화이므로 최종 본문 소유권이 없다.
                    current_turn = []
                    for call in event.get_function_calls():
                        stage, detail = _status_for_call(call)
                        emitted_output = True
                        yield sse_status(stage, detail)
                    continue

                # 2) function response → 새 출처 노출(중복 제거), error면 error_type별 status.
                #    search형(results 리스트)·fetch형(단일 source dict) 응답을 모두 처리.
                responses = event.get_function_responses()
                if responses:
                    for resp in responses:
                        payload = resp.response or {}
                        # 충분성 힌트 누적(성공·에러 모두). 에러 응답도 result_count=0·
                        # needs_followup=True를 실어 오므로 얕음 판정 근거로 함께 관찰한다.
                        observed_tool_calls.append(
                            {
                                "tool_name": getattr(resp, "name", "") or "",
                                "status": payload.get("status"),
                                "result_count": payload.get("result_count"),
                                "needs_followup": payload.get("needs_followup"),
                            }
                        )
                        if payload.get("status") == "error":
                            stage, detail = _status_for_error(payload)
                            emitted_output = True
                            yield sse_status(stage, detail)
                            continue
                        sources_in_payload = _sources_from_response(payload)
                        for source in sources_in_payload:
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
                    continue

                # 3) partial은 도구 호출 여부가 확정될 때까지 사용자에게 노출하지 않는다.
                if event.partial:
                    chunk = _event_text(event)
                    if chunk:
                        current_turn.append(chunk)
                    continue

                # 4) 최종 집계 텍스트 → partial이 없던 경로의 본문 확정.
                if event.is_final_response():
                    text = _event_text(event)
                    if text:
                        final_text = text

            # 스트림 완료: 이번 턴 function_response에서 관측한 출처로 인용을 검증한다. 세션
            # state 재조회는 병렬 도구 실행 때 레지스트리 유실 수를 계측하는 데만 쓴다.
            # aggregate final response가 최종 본문의 정본이다. partial은 aggregate가 비는
            # 비스트리밍/중단 경로에서만 fallback으로 조립한다(보정 턴과 같은 소유권 규약).
            if current_turn:
                turn_texts.append("".join(current_turn))
                current_turn = []
            streamed_text = "".join(turn_texts)
            answer_text = final_text or streamed_text
            refreshed = await service.get_session(
                app_name=settings.app_name,
                user_id=_POC_USER_ID,
                session_id=resolved_session_id,
            )
            state = refreshed.state if refreshed is not None else {}
            state_sources = get_sources(state)
            current_source_ids = {
                source["id"] for source in observed_sources if source.get("id") is not None
            }
            sources = _reconcile_sources(observed_sources)

            # 병렬 도구 state 유실 관측용 메트릭: state가 잃었지만 스트림엔 있던 출처 수.
            state_source_ids = {
                source["id"] for source in state_sources if source.get("id") is not None
            }
            recovered = len(current_source_ids - state_source_ids)
            if recovered > 0:
                logger.warning(
                    "세션 state에서 유실된 출처 %d개를 스트림 관찰본으로 복구했습니다"
                    "(병렬 도구 실행 추정, session_id=%s).",
                    recovered,
                    resolved_session_id,
                )

            citation, done_payload = _finalize_answer(
                answer_text,
                sources,
                resolved_session_id,
            )
            done_payload["model"] = active_model
            if citation.removed_markers:
                # 결정론 메트릭: 무효 인용을 몇 개 잘라냈는지 + 그 시점 유효 출처 id를 남긴다.
                # 유효 id를 함께 찍어, 마커 소실의 원인이 (a)모델이 실제 source_id가 아닌
                # 번호(카드 순번 등)를 인용 vs (b)출처 등록 유실 중 어느 쪽인지 로그 한 줄로
                # 규명되게 한다(등록된 id가 있는데 마커만 어긋나면 (a), 등록 자체가 비면 (b)).
                logger.warning(
                    "무효 인용 마커 %d개를 본문에서 제거했습니다: %s (유효 출처 id=%s)",
                    len(citation.removed_markers),
                    citation.removed_markers,
                    sorted(s["id"] for s in sources),
                )

            # 단일 root_agent(pro) 루프의 답변을 그대로 마감한다. typed-flow(product/policy/
            # recency)·충분성 게이트·강제 재진입(correction)은 삭제했다 — 모든 질의가 ADK Runner
            # 네이티브 도구 루프를 타고, 접지 backstop은 별도 모듈인 인용 검증(validate_citations)
            # 이 맡는다.
            final_done = done_payload

            # 최후 방어: 마감 후에도 done.text가 비면 빈 응답을 그대로 내보내지 않는다.
            # 라이브로 아무것도 안 흘렸으면 delta로도 흘려 프론트가 "(응답이 없었어요)" 대신 이
            # 안내를 렌더하게 한다. 원인 규명용으로 상태를 로그에 남긴다(빈 성공 위장 금지 정신).
            if not (final_done.get("text") or "").strip():
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
            remaining_delta = _final_body_delta(final_done)
            if remaining_delta:
                yield sse_delta(remaining_delta)
            yield sse_done(final_done)

        except asyncio.TimeoutError:
            logger.error(
                "LLM 응답이 %s초 내 오지 않아 스트림을 종료합니다(session_id=%s).",
                settings.sse_timeout_s,
                resolved_session_id,
            )
            error_text = "응답이 너무 지연되고 있어요. 잠시 후 다시 시도해 주세요."
            yield sse_error(error_text)
            best_effort = _best_effort_text(turn_texts, current_turn, final_text)
            _, error_done = _finalize_answer(
                best_effort,
                observed_sources,
                resolved_session_id,
                require_evidence=requires_grounding or bool(observed_tool_calls),
                fallback_text=error_text,
            )
            error_done["model"] = active_model
            for source in error_done.get("sources", []):
                yield sse_source(source)
            remaining_delta = _final_body_delta(error_done)
            if remaining_delta:
                yield sse_delta(remaining_delta)
            yield sse_done(error_done)
        except Exception as exc:  # noqa: BLE001 — SSE 스트림 최상위 방어선(마지막 수단)
            # 어떤 예외든 제너레이터를 예외로 종료시키지 않고 사용자에게 error를 알린 뒤 done으로
            # 스트림을 정상 마감한다. **빈 done으로 마감하지 않는다**: 모델이 완주한 뒤 재조립·세션
            # 재조회·검증·게이트 구간에서 예외가 나면 이미 만들어 둔 답이 통째로 사라진다(실측:
            # 사용자에게 나간 본문 0자). 게이트에만 있던 비파괴 원칙을 파이프라인 전체로 올려,
            # 그 시점까지 확보한 최선의 본문·출처로 마감한다. 스택트레이스는 반드시 로그에 남긴다.
            logger.exception("스트림 처리 중 예외 발생: %s", exc)
            error_text = "일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요."
            yield sse_error(error_text)
            best_effort = _best_effort_text(turn_texts, current_turn, final_text)
            _, error_done = _finalize_answer(
                best_effort,
                observed_sources,
                resolved_session_id,
                require_evidence=requires_grounding or bool(observed_tool_calls),
                fallback_text=error_text,
            )
            error_done["model"] = active_model
            for source in error_done.get("sources", []):
                yield sse_source(source)
            remaining_delta = _final_body_delta(error_done)
            if remaining_delta:
                yield sse_delta(remaining_delta)
            yield sse_done(error_done)
        finally:
            # 타임아웃·클라이언트 중단 시 미소진 제너레이터의 자원을 정리한다.
            await timed_event_stream.aclose()
            await event_stream.aclose()
    finally:
        if lock is not None:
            lock.release()
