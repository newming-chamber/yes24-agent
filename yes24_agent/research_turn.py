"""ADK research 턴 이벤트 소비와 current-turn 결과 조립."""

from collections.abc import AsyncIterator, Callable

from google.adk.runners import Runner
from google.genai import types
from pydantic import BaseModel

from yes24_agent.adk_stream import SET_MODEL_RESPONSE_TOOL_NAME, iter_adk_events
from yes24_agent.event_translate import (
    _reconcile_sources,
    _sources_from_response,
    _status_for_call,
    _status_for_error,
    build_source_event,
)
from yes24_agent.postprocess import validate_citations
from yes24_agent.session_service import _POC_USER_ID
from yes24_agent.sources import merge_turn_source_records
from yes24_agent.sse import sse_status
from yes24_agent.turn_assembly import _event_text


async def consume_research_turn(
    event_stream,
    *,
    timeout_s: float,
    observed_sink: list[dict],
    text_sink: list[str],
    submission_type: type[BaseModel] | None,
    submission_sink: list[BaseModel],
    source_enricher: Callable[[dict, dict], None] | None,
    tool_call_sink: list[dict],
) -> AsyncIterator[str]:
    partial_pieces: list[str] = []
    final_text = ""
    timed_event_stream = iter_adk_events(event_stream, timeout_s=timeout_s)
    try:
        async for event in timed_event_stream:
            if not event.partial and event.get_function_calls():
                partial_pieces = []
                for call in event.get_function_calls():
                    if (
                        call.name == SET_MODEL_RESPONSE_TOOL_NAME
                        and submission_type is not None
                        and not submission_sink
                    ):
                        try:
                            structured = submission_type.model_validate(call.args or {})
                        except (TypeError, ValueError):
                            pass
                        else:
                            submission_sink[:] = [structured]
                        continue
                    stage, detail = _status_for_call(call)
                    yield sse_status(stage, detail)
                continue
            responses = event.get_function_responses()
            if responses:
                for resp in responses:
                    payload = resp.response or {}
                    if (
                        resp.name == SET_MODEL_RESPONSE_TOOL_NAME
                        and submission_type is not None
                        and not submission_sink
                    ):
                        try:
                            structured = submission_type.model_validate(payload)
                        except (TypeError, ValueError):
                            pass
                        else:
                            submission_sink[:] = [structured]
                        continue
                    tool_call_sink.append(
                        {
                            "tool_name": getattr(resp, "name", "") or "",
                            "status": payload.get("status"),
                            "result_count": payload.get("result_count"),
                            "needs_followup": payload.get("needs_followup"),
                        }
                    )
                    if payload.get("status") == "error":
                        stage, detail = _status_for_error(payload)
                        yield sse_status(stage, detail)
                        continue
                    for source in _sources_from_response(payload):
                        source_id = source.get("source_id")
                        source_event = build_source_event(source)
                        evidence_segments = source.get("evidence_segments")
                        if isinstance(evidence_segments, list):
                            source_event["_evidence_segments"] = evidence_segments
                        if source_enricher is not None:
                            source_enricher(source, source_event)
                        for index, observed in enumerate(observed_sink):
                            if observed.get("id") == source_id:
                                observed_sink[index] = merge_turn_source_records(
                                    observed, source_event
                                )
                                break
                        else:
                            observed_sink.append(source_event)
                continue
            if event.partial:
                chunk = _event_text(event)
                if chunk:
                    partial_pieces.append(chunk)
                continue
            if event.is_final_response():
                text = _event_text(event)
                if text:
                    final_text = text
    finally:
        await timed_event_stream.aclose()
    assembled_text = final_text or "".join(partial_pieces)
    if assembled_text and submission_type is not None and not submission_sink:
        try:
            structured = submission_type.model_validate_json(assembled_text)
        except (TypeError, ValueError):
            pass
        else:
            submission_sink[:] = [structured]
            assembled_text = ""
    text_sink.append(assembled_text)


async def run_research_turn(
    service,
    run_config,
    resolved_session_id: str,
    settings,
    *,
    agent,
    user_message: str,
    observed_sources: list[dict],
    result_sink: list[tuple],
    submission_type: type[BaseModel] | None = None,
    observed_tool_calls: list[dict] | None = None,
    source_enricher: Callable[[dict, dict], None] | None = None,
) -> AsyncIterator[str]:
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
    correction_sources: list[dict] = []
    submission_sink: list[BaseModel] = []
    try:
        async for frame in consume_research_turn(
            correction_stream,
            timeout_s=settings.sse_timeout_s,
            observed_sink=correction_sources,
            text_sink=corrected_text_sink,
            submission_type=submission_type,
            submission_sink=submission_sink,
            source_enricher=source_enricher,
            tool_call_sink=observed_tool_calls if observed_tool_calls is not None else [],
        ):
            yield frame
    finally:
        await correction_stream.aclose()

    corrected_text = corrected_text_sink[0] if corrected_text_sink else ""
    sources2 = _reconcile_sources(correction_sources)
    observed_sources[:] = _reconcile_sources(
        [*observed_sources, *correction_sources],
    )
    citation2 = validate_citations(corrected_text, sources2)
    result_sink.append(
        (
            corrected_text,
            sources2,
            citation2,
            submission_sink[-1] if submission_sink else None,
        )
    )


