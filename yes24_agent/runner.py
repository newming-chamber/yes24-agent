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
    build_source_event,
)
from yes24_agent.grounding import (
    POLICY_SOURCE_TYPES,
    PRODUCT_SOURCE_TYPES,
    has_price_claim,
    has_product_grounding,
)
from yes24_agent.orchestrator import apply_sufficiency_gate
from yes24_agent.postprocess import (
    build_done_payload,
    has_tool_call_leak,
    strip_tool_call_leaks,
    validate_citations,
)
from yes24_agent.query_understanding import (
    GROUNDED_INTENTS,
    POLICY,
    PRODUCT,
    RECENCY,
    WEB,
    understand,
)
from yes24_agent.rbti.persona import is_valid_code
from yes24_agent.routing import FLASH, PRO, select_route
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


def _best_effort_done(
    turn_texts: list[str],
    current_turn: list[str],
    saw_partial_text: bool,
    final_text: str,
    observed_sources: list[dict],
    session_id: str,
) -> dict:
    """실패 마감용 done 페이로드 — 그 시점까지 확보된 최선의 본문·출처를 싣는다.

    스트림 후처리(재조립·세션 재조회·인용 검증·게이트) 어디서 터져도, 모델이 이미 만들어 낸
    본문을 버리지 않는다("비파괴"를 게이트 안이 아니라 파이프라인 전체의 계약으로). 본문은 정상
    경로와 **같은 조립기**를 쓰고(_merge_restated_turns — 프리앰블 재진술 제거), 인용은 관찰
    출처로 검증한다(무효 마커는 정상 경로와 동일하게 제거되므로 환각 인용이 새지 않는다).
    확보된 본문이 없으면 빈 text를 담아 호출부의 최후 방어 안내가 채우게 둔다.
    """
    pieces = [*turn_texts, "".join(current_turn)] if current_turn else list(turn_texts)
    text = _merge_restated_turns(pieces) if saw_partial_text else final_text
    citation = validate_citations(text or "", observed_sources)
    return build_done_payload(
        sources=observed_sources,
        used_source_ids=citation.used_source_ids,
        session_id=session_id,
        supports=citation.supports,
    ) | {"text": citation.text}


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
        # 본문 홀드 정책 — **질의가 접지를 요구하는가** 하나로 가른다(길이 임계·버퍼 카운터 없음).
        #  - 접지 불필요(잡담·정체성·일반지식 = 대다수): 첫 토큰부터 라이브 스트리밍. 출처가 아예
        #    없으므로 "출처 먼저 → 본문" 제약이 원천 성립불가 — 홀드가 기여하는 바가 0인데도 예전
        #    길이 임계(200자)는 짧은 답을 통째로 홀드해 delta 1건으로 뱉었다(실측: 12턴 중 6턴이
        #    first_delta == done, 환율 턴은 16.4초 백지 후 한 덩어리).
        #  - 접지 필요(상품·정책·시의성): 도구 결과(source)가 나가기 전까지 홀드하고, 첫 출처가
        #    방출된 뒤부터 라이브로 흘린다. 도구 전 텍스트는 진행 발화이므로 ack로 빠진다.
        # 실제 값은 질의이해(understand) 직후에 정해진다.
        stream_body_live = False
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

        # 질의이해: 값싼 모델 1회로 질의의 의미를 분류한다(intent·multistep·confidence). 분류
        # 실패·저확신이면 안전한 폴백(pro + 게이트 적용)으로 떨어진다 — 키워드 매칭은 쓰지 않는다.
        understanding = await understand(message, settings)
        search_query = understanding.standalone_query
        # 홀드 정책 확정: 접지가 필요 없는 질의는 첫 토큰부터 라이브로 흘린다. 접지가 필요한
        # 질의는 **그 턴이 필요로 하는 접지가 실제로 나온 뒤** 본문을 연다 — 상품 질의는 Yes24 상품
        # 출처, 정책 질의는 정책 페이지(notice), 그 외(시의성)는 아무 출처나. 웹 검색만 하고 책값을
        # 지어낸 초안이 delta로 새지 않도록(그 초안은 게이트가 폐기한다) 접지 타입을 구분한다.
        stream_body_live = not understanding.needs_grounding
        if understanding.intent == PRODUCT:
            unlock_types = PRODUCT_SOURCE_TYPES
        elif understanding.intent == POLICY:
            unlock_types = POLICY_SOURCE_TYPES
        else:
            unlock_types = None  # 아무 출처나 나오면 연다

        # 하이브리드 모델 라우팅: 확신 있는 단일단계 질의만 flash(빠른 즉답)로 내리고, 다단계·
        # 저확신·분류 실패는 pro로 남긴다(select_route). 무출처 게이트의 재검색은 이와 별개로
        # 항상 pro(correction_agent)라 정확성이 보장된다.
        route = select_route(understanding, hybrid_routing=settings.hybrid_routing)
        main_agent = root_agent_flash if route == FLASH else root_agent

        logger.info(
            "모델 라우팅=%s intent=%s multistep=%s confident=%s (session_id=%s)",
            route,
            understanding.intent,
            understanding.multistep,
            understanding.confident,
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
                        and active_route == PRO
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
                        active_route = FLASH
                        # 폐기되는 pro 시도가 홀드해 둔 본문 버퍼를 리셋한다. delta 홀드로
                        # 프리앰블이 delta 없이 버퍼에만 쌓인 채 폴백할 수 있는데
                        # (emitted_output=False라 폴백 발동), 안 지우면 폐기된 pro 프리앰블이
                        # flash 답변 대신 done.text로 샌다. 출처는 방출 시 emitted_output을
                        # 켜므로 이 경로엔 없다(있으면 폴백 자체가 차단됨).
                        turn_texts = []
                        current_turn = []
                        saw_partial_text = False
                        final_text = ""
                        stream_body_live = False
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
                    if stream_body_live:
                        # (b) 스트리밍 중 도구 도착: 리셋만(ack 없음 — 이미 delta로 흘렀다).
                        stream_body_live = False
                    elif preamble:
                        # (a) preamble의 첫 문장(들)을 응대로 흘리되, 무출처 상품사실(가격·제목)이
                        # 섞였으면 방출하지 않는다(게이트 우회 차단). 방출하든 억제하든 이 텍스트는
                        # 진행 발화이므로 done.text(turn_texts)에는 넣지 않는다.
                        # ack도 본문과 같은 누출 가드를 통과시킨다 — 이 버퍼는 정의상 function_call
                        # 직전에서 잘린 텍스트라 tool-call 서술이 미완성 인자 블록째로 남기 쉽다
                        # (실측: "…비교해 드릴게요.call:yes24_search{query:"). done.text에 이미 적용
                        # 중인 스트립을 사용자 노출 경로 전체로 확대한다(원칙 4b: 디버그·도구 발화
                        # 본문 혼입 금지).
                        preamble, _ = strip_tool_call_leaks(preamble)
                        ack_text, _ = extract_ack(preamble, settings.ack_max_chars)
                        # ack은 도구 결과가 오기 **전** 발화라 어떤 가격도 접지될 수 없다 —
                        # 가격을 말하면 방출하지 않는다(게이트 우회 차단).
                        if ack_text.strip() and not has_price_claim(ack_text):
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
                        sources_in_payload = _sources_from_response(payload)
                        # 이 턴이 필요로 하는 접지가 나왔으면(중복 payload여도 앞서 방출됨) "출처
                        # 먼저 → 본문" 순서가 충족됐으므로 본문 라이브를 연다.
                        if any(
                            unlock_types is None
                            or src.get("type", "search_result") in unlock_types
                            for src in sources_in_payload
                        ):
                            stream_body_live = True
                        for source in sources_in_payload:
                            source_id = source.get("source_id")
                            if source_id in sent_source_ids:
                                continue
                            sent_source_ids.add(source_id)
                            source_event = build_source_event(source)
                            observed_sources.append(source_event)
                            emitted_output = True
                            yield sse_source(source_event)
                    continue

                # 3) partial 텍스트 조각 → 버퍼에 누적. 라이브가 열려 있으면(접지 불필요 턴이거나
                #    출처가 이미 나간 뒤) 곧바로 흘리고, 아니면 홀드한다.
                if event.partial:
                    chunk = _event_text(event)
                    if chunk:
                        saw_partial_text = True
                        current_turn.append(chunk)
                        if stream_body_live:
                            emitted_output = True
                            if not live_streamed:
                                # 홀드해 둔 누적분을 먼저 flush한 뒤 이어 흘린다(출처 방출 직후).
                                live_streamed = True
                                yield sse_delta("".join(current_turn))
                            else:
                                yield sse_delta(chunk)
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

            # 이 턴의 답이 도구 접지를 요구하는가 — 게이트에 넘길 **구조 신호**(관측 사실 + 분류).
            # 게이트는 이 값과 유효 인용 수·실제 도구 호출 기록만으로 원인을 가른다(미완결 vs 얕음).
            # 예전엔 여기서 없었던 도구 호출 레코드를 지어내 observed_tool_calls에 넣어 얕음 경로를
            # 태웠는데, 그러면 관측 데이터가 거짓이 되고 서로 다른 원인이 한 kind로 뭉개져 폴백까지
            # 잘못 상속됐다(미완결의 원답=약속문인데 shallow의 "원답 유지"를 물려받아 약속문이 최종
            # 확정). 이제 runner는 사실만 전달한다.
            #  - 이번 턴 상품 출처를 실제로 관측했다 → 상품 턴이 확실하다.
            #  - 아니면 분류가 접지 필요 부류(product·policy·recency)라고 확신하면 그대로 따른다.
            #  - 분류를 신뢰할 수 없을 때만(안전 폴백) 세션에 이미 근거가 있는지를 본다 — 있으면
            #    앞 턴이 이미 근거와 함께 이행한 대화의 후속 발화로 보고 배제한다(작별·소감 턴을
            #    재검색이 새 추천으로 갈아치우던 파괴적 오탐 차단).
            if has_product_grounding(observed_sources):
                needs_grounding = True
            elif understanding.confident:
                needs_grounding = understanding.intent in GROUNDED_INTENTS
            else:
                needs_grounding = not has_product_grounding(sources)
            # 정책 질의 턴인가 — 정책 규정을 답하려면 Yes24 정책 페이지 접지를 요구하는 조건.
            policy_turn = understanding.intent == POLICY
            # 상품 사실 게이트의 면제 조건(fail-closed): **웹 사실 질의임을 확신할 때만** 끈다.
            # 주가·시급·뉴스 답변은 줄머리 볼드 + 가격("**최저임금** … 10,320원")이 책 추천과
            # 마크업 구조가 같아, 턴 맥락 없이는 구별할 수 없다. 분류가 애매하면 켠 채로 둔다.
            product_context = not (
                understanding.confident and understanding.intent in (WEB, RECENCY)
            )

            # 답이 tool-call 서술뿐이라 스트립 후 본문이 빈 경우도 "도구를 안 부른 미완결"이다 —
            # 게이트가 인용 0 + 도구 호출 0으로 그렇게 판정하므로 별도 신호 주입이 필요 없다.
            if has_tool_call_leak(answer_text):
                logger.warning(
                    "tool-call 서술 누출을 본문에서 제거했습니다(session_id=%s).",
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
                needs_grounding=needs_grounding,
                policy_turn=policy_turn,
                product_context=product_context,
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
                _best_effort_done(
                    turn_texts, current_turn, saw_partial_text, final_text,
                    observed_sources, resolved_session_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 — SSE 스트림 최상위 방어선(마지막 수단)
            # 어떤 예외든 제너레이터를 예외로 종료시키지 않고 사용자에게 error를 알린 뒤 done으로
            # 스트림을 정상 마감한다. **빈 done으로 마감하지 않는다**: 모델이 완주한 뒤 재조립·세션
            # 재조회·검증·게이트 구간에서 예외가 나면 이미 만들어 둔 답이 통째로 사라진다(실측:
            # 사용자에게 나간 본문 0자). 게이트에만 있던 비파괴 원칙을 파이프라인 전체로 올려,
            # 그 시점까지 확보한 최선의 본문·출처로 마감한다. 스택트레이스는 반드시 로그에 남긴다.
            logger.exception("스트림 처리 중 예외 발생: %s", exc)
            yield sse_error("일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요.")
            yield sse_done(
                _best_effort_done(
                    turn_texts, current_turn, saw_partial_text, final_text,
                    observed_sources, resolved_session_id,
                )
            )
        finally:
            # 타임아웃·클라이언트 중단 시 미소진 제너레이터의 자원을 정리한다.
            await event_stream.aclose()
    finally:
        if lock is not None:
            lock.release()
