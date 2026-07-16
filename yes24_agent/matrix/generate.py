"""중앙 selector의 상세 근거로 16개 RBTI 카드를 생성한다."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from yes24_agent.event_translate import project_public_source
from yes24_agent.matrix.planning import matrix_codes
from yes24_agent.matrix.retrieval import SharedPool
from yes24_agent.postprocess import (
    build_done_payload,
    escape_citation_markers,
    validate_citations,
)
from yes24_agent.product_selection import (
    ProductSelection,
    ProductSelectionSubmission,
    render_product_submission,
    resolve_product_rationale,
    validate_product_submission,
)
from yes24_agent.rbti.persona import AXIS_ORDER, AXIS_VALUE_LABELS_KO

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColumnResult:
    code: str
    col: int
    text: str
    picks: list[dict]
    done_payload: dict
    gate_reason: str | None


def _render_product_column(pool: SharedPool, code: str) -> tuple[str, list[dict]]:
    planned_picks = pool.picks.get(code)
    if not planned_picks or len(planned_picks) != pool.requested_count:
        raise ValueError("matrix pick 수가 요청 수량과 다름")
    by_id = {candidate["source_id"]: candidate for candidate in pool.candidates}
    if any(pick.source_id not in by_id for pick in planned_picks):
        raise ValueError("matrix selected source가 상세 후보에 없음")
    if any(
        pick.rationale.constraint_text is None
        or pick.rationale.constraint_text not in pool.question
        for pick in planned_picks
    ):
        raise ValueError("matrix 추천 이유의 질문 원문 span이 무효")

    submission = ProductSelectionSubmission(
        selections=[
            ProductSelection(
                source_id=pick.source_id,
                evidence_fields=[pick.rationale.evidence_field],
                rationales=[pick.rationale],
            )
            for pick in planned_picks
        ],
    )
    validated = validate_product_submission(
        submission,
        pool.sources,
        expected_constraints=pool.expected_constraints,
        expected_count=pool.requested_count,
    )
    if validated is None:
        raise ValueError("matrix 선택 근거가 상세 출처 원문과 일치하지 않음")

    sources_by_id = {source["id"]: source for source in pool.sources}
    public_sources = [project_public_source(source) for source in pool.sources]
    selections_by_id = {selection.source_id: selection for selection in validated.selections}
    values_by_axis = {axis: value for value, (axis, _allowed) in zip(code, AXIS_ORDER)}
    blocks: list[str] = []
    for pick in planned_picks:
        selection = selections_by_id[pick.source_id]
        facts_submission = validated.model_copy(
            update={"selections": [selection.model_copy(update={"rationales": []})]}
        )
        facts = render_product_submission(facts_submission, public_sources)
        rationale_text = resolve_product_rationale(pick.rationale, sources_by_id[pick.source_id])
        if rationale_text is None or pick.rationale.constraint_text is None:
            raise ValueError("matrix 추천 이유 원문 구간을 찾을 수 없음")
        connection = " · ".join(
            AXIS_VALUE_LABELS_KO[axis][values_by_axis[axis]] for axis in pick.axis_connections
        )
        reason = (
            f"- RBTI 선택 관점: {connection}\n"
            f"- Yes24 상세 근거: {escape_citation_markers(rationale_text)} [{pick.source_id}]"
        )
        blocks.append(f"{facts}\n{reason}")
    return "\n\n".join(blocks), [{"id": pick.source_id} for pick in planned_picks]


def _fallback_column(
    pool: SharedPool, code: str, col: int, reason: str, session_id: str
) -> ColumnResult:
    if pool.status == "empty":
        notice = (
            "이 16뷰는 Yes24 도서 탐색 전용이에요. "
            "이 질문과 관련된 Yes24 도서 근거를 찾지 못했어요."
        )
    elif pool.status == "no_match":
        notice = "Yes24 상세 근거에서 요청한 조건을 만족하는 도서를 찾지 못했어요."
    else:
        notice = "지금은 답을 완성하지 못했어요. 잠시 후 다시 시도해 볼 수 있어요."
    citation = validate_citations(notice, pool.sources)
    done_payload = build_done_payload(
        pool.sources, citation.used_source_ids, session_id, citation.supports
    )
    return ColumnResult(code, col, notice, [], done_payload, reason)


async def generate_column(
    pool: SharedPool,
    code: str,
    col: int,
    *,
    session_id: str = "",
) -> ColumnResult:
    try:
        raw, picks = _render_product_column(pool, code)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("matrix 생성 실패 code=%s: %s", code, exc)
        return _fallback_column(pool, code, col, "error", session_id)
    if not raw:
        return _fallback_column(pool, code, col, "empty", session_id)

    citation = validate_citations(raw, pool.sources)
    if not citation.used_source_ids:
        return _fallback_column(pool, code, col, "unsourced", session_id)
    done_payload = build_done_payload(
        pool.sources, citation.used_source_ids, session_id, citation.supports
    )
    return ColumnResult(code, col, citation.text, picks, done_payload, None)


async def generate_matrix(
    pool: SharedPool,
    *,
    session_id: str = "",
) -> AsyncIterator[ColumnResult]:
    codes = matrix_codes()
    if pool.status != "ok":
        for col, code in enumerate(codes):
            yield _fallback_column(pool, code, col, pool.status, session_id)
        return

    for col, code in enumerate(codes):
        yield await generate_column(pool, code, col, session_id=session_id)
