"""Yes24 정책 탐색·typed 근거 선택·표현 조립 흐름."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from google.adk.agents.readonly_context import ReadonlyContext

from yes24_agent.adk_stream import SET_MODEL_RESPONSE_TOOL_NAME
from yes24_agent.agent_runtime import (
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
from yes24_agent.policy_evidence import (
    POLICY_DIRECTORY_URLS,
    PolicyEvidenceSubmission,
    is_selectable_policy_source,
    project_policy_sources,
    render_policy_submission,
    validate_policy_submission,
)
from yes24_agent.postprocess import (
    build_done_payload,
    escape_citation_markers,
    validate_citations,
)
from yes24_agent.research_turn import run_research_turn
from yes24_agent.sources import POLICY_SOURCE_TYPES
from yes24_agent.yes24.urls import POLICY_SEEDS

logger = logging.getLogger(__name__)

PolicySection = tuple[str, list[str]]
PolicySectionsBySource = dict[int, list[PolicySection] | None]


_POLICY_FORCED_TOOLS = ["yes24_fetch"]
_POLICY_COMPLETION_TOOLS = [SET_MODEL_RESPONSE_TOOL_NAME, *_POLICY_FORCED_TOOLS]


def _policy_navigation_urls(payloads: list[dict]) -> list[str]:
    """정제가 남은 정책 URL과 실제 응답의 미열람 page 링크를 반환한다."""
    if not payloads:
        return list(dict.fromkeys(seed["url"] for seed in POLICY_SEEDS.values()))
    refinable, completed = navigation_observation_urls(payloads)
    candidates = list(refinable)
    for payload in payloads:
        for link in payload.get("links", []):
            if not isinstance(link, dict) or link.get("kind") != "page":
                continue
            url = link.get("url")
            if isinstance(url, str) and url not in completed and url not in candidates:
                candidates.append(url)
    return candidates


def _policy_evidence_payloads(payloads: list[dict]) -> list[dict]:
    """명시적 document 또는 완결 FAQ Q+A가 있는 모든 current-turn 원문을 반환한다."""
    return [
        payload
        for payload in payloads
        if is_selectable_policy_source(
            payload.get("url"),
            payload.get("evidence_segments"),
        )
    ]


def _force_policy_evidence_first(callback_context, llm_request):  # noqa: ARG001
    """typed 정책 선택을 원문 열람 뒤 구조 제출 함수로 이어 간다."""
    payloads = current_turn_function_payloads(llm_request)
    allowed_urls = _policy_navigation_urls(payloads)
    if not payloads:
        restrict_tool_urls(llm_request, "yes24_fetch", allowed_urls)
        force_call_tools(llm_request, _POLICY_FORCED_TOOLS)
        return None
    evidence_payloads = _policy_evidence_payloads(payloads)
    restrict_terminal_evidence(
        llm_request,
        evidence_payloads,
        source_types=POLICY_SOURCE_TYPES,
    )
    strip_typed_evidence_duplicates(
        llm_request,
        source_types=POLICY_SOURCE_TYPES,
        drop_evidence_urls=POLICY_DIRECTORY_URLS,
    )
    if allowed_urls:
        restrict_tool_urls(llm_request, "yes24_fetch", allowed_urls)
        if not evidence_payloads:
            force_call_tools(llm_request, _POLICY_FORCED_TOOLS)
        else:
            force_call_tools(llm_request, _POLICY_COMPLETION_TOOLS)
    else:
        force_call_tools(llm_request, [SET_MODEL_RESPONSE_TOOL_NAME])
    return None


_POLICY_EVIDENCE_INSTRUCTION = """Yes24 정책 질문은 최종 자유산문 대신 지정된
PolicyEvidenceSubmission 구조로 한 번만 제출하세요. 이번 턴 yes24_fetch가 반환한 type=notice
출처의 evidence_segments 중 질문에 직접 답하는 원문의 segment_id만 evidence_segment_ids에
선택하세요. 무관한 중간 구간은 넣지 말고, 관련 내용이 떨어져 있으면 필요한 ID를 각각 고르세요.
FAQ entry_id·role이 있으면 같은 entry의 답변에 필요한 answer segment를 선택하세요. question은
적용 범위 검증을 위해 서버가 같은
entry에서 자동으로 결합하므로 question만 단독 선택하지 마세요.
FAQ의 question은 해당 answer의 적용 범위이므로, 사용자 발화가 question의 더 좁은 한정조건을
명시하지 않았다면 그 entry를 일반 정책의 근거로 선택하지 마세요.
첫 열람은 위 정책 입구 중 질문과 범위가 맞는 URL을 그대로 사용하고, 질문의
정책 주제를 find에 넣어 관련 구간과 링크를 확인하세요. 읽은 카테고리에 직접 답하는 entry가 없고
하위 카테고리 링크가 있으면 다른 성격의 페이지를 열기 전에 질문에 가장 가까운 하위 카테고리를
확인하세요. role=directory 입구의 기본 Q+A는 탐색 편의를 위한 목록이므로 최종 근거로 선택하지
말고, 실제 page 링크로 이동한 뒤 하위 FAQ entry를 선택하세요. role=document는 본문이 직접
답하면 선택할 수 있습니다. 제출 전
질문의 명시적 요구마다
직접 답하는 선택 원문이 있는지 대조하세요. 일부 요구의 근거만 있으면 성공으로 제출하지 말고
관련 페이지를 더 읽으며, 끝내 전체 요구를 뒷받침하지 못하면 missing_reason을 제출하세요.

원문을 복사·요약·바꿔쓰기·계산·추론한 문장은 제출하지 마세요. 질문에 직접 답하는 원문을 끝내
확보하지 못했을 때만 빈 evidence_segment_ids와 상황에 맞는 missing_reason을 제출하세요. 검증된
선택 원문은 출처 snippet으로 그대로 공개되며, 별도 근거 격리 표현 단계가 그 원문만 보고 최종
답을 구성합니다."""


def _policy_instruction(ctx: ReadonlyContext) -> str:
    seed_guide = "\n".join(
        f"- {seed['label']} [{seed['role']}]: {seed['url']}"
        for seed in POLICY_SEEDS.values()
    )
    return (
        f"{nonproduct_typed_instruction_provider(ctx)}\n\n"
        f"## 사용 가능한 정책 입구\n{seed_guide}\n\n## 보정 계약\n\n"
        f"{_POLICY_EVIDENCE_INSTRUCTION}"
    )


def build_policy_agent():
    settings = get_settings()
    return build_llm_agent(
        model=settings.model_name,
        thinking_budget=settings.thinking_budget,
        name="yes24_assistant",
        description="게이트 발동 시 도구로 재확인해 인용과 함께 답을 재생성하는 보정용",
        instruction=_policy_instruction,
        before_model_callback=_force_policy_evidence_first,
        output_schema=PolicyEvidenceSubmission,
    )


_POLICY_SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["heading", "items"],
            },
        }
    },
    "required": ["sections"],
}

_POLICY_SYNTHESIS_INSTRUCTION = """사용자 질문과 검증을 통과한 Yes24 정책 원문만 입력됩니다.
정책 원문 밖의 사실·연락 정보·메뉴·절차를 만들거나 모델 기억으로 보충하지 마세요. 질문이 요구한
항목이 여러 가지면 짧은 결론 뒤 각 요구와 별개의 제한·비용·행동을 서로 다른 제목과 목록으로
구분해 스캔 가능한 한국어 답으로 재구성하세요. 원문에 있더라도 질문과 적용 범위가 다른 정책은
답에 넣지 마세요. 웹 페이지의
탐색·접기 UI 문구나 원문 전체를 복사하지 말고, 내부 필드·출처 ID·인용 마커를 쓰지 마세요.
근거가 직접 말하지 않은 단계는 추론하지 마세요. 각 heading은 사실 주장이 없는 짧은 범주명으로,
각 items는 그 범주의 직접 답으로 쓰고 sections JSON 구조만 제출하세요."""

def _policy_done_payload(
    submission: PolicyEvidenceSubmission,
    sources: list[dict],
    *,
    session_id: str,
    model: str,
    selection_model: str | None = None,
    sections: PolicySectionsBySource | None = None,
) -> dict:
    """검증된 정책 원문과 격리 합성 결과만 최종 본문·출처로 조립한다."""
    public_sources = (
        project_policy_sources(submission, sources)
        if submission.evidence_segment_ids
        else []
    )
    public_ids = {
        source["id"]
        for source in public_sources
        if isinstance(source.get("id"), int)
    }
    if (
        sections is not None
        and set(sections) == public_ids
        and len(public_ids) == len(public_sources)
    ):
        blocks: list[str] = []
        for source in public_sources:
            source_id = source["id"]
            source_sections = sections[source_id]
            marker = f" [{source_id}]"
            if source_sections:
                blocks.extend(
                    "\n".join(
                        [
                            f"**{escape_citation_markers(heading)}**",
                            *(f"- {escape_citation_markers(item)}{marker}" for item in items),
                        ]
                    )
                    for heading, items in source_sections
                )
            else:
                blocks.append(f"{escape_citation_markers(source['snippet'])}{marker}")
        text = "\n\n".join(blocks)
    else:
        text = render_policy_submission(submission, sources)
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
    return payload


async def _synthesize_policy_sections(
    question: str,
    sources: list[dict],
    settings,
) -> PolicySectionsBySource | None:
    """선택 원문 외 컨텍스트가 없는 flash 표현 변환기다.

    사실 판정이나 점수화 없이 각 출처 원문을 서로 다른 호출에서 표현만 바꾼다. 실패·빈 응답·
    스키마 위반은 재시도하지 않고 해당 출처의 exact 원문으로 폴백한다.
    """
    source_inputs: list[tuple[int, str]] = []
    for source in sources:
        source_id = source.get("id")
        snippet = source.get("snippet")
        if not isinstance(source_id, int) or not isinstance(snippet, str) or not snippet.strip():
            return None
        source_inputs.append((source_id, snippet))
    if not source_inputs:
        return None

    async def synthesize_source(source_id: int, snippet: str) -> tuple[int, dict | None]:
        contents = json.dumps(
            {"question": question, "selected_policy_text": snippet},
            ensure_ascii=False,
        )
        data = await generate_isolated_json(
            instruction=_POLICY_SYNTHESIS_INSTRUCTION,
            schema=_POLICY_SYNTHESIS_SCHEMA,
            contents=contents,
            settings=settings,
        )
        return source_id, data

    results = await asyncio.gather(
        *(synthesize_source(source_id, snippet) for source_id, snippet in source_inputs)
    )
    synthesized: PolicySectionsBySource = {}
    for source_id, data in results:
        synthesized[source_id] = _parse_policy_sections(data)
    return synthesized


def _parse_policy_sections(data: dict | None) -> list[PolicySection] | None:
    """한 출처만 본 표현 결과를 렌더 가능한 섹션으로 정규화한다."""
    if data is None:
        return None
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        return None
    sections: list[PolicySection] = []
    for section in raw_sections:
        if not isinstance(section, dict):
            return None
        heading = section.get("heading")
        raw_items = section.get("items")
        if (
            not isinstance(heading, str)
            or not heading.strip()
            or not isinstance(raw_items, list)
            or not raw_items
            or any(not isinstance(item, str) or not item.strip() for item in raw_items)
        ):
            return None
        sections.append(
            (
                heading.strip(),
                [item.strip() for item in raw_items],
            )
        )
    return sections


async def run_policy_evidence_turn(
    service,
    run_config,
    resolved_session_id: str,
    settings,
    *,
    user_message: str,
    observed_sources: list[dict],
    result_sink: list[dict],
) -> AsyncIterator[str]:
    """Yes24 정책을 열람하고 검증된 원문 구간만 서버에서 조립한다."""
    policy_agent = build_policy_agent()
    research_sink: list[tuple] = []

    try:
        async for frame in run_research_turn(
            service,
            run_config,
            resolved_session_id,
            settings,
            agent=policy_agent,
            user_message=user_message,
            observed_sources=observed_sources,
            result_sink=research_sink,
            submission_type=PolicyEvidenceSubmission,
        ):
            yield frame
    except Exception as exc:  # noqa: BLE001 — 정책을 자유산문으로 폴백하지 않는다
        logger.exception(
            "정책 근거 턴 실패 → evidence-missing으로 마감합니다(session_id=%s): %s",
            resolved_session_id,
            exc,
        )
        missing = PolicyEvidenceSubmission(
            evidence_segment_ids=[],
            missing_reason="source_unavailable",
        )
        result_sink.append(
            _policy_done_payload(
                missing,
                [],
                session_id=resolved_session_id,
                model=str(policy_agent.model),
            )
        )
        return

    _, sources, _, submission = research_sink[0]
    if isinstance(submission, PolicyEvidenceSubmission):
        logger.info(
            "정책 typed 제출: evidence=%d missing_reason=%s",
            len(submission.evidence_segment_ids),
            submission.missing_reason,
        )
    validated = validate_policy_submission(
        submission if isinstance(submission, PolicyEvidenceSubmission) else None,
        sources,
    )
    if validated is None:
        logger.warning(
            "정책 typed 근거가 current-turn notice와 맞지 않아 fail-closed합니다(session_id=%s).",
            resolved_session_id,
        )
        validated = PolicyEvidenceSubmission(
            evidence_segment_ids=[],
            missing_reason="insufficient_evidence",
        )
        sources = []

    sections = None
    answer_model = str(policy_agent.model)
    if validated.evidence_segment_ids:
        sections = await _synthesize_policy_sections(
            user_message,
            project_policy_sources(validated, sources),
            settings,
        )
        if sections and any(source_sections for source_sections in sections.values()):
            answer_model = settings.flash_model_name

    result_sink.append(
        _policy_done_payload(
            validated,
            sources,
            session_id=resolved_session_id,
            model=answer_model,
            selection_model=str(policy_agent.model),
            sections=sections,
        )
    )
