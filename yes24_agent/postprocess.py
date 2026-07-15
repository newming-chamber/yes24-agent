"""인용 마커 검증 — 답변 본문의 `[n]`·`[n, m, ...]` 마커를 실제 출처와 대조한다.

모델(Gemini)은 프롬프트 지시만으로는 25~50%의 인용 오류율을 보이므로(Stanford/Tow Center),
합성된 답변을 그대로 신뢰하지 않고 사후 검증한다. 존재하지 않는 source_id를 가리키는
마커(또는 그룹형 마커 내부의 개별 id)는 본문에서 제거하고 로그를 남긴다. 이 모듈은 순수
함수 계층으로, config·ADK를 import하지 않는다.
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 대괄호+숫자(콤마로 구분된 그룹 포함)만 마커로 간주한다. `[1]`, `[1, 2]`, `[1,2,3]` 모두 매칭.
MARKER_PATTERN = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def cited_ids(text: str) -> set[int]:
    """본문이 인용한 source_id 집합(복합 마커 `[1, 2]`를 펼쳐 반환).

    "본문에서 인용 id 뽑기"의 **단일 정의**다. 예전엔 이 판정이 세 모듈에 각기 구현돼 있었고
    의미까지 달랐다(postprocess만 복합 마커 지원, product_gate·turn_assembly는 단일 `[1]`만) —
    그 틈으로 지어낸 책에 `[1, 2]`만 달면 오매핑 검사가 인용 0건으로 읽고 **스킵**되는 우회로가
    열렸다(검증망의 구멍이 정상 통과로 위장). 인용 마커의 문법은 한 곳에서만 정의한다.
    """
    return {
        int(n)
        for match in MARKER_PATTERN.finditer(text or "")
        for n in match.group(1).split(",")
    }
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


# 미완성 tool-call 누출: 인자 블록이 **닫히기 전에 텍스트가 끝난** 잔재
# (실측: ack가 "…비교해 드릴게요.call:yes24_search{query:" 로 끝남). 스트림이 도구 호출 경계에서
# 잘리면 닫는 괄호가 없어 _TOOL_CALL_LEAK(닫힘 필수)를 빠져나간다. 절단은 반드시 **끝**에서만
# 일어나므로 문자열 끝($)에 앵커하고, 도구 이름 + 열린 인자 블록을 함께 요구해 정상 본문을
# 건드리지 않는다(같은 부류의 누출을 여는 쪽 신호만으로 마감한다).
_TOOL_CALL_LEAK_TRUNCATED = re.compile(
    r"(?:`|\[)?"  # 선택적 래핑
    r"(?:call\s*:|tool_call\s*:|print\s*\()?\s*"  # 선택적 call:/tool_call:/print( 프리픽스
    r"\b(?:" + "|".join(_TOOL_NAMES) + r")\b"  # 우리 도구 이름
    r"\s*[\{(]"  # 인자 블록 시작(닫히지 않음)
    r"[^{}()]*$",  # 닫는 괄호 없이 텍스트 끝
    re.IGNORECASE,
)


# 도구 '응답' 에코 누출: 모델이 받은 함수 응답 dict를 본문 텍스트로 그대로 되풀이하는 실패모드
# (실측 live5x A4: done.text 서두에 `{'yes24_fetch_response': {'checked_at': …}}` 한 줄).
# 호출 서술(_TOOL_CALL_LEAK)과 달리 중첩 괄호 리터럴이라 정규식으로 전체를 안전히 잡을 수 없어
# **줄 단위**로 걷어낸다 — 줄이 dict/JSON 리터럴로 시작하고(선두 { 또는 [) 우리 도구의 응답 키
# (`'<tool>_response'`)를 담을 때만 제거한다(정상 산문이 도구 이름을 언급해도 리터럴 선두 조건에
# 안 걸려 보호됨).
_TOOL_RESPONSE_KEY = re.compile(
    r"['\"](?:" + "|".join(_TOOL_NAMES) + r")_response['\"]"
)


def _is_tool_response_echo_line(line: str) -> bool:
    """줄이 도구 응답 dict/JSON 에코인지 — 리터럴 선두({/[) + 도구 응답 키를 모두 요구한다."""
    stripped = line.lstrip()
    return stripped.startswith(("{", "[")) and _TOOL_RESPONSE_KEY.search(stripped) is not None


def has_tool_call_leak(text: str) -> bool:
    """본문에 tool-call/응답 에코 누출 잔재가 있는지 판정한다(스트립 여부 판단용, 부수효과 없음)."""
    if not text:
        return False
    if _TOOL_CALL_LEAK.search(text) is not None:
        return True
    if _TOOL_CALL_LEAK_TRUNCATED.search(text) is not None:
        return True
    return any(_is_tool_response_echo_line(line) for line in text.splitlines())


def strip_tool_call_leaks(text: str) -> tuple[str, int]:
    """tool-call/응답 에코 누출 잔재를 제거한 본문과 제거 개수를 돌려준다.

    Gemini가 함수 호출 대신 텍스트로 서술한 `call:yes24_search{...}` 류 잔재와, 함수 응답
    dict를 본문으로 에코한 `{'yes24_fetch_response': …}` 류 줄이 done.text로 새는 실패모드를
    구조로 막는다(마커 검증과 동일 정신 — 프롬프트가 아니라 사후 가드). 제거로 생긴 이중
    공백·빈 줄만 정리하고 나머지 본문은 그대로 둔다.
    """
    if not text:
        return text, 0
    cleaned, count = _TOOL_CALL_LEAK.subn("", text)
    cleaned, truncated_count = _TOOL_CALL_LEAK_TRUNCATED.subn("", cleaned)
    count += truncated_count
    kept_lines = []
    for line in cleaned.splitlines():
        if _is_tool_response_echo_line(line):
            count += 1
            continue
        kept_lines.append(line)
    cleaned = "\n".join(kept_lines)
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
            # 마커 제거 흔적 정리(제거 시에만 동작): 마커를 지우면 "공백 마침표"(" .")·중복 공백
            # ("단어  단어")이 남는다. **마커 뒤가 문장부호면** 그 앞 공백을 모두 없애 고아 마침표를
            # 막는다 — 앞 조각의 후행 공백과 남은 본문 선두 공백 양쪽을 정리하며, 앞 조각이 공백으로
            # 끝나지 않아도("발생합니다[9] .") 부호 앞 선두 공백을 흡수한다. 부호가 아닌 일반 단어
            # 앞이면 앞 조각이 공백으로 끝날 때만 중복 공백을 하나로 줄인다.
            # output_len·cursor를 함께
            # 조정하므로 이후 유효 마커의 위치(supports 인덱스)는 정합을 유지한다(공백 정리만).
            rest = text[cursor:]
            stripped_rest = rest.lstrip(" \t")
            leading_ws = len(rest) - len(stripped_rest)
            prefix_ends_space = bool(cleaned_parts) and cleaned_parts[-1].endswith((" ", "\t"))
            if stripped_rest[:1] in (".", ",", "!", "?", ";", ":", ")", "]"):
                cursor += leading_ws  # 부호 앞 선두 공백 흡수(미출력)
                if prefix_ends_space:
                    trimmed = cleaned_parts[-1].rstrip(" \t")
                    output_len -= len(cleaned_parts[-1]) - len(trimmed)
                    cleaned_parts[-1] = trimmed
            elif prefix_ends_space and leading_ws:
                cursor += leading_ws  # 중복 공백 → 하나로
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
