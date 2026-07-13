"""오케스트레이션 — 충분성 게이트 판정 → 재검색 재진입 → 채택/폴백 정책.

`runner.py`는 SSE 프레임을 흘리는 껍데기이고, "1차 답변이 근거에 충분한가"를 판정해
필요하면 같은 세션에서 도구를 강제로 재실행(재진입 1회)하고 그 결과를 채택할지 폴백할지
결정하는 **모델 바깥 오케스트레이션**은 이 모듈이 담당한다(architecture-blueprint.md §1).

퍼플렉시티의 gap 검증·claude code의 "충분히 답할 때까지 멈추지 말라"를 프롬프트가 아니라
코드 제어흐름으로 물리화한 계층이다. `product_gate`/`sufficiency_gate`의 순수 판정을 소비해
재검색 스트림을 조립하되, SSE 방출 시점·인용 매핑·재진입 정책은 runner 시절과 동일하다
(순수 이동 — 동작 불변). 재진입은 무출처 게이트와 동일하게 딱 1회이며 여기서 재귀하지 않는다.
"""

import asyncio
import logging
from collections.abc import AsyncIterator

from google.adk.runners import Runner
from google.genai import types

from yes24_agent.agent import correction_agent, policy_correction_agent
from yes24_agent.event_translate import (
    _reconcile_sources,
    _sources_from_response,
    _status_for_call,
    _status_for_error,
)
from yes24_agent.policy_gate import (
    UNSOURCED_POLICY_NOTICE,
    evaluate_policy_answer,
)
from yes24_agent.postprocess import build_done_payload, validate_citations
from yes24_agent.product_gate import (
    UNSOURCED_PRODUCT_NOTICE,
    evaluate_product_answer,
)
from yes24_agent.session_service import _POC_USER_ID
from yes24_agent.sources import get_sources
from yes24_agent.sse import sse_delta, sse_source, sse_status
from yes24_agent.sufficiency_gate import evaluate as evaluate_sufficiency
from yes24_agent.turn_assembly import _event_text

logger = logging.getLogger(__name__)

# 게이트 종류 → 보정 에이전트. 정책은 yes24_fetch를 강제하는 policy_correction_agent로,
# 그 외(product·shallow)는 검색을 강제하는 correction_agent로 재검색한다.
_CORRECTION_AGENTS = {"policy": policy_correction_agent}
_DEFAULT_CORRECTION_AGENT = correction_agent


async def _consume_correction_turn(
    event_stream,
    *,
    timeout_s: float,
    sent_source_ids: set[int],
    observed_sink: list[dict],
    text_sink: list[str],
    stream_live: bool = False,
) -> AsyncIterator[str]:
    """재검색(보정) 턴을 소비한다. 도구 status·새 출처(source)는 항상 라이브 yield한다.

    본문 delta 방출은 stream_live로 갈린다:
    - stream_live=False(기본): 1차가 이미 본문을 라이브로 흘린 경우 — 이중 본문 방지를 위해
      delta를 홀드하고 확정 본문은 done.text로 한 번에 교체한다(text_sink).
    - stream_live=True: 1차 본문이 사용자에게 안 보인 경우(무도구 초안은 이제 홀드돼 미노출) —
      보정 답을 라이브로 흘려 "한번에" 대신 토큰 스트리밍한다. 단 "출처 먼저" 유지를 위해
      보정의 첫 function_response(=source 방출) 전 partial은 홀드하고, 그 이후 partial만 delta로
      흘린다(메인 루프 홀드와 동일 패턴). 재검색이 만든 최종 본문은 text_sink에, 새 출처는
      observed_sink에 담는다. 같은 스트림 소비 규약(function_call→status, timeout 상한)을 따른다.
    """
    partial_pieces: list[str] = []
    final_text = ""
    # 보정에서 실제 source가 1건이라도 방출된 뒤 True(출처 먼저 → 본문). stream_live일 때만 쓰인다.
    # 트리거를 function_response 관측이 아니라 "source 실제 방출"로 둔다 — 강제검색이 에러·0건이면
    # source 없이 본문이 흐를 수 있어 순서 위반이 되므로, source가 안 나갔으면 본문을 계속 홀드한다.
    body_live = False
    while True:
        try:
            event = await asyncio.wait_for(event_stream.__anext__(), timeout=timeout_s)
        except StopAsyncIteration:
            break
        if not event.partial and event.get_function_calls():
            for call in event.get_function_calls():
                stage, detail = _status_for_call(call)
                yield sse_status(stage, detail)
            continue
        responses = event.get_function_responses()
        if responses:
            for resp in responses:
                payload = resp.response or {}
                if payload.get("status") == "error":
                    stage, detail = _status_for_error(payload)
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
                        "image_url": source.get("image_url"),  # 검색 결과 표지(있을 때만 노출)
                        # 재검색(correction) 카드에도 저자·가격을 노출한다(runner source_event와
                        # 동일). 이 경로가 A의 헤드라인 케이스(감정 추천→correction 카드만 노출)라,
                        # 빠뜨리면 그 카드는 끝까지 값이 없다(finalize 백필은 기존 카드 미갱신).
                        "author": source.get("author"),
                        "price": source.get("price"),
                    }
                    observed_sink.append(source_event)
                    yield sse_source(source_event)
                    # 실제 source를 방출한 직후에만 본문 라이브를 언락한다("출처 먼저" 불변식).
                    if stream_live:
                        body_live = True
            continue
        if event.partial:
            chunk = _event_text(event)
            if chunk:
                partial_pieces.append(chunk)  # done.text 조립용 누적.
                if body_live:
                    yield sse_delta(chunk)  # stream_live·출처 방출 후에만 라이브로 흘린다.
            continue
        if event.is_final_response():
            text = _event_text(event)
            if text:
                final_text = text
    text_sink.append(final_text or "".join(partial_pieces))


async def _run_research_turn(
    service,
    run_config,
    resolved_session_id: str,
    settings,
    *,
    agent,
    directive: str,
    sent_source_ids: set[int],
    observed_sources: list[dict],
    result_sink: list[tuple],
    stream_live: bool = False,
) -> AsyncIterator[str]:
    """게이트 발동 시의 재검색 2차 턴을 공통으로 실행한다(무출처·정책·얕음 게이트 공용).

    도구 사용을 강제하는 보정 에이전트(agent)를 같은 세션에서 돌려(지시만으론 모델이 도구를
    건너뛰는 비결정성 → 도구 강제로 결정론화) directive대로 재확인시키고, status·source는
    라이브 yield한다. 본문 delta는 stream_live에 따른다(_consume_correction_turn 규약): 1차 본문
    미노출이면 라이브 스트리밍, 노출됐으면 홀드 후 done.text 교체. 스트림이 끝나면 최신 state
    출처로 인용을 재검증해 (corrected_text, sources2, citation2)를 result_sink에 담는다 —
    채택/폴백 정책은 호출부(게이트 종류별)가 정한다. 재진입은 딱 1회로, 여기서 재귀하지 않는다.
    """
    correction_runner = Runner(
        agent=agent,
        app_name=settings.app_name,
        session_service=service,
    )
    correction_message = types.Content(role="user", parts=[types.Part(text=directive)])
    correction_stream = correction_runner.run_async(
        user_id=_POC_USER_ID,
        session_id=resolved_session_id,
        new_message=correction_message,
        run_config=run_config,
    )
    corrected_text_sink: list[str] = []
    try:
        async for frame in _consume_correction_turn(
            correction_stream,
            timeout_s=settings.sse_timeout_s,
            sent_source_ids=sent_source_ids,
            observed_sink=observed_sources,
            text_sink=corrected_text_sink,
            stream_live=stream_live,
        ):
            yield frame
    finally:
        await correction_stream.aclose()

    corrected_text = corrected_text_sink[0] if corrected_text_sink else ""
    refreshed2 = await service.get_session(
        app_name=settings.app_name,
        user_id=_POC_USER_ID,
        session_id=resolved_session_id,
    )
    state2 = refreshed2.state if refreshed2 is not None else {}
    sources2 = _reconcile_sources(get_sources(state2), observed_sources)
    citation2 = validate_citations(corrected_text, sources2)
    result_sink.append((corrected_text, sources2, citation2))


async def apply_sufficiency_gate(
    citation,
    done_payload: dict,
    *,
    service,
    run_config,
    resolved_session_id: str,
    settings,
    observed_sources: list[dict],
    observed_tool_calls: list[dict],
    sent_source_ids: set[int],
    result_sink: list[dict],
    live_streamed: bool,
    standalone_query: str = "",
) -> AsyncIterator[str]:
    """충분성 게이트 판정 → (발동 시) 재검색 재진입 → 채택/폴백을 수행한다.

    1차 답변(citation)과 그 done_payload를 받아, 근거 충분성을 판정하고 필요하면 도구 강제
    재검색(딱 1회)을 돌린 뒤 최종 done_payload를 `result_sink`에 담는다. runner는 이 제너레이터의
    프레임을 그대로 흘리고 result_sink[0]을 sse_done으로 마감한다.

    **터미널 본문 방출의 단일 지점**이다(handoff delta-hold 후속). runner는 1차 본문을 루프에서
    라이브로 흘렸을 때만(live_streamed=True) 이미 방출했고, 무도구 초안·비스트리밍 폴백은
    홀드된 채 여기로 온다. 그래서:
    - 게이트 미발동 & not live_streamed: 여기서 terminal 본문을 delta 1건으로 flush(잡담 flush).
    - 게이트 발동 & not live_streamed: 홀드된 1차 초안을 flush하지 않고 폐기(미노출) → 보정 답을
      라이브 스트리밍(사용자는 검증된 보정 답만 토큰 단위로 본다).
    - 게이트 발동 & live_streamed: 1차 본문이 이미 보였으므로 보정은 홀드→done.text 교체
      (이중 본문 방지).

    ① 무출처 상품 주장(책 사실인데 Yes24 상품 접지 0)·인용-제목 오매핑(cited-but-fabricated)·
    ② 얕은 결과(마지막 검색이 0건이라 근거 없음)를 판정한다. ①이 발동하면 ②는 보지 않는다
    (무출처·얕음 재검색 중복 차단). cited_sources는 실제 인용된 최종 출처(제목 대조·접지),
    observed는 이번 턴 도구 관찰본(검색은 했으나 인용을 빠뜨린 경우까지 접지로 인정해 오탐
    방지), observed_tool_calls는 얕음 판정 힌트.
    """
    decision = evaluate_sufficiency(
        citation.text,
        cited_sources=done_payload["sources"],
        observed_sources=observed_sources,
        observed_tool_calls=observed_tool_calls,
    )
    done_payload["text"] = citation.text
    if decision is None:
        # 게이트 미발동: 1차 본문을 루프에서 라이브로 안 흘렸으면(무도구 초안·비스트리밍 폴백)
        # 여기서 terminal 본문을 delta 1건으로 flush한다(잡담 flush 이관). live_streamed면 이미
        # 흘렀으니 재방출하지 않는다. 도구를 썼다면 source가 앞서 나갔으므로 순서 정합.
        if not live_streamed and citation.text:
            yield sse_delta(citation.text)
    else:
        # 감지 시 되물음으로 끝내지 않고, 원 질문에 맞는 도구로 실제 확인해 답을
        # 재생성한다(재검색 에스컬레이트, 무출처·얕음 공용). 재진입은 딱 1회.
        logger.warning(
            "충분성 게이트 발동(%s/%s) → 재검색 에스컬레이트(session_id=%s, 원문 %d자).",
            decision.kind,
            decision.reason,
            resolved_session_id,
            len(citation.text),
        )
        yield sse_status("verifying", decision.status_detail)

        # 보정 답 라이브 스트리밍 조건: (1) 1차 본문 미노출(홀드된 초안이라 폐기 가능) AND
        # (2) product/policy 게이트. shallow는 제외한다 — shallow 재실패 폴백은 "원 답변 유지"
        # (done.text=원 초안)라, 초안을 폐기(미노출)하는 스트리밍 경로면 사용자가 안 본 초안을
        # done.text로 되살려 원칙 4b 위반. product/policy는 재실패 폴백이 안전 안내(notice)라 초안
        # 폐기와 정합. shallow는 대개 검색 후라 live_streamed=True(홀드행)이므로 비용도 ~0.
        stream_correction = (not live_streamed) and decision.kind != "shallow"
        # 정책 보정은 원 질문의 주제로 앵커한다 — 재검색 턴은 새 user 메시지(directive)라, 질문을
        # 함께 실어야 모델이 세션 히스토리 대신 이 주제에 정확히 맞는 정책 페이지를 열고 카테고리
        # 탈선(예: 배송비 질문에 반품 페이지)을 피한다. 질문이 없으면 기존 지시 그대로.
        directive = decision.directive
        if decision.kind == "policy" and standalone_query.strip():
            directive = f'사용자 질문: "{standalone_query.strip()}"\n\n{decision.directive}'
        research_sink: list[tuple] = []
        async for frame in _run_research_turn(
            service,
            run_config,
            resolved_session_id,
            settings,
            agent=_CORRECTION_AGENTS.get(decision.kind, _DEFAULT_CORRECTION_AGENT),
            directive=directive,
            sent_source_ids=sent_source_ids,
            observed_sources=observed_sources,
            result_sink=research_sink,
            stream_live=stream_correction,
        ):
            yield frame
        corrected_text, sources2, citation2 = research_sink[0]
        done_payload2 = build_done_payload(
            sources=sources2,
            used_source_ids=citation2.used_source_ids,
            session_id=resolved_session_id,
            supports=citation2.supports,
        )

        if decision.kind == "policy":
            # 무출처 정책 정정: 재fetch 답이 본문을 갖고 Yes24 정책 페이지에 접지되면 채택,
            # 아니면 규정을 지어내지 않는 안전 안내로 폴백한다(환각 정책 규정 유출 차단 최우선).
            # **엄격 검증**(observed 접지 예외 없음): 실제 인용된 정책(notice) 출처로만 접지 인정.
            policy_reason = evaluate_policy_answer(
                citation2.text,
                cited_sources=done_payload2["sources"],
                observed_sources=[],
            )
            done_payload = done_payload2
            if corrected_text and policy_reason is None:
                done_payload["text"] = citation2.text
                done_payload["policy_gate_researched"] = True
                logger.warning(
                    "정책 재fetch로 답변을 인용 %d건과 함께 재생성했습니다(session_id=%s).",
                    len(done_payload["sources"]),
                    resolved_session_id,
                )
            else:
                done_payload["text"] = UNSOURCED_POLICY_NOTICE
                done_payload["policy_gate_blocked"] = True
                logger.warning(
                    "정책 재fetch도 근거 검증 실패(%s) → 안전 안내로 폴백(session_id=%s).",
                    policy_reason or "빈 본문",
                    resolved_session_id,
                )
            result_sink.append(done_payload)
            return

        # 재검색 답의 근거 재검증. **엄격 검증**: observed 접지 예외를 쓰지 않는다(빈
        # 리스트) — 강제검색이 "검색이 일어났다"는 사실만으로 접지로 오인돼 무관 결과를
        # 무시한 재환각의 폴백을 놓치던 v7 실측을 고정. 실제 인용된 Yes24 상품 출처로만
        # 접지를 인정한다.
        corrected_reason = evaluate_product_answer(
            citation2.text,
            cited_sources=done_payload2["sources"],
            observed_sources=[],
        )
        if decision.kind == "product":
            # 무출처·오매핑 정정: 재검색 답이 본문을 갖고 더 이상 근거에 어긋나지 않으면
            # 채택(상품=Yes24 인용, 사실=web_search 정보), 아니면 안전 안내로 폴백한다 —
            # 어느 경로든 환각 상품 사실은 확정 답변에 남지 않는다(유출 차단 최우선).
            done_payload = done_payload2
            if corrected_text and corrected_reason is None:
                done_payload["text"] = citation2.text
                done_payload["product_gate_researched"] = True
                logger.warning(
                    "재검색으로 답변을 인용 %d건과 함께 재생성했습니다(session_id=%s).",
                    len(done_payload["sources"]),
                    resolved_session_id,
                )
            else:
                done_payload["text"] = UNSOURCED_PRODUCT_NOTICE
                done_payload["product_gate_blocked"] = True
                logger.warning(
                    "재검색도 근거 검증 실패(%s) → 안전 안내로 폴백(session_id=%s).",
                    corrected_reason or "빈 본문",
                    resolved_session_id,
                )
        else:
            # 얕은 결과 재검색: 넓힌 검색이 본문을 냈고 상품 게이트에 걸리지 않으면(재환각
            # 미유발) 그 답으로 교체한다. 그렇지 못하면(빈 본문·재검색이 상품 환각 유발)
            # 원래 답을 그대로 둔다 — 원 답변은 이미 상품 게이트를 통과한 상태라 안전하다.
            # 무출처 게이트와 달리 안전 안내 폴백은 없다(얕음은 환각이 아니라 커버리지
            # 문제이므로, 최악에도 원 답변 유지가 정답).
            if corrected_text and corrected_reason is None:
                done_payload = done_payload2
                done_payload["text"] = citation2.text
                done_payload["shallow_gate_researched"] = True
                logger.warning(
                    "얕은 결과 재검색으로 답변을 인용 %d건과 함께 재생성했습니다"
                    "(session_id=%s).",
                    len(done_payload["sources"]),
                    resolved_session_id,
                )
            else:
                logger.info(
                    "얕은 결과 재검색이 개선을 못 내 원 답변을 유지합니다(session_id=%s).",
                    resolved_session_id,
                )
    result_sink.append(done_payload)
