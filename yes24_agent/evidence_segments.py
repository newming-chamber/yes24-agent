"""원문 텍스트를 모델이 ID로 선택할 수 있는 안정 구간으로 변환한다."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

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
