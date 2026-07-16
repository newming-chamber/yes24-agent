"""최신 웹 탐색·typed 근거 선택·표현 조립 흐름."""

import json
import logging
from collections.abc import AsyncIterator

from google.adk.agents.readonly_context import ReadonlyContext

from yes24_agent.adk_stream import SET_MODEL_RESPONSE_TOOL_NAME
from yes24_agent.agent_runtime import (
    WEB_SEARCH_ONLY,
    build_llm_agent,
    current_turn_function_payloads,
    force_call_tools,
    generate_isolated_json,
    navigation_observation_urls,
    nonproduct_typed_instruction_provider,
    restrict_terminal_evidence,
    restrict_tool_urls,
    strip_typed_evidence_duplicates,
)
from yes24_agent.config import get_settings
from yes24_agent.evidence_segments import split_evidence_text
from yes24_agent.postprocess import (
    build_done_payload,
    escape_citation_markers,
    validate_citations,
)
from yes24_agent.recency_evidence import (
    RecencyEvidenceSubmission,
    project_recency_sources,
    render_recency_submission,
    validate_recency_submission,
)
from yes24_agent.research_turn import run_research_turn
from yes24_agent.sources import WEB_SOURCE_TYPES, today_kst_iso

logger = logging.getLogger(__name__)

RecencyAnswerSegment = tuple[str, list[int]]
RecencyAnswer = list[RecencyAnswerSegment]


def _web_navigation_urls(payloads: list[dict]) -> list[str]:
    """정제가 남은 웹 URL과 검색이 공개한 미열람 URL을 반환한다."""
    refinable, completed = navigation_observation_urls(payloads)
    candidates = list(refinable)
    for payload in payloads:
        results = payload.get("results")
        if isinstance(results, list):
            for result in results:
                if not isinstance(result, dict):
                    continue
                url = result.get("url")
                if isinstance(url, str) and url not in candidates:
                    candidates.append(url)
    return [url for url in candidates if url not in completed]


def _force_recency_evidence_first(callback_context, llm_request):  # noqa: ARG001
    """최신성 typed 선택을 검색으로 시작하고 같은 턴에서 자율 탐색하게 한다."""
    payloads = current_turn_function_payloads(llm_request)
    if not payloads:
        force_call_tools(llm_request, WEB_SEARCH_ONLY)
        return None
    restrict_terminal_evidence(
        llm_request,
        payloads,
        source_types=WEB_SOURCE_TYPES,
    )
    strip_typed_evidence_duplicates(
        llm_request,
        source_types=WEB_SOURCE_TYPES,
    )
    completion_tools = [SET_MODEL_RESPONSE_TOOL_NAME, "web_search"]
    allowed_urls = _web_navigation_urls(payloads)
    if allowed_urls:
        restrict_tool_urls(llm_request, "web_fetch", allowed_urls)
        completion_tools.append("web_fetch")
    force_call_tools(llm_request, completion_tools)
    return None


_RECENCY_EVIDENCE_INSTRUCTION = """최신성 질문은 최종 자유산문 대신 지정된
RecencyEvidenceSubmission 구조로 한 번만 제출하세요. 먼저 질문의 대상과 기준 시점을 절대 시점으로
확정하세요. web_search 호출 전 상대·현재 표현을 서버 KST current_date의 절대 날짜 문자열로
바꾸고, 상대 표현을 남기지 말고 그 문자열을 모든 검색 query에 보존하세요. 이번 턴
web_search·web_fetch가 반환한 type=web 출처 중 질문을 직접 뒷받침하는 최소 원문만 선택하세요.
요청 값의 라벨·단위·대응은 원문에 직접 보존돼야 하며 숫자 나열 순서나 이웃 위치로 매핑하지 마세요.

질문의 기준 시점과 직접 연결된 관측값을 우선하세요. 그 시점의 근거가 없으면 날짜·시각·값의 정의가
명시된 가장 최근 관측값을 선택할 수 있지만 요청 시점의 값으로 바꾸지 마세요. 같은 요구에 여러 값이
있으면 정의와 시점이 명확한 비교 가능한 관측 하나만 선택하고, 구분할 수 없으면 missing_reason으로
마감하세요. checked_at과 발행·갱신 시각만으로 본문 값의 기준 시점을 만들지 마세요.

검색 원문만으로 부족하면 필요한 관련 URL의 원문을 web_fetch로 읽으세요. 직접 근거를 끝내
확보하지 못했을 때만 빈 evidence_segment_ids와 상황에 맞는 missing_reason을 제출하세요.

검증된 선택 원문과 관측된 시점 메타만 별도 근거 격리 표현 단계에 제공됩니다."""


def _recency_instruction(ctx: ReadonlyContext) -> str:
    return (
        f"{nonproduct_typed_instruction_provider(ctx)}\n\n## 보정 계약\n\n"
        f"{_RECENCY_EVIDENCE_INSTRUCTION}"
    )


def build_recency_agent():
    settings = get_settings()
    return build_llm_agent(
        model=settings.model_name,
        thinking_budget=settings.thinking_budget,
        name="yes24_assistant",
        description="게이트 발동 시 도구로 재확인해 인용과 함께 답을 재생성하는 보정용",
        instruction=_recency_instruction,
        before_model_callback=_force_recency_evidence_first,
        output_schema=RecencyEvidenceSubmission,
    )


_RECENCY_SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "citation_units": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                    },
                },
                "required": ["text", "source_ids"],
            },
        },
    },
    "required": ["citation_units"],
}

_RECENCY_SYNTHESIS_INSTRUCTION = """서버의 현재 KST 날짜, 사용자 원문 질문, 검증을 통과한 이번 턴
웹 원문 구간과 출처 시점 메타만 입력됩니다. selector가 고른 사실을 새로 선택·확장하지 말고 사용자의
핵심 요구를 직접 해결하는 하나의 간결하고 자연스러운 완결 답변을 먼저 구성하세요. 그 답변을 인용
source_id 조합이 달라지는 경계에서만 citation_units로 나누세요. 각 unit은 인용 하나가 바로 뒤에
붙을 완결 문장 하나여야 하며, 원문별 사실 목록이 아닙니다. 같은 대상·시점·장소·주체는 필요한 곳에
한 번만 쓰고, 같은 source_id 조합의 인접 사실과 직접 도출되는 안내는 한 문장 안에서 연결하세요.

질문이 여러 근거의 관계를 요구하면 함께 직접 뒷받침되는 흐름·차이·관계를 첫 unit에서 종합하고 그
문장을 지지하는 모든 source_id를 연결하세요. 서로 다른 사실은 각각 실제로 뒷받침하는 source_id의
unit으로 분리하고, 한 출처를 다른 출처의 주장에 붙이지 마세요. 행동·선택 판단은 권고임이 드러나게
쓰되 새로운 수치·조건·효과·위험을 추가하지 마세요.

source_published_at과 source_updated_at은 출처의 발행·갱신 시점일 뿐 사건·관측·값의 기준 시점이
아닙니다. 반드시 출처 시점으로만 표현하고 원문 사실의 날짜로 합치지 마세요. 원문에서 값과 대상·
기준 시점·라벨의 연결이 직접 확인되지 않으면 메타나 나열 순서로 채우지 말고 확인할 수 없다고
답하세요. 원문에 없는 부재·추세·비교·인과·전망을 만들거나 주체와 발표·시행·계획 관계를 바꾸지
말고, 내부 필드명·인용 마커·검색 과정은 쓰지 마세요."""


def _recency_done_payload(
    submission: RecencyEvidenceSubmission,
    exact_sources: list[dict],
    public_sources: list[dict],
    *,
    session_id: str,
    model: str,
    selection_model: str,
    segments: list[RecencyAnswerSegment] | None = None,
) -> dict:
    """검증된 exact 원문과 공개 projection으로 최신성 응답을 조립한다."""
    if segments:
        text = " ".join(
            f"{escape_citation_markers(segment)} "
            f"[{', '.join(str(source_id) for source_id in source_ids)}]"
            for segment, source_ids in segments
        )
    else:
        text = render_recency_submission(submission, exact_sources)
    citation = validate_citations(text, public_sources)
    payload = build_done_payload(
        sources=public_sources,
        used_source_ids=citation.used_source_ids,
        session_id=session_id,
        supports=citation.supports,
    )
    payload["text"] = citation.text
    payload["model"] = model
    payload["models"] = {
        "selection": selection_model,
        "generation": model,
    }
    return payload


async def _compose_recency_answer(
    question: str,
    sources: list[dict],
    settings,
) -> RecencyAnswer | None:
    """선택된 최신성 원문과 관측 시점만 보는 격리된 표현 변환기다."""
    writer_sources: list[dict] = []
    source_ids: set[int] = set()
    for source in sources:
        source_id = source.get("id")
        snippet = source.get("snippet")
        if not isinstance(source_id, int) or not isinstance(snippet, str) or not snippet.strip():
            return None
        source_ids.add(source_id)
        writer_sources.append(
            {
                "source_id": source_id,
                "evidence": snippet,
                "source_published_at": source.get("published_at"),
                "source_updated_at": source.get("last_updated"),
            }
        )
    if not writer_sources:
        return None

    data = await generate_isolated_json(
        instruction=_RECENCY_SYNTHESIS_INSTRUCTION,
        schema=_RECENCY_SYNTHESIS_SCHEMA,
        contents=json.dumps(
            {
                "current_date": today_kst_iso(),
                "question": question,
                "selected_sources": writer_sources,
            },
            ensure_ascii=False,
        ),
        settings=settings,
    )
    raw_segments = data.get("citation_units") if isinstance(data, dict) else None
    if not isinstance(raw_segments, list):
        return None

    segments: list[RecencyAnswerSegment] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            return None
        text = raw_segment.get("text")
        raw_source_ids = raw_segment.get("source_ids")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(split_evidence_text(text)) != 1
            or not isinstance(raw_source_ids, list)
            or not raw_source_ids
            or any(
                not isinstance(source_id, int) or source_id not in source_ids
                for source_id in raw_source_ids
            )
        ):
            return None
        deduplicated_ids = list(dict.fromkeys(raw_source_ids))
        segments.append((text.strip(), deduplicated_ids))
    return segments or None

async def run_recency_evidence_turn(
    service,
    run_config,
    resolved_session_id: str,
    settings,
    *,
    user_message: str,
    observed_sources: list[dict],
    result_sink: list[dict],
) -> AsyncIterator[str]:
    """최신 웹 근거를 탐색하고 검증된 원문 구간만 격리 writer에 제공한다."""
    recency_agent = build_recency_agent()
    research_sink: list[tuple] = []
    try:
        async for frame in run_research_turn(
            service,
            run_config,
            resolved_session_id,
            settings,
            agent=recency_agent,
            user_message=user_message,
            observed_sources=observed_sources,
            result_sink=research_sink,
            submission_type=RecencyEvidenceSubmission,
        ):
            yield frame
    except Exception as exc:  # noqa: BLE001 — 최신 사실을 자유산문으로 폴백하지 않는다
        logger.exception(
            "최신성 근거 턴 실패 → evidence-missing으로 마감합니다(session_id=%s): %s",
            resolved_session_id,
            exc,
        )
        missing = RecencyEvidenceSubmission(
            evidence_segment_ids=[],
            missing_reason="source_unavailable",
        )
        result_sink.append(
            _recency_done_payload(
                missing,
                [],
                [],
                session_id=resolved_session_id,
                model=str(recency_agent.model),
                selection_model=str(recency_agent.model),
            )
        )
        return

    _, sources, _, submission = research_sink[0]
    validated = validate_recency_submission(
        submission if isinstance(submission, RecencyEvidenceSubmission) else None,
        sources,
    )
    if validated is None:
        logger.warning(
            "최신성 typed 근거가 current-turn 웹 원문과 맞지 않아 fail-closed합니다"
            "(session_id=%s).",
            resolved_session_id,
        )
        validated = RecencyEvidenceSubmission(
            evidence_segment_ids=[],
            missing_reason="reference_time_unavailable",
        )
        sources = []

    answer_model = str(recency_agent.model)
    selection_model = str(recency_agent.model)
    segments: list[RecencyAnswerSegment] | None = None
    exact_sources = list(sources)
    public_sources: list[dict] = []
    if validated.evidence_segment_ids:
        public_sources = project_recency_sources(validated, exact_sources)
        segments = await _compose_recency_answer(
            user_message,
            public_sources,
            settings,
        )

    if segments:
        answer_model = settings.flash_model_name

    result_sink.append(
        _recency_done_payload(
            validated,
            exact_sources,
            public_sources,
            session_id=resolved_session_id,
            model=answer_model,
            selection_model=selection_model,
            segments=segments,
        )
    )
