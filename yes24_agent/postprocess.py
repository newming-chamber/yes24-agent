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
# 이 패턴과 구분되지 않는다 — 프로즈/코드 스팬 분할(`code_span_ranges`) 외에 이스케이프
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


def code_span_ranges(text: str) -> list[tuple[int, int]]:
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


def prose_citation_ids(text: str) -> list[int]:
    """본문 프로즈가 인용한 source_id를 등장 순서대로(중복 허용) 돌려준다.

    본문만 보고 "무엇이 인용됐는가"를 물어야 하는 바깥(QA 하네스 등)을 위한 공개 진입점이다.
    유효성 대조 없이 마커 판정만 하며, 코드 스팬 제외는 `validate_citations`와 **같은 눈**을
    쓴다 — 판정을 두 벌 두면 한 벌만 고쳐진다(하네스가 자체 정규식을 들었을 때 코드블록 안
    `[1]`을 인용으로 세어 정상 턴을 실패로 몰았다).
    """
    code_ranges = code_span_ranges(text)
    return [
        int(part)
        for match in MARKER_PATTERN.finditer(text)
        if not _within_code_span(match.start(), code_ranges)
        for part in match.group(1).split(",")
    ]


# 인용 마커의 **방언**: 모델이 canonical `[n]` 대신 도구 결과의 필드명을 라벨로 달아 쓰는 형태
# (`[source:6]`·`[출처 3]`·`[src 1]`). 2026-08-04 라이브 QA에서 한 턴 전체가 이 표기로 나와
# 마커가 하나도 인식되지 않았고, 깨진 리터럴이 본문에 노출된 채 done.sources가 0건이 됐다 —
# 접지가 통째로 새는 구멍이다.
#
# 라벨을 **열거로 좁힌 것이 이 규칙의 핵심**이다. 이것은 콘텐츠 키워드 매칭이 아니라 우리
# 인용 프로토콜이 자기 어휘(`source_id`와 그 표기 변형)의 별칭을 받아들이는 문법 확장이다.
# 임의 라벨까지 열면 `[그림 1]`·`[표 2]` 같은 일반 프로즈 대괄호를 인용으로 오인해, 이번에
# 고치려는 것과 반대 방향의 본문 파손이 생긴다(P0 2026-07-20과 같은 병).
#
# **링크형**(`[1](url)`·`[source: 1](url)`)도 같은 문법에 속한다: 라벨은 없어도 되지만 뒤에
# 마크다운 링크가 붙으면 그 인용이다. url 부분은 흡수해서 지운다 — 출처 url의 정본 채널은
# refs/카드이고, 본문 프로즈에 생 url을 노출하지 않는다. 예전엔 링크형을 `(?!\()`로 통째로
# 비껴 갔는데, canonical MARKER_PATTERN에는 그 가드가 없어 `[1]`만 마커로 소비되고 `(url)`은
# 프로즈에 남았다(무효 id면 고아 괄호 잔해). 흡수로 바꾸면 링크형도 canonical 검증 한 경로로
# 수렴하므로 `(?!\()` 가드는 필요 없어져 삭제했다.
_DIALECT_LABELS = ("source_id", "source", "src", "출처")
# 마크다운 링크의 url 조각 — 괄호 한 겹 중첩(`(…_(bar))`)까지 받고, 공백이 끼면(제목 문법
# `[1](url "t")`) 링크로 보지 않는다. 괄호 안이 **스킴 있는 주소**일 때만 링크로 인정한다:
# 아무 괄호나 흡수하면 마커 뒤에 바로 붙은 삽입구(`[1](주석)`)를 지워 본문을 파손한다 —
# 과잉 수용이 곧 본문 파손이라는 P0 2026-07-20의 교훈이 여기에도 그대로 적용된다.
#
# 링크 문법은 **출구(흡수)와 스트림 홀드(미확정 꼬리 판정)가 한 벌을 공유**한다. 두 벌로
# 두면 한 벌만 고치는 실수가 반복된다 — 실제로 출구만 괄호 중첩을 받고 홀드가 못 받아,
# 중첩 url이 도착하는 도중(`[1](…/문학_(` 시점) 홀드가 깨져 마커+미완성 url이 방출되고
# 링크 완성 시 정규화된 본문이 짧아져 `feed`가 뒤 본문을 통째로 삼켰다(퍼즈 9,962/20,000).
def _link_body(*, partial: bool) -> str:
    """링크 몸통 문법. `partial`이면 **아직 닫히지 않은** 중첩 괄호까지 받는다(홀드 전용).

    홀드가 물어야 하는 것은 "이 꼬리가 링크로 확정될 수 있는가"이므로, 완성형 문법의 모든
    접두부를 받아야 한다. 닫는 괄호를 선택으로 여는 것이 그 접두부 집합과 정확히 같다.
    """
    return r"(?:[^()\s]|\([^()\s]*\)" + ("?" if partial else "") + r")*"


_MARKER_LINK = r"\(\w[\w+.-]*://" + _link_body(partial=False) + r"\)"
# 홀드 꼬리는 스킴도 닫는 괄호도 **요구하지 않는다** — `[1](ht`처럼 스킴이 아직 미완성인
# 꼬리도 링크로 확정될 수 있어서다(스킴 문자는 `[^()\s]`의 부분집합이라 몸통 문법이 그
# 접두부를 이미 덮는다). 과잉 홀드는 다음 청크에 전량 방류되므로 무해하고, 과소 홀드만이
# 되돌릴 수 없는 부분 방류를 낳는다.
_OPEN_LINK_TAIL = r"\(" + _link_body(partial=True)
_DIALECT_MARKER_PATTERN = re.compile(
    r"(?<!\\)\[\s*(?P<label>(?:" + "|".join(_DIALECT_LABELS) + r")\s*[:#]?\s*)?"
    r"(?P<ids>\d+(?:\s*,\s*\d+)*)\s*\](?P<link>" + _MARKER_LINK + r")?",
    re.IGNORECASE,
)


def normalize_marker_dialects(text: str) -> str:
    """마커 방언·링크형을 canonical `[n]`·`[n, m]`으로 재작성한다(검증은 그 뒤 기존 경로가 한다).

    정규화는 인용을 **인정**하지 않는다 — 형태만 맞춰 줄 뿐이라, 존재하지 않는 id를 가리키는
    방언은 이어지는 `validate_citations`가 평소처럼 지운다. 코드 스팬 안의 방언은 리터럴이므로
    마커 검증과 **같은 눈**(`code_span_ranges`)으로 건너뛴다 — 판정을 두 벌 두면 한 벌만
    고치는 실수가 반복된다.

    라벨도 링크도 없는 마커는 이미 canonical이므로 손대지 않는다(모델이 쓴 공백 표기 보존).
    """
    code_ranges = code_span_ranges(text)

    def _replace(match: re.Match) -> str:
        if _within_code_span(match.start(), code_ranges):
            return match.group(0)
        if not match.group("label") and not match.group("link"):
            return match.group(0)
        ids = ", ".join(part.strip() for part in match.group("ids").split(","))
        return f"[{ids}]"

    return _DIALECT_MARKER_PATTERN.sub(_replace, text)


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
    제외하고 원문 그대로 보존한다(`code_span_ranges`).

    검증 전에 마커 방언(`[source:6]` 등)을 canonical 형태로 정규화한다
    (`normalize_marker_dialects`) — 표기가 무엇이든 검증·제거 규칙은 하나다.
    """
    text = normalize_marker_dialects(text)
    valid_ids = {source["id"] for source in sources}
    code_ranges = code_span_ranges(text)

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
                f"존재하지 않는 source_id({', '.join(str(i) for i in invalid_in_marker)})를 "
                f"인용한 마커 {match.group(0)}를 본문에서 제거합니다."
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
                f"마커 {match.group(0)}에서 존재하지 않는 "
                f"source_id({', '.join(str(i) for i in invalid_in_marker)})를 제거하고 "
                f"{marker_text}로 재작성합니다."
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


def renumber_markers(
    text: str,
    mapping: dict[int, int],
    *,
    code_ranges: list[tuple[int, int]] | None = None,
    offset: int = 0,
) -> str:
    """프로즈 마커 안의 id만 표시 번호로 갈아끼운다(구분자·공백 표기 그대로).

    코드 스팬 안의 `[n]`은 배열 인덱스·수식이므로 검증과 **같은 눈**으로 건너뛴다
    (`code_span_ranges`) — 판정을 두 벌 두면 한 벌만 고치는 실수가 반복된다.

    `text`가 더 긴 본문의 **조각**일 때는 그 본문 전체의 `code_ranges`와 조각의 시작
    위치 `offset`을 넘긴다(스트리밍 증분 치환). 조각만 보면 앞서 열린 펜스를 못 본다.
    """
    if code_ranges is None:
        code_ranges = code_span_ranges(text)

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
        code_ranges = code_span_ranges(text)
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


# 표시 번호 배정·정규화가 아직 흔들릴 수 있는 꼬리 — 청크 경계에 걸려 아직 **어떤 형태로
# 확정될지 모르는** 마커. 이 패턴에 걸리는 꼬리는 다음 청크가 올 때까지 방출을 미룬다.
# 세 가지가 흔들린다:
#   ① 닫히지 않은 마커(`[4` + `9]`),
#   ② 아직 라벨을 다 못 받은 방언(`[sour` + `ce: 9]`) — 라벨 열거에서 **파생**한 접두부라
#      목록이 두 벌로 갈라지지 않는다,
#   ③ 닫혔지만 뒤에 링크가 붙을 수 있는 마커(`[1]` + `(url)`) — `[1]`을 먼저 흘리면 url이
#      도착했을 때 이미 흘린 구간을 되돌려야 한다(append-only 위반 = 마감 reset).
# ②·③의 문법은 둘 다 **출구 문법에서 파생**한다(`_LABEL_PREFIXES`·`_OPEN_LINK_TAIL`) —
# 손으로 다시 적으면 두 벌이 갈라져, 출구는 흡수하는데 홀드는 놓치는 구멍이 생긴다.
_LABEL_PREFIXES = sorted(
    {label[:size] for label in _DIALECT_LABELS for size in range(1, len(label) + 1)},
    key=len,
    reverse=True,
)
_OPEN_MARKER_TAIL = re.compile(
    r"(?<!\\)\[(?:\s*(?:" + "|".join(_LABEL_PREFIXES) + r")\s*[:#]?)?"
    r"\s*\d*(?:\s*,\s*\d*)*\s*"
    r"(?:\](?:" + _OPEN_LINK_TAIL + r")?)?\Z",
    re.IGNORECASE,
)


def _stable_prefix(text: str) -> str:
    """표시 번호 배정이 확정된 접두부만 남긴다(불안정한 꼬리는 다음 청크로 이월).

    흔들리는 것은 둘뿐이다:
    ① 형태가 아직 확정되지 않은 마커 꼬리(`_OPEN_MARKER_TAIL` — 미완성 마커·미완성 방언
       라벨·링크가 붙을 수 있는 닫힌 마커).
    ② 마지막 줄의 **열린 인라인 코드 백틱** — 닫히는 순간 그 구간의 `[n]`은 마커가 아니라
       리터럴로 재분류된다(`code_span_ranges`와 같은 눈). 백틱 앞에서 끊어 두면 재분류
       대상이 이미 흘러간 본문에 들어갈 수 없다.
    펜스 블록은 닫히지 않아도 `_FENCE_PATTERN`이 본문 끝까지 코드 스팬으로 잡으므로
    분류가 흔들리지 않는다(그래서 여기서 따로 붙잡지 않는다).
    """
    cut = len(text)
    open_marker = _OPEN_MARKER_TAIL.search(text)
    if open_marker:
        cut = open_marker.start()
    tick = text.rfind("`", text.rfind("\n") + 1)
    if tick >= 0 and not _within_code_span(tick, code_span_ranges(text)):
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
        self._consumed = 0  # 표시 번호로 확정·방출한 **정규화** 본문 길이

    def feed(
        self, raw_text: str, valid_ids, *, final: bool = False
    ) -> tuple[str, dict[int, int]]:
        """누적 원시 본문을 받아 `(이번에 흘릴 표시 본문 조각, 새로 배정된 매핑)`을 돌려준다.

        `final=True`면 이월해 둔 꼬리까지 전부 방출한다(스트림 마감 — 홀드가 화면에서
        영구히 사라지는 구멍을 막는다).

        마커 방언·링크형은 출구(`validate_citations`)와 **같은 함수**로 여기서 먼저 흡수한다.
        예전엔 정규화가 출구에만 있어, 방언으로 쓴 턴은 스트리밍 내내 원시 세션 id가 방언
        표기 그대로 노출되다가 출구에서 본문이 바뀌어 마감 reset을 확정적으로 탔다 —
        2026-08-04 재설계가 없애려던 UX가 방언 턴에서만 되살아나 있었다.

        정규화는 조각이 아니라 **누적 본문 전체**에 건다. 조각에 걸면 좌표(offset)가 어긋나고
        청크 경계에 걸친 방언이 두 조각으로 쪼개져 문법을 빠져나간다. 누적본에 걸어도 방출본이
        append-only인 근거는 `_stable_prefix`다 — 확정 접두부는 어떤 마커 후보의 중간에서도
        끊기지 않으므로, 뒤에 무엇이 붙어도 이전 정규화 결과가 새 정규화 결과의 접두부다.
        """
        stable = normalize_marker_dialects(raw_text if final else _stable_prefix(raw_text))
        if len(stable) <= self._consumed:
            return "", {}
        code_ranges = code_span_ranges(stable)
        segment = stable[self._consumed :]
        assigned_before = set(self._mapping)
        assign_display_numbers(
            segment, valid_ids, self._mapping, code_ranges=code_ranges, offset=self._consumed
        )
        rendered = renumber_markers(
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
    renumbered = validate_citations(renumber_markers(citation.text, mapping), public_sources)
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
