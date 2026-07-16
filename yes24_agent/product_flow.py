"""상품 검색·상세·typed 선택·표현 조립 흐름."""

import json
import logging
from collections.abc import AsyncIterator, Sequence

from google.adk.agents.readonly_context import ReadonlyContext

from yes24_agent.adk_stream import SET_MODEL_RESPONSE_TOOL_NAME
from yes24_agent.agent_runtime import (
    YES24_SEARCH_ONLY,
    build_llm_agent,
    current_turn_has_function_response,
    force_call_tools,
    generate_isolated_json,
    latest_function_response,
    typed_instruction_provider,
)
from yes24_agent.config import get_settings
from yes24_agent.postprocess import build_done_payload, validate_citations
from yes24_agent.product_selection import (
    ProductConstraint,
    ProductSelectionSubmission,
    product_evidence_fields,
    project_product_sources,
    render_product_submission,
    validate_product_submission,
)
from yes24_agent.research_turn import run_research_turn

logger = logging.getLogger(__name__)


_PRODUCT_DETAIL_TOOLS = ["yes24_fetch", "fetch_many"]
_PRODUCT_COMPLETION_TOOLS = [
    SET_MODEL_RESPONSE_TOOL_NAME,
    "yes24_search",
    *_PRODUCT_DETAIL_TOOLS,
]
def _force_product_selection_first(callback_context, llm_request):  # noqa: ARG001
    """typed 상품 선택을 검색→상세→구조 제출 함수 순서로 제한한다."""
    if not current_turn_has_function_response(llm_request):
        force_call_tools(llm_request, YES24_SEARCH_ONLY)
        return None
    latest = latest_function_response(llm_request)
    if latest is None:
        return None

    name, payload = latest
    _force_product_after_response(llm_request, name, payload)
    if name in _PRODUCT_DETAIL_TOOLS:
        force_call_tools(llm_request, _PRODUCT_COMPLETION_TOOLS)
    return None


def _force_product_after_response(llm_request, name: str, payload: dict) -> None:
    """Yes24 검색을 선택한 correction을 상세 열람과 typed 제출로 이어 간다."""
    if name == SET_MODEL_RESPONSE_TOOL_NAME:
        return
    if name == "yes24_search":
        has_results = payload.get("status") == "ok" and payload.get("result_count", 0) > 0
        if has_results:
            force_call_tools(llm_request, _PRODUCT_DETAIL_TOOLS)
        return


_PRODUCT_CORRECTION_INSTRUCTION = """상품 typed 경로에서는 Yes24 검색 후보의 상세 페이지를 읽고,
최종 자유산문 대신 지정된 ProductSelectionSubmission 구조로 한 번만 제출하세요. 상세에서 직접
관측한 source_id만 한 번 넣고, evidence_fields에는 같은 상세 응답에서 사용자 질문을 직접
뒷받침하는 nonempty top-level 필드 이름만 넣으세요. 숫자 조건은 canonical 상품 필드로
검증되므로 rationales에 제출하지 마세요.

각 selection의 content_rationale에는 실제 반환된 intro, toc, pub_review, weekly_reviews 중
사용자 질문이나 추천 선택을 직접 뒷받침하는 evidence_field와 segment_id 하나를 제출하세요.
rationales에는 실제 적용한 사용자 조건별 원문을 evidence_field, segment_id, constraint_text로
제출하되, title은 제목 자체가 조건을 직접 나타낼 때만 사용하세요. constraint_text는 해당 요구를
표현한 현재 사용자 질문의 정확한 연속 문자열이어야 합니다. 한 요구에 여러 구간이 필요하면 같은
constraint_text로 각각 제출하고, 동일한 조합은 중복하지 마세요. 추가 조건 근거가 없으면
rationales는 비워 두되 content_rationale은 항상 제출하세요.

모든 evidence_field와 segment_id는 같은 상세 응답의 evidence_segments 한 항목과 정확히
일치해야 합니다. 원문이 사용자의 실제 대상·용도·상황과 어긋나거나 상품 유형이 다른 후보는
선택하지 마세요. 상품 원문이 입증하지 않는 사용자 맥락을 constraint_text로 제출하거나, 원문에
없는 상품 사실·특성·독자 효과·치료나 결과의 보장을 만들지 마세요. 검색 결과가 없거나 상세 열람에
실패하면 빈 selections와 상황에 맞는 missing_reason만 제출하세요. 최종 이유는 서버가 검증된
선택과 원문으로 조립합니다.

사용자가 결과 개수를 명시했다면 그 개수는 완료 조건입니다. 일부만 성공으로 제출하지 말고,
상세에서 대상·조건이 맞지 않는 후보를 발견하면 이미 확보한 검색 결과의 다른 후보를 열어 대체한
뒤 요청 개수만큼 제출하세요. 충분한 근거를 끝내 확보하지 못했을 때만 missing_reason을 제출합니다.
선택 맥락 자체를 제목 검색어로 삼지 말고, 실제 상품을 가르는 속성으로 검색하세요. 여러 독립
검색어가 필요하면 같은 모델 턴에
병렬로 호출하고, 같은 도구와 같은 인자를 한 모델 응답에서 반복하지 마세요.
모든 선택은 하나의 최종 구조에 담습니다. 검색 결과에 사용자 조건과 직접 대조할 후보가
있으면 그 상세를 읽는 단계로 진행하고, 관련 후보가 없거나 필수 조건 근거가 빠진 경우에만 목적을
바꾼 재검색을 하세요."""


def _product_instruction(ctx: ReadonlyContext) -> str:
    return (
        f"{typed_instruction_provider(ctx)}\n\n## 보정 계약\n\n"
        f"{_PRODUCT_CORRECTION_INSTRUCTION}"
    )


def build_product_agent():
    settings = get_settings()
    return build_llm_agent(
        model=settings.model_name,
        thinking_budget=settings.thinking_budget,
        name="yes24_assistant",
        description="게이트 발동 시 도구로 재확인해 인용과 함께 답을 재생성하는 보정용",
        instruction=_product_instruction,
        before_model_callback=_force_product_selection_first,
        output_schema=ProductSelectionSubmission,
    )


def _enrich_product_source(source: dict, source_event: dict) -> None:
    if source_event.get("type") == "book_detail":
        source_event["_evidence_fields"] = sorted(product_evidence_fields(source))


_PRODUCT_RATIONALE_SCHEMA = {
    "type": "object",
    "properties": {
        "rationales": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "integer"},
                    "rationale": {"type": "string"},
                },
                "required": ["source_id", "rationale"],
            },
        }
    },
    "required": ["rationales"],
}

_PRODUCT_RATIONALE_INSTRUCTION = """사용자 질문과 이미 선택·검증된 Yes24 상품만 입력됩니다.
상품을 다시 선택·탈락·점수화하거나 적합 여부를 판정하지 말고, 각 source_id의 검증 원문으로
사용자 질문에 직접 답하는 내용을 짧고 자연스럽게 설명하세요. 추천 질문이면 원문과 사용자의 실제
대상·용도·상황을 연결하세요. 원문 밖 상품 사실이나 효능을
만들지 말고, canonical facts에 이미 표시되는 가격·쪽수·평점·날짜 같은 숫자 상품 정보를 반복하지
마세요. 사용자 맥락을 출처가 입증한 상품 사실처럼 표현하지 마세요. 입력된 source_id마다 정확히
하나의 rationale을 같은 ID로 제출하고, 내부 필드명·인용 마커는 쓰지 마세요."""

def _confirmed_yes24_zero(observations: Sequence[dict]) -> bool:
    """도구 실패와 구분되는 이번 턴 Yes24 정상 0건 관측인지 판정한다."""
    return any(
        observation.get("tool_name") == "yes24_search"
        and observation.get("status") == "ok"
        and observation.get("result_count") == 0
        for observation in observations
    )


def _product_done_payload(
    submission: ProductSelectionSubmission,
    sources: list[dict],
    *,
    session_id: str,
    model: str,
    selection_model: str | None = None,
    contextual_rationales: dict[int, str] | None = None,
) -> dict:
    """typed 선택만 canonical source 사실과 exact evidence로 최종 조립한다."""
    text = render_product_submission(
        submission,
        sources,
        contextual_rationales=contextual_rationales,
    )
    public_sources = project_product_sources(submission, sources)
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
        "selection": selection_model or model,
        "generation": model,
    }
    payload["product_selection"] = {
        "state": "selected" if submission.selections else "evidence_missing",
        "source_ids": [selection.source_id for selection in submission.selections],
        "missing_reason": submission.missing_reason,
    }
    return payload


async def _synthesize_product_rationales(
    question: str,
    sources: list[dict],
    settings,
) -> dict[int, str] | None:
    """선택 상품의 원문과 사용자 맥락만 잇는 격리된 표현 변환기다."""
    writer_sources: list[dict] = []
    expected_ids: list[int] = []
    for source in sources:
        source_id = source.get("id")
        snippet = source.get("snippet")
        if not isinstance(source_id, int) or not isinstance(snippet, str) or not snippet.strip():
            return None
        canonical_facts = {
            key: value
            for key, value in source.items()
            if key not in {"id", "url", "type", "snippet", "checked_at", "image_url"}
            and value is not None
        }
        expected_ids.append(source_id)
        writer_sources.append(
            {
                "source_id": source_id,
                "canonical_facts": canonical_facts,
                "evidence": snippet,
            }
        )
    if not expected_ids:
        return None

    contents = json.dumps(
        {"question": question, "selected_products": writer_sources},
        ensure_ascii=False,
    )
    data = await generate_isolated_json(
        instruction=_PRODUCT_RATIONALE_INSTRUCTION,
        schema=_PRODUCT_RATIONALE_SCHEMA,
        contents=contents,
        settings=settings,
    )
    if data is None:
        return None
    raw_rationales = data.get("rationales")
    if not isinstance(raw_rationales, list) or len(raw_rationales) != len(expected_ids):
        return None

    rationales: dict[int, str] = {}
    for item in raw_rationales:
        if not isinstance(item, dict):
            return None
        source_id = item.get("source_id")
        rationale = item.get("rationale")
        if (
            not isinstance(source_id, int)
            or source_id in rationales
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            return None
        rationales[source_id] = rationale.strip()
    if set(rationales) != set(expected_ids):
        return None
    return rationales


async def _finalize_product_submission(
    submission: ProductSelectionSubmission,
    sources: list[dict],
    *,
    question: str,
    settings,
    session_id: str,
    selection_model: str,
) -> dict:
    """검증된 선택의 사실은 서버가, 사용자 맥락 설명은 격리 writer가 소유한다."""
    contextual_rationales = None
    answer_model = selection_model
    if submission.selections:
        contextual_rationales = await _synthesize_product_rationales(
            question,
            project_product_sources(submission, sources),
            settings,
        )
        if contextual_rationales is not None:
            answer_model = settings.flash_model_name
    return _product_done_payload(
        submission,
        sources,
        session_id=session_id,
        model=answer_model,
        selection_model=selection_model,
        contextual_rationales=contextual_rationales,
    )


async def run_product_selection_turn(
    service,
    run_config,
    resolved_session_id: str,
    settings,
    *,
    user_message: str,
    observed_sources: list[dict],
    result_sink: list[dict],
    expected_constraints: Sequence[ProductConstraint],
    expected_count: int | None = None,
    observed_tool_calls: list[dict] | None = None,
) -> AsyncIterator[str]:
    """상품 질문을 검색→상세→typed 선택 한 턴으로 실행하고 서버에서 조립한다."""
    product_agent = build_product_agent()
    research_sink: list[tuple] = []
    tool_calls = observed_tool_calls if observed_tool_calls is not None else []
    try:
        async for frame in run_research_turn(
            service,
            run_config,
            resolved_session_id,
            settings,
            agent=product_agent,
            user_message=user_message,
            observed_sources=observed_sources,
            result_sink=research_sink,
            submission_type=ProductSelectionSubmission,
            observed_tool_calls=tool_calls,
            source_enricher=_enrich_product_source,
        ):
            yield frame
    except Exception as exc:  # noqa: BLE001 — 상품 사실을 원문 자유산문으로 폴백하지 않는다
        logger.exception(
            "상품 선택 턴 실패 → typed evidence-missing으로 마감합니다(session_id=%s): %s",
            resolved_session_id,
            exc,
        )
        missing = ProductSelectionSubmission(
            selections=[],
            missing_reason=(
                "no_results" if _confirmed_yes24_zero(tool_calls) else "detail_unavailable"
            ),
        )
        result_sink.append(
            _product_done_payload(
                missing,
                [],
                session_id=resolved_session_id,
                model=str(product_agent.model),
            )
        )
        return

    _, sources, _, submission = research_sink[0]
    validated = validate_product_submission(
        submission,
        sources,
        expected_constraints=expected_constraints,
        expected_count=expected_count,
        user_query=user_message,
    )
    if validated is None:
        logger.warning(
            "상품 typed 선택이 current-turn 상세 근거와 맞지 않아 fail-closed합니다"
            "(session_id=%s).",
            resolved_session_id,
        )
        validated = ProductSelectionSubmission(
            selections=[],
            missing_reason=(
                "no_results"
                if _confirmed_yes24_zero(tool_calls)
                else "insufficient_evidence"
            ),
        )
        sources = []

    result_sink.append(
        await _finalize_product_submission(
            validated,
            sources,
            question=user_message,
            settings=settings,
            session_id=resolved_session_id,
            selection_model=str(product_agent.model),
        )
    )


