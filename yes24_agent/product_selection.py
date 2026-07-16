"""상품 선택의 구조화 제출, current-turn 검증, 서버 소유 렌더링."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from yes24_agent.postprocess import escape_citation_markers

MissingReason = Literal[
    "no_results",
    "detail_unavailable",
    "insufficient_evidence",
    "needs_clarification",
]
EvidenceField = Literal[
    "title",
    "author",
    "publisher",
    "pub_date",
    "price",
    "rating",
    "page_count",
    "intro",
    "toc",
    "pub_review",
    "weekly_reviews",
]
RationaleEvidenceField = Literal[
    "title",
    "intro",
    "toc",
    "pub_review",
    "weekly_reviews",
]
ContentRationaleEvidenceField = Literal[
    "intro",
    "toc",
    "pub_review",
    "weekly_reviews",
]
PRODUCT_RATIONALE_FIELDS: tuple[RationaleEvidenceField, ...] = (
    "title",
    "intro",
    "toc",
    "pub_review",
    "weekly_reviews",
)


class NumericEvidenceField(str, Enum):
    PRICE = "price"
    RATING = "rating"
    PAGE_COUNT = "page_count"


class ConstraintOperator(str, Enum):
    LT = "lt"
    LTE = "lte"
    EQ = "eq"
    GTE = "gte"
    GT = "gt"


_MISSING_MESSAGES: dict[str, str] = {
    "no_results": (
        "이번 Yes24 검색 범위에서 질문에 맞는 상품을 확인하지 못했어요. "
        "이 결과만으로 해당 상품이 존재하지 않는다고 단정할 수는 없어요."
    ),
    "detail_unavailable": "상품 상세 근거를 확인하지 못해 사실을 단정하지 않았어요.",
    "insufficient_evidence": "질문의 조건을 뒷받침할 상품 근거가 충분하지 않았어요.",
    "needs_clarification": "찾는 상품의 분야나 대상, 조건을 조금 더 알려 주세요.",
}

_EVIDENCE_FIELD_LABELS: dict[EvidenceField, str] = {
    "title": "상품명",
    "author": "저자",
    "publisher": "출판사",
    "pub_date": "출간일",
    "price": "판매가",
    "rating": "평점",
    "page_count": "쪽수",
    "intro": "상품 소개",
    "toc": "목차",
    "pub_review": "출판사 리뷰",
    "weekly_reviews": "독자 리뷰",
}


class ProductRationale(BaseModel):
    """사용자 원문 조건과 상세 원문 구간을 잇는 검증 가능한 참조."""

    model_config = ConfigDict(extra="forbid")

    evidence_field: RationaleEvidenceField
    segment_id: str = Field(min_length=1)
    constraint_text: str | None = Field(default=None, min_length=1)


class ProductContentRationale(BaseModel):
    """제목 반복이 아닌 상세 본문의 선택 근거 참조."""

    model_config = ConfigDict(extra="forbid")

    evidence_field: ContentRationaleEvidenceField
    segment_id: str = Field(min_length=1)


class ProductSelection(BaseModel):
    """모델이 고른 상품과 같은 상세 응답 안의 근거 필드."""

    model_config = ConfigDict(extra="forbid")

    source_id: int = Field(gt=0)
    evidence_fields: list[EvidenceField]
    rationales: list[ProductRationale] = Field(default_factory=list)
    content_rationale: ProductContentRationale | None = None

    @field_validator("evidence_fields")
    @classmethod
    def _unique_fields(cls, fields: list[EvidenceField]) -> list[EvidenceField]:
        unique: list[EvidenceField] = []
        for field in fields:
            cleaned = field.strip()
            if cleaned and cleaned not in unique:
                unique.append(cleaned)
        if not unique:
            raise ValueError("evidence_fields가 하나 이상 필요합니다")
        return unique


class ProductConstraint(BaseModel):
    """모델이 원 질문에서 추출한 canonical 숫자 조건."""

    model_config = ConfigDict(extra="forbid")

    field: NumericEvidenceField
    operator: ConstraintOperator
    value: int | float

    @field_validator("value", mode="before")
    @classmethod
    def _numeric_value(cls, value: int | float) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("constraint value는 숫자여야 합니다")
        if not math.isfinite(value):
            raise ValueError("constraint value는 유한한 숫자여야 합니다")
        return value


class ProductSelectionSubmission(BaseModel):
    """선택 성공 또는 근거 부족을 나타내는 typed 제출."""

    model_config = ConfigDict(extra="forbid")

    selections: list[ProductSelection] = Field(default_factory=list)
    missing_reason: MissingReason | None = None

    @model_validator(mode="after")
    def _normalize_selection_state(self) -> ProductSelectionSubmission:
        if self.selections:
            self.missing_reason = None
        elif self.missing_reason is None:
            raise ValueError("빈 selections에는 missing_reason이 필요합니다")
        return self


def resolve_product_rationale(
    rationale: ProductRationale | ProductContentRationale,
    source: dict,
) -> str | None:
    """구조화된 추천 이유 ID를 같은 상세 출처의 실제 원문으로 역참조한다."""
    segments = source.get("_evidence_segments")
    if not isinstance(segments, list):
        return None
    for segment in segments:
        if (
            isinstance(segment, dict)
            and segment.get("segment_id") == rationale.segment_id
            and segment.get("field_path") == rationale.evidence_field
            and isinstance(segment.get("text"), str)
            and segment["text"]
        ):
            return segment["text"]
    return None


def validate_product_submission(
    submission: ProductSelectionSubmission | None,
    current_sources: list[dict],
    *,
    expected_constraints: Sequence[ProductConstraint],
    expected_count: int | None = None,
    user_query: str | None = None,
) -> ProductSelectionSubmission | None:
    """선택·근거를 이번 턴 상세와, 상류의 숫자 조건을 canonical 값과 대조한다."""
    if submission is None:
        return None
    if not submission.selections:
        return submission if submission.missing_reason else None
    if expected_count is not None and len(submission.selections) != expected_count:
        return None

    detail_sources = {
        source.get("id"): source
        for source in current_sources
        if source.get("type") == "book_detail" and source.get("id") is not None
    }
    selected_ids: set[int] = set()
    validated_selections: list[ProductSelection] = []
    for selection in submission.selections:
        if selection.source_id in selected_ids or selection.source_id not in detail_sources:
            return None
        evidence_source = detail_sources[selection.source_id]
        available_fields = product_evidence_fields(evidence_source)
        if any(field not in available_fields for field in selection.evidence_fields):
            return None
        if user_query is not None:
            if (
                selection.content_rationale is None
                or resolve_product_rationale(
                    selection.content_rationale,
                    evidence_source,
                )
                is None
            ):
                return None
        elif (
            selection.content_rationale is not None
            and resolve_product_rationale(selection.content_rationale, evidence_source) is None
        ):
            return None
        seen_rationales: set[tuple[str | None, str, str]] = set()
        for rationale in selection.rationales:
            if resolve_product_rationale(rationale, evidence_source) is None:
                return None
            reference = (
                rationale.constraint_text,
                rationale.evidence_field,
                rationale.segment_id,
            )
            if reference in seen_rationales:
                return None
            seen_rationales.add(reference)
            if user_query is not None:
                if (
                    rationale.constraint_text is None
                    or rationale.constraint_text not in user_query
                ):
                    return None
        if not product_constraints_satisfied(evidence_source, expected_constraints):
            return None
        selected_ids.add(selection.source_id)
        validated_selections.append(selection)
    return submission.model_copy(update={"selections": validated_selections})


def render_product_submission(
    submission: ProductSelectionSubmission,
    current_sources: list[dict],
    contextual_rationales: Mapping[int, str] | None = None,
) -> str:
    """검증된 canonical 사실과 격리 작성 또는 exact 폴백 이유를 렌더한다."""
    if not submission.selections:
        return _MISSING_MESSAGES.get(submission.missing_reason or "", "")

    by_id = {source["id"]: source for source in current_sources}
    blocks: list[str] = []
    for selection in submission.selections:
        source = by_id[selection.source_id]
        facts = _product_facts(source)

        title = escape_citation_markers(source.get("title") or "제목 미상")
        lines = [f"**{title}** [{selection.source_id}]"]
        if facts:
            lines.append(f"- {' · '.join(facts)} [{selection.source_id}]")
        if selection.content_rationale is not None:
            rationale_text = (
                contextual_rationales.get(selection.source_id)
                if contextual_rationales is not None
                else resolve_product_rationale(selection.content_rationale, source)
            )
            if rationale_text is None:
                raise ValueError("검증된 추천 이유 원문 구간을 찾을 수 없습니다")
            lines.append(
                f"- {escape_citation_markers(rationale_text)} [{selection.source_id}]"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def project_product_sources(
    submission: ProductSelectionSubmission,
    current_sources: list[dict],
) -> list[dict]:
    """selected source의 canonical 필드만 복사하고 원문 snippet은 공개하지 않는다."""
    by_id = {source.get("id"): source for source in current_sources}
    projected: list[dict] = []
    for selection in submission.selections:
        source = by_id[selection.source_id]
        projection = {
            key: value
            for key, value in source.items()
            if key
            not in {
                "snippet",
                "_evidence_fields",
                "_evidence_segments",
            }
        }
        rationale_texts = [
            *(
                [resolve_product_rationale(selection.content_rationale, source)]
                if selection.content_rationale is not None
                else []
            ),
            *[
            text
            for rationale in selection.rationales
            if (text := resolve_product_rationale(rationale, source)) is not None
            ],
        ]
        if any(text is None for text in rationale_texts):
            raise ValueError("검증된 추천 이유 원문 구간을 찾을 수 없습니다")
        if rationale_texts:
            projection["snippet"] = "\n\n".join(
                dict.fromkeys(text for text in rationale_texts if text is not None)
            )
        projected.append(projection)
    return projected


def _product_facts(source: dict) -> list[str]:
    """공개 source DTO에 존재하는 canonical 상품 필드만 표시한다."""
    facts: list[str] = []
    for label, field in (
        ("저자", "author"),
        ("출판사", "publisher"),
        ("출간일", "pub_date"),
    ):
        value = source.get(field)
        if value:
            facts.append(f"{label}: {escape_citation_markers(str(value))}")
    price = source.get("price")
    if isinstance(price, int):
        facts.append(f"판매가: {price:,}원")
    page_count = source.get("page_count")
    if isinstance(page_count, int):
        facts.append(f"쪽수: {page_count}쪽")
    rating = source.get("rating")
    if isinstance(rating, (int, float)):
        facts.append(f"평점: {rating:g}")
    return facts


def product_evidence_fields(source: dict) -> set[EvidenceField]:
    """상세 DTO에서 실제 nonempty인 허용 근거 필드 이름을 반환한다."""
    observed = source.get("_evidence_fields")
    if isinstance(observed, list):
        return {field for field in observed if field in _EVIDENCE_FIELD_LABELS}

    meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    available: set[EvidenceField] = set()
    for field in _EVIDENCE_FIELD_LABELS:
        value = source.get(field, meta.get(field))
        if isinstance(value, str) and value.strip():
            available.add(field)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            available.add(field)
        elif isinstance(value, list) and any(
            isinstance(item, str) and item.strip() for item in value
        ):
            available.add(field)
    return available


def product_constraints_satisfied(
    source: dict,
    constraints: Sequence[ProductConstraint],
) -> bool:
    """상세 DTO의 canonical 숫자값이 모든 구조화 조건을 만족하는지 판정한다."""
    meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    for constraint in constraints:
        field = constraint.field.value
        observed = source.get(field, meta.get(field))
        if not isinstance(observed, (int, float)) or isinstance(observed, bool):
            return False
        target = constraint.value
        if constraint.operator is ConstraintOperator.LT and not observed < target:
            return False
        if constraint.operator is ConstraintOperator.LTE and not observed <= target:
            return False
        if constraint.operator is ConstraintOperator.EQ and not observed == target:
            return False
        if constraint.operator is ConstraintOperator.GTE and not observed >= target:
            return False
        if constraint.operator is ConstraintOperator.GT and not observed > target:
            return False
    return True
