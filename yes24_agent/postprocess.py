"""인용 마커 검증 — 답변 본문의 `[n]`·`[n, m, ...]` 마커를 실제 출처와 대조한다.

모델(Gemini)은 프롬프트 지시만으로는 25~50%의 인용 오류율을 보이므로(Stanford/Tow Center),
합성된 답변을 그대로 신뢰하지 않고 사후 검증한다. 존재하지 않는 source_id를 가리키는
마커(또는 그룹형 마커 내부의 개별 id)는 본문에서 제거하고 로그를 남긴다. 이 모듈은 순수
함수 계층으로, config·ADK를 import하지 않는다.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from yes24_agent.event_translate import project_public_source

logger = logging.getLogger(__name__)

# 서버가 붙인 ASCII 대괄호+숫자만 마커로 간주한다. 원문 안의 예약 문법은
# `escape_citation_markers`가 전각 괄호로 바꿔 모델/도구 데이터와 서버 마커를 구분한다.
MARKER_PATTERN = re.compile(r"(?<!\\)\[(\d+(?:\s*,\s*\d+)*)\]")
EVIDENCE_UNAVAILABLE_TEXT = (
    "근거를 확인하지 못해 답을 단정하지 않았어요. 잠시 후 다시 시도해 주세요."
)


def escape_citation_markers(text: str) -> str:
    """신뢰하지 않는 원문 안의 인용 예약 문법을 일반 표시 텍스트로 이스케이프한다."""
    return MARKER_PATTERN.sub(lambda match: f"［{match.group(1)}］", text)


def render_cited_source_texts(selected_texts: Mapping[int, Sequence[str]]) -> str:
    """서버가 선택한 출처별 원문에 예약 마커를 이스케이프하고 인용을 붙인다."""
    return "\n\n".join(
        f"{escape_citation_markers(' '.join(texts))} [{source_id}]"
        for source_id, texts in selected_texts.items()
    )


def cited_ids(text: str) -> set[int]:
    """본문이 인용한 source_id 집합(복합 마커 `[1, 2]`를 펼쳐 반환).

    "본문에서 인용 id 뽑기"의 **단일 정의**다. 예전엔 이 판정이 세 모듈에 각기 구현돼 있었고
    의미까지 달랐다. 인용 마커 문법은 이 함수 한 곳에서만 정의한다.
    """
    return {
        int(n) for match in MARKER_PATTERN.finditer(text or "") for n in match.group(1).split(",")
    }


# 소수점은 문장 경계가 아니다. 점 양쪽이 모두 숫자인 경우만 제외하고 나머지 문장부호를 찾는다.
SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?<!\d)\.|\.(?!\d)|[!?\n]")
_PUNCT = re.compile(r"[^\w\s]")


@dataclass
class CitationResult:
    """인용 마커 검증 결과."""

    text: str
    """무효 마커가 제거된 최종 본문."""
    supports: list[dict]
    """최종 본문 기준, 유효 마커마다 하나씩(중복 허용) 대응하는 근거 세그먼트.

    그룹형 마커(`[2, 3, 4]`)는 하나의 segment에 `source_ids`가 여러 개 담긴다
    (Gemini `groundingSupports` 스키마와 동일하게 다중 source_ids를 지원).

    [프론트 계약] start_index/end_index는 Python 문자(유니코드 코드포인트) 기준이다.
    JS의 `String.prototype.slice`는 UTF-16 코드유닛 기준이라 이모지 등 astral 문자가
    앞에 있으면 인덱스가 어긋난다 — 프론트는 인덱스 대신 `segment.text`를 신뢰할 것.
    """
    used_source_ids: list[int]
    """실제로 인용된 출처 id (등장 순서, 중복 제거)."""
    removed_markers: list[str]
    """제거된 무효 마커(또는 그룹형 마커 내부에서 제거된 개별 id) 원문 (예: `"[9]"`)."""

    @property
    def meaningful_support_count(self) -> int:
        """실제 주장 문자가 있는 support 개수."""
        return sum(support_is_meaningful(support) for support in self.supports)


def support_is_meaningful(support: dict) -> bool:
    """마커 앞 구간에 문자나 숫자가 있어 검증할 주장이 존재하는지 판정한다."""
    segment = support.get("segment")
    text = segment.get("text") if isinstance(segment, dict) else None
    return isinstance(text, str) and any(char.isalnum() for char in text)


def validate_citations(text: str, sources: list[dict]) -> CitationResult:
    """본문의 `[n]`·`[n, m, ...]` 마커를 `sources`의 id 집합과 대조해 검증한다.

    그룹형 마커는 내부 id를 각각 검증한다:
    - 전부 유효 → 마커 원문을 그대로 유지한다.
    - 일부만 유효 → 유효 id만 남긴 형태로 재작성한다 (예: `[2, 99, 3]` → `[2, 3]`).
    - 전부 무효 → 마커 전체를 제거한다 (단일 무효 마커와 동일하게 처리).

    유효하지 않은 id는 경고 로그를 남기고 `removed_markers`에 기록한다.
    반환되는 `supports`의 인덱스는 (재작성·제거 반영 후) 최종 본문 기준이다.

    """
    valid_ids = {source["id"] for source in sources}

    cleaned_parts: list[str] = []
    # (최종 본문 기준 마커 시작/끝 인덱스, 마커에 담긴 유효 source_id 목록)
    marker_positions: list[tuple[int, int, list[int]]] = []
    removed_markers: list[str] = []
    used_source_ids: list[int] = []
    seen_ids: set[int] = set()

    cursor = 0  # 원문 기준 커서
    output_len = 0  # 지금까지 만들어진 cleaned 본문의 길이

    for match in MARKER_PATTERN.finditer(text):
        raw_ids = [int(part) for part in match.group(1).split(",")]

        prefix = text[cursor : match.start()]
        cleaned_parts.append(prefix)
        output_len += len(prefix)
        cursor = match.end()

        # 마커 내부 id를 유효/무효로 분리한다 (등장 순서 유지, 마커 내부 중복은 제거)
        valid_in_marker: list[int] = []
        seen_in_marker: set[int] = set()
        invalid_in_marker: list[int] = []
        for source_id in raw_ids:
            if source_id in valid_ids:
                if source_id not in seen_in_marker:
                    seen_in_marker.add(source_id)
                    valid_in_marker.append(source_id)
            else:
                invalid_in_marker.append(source_id)

        if not valid_in_marker:
            # 전부 무효 → 마커 전체 제거 (기존 단일 무효 마커와 동일 처리)
            removed_markers.append(match.group(0))
            logger.warning(
                "존재하지 않는 source_id(%s)를 인용한 마커 %s를 본문에서 제거합니다.",
                ", ".join(str(i) for i in invalid_in_marker),
                match.group(0),
            )
            # 마커 제거 경계 처리: 마커는 양옆의 잉여 공백을 함께 소유하므로, 지울 때 앞 조각의
            # 후행 공백과 뒤 본문의 선두 공백을 흡수한 뒤 seam을 잇는다. 공백은 새로 만들지 않고
            # **삭제만** 한다. 양쪽이 이어 붙으면 안 되는 자리에만 한 칸을 남긴다.
            # 이어붙일지(공백 0)는 인접 문자가 구두점(_PUNCT: \w·\s가 아닌 문자)이면 그 앞에 공백을
            # 두지 않는 조판 규칙으로 판정한다. 특정 부호를 열거하지 않는 구조 술어다.
            # 모델이 부호 앞에 공백을 흘렸든(`습니다 . [9]`) 뒤에 흘렸든(`발생합니다[9] .`) 고아
            # 공백이 남지 않는다. 델타 조정으로 이후 유효 마커의 인덱스를 유지한다.
            rest = text[cursor:]
            rest_core = rest.lstrip(" \t")
            prefix = cleaned_parts[-1] if cleaned_parts else ""
            prefix_core = prefix.rstrip(" \t")
            # 앞 조각 꼬리가 "공백+구두점"이면(모델이 부호 앞에 공백을 흘린 잔재) 그 공백을 없앤다.
            if prefix_core and _PUNCT.match(prefix_core[-1]):
                prefix_core = prefix_core[:-1].rstrip(" \t") + prefix_core[-1]
            seam_had_space = prefix_core != prefix or rest_core != rest
            attach = (
                not prefix_core
                or not rest_core
                or bool(_PUNCT.match(rest_core[0]))  # 구두점은 앞 공백 없이 붙인다
                or not seam_had_space  # 원래 공백이 없던 자리는 그대로 붙인다
            )
            new_prefix = prefix_core if attach else prefix_core + " "
            output_len -= len(prefix) - len(new_prefix)
            cleaned_parts[-1] = new_prefix
            cursor += len(rest) - len(rest_core)  # 뒤 본문 선두 공백 흡수(미출력)
            continue

        if invalid_in_marker:
            # 일부만 무효 → 유효 id만 남긴 형태로 재작성
            marker_text = f"[{', '.join(str(i) for i in valid_in_marker)}]"
            for source_id in invalid_in_marker:
                removed_markers.append(f"[{source_id}]")
            logger.warning(
                "마커 %s에서 존재하지 않는 source_id(%s)를 제거하고 %s로 재작성합니다.",
                match.group(0),
                ", ".join(str(i) for i in invalid_in_marker),
                marker_text,
            )
        else:
            # 전부 유효 → 원문(공백 스타일 포함) 그대로 유지
            marker_text = match.group(0)

        marker_start = output_len
        cleaned_parts.append(marker_text)
        output_len += len(marker_text)
        marker_positions.append((marker_start, output_len, valid_in_marker))

        for source_id in valid_in_marker:
            if source_id not in seen_ids:
                seen_ids.add(source_id)
                used_source_ids.append(source_id)

    cleaned_parts.append(text[cursor:])
    final_text = "".join(cleaned_parts)

    supports = [
        _build_support(final_text, marker_start, source_ids)
        for marker_start, _marker_end, source_ids in marker_positions
    ]
    meaningless_positions = [
        position
        for position, support in zip(marker_positions, supports)
        if not support_is_meaningful(support)
    ]
    if meaningless_positions:
        for marker_start, marker_end, _source_ids in reversed(meaningless_positions):
            removed_markers.append(final_text[marker_start:marker_end])
            final_text = final_text[:marker_start] + final_text[marker_end:]
        validated = validate_citations(final_text, sources)
        validated.removed_markers = [*removed_markers, *validated.removed_markers]
        return validated

    return CitationResult(
        text=final_text,
        supports=supports,
        used_source_ids=used_source_ids,
        removed_markers=removed_markers,
    )


def _build_support(final_text: str, marker_start: int, source_ids: list[int]) -> dict:
    """마커 직전 문장을 근사한 세그먼트를 만든다.

    완벽한 문장 분할이 목표가 아니라 프론트가 호버 스니펫을 만들 근사치면 충분하다.
    마지막 문장 경계(`. ! ? \\n`) 이후부터 마커 시작 위치 전까지를 세그먼트로 삼는다.
    """
    content_end = marker_start
    while content_end > 0 and final_text[content_end - 1] in " \t":
        content_end -= 1
    search_region = final_text[:content_end]
    boundary_ends = [
        match.end()
        for match in SENTENCE_BOUNDARY_PATTERN.finditer(search_region)
        if match.end() < content_end
    ]
    seg_start = boundary_ends[-1] if boundary_ends else 0

    while seg_start < marker_start and final_text[seg_start] in " \t":
        seg_start += 1

    return {
        "segment": {
            "start_index": seg_start,
            "end_index": marker_start,
            "text": final_text[seg_start:marker_start],
        },
        "source_ids": source_ids,
    }


def build_done_payload(
    sources: list[dict],
    used_source_ids: list[int],
    session_id: str,
    supports: list[dict],
) -> dict:
    """`done` SSE 이벤트 payload를 만든다. 실제로 인용된 출처만, 등장 순서대로 포함한다."""
    by_id = {source["id"]: source for source in sources}
    ordered_sources = [
        project_public_source(by_id[source_id])
        for source_id in used_source_ids
        if source_id in by_id
    ]

    return {
        "sources": ordered_sources,
        "grounding_supports": [support for support in supports if support_is_meaningful(support)],
        "session_id": session_id,
        # 인용된 출처 id(등장 순서). sources와 source 이벤트 모두 같은 cited-only 집합을 쓰며,
        # 프론트가 본문 마커와 출처 카드를 연결하는 표시용 메타다.
        "cited_ids": [source_id for source_id in used_source_ids if source_id in by_id],
    }


def build_evidence_unavailable_payload(session_id: str, *, model: str | None = None) -> dict:
    """접지가 필수인 경로의 보정 실패를 무출처 안전 응답으로 마감한다."""
    payload = build_done_payload([], [], session_id, [])
    payload["text"] = EVIDENCE_UNAVAILABLE_TEXT
    payload["model"] = model
    return payload
