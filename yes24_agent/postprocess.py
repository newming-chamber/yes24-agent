"""인용 마커 검증 — 답변 본문의 `[n]`·`[n, m, ...]` 마커를 실제 출처와 대조한다.

모델(Gemini)은 프롬프트 지시만으로는 25~50%의 인용 오류율을 보이므로(Stanford/Tow Center),
합성된 답변을 그대로 신뢰하지 않고 사후 검증한다. 존재하지 않는 source_id를 가리키는
마커(또는 그룹형 마커 내부의 개별 id)는 본문에서 제거하고 로그를 남긴다. 이 모듈은 순수
함수 계층으로, config·ADK를 import하지 않는다.
"""

import logging
import re
from dataclasses import dataclass

from yes24_agent.event_translate import project_public_source

logger = logging.getLogger(__name__)

# 서버가 붙인 ASCII 대괄호+숫자만 마커로 간주한다. 도구 데이터 안의 리터럴 `[1]`은
# 이 패턴과 구분되지 않는다 — 프로즈/코드 스팬 분할(`_code_span_ranges`) 외에 이스케이프
# 계층은 없다(docs/known-limitations.md).
MARKER_PATTERN = re.compile(r"(?<!\\)\[(\d+(?:\s*,\s*\d+)*)\]")


# 코드 스팬(펜스 블록 ```…``` · 인라인 코드 `…`)은 프로즈가 아니라 그대로 표시되는 리터럴
# 영역이다. 그 안의 `[0]`·`[1, 2]`는 배열 인덱스·수식이지 인용 마커가 아니다. 인용 마커는
# 프로즈 계층의 관례이므로, 마커 검증은 코드 스팬을 제외한 프로즈에서만 한다 — 코드 vs 프로즈
# 분할이 인용마커 오탐 방지의 본질이며, 특정 부호·키워드를 열거하지 않는 구조 술어다.
_FENCE_PATTERN = re.compile(
    r"^[ \t]*(`{3,}|~{3,}).*?(?:\n[ \t]*\1[ \t]*$|\Z)", re.MULTILINE | re.DOTALL
)
_INLINE_CODE_PATTERN = re.compile(r"(`+)[^\n]*?\1")


def _code_span_ranges(text: str) -> list[tuple[int, int]]:
    """본문에서 코드 스팬(펜스 블록·인라인 코드)의 문자 구간 `[start, end)`를 찾는다.

    펜스 블록을 먼저 찾아 같은 길이 공백으로 가린 뒤 인라인 코드를 찾는다. 그래야 인라인 백틱
    스캔이 펜스 블록 내부를 가로지르지 않고, 문자 인덱스는 원문과 그대로 정렬된다. 인라인은
    줄 안에서만 짝을 맞춰(줄바꿈 불포함) 떠도는 백틱이 프로즈 영역을 통째로 삼키지 않게 한다.
    """
    ranges: list[tuple[int, int]] = []
    masked = list(text)
    for match in _FENCE_PATTERN.finditer(text):
        ranges.append((match.start(), match.end()))
        masked[match.start() : match.end()] = " " * (match.end() - match.start())
    for match in _INLINE_CODE_PATTERN.finditer("".join(masked)):
        ranges.append((match.start(), match.end()))
    return ranges


def _within_code_span(pos: int, ranges: list[tuple[int, int]]) -> bool:
    """`pos`가 코드 스팬 구간 안에 있으면 참 — 그 자리의 `[n]`은 인용 마커로 취급하지 않는다."""
    return any(start <= pos < end for start, end in ranges)


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


def _seam_parts(prefix: str, rest: str) -> tuple[str, str]:
    """마커를 지운 자리의 앞뒤 조각을 조판 규칙대로 다듬어 `(앞, 뒤)`로 돌려준다.

    마커는 양옆의 잉여 공백을 함께 소유하므로, 지울 때 앞 조각의 후행 공백과 뒤 본문의
    선두 공백을 흡수한 뒤 seam을 잇는다. 공백은 새로 만들지 않고 **삭제만** 한다. 양쪽이
    이어 붙으면 안 되는 자리에만 한 칸을 남긴다. 이어붙일지(공백 0)는 인접 문자가
    구두점(_PUNCT: \\w·\\s가 아닌 문자)이면 그 앞에 공백을 두지 않는 조판 규칙으로 판정한다.
    특정 부호를 열거하지 않는 구조 술어다. 모델이 부호 앞에 공백을 흘렸든(`습니다 . [9]`)
    뒤에 흘렸든(`발생합니다[9] .`) 고아 공백이 남지 않는다.

    마커를 지우는 경로는 이제 **무효 id 제거 하나뿐**이고, 조판 판정도 여기 한 곳뿐이다.
    (`《사과가 쿵!》 ,`처럼 구두점 앞 공백이 남던 관측은 빈 근거 제거 경로가 이 규칙을
    건너뛴 탓이었는데, 그 경로 자체가 오탐 100%로 삭제됐다 — `validate_citations` 말미 주석.)
    """
    rest_core = rest.lstrip(" \t")
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
    return (prefix_core if attach else prefix_core + " "), rest_core


def validate_citations(text: str, sources: list[dict]) -> CitationResult:
    """본문의 `[n]`·`[n, m, ...]` 마커를 `sources`의 id 집합과 대조해 검증한다.

    그룹형 마커는 내부 id를 각각 검증한다:
    - 전부 유효 → 마커 원문을 그대로 유지한다.
    - 일부만 유효 → 유효 id만 남긴 형태로 재작성한다 (예: `[2, 99, 3]` → `[2, 3]`).
    - 전부 무효 → 마커 전체를 제거한다 (단일 무효 마커와 동일하게 처리).

    유효하지 않은 id는 경고 로그를 남기고 `removed_markers`에 기록한다.
    반환되는 `supports`의 인덱스는 (재작성·제거 반영 후) 최종 본문 기준이다.

    코드 스팬(펜스 블록·인라인 코드) 안의 `[n]`은 배열 인덱스·수식이므로 마커 검증에서
    제외하고 원문 그대로 보존한다(`_code_span_ranges`).
    """
    valid_ids = {source["id"] for source in sources}
    code_ranges = _code_span_ranges(text)

    cleaned_parts: list[str] = []
    # (최종 본문 기준 마커 시작/끝 인덱스, 마커에 담긴 유효 source_id 목록)
    marker_positions: list[tuple[int, int, list[int]]] = []
    removed_markers: list[str] = []
    used_source_ids: list[int] = []
    seen_ids: set[int] = set()

    cursor = 0  # 원문 기준 커서
    output_len = 0  # 지금까지 만들어진 cleaned 본문의 길이

    for match in MARKER_PATTERN.finditer(text):
        if _within_code_span(match.start(), code_ranges):
            # 코드/인라인 코드 안의 `[n]`은 배열 인덱스·수식이지 인용 마커가 아니다. 커서를
            # 진전시키지 않고 건너뛰어, 이 대괄호가 다음 프로즈 마커의 prefix(또는 말미)에
            # 원문 그대로 실려 나가게 둔다.
            continue
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
            rest = text[cursor:]
            prefix = cleaned_parts[-1] if cleaned_parts else ""
            new_prefix, rest_core = _seam_parts(prefix, rest)
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
    # 근거 세그먼트가 빈 마커를 **본문에서 지우던** 블록은 삭제했다(2026-08-04, 팀리드 승인).
    # 캐치 0 · 오탐 100%였다: 재생 47턴에서 유령 id 제거는 0건인데, 관측된 제거는 전부
    # 책 제목 끝의 `!`·`?`를 문장 경계로 오독한 것이었다(`《사과가 쿵!》 [1]` → 세그먼트가
    # `》` 한 글자 → 삭제). 세그먼트는 프론트 호버 스니펫용 **근사치**이지 접지의 판정
    # 근거가 아닌데, 그 근사 실패로 접지된 인용을 죽였다. 2026-07-15 게이트 스택 삭제와
    # 같은 근거·같은 결론이다.
    #
    # `support_is_meaningful`은 남는다 — `build_done_payload`가 공개 grounding_supports를
    # 그 술어로 거르므로, 빈 근거는 호버 스니펫에서 빠지되 **본문 마커는 살아 링크가 된다**.
    # 4a의 실제 방어선은 무효 id 제거(위 분기)이고 그건 무손상이다.
    return CitationResult(
        text=final_text,
        supports=supports,
        used_source_ids=used_source_ids,
        removed_markers=removed_markers,
    )


def _renumber_markers(
    text: str,
    mapping: dict[int, int],
    *,
    code_ranges: list[tuple[int, int]] | None = None,
    offset: int = 0,
) -> str:
    """프로즈 마커 안의 id만 표시 번호로 갈아끼운다(구분자·공백 표기 그대로).

    코드 스팬 안의 `[n]`은 배열 인덱스·수식이므로 검증과 **같은 눈**으로 건너뛴다
    (`_code_span_ranges`) — 판정을 두 벌 두면 한 벌만 고치는 실수가 반복된다.

    `text`가 더 긴 본문의 **조각**일 때는 그 본문 전체의 `code_ranges`와 조각의 시작
    위치 `offset`을 넘긴다(스트리밍 증분 치환). 조각만 보면 앞서 열린 펜스를 못 본다.
    """
    if code_ranges is None:
        code_ranges = _code_span_ranges(text)

    def _replace(match: re.Match) -> str:
        if _within_code_span(match.start() + offset, code_ranges):
            return match.group(0)
        inner = re.sub(
            r"\d+",
            lambda digits: str(mapping.get(int(digits.group()), int(digits.group()))),
            match.group(1),
        )
        return f"[{inner}]"

    return MARKER_PATTERN.sub(_replace, text)


def assign_display_numbers(
    text: str,
    valid_ids,
    mapping: dict[int, int] | None = None,
    *,
    code_ranges: list[tuple[int, int]] | None = None,
    offset: int = 0,
) -> dict[int, int]:
    """세션 누적 id → 표시 번호(1..n) 배정 — 규칙은 **본문 프로즈 마커의 첫 등장 순서** 하나뿐.

    스트림(`StreamRenumberer`, 증분)과 출구(`renumber_for_display`, 일괄)가 이 한 함수를
    공유한다. 그래야 무효 마커가 제거되지 않는 턴에서 두 경로의 배정이 같아지고, 스트리밍
    본문과 정본이 바이트 동일해져 마감 reset이 사라진다(2026-08-04 재설계의 핵심 등식).

    `mapping`을 주면 그 위에 이어서 배정한다(스트림이 청크를 넘어 배정을 이어가는 경로).
    `valid_ids`에 없는 id는 배정하지 않는다 — 인용 검증이 지울 후보라 번호를 먹이면
    뒤 번호가 통째로 밀린다.
    """
    if code_ranges is None:
        code_ranges = _code_span_ranges(text)
    if mapping is None:
        mapping = {}
    for match in MARKER_PATTERN.finditer(text):
        if _within_code_span(match.start() + offset, code_ranges):
            continue
        for part in match.group(1).split(","):
            source_id = int(part)
            if source_id in valid_ids and source_id not in mapping:
                mapping[source_id] = len(mapping) + 1
    return mapping


# 표시 번호 배정이 아직 흔들릴 수 있는 꼬리 — 청크 경계에 걸려 아직 닫히지 않은 마커
# (`[4` + `9]`). 이 패턴에 걸리는 꼬리는 다음 청크가 올 때까지 방출을 미룬다.
_OPEN_MARKER_TAIL = re.compile(r"\[\d*(?:\s*,\s*\d*)*\Z")


def _stable_prefix(text: str) -> str:
    """표시 번호 배정이 확정된 접두부만 남긴다(불안정한 꼬리는 다음 청크로 이월).

    흔들리는 것은 둘뿐이다:
    ① 아직 닫히지 않은 마커(`_OPEN_MARKER_TAIL`).
    ② 마지막 줄의 **열린 인라인 코드 백틱** — 닫히는 순간 그 구간의 `[n]`은 마커가 아니라
       리터럴로 재분류된다(`_code_span_ranges`와 같은 눈). 백틱 앞에서 끊어 두면 재분류
       대상이 이미 흘러간 본문에 들어갈 수 없다.
    펜스 블록은 닫히지 않아도 `_FENCE_PATTERN`이 본문 끝까지 코드 스팬으로 잡으므로
    분류가 흔들리지 않는다(그래서 여기서 따로 붙잡지 않는다).
    """
    cut = len(text)
    open_marker = _OPEN_MARKER_TAIL.search(text)
    if open_marker:
        cut = open_marker.start()
    tick = text.rfind("`", text.rfind("\n") + 1)
    if tick >= 0 and not _within_code_span(tick, _code_span_ranges(text)):
        cut = min(cut, tick)
    return text[:cut]


class StreamRenumberer:
    """스트리밍 델타의 마커를 표시 번호로 **증분 치환**하는 변환기(배정 규칙은 출구와 공유).

    사용자는 스트리밍 내내 `[49, 59, 78]` 같은 세션 누적 id를 보고 있었고, 답이 끝나는
    순간 reset으로 화면이 다시 그려지며 `[1][2][3]`이 됐다(2026-08-04 도그푸딩 실측:
    원시 id 노출 32/40턴, reset 35/40턴 — 그중 96%는 본문이 한 글자도 안 바뀌고 번호만
    달랐다). 표시 번호를 **첫 등장 시점에** 배정하면 그 96%가 바이트 동일해져 reset이
    원리적으로 사라진다.

    한 번 배정한 번호는 절대 바뀌지 않고(sticky), 이미 방출한 구간은 다시 보지 않는다 —
    그래서 방출본은 언제나 append-only이고, 프론트가 앞을 되돌릴 일이 없다.
    """

    def __init__(self) -> None:
        self._mapping: dict[int, int] = {}
        self._consumed = 0  # 표시 번호로 확정·방출한 원시 본문 길이

    def feed(
        self, raw_text: str, valid_ids, *, final: bool = False
    ) -> tuple[str, dict[int, int]]:
        """누적 원시 본문을 받아 `(이번에 흘릴 표시 본문 조각, 새로 배정된 매핑)`을 돌려준다.

        `final=True`면 이월해 둔 꼬리까지 전부 방출한다(스트림 마감 — 홀드가 화면에서
        영구히 사라지는 구멍을 막는다).
        """
        stable = raw_text if final else _stable_prefix(raw_text)
        if len(stable) <= self._consumed:
            return "", {}
        code_ranges = _code_span_ranges(stable)
        segment = stable[self._consumed :]
        assigned_before = set(self._mapping)
        assign_display_numbers(
            segment, valid_ids, self._mapping, code_ranges=code_ranges, offset=self._consumed
        )
        rendered = _renumber_markers(
            segment, self._mapping, code_ranges=code_ranges, offset=self._consumed
        )
        self._consumed = len(stable)
        return rendered, {
            old: new for old, new in self._mapping.items() if old not in assigned_before
        }


def renumber_for_display(
    citation: CitationResult, sources: list[dict]
) -> tuple[CitationResult, list[dict]]:
    """세션 누적 id를 **이번 답변의 등장 순서 1..n**으로 갈아끼운다(공개 표시층).

    내부 `source_id`는 세션 레지스트리에서 단조 증가하므로, 검색이 많이 쌓이는 턴이면 첫
    답변인데도 본문이 `[30]`으로 시작하고 카드가 30·40·50·60으로 나간다. 사용자는 "출처를
    30개 봤다는 건가" 하고 카드를 세게 된다(실제 4개, 2026-08-03 UX 평가 실측). 번호는
    **표시 규약**이지 식별자가 아니므로 공개 직전에 1..n으로 다시 매긴다(퍼플렉시티 방식).

    순서가 중요하다: 검증(`validate_citations`)은 모델이 실제로 쓴 내부 id로 해야 하고,
    재번호는 그 **이후**에만 성립한다(먼저 바꾸면 대조할 id 집합이 사라진다). 재번호한
    본문을 다시 검증에 통과시켜 support 인덱스를 새 본문 길이에 맞춘다 — `[30]`→`[1]`은
    길이가 줄어 인덱스가 밀리므로, 인덱스 보정을 손으로 하지 않고 같은 함수에 맡긴다.

    배정 규칙 자체는 `assign_display_numbers`가 소유하고 **스트림도 같은 함수를 쓴다** —
    무효 마커 제거가 없는 턴이면 두 경로의 배정이 같아 정본이 스트리밍 본문과 바이트
    동일해지고, 마감 reset이 나지 않는다.

    반환하는 출처 목록은 **공개용 사본**이다(내부 레지스트리·세션 state는 손대지 않는다).
    """
    mapping = assign_display_numbers(citation.text, {source["id"] for source in sources})
    if all(old == new for old, new in mapping.items()):
        return citation, sources

    by_id = {source["id"]: source for source in sources}
    public_sources = [
        {**by_id[old], "id": new} for old, new in mapping.items() if old in by_id
    ]
    renumbered = validate_citations(_renumber_markers(citation.text, mapping), public_sources)
    renumbered.removed_markers = [*citation.removed_markers, *renumbered.removed_markers]
    return renumbered, public_sources


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
