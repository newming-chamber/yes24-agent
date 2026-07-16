"""최신성 웹 답변의 current-turn 원문 구간 선택·검증·투영."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yes24_agent.agent_runtime import restore_terminal_evidence_ids
from yes24_agent.evidence_segments import (
    project_selected_source_texts,
    select_source_evidence_segments,
)
from yes24_agent.postprocess import render_cited_source_texts

RecencyMissingReason = Literal[
    "source_unavailable",
    "reference_time_unavailable",
    "needs_clarification",
]

_MISSING_MESSAGES: dict[RecencyMissingReason, str] = {
    "source_unavailable": "최신 정보를 확인할 웹 근거를 가져오지 못했어요.",
    "reference_time_unavailable": "요청한 기준 시점을 직접 확인할 수 있는 웹 근거를 찾지 못했어요.",
    "needs_clarification": "확인할 대상이나 기준 시점을 조금 더 구체적으로 알려 주세요.",
}


class RecencyEvidenceSubmission(BaseModel):
    """최신성 exact 원문 ID 선택 또는 근거 부족을 나타내는 typed 제출."""

    model_config = ConfigDict(extra="forbid")

    evidence_segment_ids: list[str] = Field(default_factory=list)
    missing_reason: RecencyMissingReason | None = None

    @model_validator(mode="after")
    def _evidence_or_missing(self) -> RecencyEvidenceSubmission:
        if bool(self.evidence_segment_ids) == (self.missing_reason is not None):
            raise ValueError("evidence_segment_ids와 missing_reason 중 정확히 하나가 필요합니다")
        return self


def validate_recency_submission(
    submission: RecencyEvidenceSubmission | None,
    current_sources: list[dict],
) -> RecencyEvidenceSubmission | None:
    """선택한 ID를 이번 턴 웹 응답의 동일 원문 구간과 대조한다."""
    if submission is None:
        return None
    if submission.missing_reason:
        return submission

    restored_ids = restore_terminal_evidence_ids(
        submission.evidence_segment_ids,
        current_sources,
        source_type="web",
    )
    if restored_ids is None:
        return None
    submission = submission.model_copy(update={"evidence_segment_ids": restored_ids})
    selected = select_source_evidence_segments(
        submission.evidence_segment_ids,
        current_sources,
        source_type="web",
    )
    if selected is None:
        return None
    return submission


def _selected_recency_texts(
    submission: RecencyEvidenceSubmission,
    current_sources: list[dict],
) -> dict[int, list[str]]:
    """검증된 flat ID를 출처별 문서 순서의 원문으로 확장한다."""
    selected = select_source_evidence_segments(
        submission.evidence_segment_ids,
        current_sources,
        source_type="web",
    )
    if selected is None:
        raise ValueError("최신성 원문 범위가 current-turn 출처와 일치하지 않습니다")
    return selected


def render_recency_submission(
    submission: RecencyEvidenceSubmission,
    current_sources: list[dict],
) -> str:
    """표현 합성 실패 시 검증된 웹 원문 또는 근거 부족을 안전하게 렌더한다."""
    if not submission.evidence_segment_ids:
        return render_recency_limitation(submission)

    selected = _selected_recency_texts(submission, current_sources)
    return render_cited_source_texts(selected)


def render_recency_limitation(
    submission: RecencyEvidenceSubmission,
) -> str:
    """근거 부족 상태를 과장 없이 고정 문구로 표시한다."""
    return _MISSING_MESSAGES[submission.missing_reason or "reference_time_unavailable"]


def project_recency_sources(
    submission: RecencyEvidenceSubmission,
    current_sources: list[dict],
) -> list[dict]:
    """최종 출처에는 선택된 최신성 원문만 snippet으로 공개한다."""
    selected_texts = _selected_recency_texts(submission, current_sources)
    return project_selected_source_texts(selected_texts, current_sources)
