"""인용 마커 검증 — 답변 본문의 `[n]`·`[n, m, ...]` 마커를 실제 출처와 대조한다.

모델(Gemini)은 프롬프트 지시만으로는 25~50%의 인용 오류율을 보이므로(Stanford/Tow Center),
합성된 답변을 그대로 신뢰하지 않고 사후 검증한다. 존재하지 않는 source_id를 가리키는
마커(또는 그룹형 마커 내부의 개별 id)는 본문에서 제거하고 로그를 남긴다. 이 모듈은 순수
함수 계층으로, 다른 프로젝트 모듈(config 등)을 import하지 않는다.
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 대괄호+숫자(콤마로 구분된 그룹 포함)만 마커로 간주한다. `[1]`, `[1, 2]`, `[1,2,3]` 모두 매칭.
MARKER_PATTERN = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
# 세그먼트 근사에 쓰이는 문장 경계 문자
SENTENCE_BOUNDARY_PATTERN = re.compile(r"[.!?\n]")

# 우리 도구 이름. tool-call 누출 스트립을 이 집합에 앵커해 보수적으로 잡는다(정상 본문 보호).
_TOOL_NAMES = (
    "yes24_search",
    "yes24_fetch",
    "fetch_many",
    "yes24_browse",
    "web_search",
    "web_fetch",
)
# Gemini가 함수 호출 대신 텍스트로 서술한 tool-call 잔재를 잡는 패턴. 형태:
# `call:yes24_search{query:위로 에세이,section:book}` (실측), `web_search(query=날씨)` 등.
# **오탐 0 지향** — 다음을 모두 요구해 정상 본문(코드 예시·일반 중괄호)을 지우지 않는다:
#   ① 우리 도구 이름(_TOOL_NAMES)이 정확히 등장하고, ② 곧바로 인자 블록 `{...}`/`(...)`이 붙으며,
#   ③ 그 블록 안에 인자 구분자 `:` 또는 `=`가 있다(`{제목}`·`(을 사용)` 같은 일반 괄호는 제외).
# 앞뒤의 `call:`/`tool_call:`/`print(` 프리픽스와 백틱·대괄호 래핑도 함께 걷어낸다.
_TOOL_CALL_LEAK = re.compile(
    r"(?:`|\[)?"  # 선택적 래핑(백틱 또는 대괄호)
    r"(?:call\s*:|tool_call\s*:|print\s*\()?\s*"  # 선택적 call:/tool_call:/print( 프리픽스
    r"\b(?:" + "|".join(_TOOL_NAMES) + r")\b"  # 우리 도구 이름
    r"\s*[\{(]"  # 인자 블록 시작 { 또는 (
    r"[^{}()]*[:=][^{}()]*"  # 인자 내용(구분자 : 또는 = 포함)
    r"[\})]"  # 인자 블록 끝 } 또는 )
    r"\)?"  # print( 대응 선택적 )
    r"(?:`|\])?",  # 선택적 래핑 닫기
    re.IGNORECASE,
)


def has_tool_call_leak(text: str) -> bool:
    """본문에 tool-call 누출 잔재가 있는지 판정한다(스트립 여부 판단용, 부수효과 없음)."""
    return bool(text) and _TOOL_CALL_LEAK.search(text) is not None


def strip_tool_call_leaks(text: str) -> tuple[str, int]:
    """tool-call 누출 잔재를 제거한 본문과 제거 개수를 돌려준다.

    Gemini가 함수 호출 대신 텍스트로 서술한 `call:yes24_search{...}` 류 잔재가 done.text로
    새는 실패모드(출처 0·실검색 없음)를 구조로 막는다(마커 검증과 동일 정신 — 프롬프트가 아니라
    사후 가드). 제거로 생긴 이중 공백·빈 줄만 정리하고 나머지 본문은 그대로 둔다.
    """
    if not text:
        return text, 0
    cleaned, count = _TOOL_CALL_LEAK.subn("", text)
    if count:
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)  # 제거 자리에 생긴 연속 공백
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)  # 3줄 이상 빈 줄 축소
        cleaned = cleaned.strip()
    return cleaned, count


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


def validate_citations(text: str, sources: list[dict]) -> CitationResult:
    """본문의 `[n]`·`[n, m, ...]` 마커를 `sources`의 id 집합과 대조해 검증한다.

    그룹형 마커는 내부 id를 각각 검증한다:
    - 전부 유효 → 마커 원문을 그대로 유지한다.
    - 일부만 유효 → 유효 id만 남긴 형태로 재작성한다 (예: `[2, 99, 3]` → `[2, 3]`).
    - 전부 무효 → 마커 전체를 제거한다 (단일 무효 마커와 동일하게 처리).

    유효하지 않은 id는 경고 로그를 남기고 `removed_markers`에 기록한다.
    반환되는 `supports`의 인덱스는 (재작성·제거 반영 후) 최종 본문 기준이다.

    마커 검증에 앞서 tool-call 누출 잔재(`call:yes24_search{...}` 류)를 먼저 제거한다 —
    done.text 조립의 두 경로(runner 1차·orchestrator 재검색)가 모두 이 함수를 지나므로,
    여기서 걷어내면 어느 경로로 나가든 done.text에 원시 tool-call 텍스트가 남지 않는다.
    """
    stripped, leak_count = strip_tool_call_leaks(text)
    if leak_count:
        logger.warning(
            "tool-call 누출 잔재 %d건을 done.text에서 제거했습니다(모델이 함수 호출 대신 "
            "텍스트로 서술한 실패모드).",
            leak_count,
        )
        text = stripped

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
    search_region = final_text[:marker_start]
    boundary_ends = [m.end() for m in SENTENCE_BOUNDARY_PATTERN.finditer(search_region)]
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
    ordered_sources = [by_id[source_id] for source_id in used_source_ids if source_id in by_id]

    return {
        "sources": ordered_sources,
        "grounding_supports": supports,
        "session_id": session_id,
        # 인용된 출처 id(등장 순서). 스트리밍 중 관찰된 출처 카드는 프론트에 그대로 남기고,
        # 그중 실제 인용된 것만 강조하기 위한 표시용 메타다. sources는 이미 인용된 것만 담지만,
        # 프론트는 관찰 카드 전체를 유지한 채 이 집합으로 강조/디밍만 하므로 명시 필드로 제공한다.
        # 인용 검증(validate_citations)·[n] 매핑과 무관한 가법 필드(계약 불변).
        "cited_ids": [source_id for source_id in used_source_ids if source_id in by_id],
    }
