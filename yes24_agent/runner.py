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

from yes24_agent.agent import root_agent, root_agent_flash
from yes24_agent.config import get_settings
from yes24_agent.event_translate import (
    _reconcile_sources,
    _sources_from_response,
    _status_for_call,
    _status_for_error,
)
from yes24_agent.orchestrator import apply_sufficiency_gate
from yes24_agent.postprocess import (
    build_done_payload,
    has_tool_call_leak,
    validate_citations,
)
from yes24_agent.product_gate import detect_unsourced_product_claim
from yes24_agent.query_understanding import understand
from yes24_agent.rbti.persona import is_valid_code
from yes24_agent.routing import FLASH, classify_complexity, is_identity_meta
from yes24_agent.session_service import (
    _POC_USER_ID,
    _get_session_lock,
    _get_session_service,
    _resolve_session,
    _session_locks,  # noqa: F401 — 테스트가 runner_module 경유로 락 dict를 초기화(동일 객체 참조)
)
from yes24_agent.sources import get_sources
from yes24_agent.sse import sse_ack, sse_delta, sse_done, sse_error, sse_source, sse_status
from yes24_agent.turn_assembly import _event_text, _merge_restated_turns, extract_ack

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
            # RBTI 페르소나 코드를 세션 state에 out-of-band로 반영한다. content 없는
            # system 이벤트의 state_delta로 써(ADK 2.3.0 append_event 실측 확인) LLM 턴
            # 히스토리를 오염시키지 않는다. 유효 코드가 새로 들어온 경우만 write하고,
            # 이미 같은 코드면 중복 이벤트를 남기지 않는다. rbti=None/무효면 write skip →
            # 세션 state에 rbti 키가 없어 _instruction_provider가 페르소나를 붙이지 않는다
            # (기존과 바이트 동일). 실패 시 아래 except가 done 1회 불변식을 지킨다.
            if rbti and is_valid_code(rbti):
                code = rbti.upper()
                if session.state.get("rbti") != code:
                    await service.append_event(
                        session,
                        Event(author="system", actions=EventActions(state_delta={"rbti": code})),
                    )
        except Exception as exc:  # noqa: BLE001 — 스트림 시작 전 방어선(done 1회 불변식 보장)
            logger.exception("세션 준비 실패: %s", exc)
            yield sse_error("대화 세션을 준비하지 못했어요. 잠시 후 다시 시도해 주세요.")
            yield sse_done(
                {"sources": [], "grounding_supports": [], "session_id": session_id or ""}
            )
            return

        resolved_session_id = session.id

        # 제출 즉시 체감 반응(<200ms 목표) — 첫 status를 곧바로 흘려보낸다.
        yield sse_status("thinking", "질문을 확인하고 있어요")

        sent_source_ids: set[int] = set()
        # 스트림에서 관찰한 출처를 누적한다. 병렬 도구 실행 시 세션 state가 유실될 수
        # 있어(_reconcile_sources 참고), done 조립의 유실 방지용 완전한 사본으로 쓴다.
        observed_sources: list[dict] = []
        # 이번 턴 검색성 도구 호출의 충분성 힌트를 누적한다(tool_name·result_count·
        # needs_followup·status). 충분성 게이트가 "마지막 검색이 얕았는지(결과 0건)"를 판정해
        # 재검색을 트리거하는 데 쓴다 — 지금까진 도구가 반환만 하고 아무도 읽지 않던 힌트다.
        observed_tool_calls: list[dict] = []
        # done.text 조립용 턴별 누적. pro 계열 모델은 도구 호출 사이 각 LLM 턴을 처음부터
        # 다시 생성하며 선두 프리앰블을 재진술한다 → 턴 경계(function_call)로 나눠 두고
        # 조립 시 재진술을 제거한다(_merge_restated_turns). current_turn은 진행 중 턴의 조각.
        turn_texts: list[str] = []
        current_turn: list[str] = []
        saw_partial_text = False
        final_text = ""
        # delta 홀드 + 임계 판별: 도구를 쓰는 턴에서는 도구-후 텍스트를 버퍼(current_turn)에 담아
        # 두고, 그 길이로 '도구 사이 내레이션'(짧음)과 '최종 답변'(김)을 가른다. 버퍼가
        # body_stream_threshold_chars 미만에서 function_call이 닫으면 내레이션 → ack(본문 제외),
        # 임계를 넘으면 최종 답변 → 그 시점부터 라이브 토큰 스트리밍 시작(누적분 flush 후 이어감).
        # 목적 (1) "출처 먼저 → 본문" 순서(도구 라운드 관측 전엔 스트리밍 금지), (2) 도구 사이
        # 발화가 본문·delta를 오염시키던 결함 차단, (3) 최종 답변의 토큰 스트리밍 체감 보존.
        # 정체성·메타 무도구 턴은 아래에서 stream_body_live=True로 시작해 첫 토큰부터 흘린다.
        stream_body_live = False
        # 이번 턴에 도구 라운드(function_response)를 하나라도 봤는지. 임계 기반 라이브 스트리밍은
        # 이 뒤에만 허용해 "출처 먼저 → 본문"을 지킨다(도구 전 프리앰블은 길어도 홀드→ack).
        tool_round_seen = False
        # 현재 버퍼(current_turn)에 쌓인 글자 수. 임계 판별용 러닝 카운터(매 partial마다 join 방지).
        pending_chars = 0
        # 인터스티셜 응대(ack): function_call 직전의 버퍼 텍스트(프리앰블·도구 사이 내레이션)를
        # `event: ack`로 흘린다. 도구를 부른 턴만 ack가 뜨고(무도구·잡담·정체성 턴은 function_call이
        # 없어 자동 배제), ack로 간 텍스트는 done.text 조립에서 빠진다(4b 불변).
        # 본문을 루프에서 실제로 라이브 delta로 흘렸는지. 무도구 잡담·비스트리밍 폴백처럼 홀드된
        # 터미널 본문은 여기서 flush하지 않고 게이트 단계(apply_sufficiency_gate)가 단일 지점에서
        # 방출한다. 이 값을 게이트에 넘겨: 미노출(False)이면 재검색 답을 라이브 스트리밍, 노출(True)
        # 이면 재검색 답을 홀드→done.text 교체(이중 본문 방지)로 가른다.
        live_streamed = False
        # 반응형 폴백의 안전 조건: 사용자에게 보이는 프레임(delta·source·도구/에러 status)을
        # 하나라도 흘렸는지. 오버로드가 첫 LLM 호출 전에 나면(아직 아무것도 안 보임) flash로
        # 조용히 재시도해도 중복 노출·모순이 없다. 반대로 이미 뭔가 흘렸으면 재시도가 본문·
        # 출처를 중복시키므로 폴백하지 않고 정직 안내로 간다. (열기 thinking status는 제외.)
        emitted_output = False

        # 질의이해: 멀티턴 대명사·생략을 해소한 standalone 질의로 검색·라우팅 입력을 맑힌다
        # (architecture-blueprint.md P4). 게이트(직전 턴+대명사 신호)를 통과하는 극소수
        # 질의에서만 좁은 flash 1회로 재작성하고, 실패·수상하면 원본으로 fallback한다. off이거나
        # 게이트 미통과면 standalone_query=원본이라 기존과 동일하다. intent는 관측용(원본 기준).
        history = getattr(session, "events", None) or []
        understanding = await understand(message, history, settings)
        search_query = understanding.standalone_query

        # 하이브리드 모델 라우팅: 질의 난도로 flash(빠른 단일 판단)/pro(다단계) 경로를 고른다.
        # 기본은 pro(정확성 우선); hybrid_routing이 켜져 있고 질의가 단순(FLASH)으로 분류될
        # 때만 flash로 내려 지연을 줄인다. 분류 실패·애매는 pro로 남아 품질을 지킨다. 무출처
        # 게이트의 재검색은 이와 별개로 항상 pro(correction_agent)라 정확성이 보장된다.
        main_agent = root_agent
        route = "pro"
        if settings.hybrid_routing and classify_complexity(search_query) == FLASH:
            main_agent = root_agent_flash
            route = "flash"

        # 정체성·메타 질의(무도구 즉답 부류)는 delta 홀드를 스킵해 첫 토큰부터 라이브로 흘린다:
        # 죽은 대기(응답 전체를 홀드했다가 게이트에서 한 번에 flush) 없이 스트리밍한다. 판별은
        # 결정론 routing.is_identity_meta로만(LLM 비결정 배제). 무도구=source 없음이라 "출처
        # 먼저 → 본문" 순서 제약이 원천 성립불가하므로 홀드가 순서에 기여하는 바가 0 —
        # 스킵해도 불변식이 깨지지 않는다. 그 외 부류는 보수적으로 홀드(초기 False)를 유지한다.
        identity_turn = is_identity_meta(search_query)
        if identity_turn:
            stream_body_live = True
        # intent(질의 주제)는 관측 전용이다 — 라우팅·게이트를 바꾸지 않고 route와 함께 로깅해
        # flash/pro 분포와 질의 유형의 상관을 관찰한다(architecture-blueprint.md P3, 실사용
        # 가치 확인 후 연결 확대). complexity(난도)와 직교하는 축이라 별도로 분류한다.
        logger.info(
            "모델 라우팅=%s intent=%s rewritten=%s (session_id=%s)",
            route,
            understanding.intent,
            understanding.rewritten,
            resolved_session_id,
        )

        runner = Runner(
            agent=main_agent,
            app_name=settings.app_name,
            session_service=service,
        )
        run_config = RunConfig(
            streaming_mode=StreamingMode.SSE,
            max_llm_calls=settings.max_llm_calls,
        )
        new_message = types.Content(role="user", parts=[types.Part(text=search_query)])

        # 반응형 폴백 상태: 지금 스트림이 어느 모델 경로인지, 폴백을 이미 1회 썼는지.
        # 재시도는 pro→flash 딱 1회로 고정한다(무한 재시도 금지).
        active_route = route
        retried_overload = False

        # 이벤트 간격에 sse_timeout_s 상한을 건다. LLM/도구가 연결만 수락하고 응답을
        # 멈추면(스톨) async for가 무한 대기하므로, __anext__를 wait_for로 감싸 다음
        # 이벤트가 제때 오지 않으면 error+done으로 마감한다.
        event_stream = runner.run_async(
            user_id=_POC_USER_ID,
            session_id=resolved_session_id,
            new_message=new_message,
            run_config=run_config,
        )
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        event_stream.__anext__(), timeout=settings.sse_timeout_s
                    )
                except StopAsyncIteration:
                    break
                except APIError as exc:
                    # 반응형 폴백: pro가 Gemini 과부하/일시장애로, 아직 사용자에게 아무것도
                    # 안 흘린 상태에서 실패하면 flash로 딱 1회 조용히 재시도한다. 그 외(이미
                    # 뭔가 흘림·이미 flash·과부하 아님·재시도 소진·기능 off)는 재-raise해
                    # 아래 정직 안내(error+done) 방어선으로 넘긴다.
                    if (
                        settings.error_fallback
                        and not retried_overload
                        and active_route == "pro"
                        and not emitted_output
                        and _is_overloaded_error(exc)
                    ):
                        logger.warning(
                            "Gemini 과부하/일시장애(code=%s) 감지 → flash로 폴백 재시도"
                            "(session_id=%s).",
                            getattr(exc, "code", "?"),
                            resolved_session_id,
                        )
                        await event_stream.aclose()
                        retried_overload = True
                        active_route = "flash"
                        # 폐기되는 pro 시도가 홀드해 둔 본문 버퍼를 리셋한다. delta 홀드로
                        # 프리앰블이 delta 없이 버퍼에만 쌓인 채 폴백할 수 있는데
                        # (emitted_output=False라 폴백 발동), 안 지우면 폐기된 pro 프리앰블이
                        # flash 답변 대신 done.text로 샌다. 출처는 방출 시 emitted_output을
                        # 켜므로 이 경로엔 없다(있으면 폴백 자체가 차단됨).
                        turn_texts = []
                        current_turn = []
                        pending_chars = 0
                        tool_round_seen = False
                        saw_partial_text = False
                        final_text = ""
                        # 홀드 초기값은 이 턴의 판정을 따른다: 정체성 턴이면 재시도도 라이브
                        # 스트리밍(죽은 대기 스킵)을 유지하고, 그 외는 보수적으로 홀드(False).
                        stream_body_live = identity_turn
                        live_streamed = False
                        # 같은 세션·같은 메시지를 flash로 재실행한다. pro 시도가 이미 이 user
                        # 메시지를 세션에 append했으므로 히스토리에 user 턴이 한 번 더 붙지만
                        # (ADK가 매 run_async 시작에 append), 아무 응답도 못 낸 실패라 중복
                        # 노출·본문 모순은 없다 ─ 드문 과부하 시의 경미한 히스토리 중복이다.
                        fallback_runner = Runner(
                            agent=root_agent_flash,
                            app_name=settings.app_name,
                            session_service=service,
                        )
                        event_stream = fallback_runner.run_async(
                            user_id=_POC_USER_ID,
                            session_id=resolved_session_id,
                            new_message=new_message,
                            run_config=run_config,
                        )
                        continue
                    raise

                # 1) function call(집계본) → 도구별 진행 status. partial 조각은 args가
                #    불완전하므로 무시하고 non-partial 집계 이벤트에서만 args를 읽는다.
                if not event.partial and event.get_function_calls():
                    # 도구 호출 = 텍스트 턴의 끝. 이 시점의 버퍼는 '도구 호출 직전 텍스트'다.
                    # 두 경로로 갈린다:
                    #  (a) 아직 스트리밍 전(임계 미만) → 내레이션 확정 → ack로만 방출하고 done.text
                    #      에서 제외한다(도구 사이 진행 발화가 본문 서두를 오염시키던 결함 소멸).
                    #  (b) 이미 라이브 스트리밍 중(임계 초과 후 도구 도착 = 드문 오판) → 이 버퍼는
                    #      최종이 아님이 확정 → done.text에서 제외한다(이미 나간 delta는 transient
                    #      수용). 다음 도구-후 텍스트를 위해 스트리밍 상태를 리셋한다.
                    preamble = "".join(current_turn)
                    current_turn = []
                    pending_chars = 0
                    if stream_body_live and not identity_turn:
                        # (b) 스트리밍 중 도구 도착: 리셋만(ack 없음 — 이미 delta로 흘렀다).
                        stream_body_live = False
                    elif preamble:
                        # (a) preamble의 첫 문장(들)을 응대로 흘리되, 무출처 상품사실(가격·제목)이
                        # 섞였으면 방출하지 않는다(게이트 우회 차단). 방출하든 억제하든 이 텍스트는
                        # 진행 발화이므로 done.text(turn_texts)에는 넣지 않는다.
                        ack_text, _ = extract_ack(preamble, settings.ack_max_chars)
                        if ack_text.strip() and not detect_unsourced_product_claim(ack_text):
                            emitted_output = True
                            yield sse_ack(ack_text.strip())
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
                        for source in _sources_from_response(payload):
                            source_id = source.get("source_id")
                            if source_id in sent_source_ids:
                                continue
                            sent_source_ids.add(source_id)
                            source_event = {
                                "id": source_id,
                                "title": source.get("title", ""),
                                "url": source.get("url", ""),
                                "type": source.get("type", "search_result"),
                                # 카드에 저자·가격을 즉시 노출하려 스트리밍 시점에 함께 싣는다.
                                # 카드는 이 관찰 이벤트에서 그려져 done까지 유지되므로(인용 강조만
                                # 덧입힘), 여기서 빠뜨리면 비인용 카드는 끝까지 저자·가격이 없다.
                                # 상품 결과에만 있는 필드라 웹 출처는 None(프론트가 생략).
                                "author": source.get("author"),
                                "price": source.get("price"),
                                # 표지 이미지 URL(검색 결과에만 존재). sse_source가 있을 때만 노출.
                                "image_url": source.get("image_url"),
                                # 평점 값(있을 때만) — product_gate 평점 값 대조용.
                                "rating": source.get("rating"),
                            }
                            observed_sources.append(source_event)
                            emitted_output = True
                            yield sse_source(source_event)
                    # 도구 라운드를 봤다(출처가 앞서 나갔다). 이제부터 도구-후 텍스트는 임계 판별
                    # 대상 — 임계를 넘으면 최종 답변으로 보고 라이브 스트리밍한다("출처 먼저 → 본문"
                    # 유지: 스트리밍은 이 관측 뒤에만 켜진다). 임계 미만에서 function_call이 닫으면
                    # 도구 사이 내레이션으로 보고 ack로 보낸다. 당장 여기서 흘리지는 않는다(홀드).
                    tool_round_seen = True
                    continue

                # 3) partial 텍스트 조각 → 버퍼에 누적. 이미 스트리밍 중이면 라이브로 흘리고,
                #    아니면 임계로 최종 답변 여부를 판별한다(도구 라운드 관측 후에만 스트리밍 허용).
                if event.partial:
                    chunk = _event_text(event)
                    if chunk:
                        saw_partial_text = True
                        current_turn.append(chunk)
                        pending_chars += len(chunk)
                        if stream_body_live:
                            emitted_output = True
                            live_streamed = True
                            yield sse_delta(chunk)
                        elif (
                            tool_round_seen
                            and pending_chars >= settings.body_stream_threshold_chars
                        ):
                            # 임계 초과 → 최종 답변으로 판정. 그 시점부터 라이브 스트리밍하되,
                            # 홀드한 누적분을 먼저 delta로 flush한 뒤 이후 조각을 이어 흘린다.
                            stream_body_live = True
                            emitted_output = True
                            live_streamed = True
                            yield sse_delta("".join(current_turn))
                    continue

                # 4) 최종 집계 텍스트 → 본문 확정(조립용). 여기서는 방출하지 않는다 — 터미널 본문
                #    (무도구 잡담·비스트리밍 폴백처럼 홀드된 본문)의 flush는 게이트 단계가 단일
                #    지점에서 처리한다(게이트 미발동 시 flush, 발동 시 폐기 후 보정 답 스트리밍).
                if event.is_final_response():
                    text = _event_text(event)
                    if text:
                        final_text = text

            # 스트림 완료: 세션을 다시 조회해 최신 state의 출처로 인용을 검증한다.
            # done.text는 사용자가 delta로 실제 본 텍스트를 담되(도구 호출 전 프리앰블 포함),
            # pro 계열 모델이 도구 호출 사이 턴마다 되풀이한 선두 재진술을 제거한 판본을 쓴다.
            # partial을 하나라도 봤으면 턴별 누적을 병합(_merge_restated_turns)하고,
            # 비스트리밍 폴백(partial 없음)일 때만 최종 집계 텍스트를 쓴다.
            if current_turn:
                turn_texts.append("".join(current_turn))
                current_turn = []
            answer_text = _merge_restated_turns(turn_texts) if saw_partial_text else final_text
            refreshed = await service.get_session(
                app_name=settings.app_name,
                user_id=_POC_USER_ID,
                session_id=resolved_session_id,
            )
            state = refreshed.state if refreshed is not None else {}
            state_sources = get_sources(state)
            sources = _reconcile_sources(state_sources, observed_sources)

            # 병렬 도구 state 유실 관측용 메트릭: state가 잃었지만 스트림엔 있던 출처 수.
            recovered = len(sources) - len(state_sources)
            if recovered > 0:
                logger.warning(
                    "세션 state에서 유실된 출처 %d개를 스트림 관찰본으로 복구했습니다"
                    "(병렬 도구 실행 추정, session_id=%s).",
                    recovered,
                    resolved_session_id,
                )

            citation = validate_citations(answer_text, sources)
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

            # tool-call 서술이 답의 전부여서 스트립(validate_citations 내부) 후 본문이 비었으면,
            # 빈 답을 그대로 내지 않도록 얕음 게이트로 재검색을 강제한다 — 모델이 "부른 척"만 한
            # 검색을 correction 에이전트가 실제로 수행한다. observed 검색 힌트에 결과 0건 신호를
            # 얹어 evaluate()의 shallow 경로를 태운다(게이트 판정·재진입 cap은 불변).
            if has_tool_call_leak(answer_text) and not citation.text.strip():
                observed_tool_calls.append(
                    {
                        "tool_name": "yes24_search",
                        "status": "ok",
                        "result_count": 0,
                        "needs_followup": True,
                    }
                )
                logger.warning(
                    "tool-call 서술만 있고 본문이 비어 얕음 게이트로 재검색을 강제합니다"
                    "(session_id=%s).",
                    resolved_session_id,
                )

            done_payload = build_done_payload(
                sources=sources,
                used_source_ids=citation.used_source_ids,
                session_id=resolved_session_id,
                supports=citation.supports,
            )

            # 충분성 게이트 판정 → (발동 시) 재검색 재진입 → 채택/폴백은 orchestrator가 맡는다.
            # 게이트는 터미널 본문 방출의 단일 지점이기도 하다: 홀드된 본문(무도구 초안 등)의 flush,
            # 또는 게이트 발동 시 초안 폐기 후 보정 답 스트리밍을 live_streamed로 판단한다. 이
            # 제너레이터가 흘리는 프레임(delta·verifying status·재검색 source)을 그대로 중계하고,
            # 최종 done_payload를 result_sink로 돌려받아 sse_done으로 마감한다(인용·재진입 불변).
            gate_sink: list[dict] = []
            async for frame in apply_sufficiency_gate(
                citation,
                done_payload,
                service=service,
                run_config=run_config,
                resolved_session_id=resolved_session_id,
                settings=settings,
                observed_sources=observed_sources,
                observed_tool_calls=observed_tool_calls,
                sent_source_ids=sent_source_ids,
                result_sink=gate_sink,
                live_streamed=live_streamed,
                standalone_query=search_query,
            ):
                yield frame

            # 최후 방어: 게이트까지 지나고도 done.text가 비면 빈 응답을 그대로 내보내지 않는다.
            # 라이브로 아무것도 안 흘렸으면 delta로도 흘려 프론트가 "(응답이 없었어요)" 대신 이
            # 안내를 렌더하게 한다. 원인 규명용으로 상태를 로그에 남긴다(빈 성공 위장 금지 정신).
            final_done = gate_sink[0]
            if not (final_done.get("text") or "").strip():
                logger.warning(
                    "done.text가 비어 최후 방어 안내로 대체합니다"
                    "(session_id=%s live_streamed=%s tools=%d sources=%d rbti=%s).",
                    resolved_session_id,
                    live_streamed,
                    len(observed_tool_calls),
                    len(sources),
                    bool(rbti),
                )
                final_done["text"] = _EMPTY_RESPONSE_FALLBACK
                if not live_streamed:
                    yield sse_delta(_EMPTY_RESPONSE_FALLBACK)
            yield sse_done(final_done)

        except asyncio.TimeoutError:
            logger.error(
                "LLM 응답이 %s초 내 오지 않아 스트림을 종료합니다(session_id=%s).",
                settings.sse_timeout_s,
                resolved_session_id,
            )
            yield sse_error("응답이 너무 지연되고 있어요. 잠시 후 다시 시도해 주세요.")
            yield sse_done(
                {"sources": [], "grounding_supports": [], "session_id": resolved_session_id}
            )
        except Exception as exc:  # noqa: BLE001 — SSE 스트림 최상위 방어선(마지막 수단)
            # 어떤 예외든 제너레이터를 예외로 종료시키지 않고 사용자에게 error를 알린 뒤
            # done으로 스트림을 정상 마감한다. 반드시 스택트레이스를 로그로 남긴다.
            logger.exception("스트림 처리 중 예외 발생: %s", exc)
            yield sse_error("일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요.")
            yield sse_done(
                {
                    "sources": [],
                    "grounding_supports": [],
                    "session_id": resolved_session_id,
                }
            )
        finally:
            # 타임아웃·클라이언트 중단 시 미소진 제너레이터의 자원을 정리한다.
            await event_stream.aclose()
    finally:
        if lock is not None:
            lock.release()
