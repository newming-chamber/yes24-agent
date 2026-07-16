"""원문 텍스트를 모델이 ID로 선택할 수 있는 안정 구간으로 변환한다."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence

_SENTENCE_TERMINATORS = frozenset(".!?。！？")
_SENTENCE_CLOSERS = frozenset("\"'’”)]}」』》")


def evidence_segment_id(text: str, *, field_path: str | None = None) -> str:
    """원문과 필드 경로가 같으면 관측 창과 무관하게 같은 ID를 만든다."""
    scope = field_path or ""
    payload = f"{scope}\0{text}".encode()
    return hashlib.sha256(payload).hexdigest()


def split_evidence_text(value: str) -> list[str]:
    """공백을 정규화한 원문을 일반 문장 경계의 연속 구간으로 나눈다."""
    text = " ".join(value.split())
    segments: list[str] = []
    start = 0
    cursor = 0
    while cursor < len(text):
        if text[cursor] not in _SENTENCE_TERMINATORS:
            cursor += 1
            continue
        end = cursor + 1
        while end < len(text) and text[end] in _SENTENCE_CLOSERS:
            end += 1
        if end < len(text) and not text[end].isspace():
            cursor += 1
            continue
        segment = text[start:end].strip()
        if segment:
            segments.append(segment)
        start = end
        while start < len(text) and text[start].isspace():
            start += 1
        cursor = start
    tail = text[start:].strip()
    if tail:
        segments.append(tail)
    return segments


def build_evidence_segments(text: str) -> list[dict]:
    """단일 관측 원문의 구간에 관측 범위와 내용 기반 ID를 붙인다."""
    segments = split_evidence_text(text)
    scope_id = evidence_segment_id("\n".join(segments), field_path="scope")
    return [
        {
            "segment_id": f"{scope_id}:{position}:{evidence_segment_id(segment)}",
            "scope_id": scope_id,
            "text": segment,
        }
        for position, segment in enumerate(segments, start=1)
    ]


def build_entry_evidence_segments(entries: Sequence[Mapping[str, str]]) -> list[dict]:
    """질문·답변 같은 entry 경계를 보존해 원문 구간과 역할을 만든다."""
    segments: list[dict] = []
    for entry in entries:
        question = entry.get("question", "").strip()
        answer = entry.get("answer", "").strip()
        if not question or not answer:
            continue
        entry_id = evidence_segment_id(question, field_path="entry")
        for role, value in (("question", question), ("answer", answer)):
            for position, segment in enumerate(split_evidence_text(value), start=1):
                text = f"질문: {segment}" if role == "question" else f"답변: {segment}"
                segments.append(
                    {
                        "segment_id": f"{entry_id}:{role}:{position}",
                        "entry_id": entry_id,
                        "scope_id": entry_id,
                        "role": role,
                        "text": text,
                    }
                )
    return segments


def qualify_evidence_segments(segments: Sequence[dict], source_id: int) -> list[dict]:
    """정책·웹 구간 ID를 current source 안에서 전역 고유하게 한정한다."""
    if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0:
        raise ValueError("source_id는 양의 정수여야 합니다")
    qualified: list[dict] = []
    for segment in segments:
        segment_id = segment.get("segment_id")
        if not isinstance(segment_id, str) or not segment_id:
            raise ValueError("evidence segment_id가 필요합니다")
        qualified.append(
            {
                **segment,
                "segment_id": f"{source_id}:{segment_id}",
            }
        )
    return qualified


def build_field_evidence_segments(
    values: Mapping[str, object], field_paths: Sequence[str]
) -> list[dict]:
    """여러 상세 필드의 원문 구간에 현재 응답 안에서 명확한 위치 ID를 붙인다."""
    observed_values: list[tuple[str, str]] = []
    for field_path in field_paths:
        value = values.get(field_path)
        texts = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        observed_values.extend(
            (field_path, text) for text in texts if isinstance(text, str)
        )
    scope_text = "\0".join(
        f"{field_path}\0{text}" for field_path, text in observed_values
    )
    scope_id = evidence_segment_id(scope_text, field_path="field_scope")
    segments: list[dict] = []
    for field_path in field_paths:
        texts = [text for path, text in observed_values if path == field_path]
        position = 0
        for text in texts:
            for segment in split_evidence_text(text):
                position += 1
                segments.append(
                    {
                        "segment_id": f"{scope_id}:{field_path}:{position}",
                        "scope_id": scope_id,
                        "field_path": field_path,
                        "text": segment,
                    }
                )
    return segments


def select_source_evidence_segments(
    segment_ids: Sequence[str],
    current_sources: Sequence[Mapping[str, object]],
    *,
    source_type: str,
    selection_validator: Callable[
        [Mapping[str, object], Sequence[Mapping[str, object]]], bool
    ]
    | None = None,
) -> dict[int, list[str]] | None:
    """flat ID를 이번 턴 출처의 exact 구간으로 역해석해 문서 순서로 묶는다."""
    requested_ids = list(segment_ids)
    if not requested_ids or len(requested_ids) != len(set(requested_ids)):
        return None

    sources: list[tuple[int, Mapping[str, object], list[Mapping[str, object]]]] = []
    segments_by_id: dict[str, list[tuple[int, Mapping[str, object]]]] = {}
    for source in current_sources:
        source_id = source.get("id")
        raw_segments = source.get("_evidence_segments")
        if (
            source.get("type") != source_type
            or isinstance(source_id, bool)
            or not isinstance(source_id, int)
            or not isinstance(raw_segments, list)
        ):
            continue
        valid_segments = [
            segment
            for segment in raw_segments
            if isinstance(segment, Mapping)
            and isinstance(segment.get("segment_id"), str)
            and isinstance(segment.get("text"), str)
            and segment["text"]
        ]
        sources.append((source_id, source, valid_segments))
        for segment in valid_segments:
            segments_by_id.setdefault(segment["segment_id"], []).append(
                (source_id, segment)
            )

    selected_ids = set(requested_ids)
    if any(len(segments_by_id.get(segment_id, [])) != 1 for segment_id in selected_ids):
        return None

    selected_texts: dict[int, list[str]] = {}
    for source_id, source, segments in sources:
        selected_segments = [
            segment for segment in segments if segment["segment_id"] in selected_ids
        ]
        if not selected_segments:
            continue
        if selection_validator is not None and not selection_validator(
            source,
            selected_segments,
        ):
            return None
        selected_texts[source_id] = [segment["text"] for segment in selected_segments]
    return selected_texts if sum(map(len, selected_texts.values())) == len(selected_ids) else None


def project_selected_source_texts(
    selected_texts: Mapping[int, Sequence[str]],
    current_sources: Sequence[Mapping[str, object]],
) -> list[dict]:
    """선택된 원문만 공개 snippet으로 남기고 내부 구간 목록을 제거한다."""
    by_id = {source.get("id"): source for source in current_sources}
    projected: list[dict] = []
    for source_id, texts in selected_texts.items():
        source = by_id[source_id]
        projection = {
            key: value
            for key, value in source.items()
            if key not in {"snippet", "_evidence_segments"}
        }
        projection["snippet"] = "\n\n".join(texts)
        projected.append(projection)
    return projected
