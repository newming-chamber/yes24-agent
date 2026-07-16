"""Yes24 정책 원문 구간 선택과 current-turn 검증·렌더링."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yes24_agent.agent_runtime import restore_terminal_evidence_ids
from yes24_agent.evidence_segments import (
    project_selected_source_texts,
    select_source_evidence_segments,
)
from yes24_agent.postprocess import render_cited_source_texts
from yes24_agent.yes24.urls import POLICY_SEEDS

PolicyMissingReason = Literal[
    "source_unavailable",
    "insufficient_evidence",
    "needs_clarification",
]

_MISSING_MESSAGE = "Yes24 정책 원문에서 질문에 직접 답하는 근거를 확인하지 못했어요."
_POLICY_SEED_ROLES = {seed["url"]: seed["role"] for seed in POLICY_SEEDS.values()}
POLICY_DIRECTORY_URLS = frozenset(
    seed["url"] for seed in POLICY_SEEDS.values() if seed["role"] == "directory"
)


def is_selectable_policy_source(url: object, segments: object) -> bool:
    """명시적 seed role 또는 완결 FAQ Q+A 구조가 있는 정책 원문인지 판정한다."""
    seed_role = _POLICY_SEED_ROLES.get(url)
    if seed_role is not None:
        return seed_role == "document"
    if not isinstance(segments, list):
        return False
    roles_by_entry: dict[str, set[str]] = {}
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        entry_id = segment.get("entry_id")
        role = segment.get("role")
        if isinstance(entry_id, str) and role in {"question", "answer"}:
            roles_by_entry.setdefault(entry_id, set()).add(role)
    return any({"question", "answer"}.issubset(roles) for roles in roles_by_entry.values())


class PolicyEvidenceSubmission(BaseModel):
    """정책 exact 원문 ID 선택 또는 근거 부족을 나타내는 typed 제출."""

    model_config = ConfigDict(extra="forbid")

    evidence_segment_ids: list[str] = Field(default_factory=list)
    missing_reason: PolicyMissingReason | None = None

    @model_validator(mode="after")
    def _evidence_xor_missing(self) -> PolicyEvidenceSubmission:
        if bool(self.evidence_segment_ids) == bool(self.missing_reason):
            raise ValueError("evidence_segment_ids와 missing_reason 중 하나만 제출해야 합니다")
        return self


def validate_policy_submission(
    submission: PolicyEvidenceSubmission | None,
    current_sources: list[dict],
) -> PolicyEvidenceSubmission | None:
    """선택한 ID를 이번 턴 notice 응답의 동일 원문 구간과 대조한다."""
    if submission is None:
        return None
    if not submission.evidence_segment_ids:
        return submission if submission.missing_reason else None

    evidence_sources = [
        source
        for source in current_sources
        if is_selectable_policy_source(
            source.get("url"),
            source.get("_evidence_segments"),
        )
    ]
    restored_ids = restore_terminal_evidence_ids(
        submission.evidence_segment_ids,
        evidence_sources,
        source_type="notice",
    )
    if restored_ids is None:
        return None
    completed_ids = _complete_policy_entry_questions(restored_ids, evidence_sources)
    if completed_ids is None:
        return None
    submission = submission.model_copy(update={"evidence_segment_ids": completed_ids})
    selected = select_source_evidence_segments(
        submission.evidence_segment_ids,
        current_sources,
        source_type="notice",
        selection_validator=_valid_policy_entry_selection,
    )
    return submission if selected is not None else None


def _complete_policy_entry_questions(
    selected_ids: list[str],
    current_sources: list[dict],
) -> list[str] | None:
    """선택 answer가 속한 FAQ question을 같은 current-turn entry에서 보충한다."""
    if len(selected_ids) != len(set(selected_ids)):
        return None
    segments_by_id: dict[str, dict] = {}
    questions_by_entry: dict[str, list[str]] = {}
    for source in current_sources:
        for segment in source.get("_evidence_segments", []):
            if not isinstance(segment, dict):
                continue
            segment_id = segment.get("segment_id")
            if not isinstance(segment_id, str) or segment_id in segments_by_id:
                continue
            segments_by_id[segment_id] = segment
            if (
                isinstance(segment.get("entry_id"), str)
                and segment.get("role") == "question"
            ):
                questions_by_entry.setdefault(segment["entry_id"], []).append(segment_id)

    if any(segment_id not in segments_by_id for segment_id in selected_ids):
        return None
    completed = list(selected_ids)
    selected_entries = {
        segment["entry_id"]
        for segment_id in selected_ids
        if isinstance((segment := segments_by_id[segment_id]).get("entry_id"), str)
        and segment.get("role") == "answer"
    }
    for entry_id in selected_entries:
        for question_id in questions_by_entry.get(entry_id, []):
            if question_id not in completed:
                completed.append(question_id)
    return completed


def _valid_policy_entry_selection(_source, segments) -> bool:
    """선택된 각 FAQ entry에 질문과 답변이 모두 포함됐는지 확인한다."""
    roles_by_entry: dict[str, set[str]] = {}
    for segment in segments:
        entry_id = segment.get("entry_id")
        role = segment.get("role")
        if entry_id is None and role is None:
            continue
        if not isinstance(entry_id, str) or role not in {"question", "answer"}:
            return False
        roles_by_entry.setdefault(entry_id, set()).add(role)
    return all({"question", "answer"}.issubset(roles) for roles in roles_by_entry.values())


def _selected_policy_texts(
    submission: PolicyEvidenceSubmission,
    current_sources: list[dict],
) -> dict[int, list[str]]:
    """검증된 flat ID를 출처별 문서 순서의 원문으로 확장한다."""
    selected = select_source_evidence_segments(
        submission.evidence_segment_ids,
        current_sources,
        source_type="notice",
        selection_validator=_valid_policy_entry_selection,
    )
    if selected is None:
        raise ValueError("정책 원문 범위가 current-turn 출처와 일치하지 않습니다")
    return selected


def render_policy_submission(
    submission: PolicyEvidenceSubmission,
    current_sources: list[dict],
) -> str:
    """표현 합성 실패 시 검증된 정책 원문과 인용을 안전하게 렌더한다."""
    if not submission.evidence_segment_ids:
        return _MISSING_MESSAGE

    selected = _selected_policy_texts(submission, current_sources)
    return render_cited_source_texts(selected)


def project_policy_sources(
    submission: PolicyEvidenceSubmission,
    current_sources: list[dict],
) -> list[dict]:
    """최종 출처에는 선택된 정책 원문만 snippet으로 공개한다."""
    selected_texts = _selected_policy_texts(submission, current_sources)
    return project_selected_source_texts(selected_texts, current_sources)
