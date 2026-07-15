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

from yes24_agent.agent import build_correction_agent
from yes24_agent.event_translate import (
    _reconcile_sources,
    _sources_from_response,
    _status_for_call,
    _status_for_error,
    build_source_event,
)
from yes24_agent.grounding import evaluate
from yes24_agent.postprocess import build_done_payload, validate_citations
from yes24_agent.session_service import _POC_USER_ID
from yes24_agent.sources import get_sources
from yes24_agent.sse import sse_delta, sse_source, sse_status
from yes24_agent.turn_assembly import _event_text, _merge_restated_turns

logger = logging.getLogger(__name__)

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
    # 본문 조립은 runner와 **같은 규약**을 쓴다: 턴 경계(function_call)로 나눠 담고 조립 시
    # 선두 재진술을 제거한다(_merge_restated_turns). 예전엔 보정 경로만 조각을 그냥 이어붙여,
    # 프리앰블 중복이 done.text로 새는 통로가 한쪽에만 열려 있었다(원칙 4b는 두 경로 공통이다).
    turn_texts: list[str] = []
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
            if partial_pieces:  # 도구 호출 = 텍스트 턴의 끝 → 턴 경계로 잘라 담는다.
                turn_texts.append("".join(partial_pieces))
                partial_pieces = []
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
                    source_event = build_source_event(source)
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
    if partial_pieces:
        turn_texts.append("".join(partial_pieces))
    # 최종 집계 텍스트가 있으면 그것이 이 턴의 확정 본문이다. 없으면(partial만 온 경우) 턴별
    # 누적을 runner와 **같은 조립기**로 병합해 선두 재진술 중복을 제거한다 — 이 경로에만 merge가
    # 없어 프리앰블 중복이 done.text로 새는 통로가 한쪽에만 열려 있었다(원칙 4b는 두 경로 공통).
    text_sink.append(final_text or _merge_restated_turns(turn_texts))


async def _run_research_turn(
    service,
    run_config,
    resolved_session_id: str,
    settings,
    *,
    agent,
    user_message: str,
    sent_source_ids: set[int],
    observed_sources: list[dict],
    result_sink: list[tuple],
    stream_live: bool = False,
) -> AsyncIterator[str]:
    """게이트 발동 시의 재확인 2차 턴을 공통으로 실행한다(무출처·정책·미완결·얕음 공용).

    보정 지시는 **에이전트의 시스템 지시**에 담겨 있고(build_correction_agent), 이 턴의 user
    메시지로는 **사용자의 원 질문을 그대로** 다시 보낸다 — 사용자가 쓴 적 없는 문장이 세션
    히스토리에 user 발화로 남지 않게 하기 위함이다(예전엔 지시문을 user 메시지로 보내 다음 턴
    맥락이 "user: 방금 답변에는 확인하지 않은 정보가…"로 오염됐다). 도구 사용은 에이전트의
    before_model_callback이 강제한다(지시만으론 비결정적). status·source는 라이브 yield하고, 본문
    delta는 stream_live 규약을 따른다. 스트림이 끝나면 최신 state 출처로 인용을 재검증해
    (corrected_text, sources2, citation2)를 result_sink에 담는다 — 채택/폴백은 호출부가 정한다.
    """
    correction_runner = Runner(
        agent=agent,
        app_name=settings.app_name,
        session_service=service,
    )
    correction_message = types.Content(role="user", parts=[types.Part(text=user_message)])
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
    needs_grounding: bool = False,
    policy_turn: bool = False,
    product_context: bool = True,
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
    decision = evaluate(
        citation.text,
        cited_sources=done_payload["sources"],
        observed_sources=observed_sources,
        observed_tool_calls=observed_tool_calls,
        citation_count=len(citation.used_source_ids),
        needs_grounding=needs_grounding,
        policy_turn=policy_turn,
        product_context=product_context,
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
        # (2) 재실패 폴백이 **원답 유지가 아닌** 게이트(product·policy·unfulfilled). shallow만
        # 제외한다 — shallow의 재실패 폴백은 원답 유지라, 초안을 폐기(미노출)하는 스트리밍 경로면
        # 사용자가 안 본 초안이 done.text로 되살아나 원칙 4b를 깬다. 미완결(unfulfilled)은 재실패
        # 폴백이 안전 안내라 초안 폐기와 정합하고, 도구 0회 턴이라 live_streamed=False가 흔하다 —
        # 이 경로를 스트리밍에 넣지 않으면 LLM 두 턴이 도는 내내 사용자가 delta를 하나도 못 본다.
        # 보정 답 라이브 스트리밍: 1차 본문이 미노출이고, **재실패 폴백이 원답 유지가 아닐 때**만
        # 초안을 폐기하고 보정 답을 흘린다. 비파괴 게이트(도구를 돌렸으나 못 찾음)는 원답을 되살릴
        # 수 있어야 하므로 홀드→done.text 교체로 간다(사용자가 안 본 초안이 done.text가 되는 일
        # 방지 — 원칙 4b).
        stream_correction = (not live_streamed) and decision.destructive
        research_sink: list[tuple] = []
        try:
            async for frame in _run_research_turn(
                service,
                run_config,
                resolved_session_id,
                settings,
                agent=build_correction_agent(
                    decision.directive, policy=decision.force_tool == "yes24_fetch"
                ),
                user_message=standalone_query,
                sent_source_ids=sent_source_ids,
                observed_sources=observed_sources,
                result_sink=research_sink,
                stream_live=stream_correction,
            ):
                yield frame
        except Exception as exc:  # noqa: BLE001 — 보정 실패는 개선 실패이지 원답 폐기 사유가 아니다
            # 보정 턴의 인프라 실패(타임아웃·429·스트림 오류)는 1차 답변의 결함이 아니다. 예외를
            # 위로 던지면 runner 최상위 방어선이 이 턴 전체를 error+빈 done으로 마감해, 이미 만들어
            # 둔 원 답변까지 사라진다(개선 시도의 실패가 원답을 파괴 — 게이트의 비파괴 원칙 위반).
            # 여기서 삼키고 1차 답변을 그대로 확정한다. 본문은 done.text로 렌더되므로 delta를 새로
            # 흘리지 않아 이중 본문 위험도 없다(보정이 일부 delta를 흘린 뒤 실패한 경우 포함).
            logger.exception(
                "보정 턴 실패(%s/%s) → 원 답변을 그대로 유지합니다(session_id=%s): %s",
                decision.kind,
                decision.reason,
                resolved_session_id,
                exc,
            )
            result_sink.append(done_payload)
            return
        corrected_text, sources2, citation2 = research_sink[0]
        done_payload2 = build_done_payload(
            sources=sources2,
            used_source_ids=citation2.used_source_ids,
            session_id=resolved_session_id,
            supports=citation2.supports,
        )

        # 채택/폴백 — **표 하나**로 정한다(kind별 분기·문구 선택을 게이트가 이미 끝냈다).
        #   채택: 재확인 답이 본문을 갖고, 출처와 어긋나지 않으며, 인용 달린 출처를 실제로 가진다.
        #   폴백: destructive면 안전 안내(Gate.notice — 원답이 환각이거나 약속문이라 남길 수 없다),
        #         아니면 **원답 유지**(도구를 돌렸는데 못 찾은 정직한 답을 파괴하지 않는다).
        # 재검증은 **엄격**하다(observed 접지 예외 없음): 강제 도구가 "실행됐다"는 사실만으로
        # 접지로 오인해 재환각의 폴백을 놓치던 실측(v7)을 고정한다.
        corrected_reason = evaluate(
            citation2.text,
            cited_sources=done_payload2["sources"],
            observed_sources=[],
            observed_tool_calls=[],
            citation_count=len(citation2.used_source_ids),
            needs_grounding=needs_grounding,
            policy_turn=policy_turn,
            product_context=product_context,
        )
        adopt = (
            bool(corrected_text)
            and corrected_reason is None
            and bool(done_payload2["sources"])
        )
        if adopt:
            done_payload = done_payload2
            done_payload["text"] = citation2.text
            done_payload["gate_researched"] = True
            logger.warning(
                "재확인(%s/%s)으로 답변을 인용 %d건과 함께 재생성했습니다(session_id=%s).",
                decision.kind,
                decision.reason,
                len(done_payload["sources"]),
                resolved_session_id,
            )
        elif decision.destructive:
            done_payload = done_payload2
            done_payload["text"] = decision.notice
            done_payload["gate_blocked"] = True
            logger.warning(
                "재확인(%s/%s)도 근거 검증 실패 → 안전 안내로 폴백(session_id=%s).",
                decision.kind,
                decision.reason,
                resolved_session_id,
            )
        else:
            logger.info(
                "재확인이 인용 달린 개선을 못 내 원 답변을 유지합니다(비파괴, session_id=%s).",
                resolved_session_id,
            )
    result_sink.append(done_payload)
