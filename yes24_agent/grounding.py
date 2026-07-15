"""접지 게이트 — 이번 턴 도구 결과로 뒷받침되지 않는 주장을 찾아내고, 무엇을 할지 정한다.

**게이트는 하나다**(2026-07-14 통합, 사용자 지적 "게이트가 왜 이렇게 많냐"). 예전엔 상품·정책·
충분성이 각각 파일·어휘·폴백 문구를 갖고 있었지만, 셋이 묻는 질문은 하나였다 —
**"이 턴의 답에 도구 결과로 뒷받침되지 않는 주장이 있는가, 있으면 무엇을 할 것인가."**
갈라진 대가는 실측됐다: 같은 입력에 매트릭스는 잡고 채팅은 놓쳤고, 폴백이 갈라져 커피값 질문에
책 취향을 되물었고, 정책 어휘 목록이 라우팅 목록과 어긋났다.

구조는 **판정(순수) + 결정(정책)** 둘뿐이다.

**판정** — 전부 같은 형태의 접지 대조이며 도메인 어휘가 없다:
1. `detect_unsourced_priced_item` — 제목 주장 + 가격이 한 항목에 있으면 그 제목은 Yes24 상품
   출처로 뒷받침돼야 한다(주장 단위 대조 → 무임승차 차단). 웹 인용은 접지가 될 수 없다.
2. `detect_title_mismap` — "제목 … [n]"의 제목이 그 출처[n]의 실제 title과 맞는가.
3. `detect_unsourced_rating_claim` — 주장한 평점 숫자가 이번 턴 출처의 rating에 실제로 있는가.
4. 정책 접지 — 정책 질의 턴인데 Yes24 정책 페이지(notice) 출처가 없는가.
매트릭스도 같은 함수(`unsupported_title_claims`)를 쓴다 — 판정이 두 벌 존재하지 않는다.

**결정**(`evaluate`) — 관측 사실(도구 실행 여부·출처 타입·인용 수)로 `Gate` 하나를 만든다:
  - kind="contradicted": 출처와 어긋나는 주장이 있다(환각) → 재확인, 실패 시 **원답 폐기**.
  - kind="missing": 접지가 필요한데 접지가 없다 → 재확인. **실패 시 원답을 버릴지는 도구를 실제로
    불렀는지로 갈린다** — 도구 0회면 원답은 약속·추측이므로 폐기(destructive), 도구를 돌렸는데
    근거를 못 찾은 것이면 정직한 "못 찾음"이므로 **유지**(비파괴).
  - force_tool: 정책 턴이면 yes24_fetch, 그 외 None(검색 도구 자율 선택).
어휘·화행 분류는 어디에도 없다. 발동 조건은 전부 구조 신호다.
"""

import re
from dataclasses import dataclass

from yes24_agent.config import get_settings
from yes24_agent.postprocess import MARKER_PATTERN, cited_ids

# Yes24 '정책' 근거로 인정하는 source type. 정책 페이지는 yes24_fetch가 notice로 반환한다.
POLICY_SOURCE_TYPES = frozenset({"notice"})

# Yes24 '상품' 출처로 인정하는 source type 집합. yes24_search=search_result,
# yes24_fetch(도서)=book_detail, yes24_browse=browse. 웹 검색·열람(web)과 정책
# 공지(notice)는 상품 가격·목록의 근거가 될 수 없으므로 상품 접지에서 제외한다.
PRODUCT_SOURCE_TYPES = frozenset({"search_result", "book_detail", "browse"})

# 가격 토큰: "15,120원", "16920 원" 등. 숫자로 시작해 쉼표를 포함할 수 있는 수 + '원'.
_PRICE_TOKEN = re.compile(r"\d[\d,]*\s*원")

# 무출처/오매핑 감지 시 보정 에이전트에 내리는 재확인 지시(시스템 지시로 주입된다). 직전 답변에서
# 지어낸 정보를 실제 도구로 확인해 인용과 함께 다시 답하게 한다 — 되물음 대신 실제 답을 회수한다.
# **질문 유형에 맞는 도구로 라우팅**하도록 명시: 책·상품이면 yes24_search, 사실·정보(최저임금·
# 법률·시세 등)면 web_search. 사실 질문을 책 추천으로 치환하지 않는다(P1).
CORRECTION_DIRECTIVE = (
    "방금 답변에는 실제로 확인하지 않은 정보(책 제목·저자·가격 등)가 섞여 있었습니다. "
    "사용자의 원래 질문에 맞는 도구로 지금 다시 확인해, 도구 결과에 실제로 있는 내용만 "
    "인용[n]과 함께 답하세요. 책·상품을 추천·안내하는 질문이면 yes24_search로 검색하고, "
    "사실·정보를 묻는 질문(최저임금·법률·시세·뉴스 등)이면 web_search로 확인해 정보 자체로 "
    "답하세요(사실 질문을 책 추천으로 바꾸지 말 것). 확인되지 않은 제목·저자·가격은 절대 쓰지 "
    "말고, 공감 서두 없이 곧바로 본론으로 답하세요."
)

# 재확인까지 접지에 실패했을 때만 쓰는 최종 안전 안내(폴백). 내부 자기수정 과정은 노출하지 않고,
# 자연스럽게 취향을 한 줄 물어 정확한 추천으로 잇는다.
UNSOURCED_PRODUCT_NOTICE = (
    "찾으시는 책의 결(감정·상황·주제·좋아하는 장르)을 한 줄만 더 알려주시면, "
    "딱 맞는 책을 Yes24에서 정확히 찾아 추천해 드릴게요."
)


def has_price_claim(text: str) -> bool:
    """본문이 가격을 주장하는지(가격 토큰 존재).

    도구 결과가 아직 없는 발화(도구 전 ack)의 사전 차단용 — 그 시점엔 어떤 가격도 접지될 수 없다.
    """
    return bool(text) and _PRICE_TOKEN.search(text) is not None


def has_product_grounding(sources: list[dict]) -> bool:
    """주어진 출처 목록에 Yes24 상품 출처(PRODUCT_SOURCE_TYPES)가 하나라도 있는지."""
    return any(source.get("type") in PRODUCT_SOURCE_TYPES for source in sources)


# ── 제목 대조 요소 ───────────────────────────────────────────────────────────

# 책 제목 마커: **볼드**·《》·『』 또는 마크다운 헤딩. group 1=볼드, 2~3=괄호, 4=헤딩 본문.
_ASSERTED_TITLE = re.compile(
    r"\*\*([^*\n]{2,})\*\*|《([^》\n]{2,})》|『([^』\n]{2,})』|^#{1,6}\s+(.+?)\s*$",
)
# 목록 항목 마커 — 불릿·번호·헤딩. **한 곳에서만 정의한다**: 모듈마다 '목록'의 정의가 달라
# 번호목록(`1. `)을 항목으로 못 보면 답변 전체가 한 블록이 되고, 그 안의 실제 검색된 책 한 권이
# 지어낸 책까지 접지시켜 통과시킨다(실행 반례 #1 — 서식만 바꾸면 환각이 새는 구멍).
_ITEM_MARKER = r"(?:[*\-•·]|\d+\.)\s+|#{1,6}\s+"
# 항목 머리 — 줄머리(목록 마커가 있으면 그 뒤). 카탈로그 항목의 제목은 관용적으로 여기 온다
# ("1. **제목** [1]", "**제목**은 15,000원"). 문장 **중간**에 박힌 볼드는 산문 강조라 제외된다
# (위치가 곧 역할). 줄머리 볼드는 목록 마커가 없어도 그 줄의 주제를 이름 짓는 자리다.
_ITEM_HEAD = re.compile(r"^\s*(?:" + _ITEM_MARKER + r")?")
# 항목 분리용: 들여쓰기 없는 최상위 마커만 새 항목의 시작으로 본다.
_TOP_ITEM = re.compile(_ITEM_MARKER)

# 제목 지지 등급 — "어느 출처가 이 제목을 더 잘 설명하나"를 비교하는 값.
# 2(포함관계: 핵심 제목이 서로 부분집합 — 같은 책의 축약·부제·판형 변형) >
# 1(토큰 과반 겹침만: 공통 접두를 공유하는 '비슷한' 제목) > 0(무관).
# -1(보류: 문자열 대조가 원천 불가 — 빈 제목·문자 체계 서로소)은 **지지로 인정하되** 등급
# 비교에선 포함관계보다 약해, 다른 출처의 지지를 빼앗지 않는다.
_TIER_CONTAINMENT = 2
_TIER_TOKEN_OVERLAP = 1
_TIER_NONE = 0
_TIER_ABSTAIN = -1

# 문자 체계(스크립트) 판정 — 한글/라틴. 정규화 제목의 스크립트가 서로소면 문자열 대조로는
# 동일성 판단이 원천 불가능하다(출처 'The Midnight Library' vs 답변의 한국어 통용 표기).
_HANGUL_CHARS = re.compile(r"[가-힣]")
_LATIN_CHARS = re.compile(r"[a-zA-Z]")

# 판본 통칭("<출판사>판")의 접미사. 답변은 같은 책의 판본을 이렇게 통칭한다 — 새 제목 주장이
# 아니라 그 출처를 가리키는 지시 표현이므로, 접미사를 떼고 출처 메타(출판사·전체 제목)와 대조한다.
_EDITION_SUFFIX = re.compile(r"(?:판본|판|본)$")


def _norm_title(text: str) -> str:
    """제목 비교용 정규화 — 한글·영숫자만 남기고 소문자화(공백·문장부호 무시)."""
    return re.sub(r"[^가-힣a-zA-Z0-9]", "", text or "").lower()


def _core_title(text: str) -> str:
    """대괄호 접두([블라인드북])·괄호 부제(…)·콜론 이후 부제를 떼어낸 핵심 제목."""
    stripped = re.sub(r"\[[^\]]*\]", "", text or "")
    stripped = re.sub(r"\([^)]*\)", "", stripped)
    return stripped.split(":")[0]


def _title_tokens(text: str) -> list[str]:
    """핵심 제목을 2자 이상 토큰으로 분해한다(토큰 겹침 비교용)."""
    return [w for w in re.split(r"[\s,./·]+", _core_title(text)) if len(w) >= 2]


def _scripts(normalized: str) -> frozenset[str]:
    """정규화 문자열에 쓰인 문자 체계 집합(한글·라틴). 숫자만이면 빈 집합."""
    found = set()
    if _HANGUL_CHARS.search(normalized):
        found.add("hangul")
    if _LATIN_CHARS.search(normalized):
        found.add("latin")
    return frozenset(found)


def _support_tier(asserted: str, source_title: str) -> int:
    """본문이 주장한 제목(asserted)을 출처 title이 뒷받침하는 등급을 반환한다.

    비교 불가(빈 제목·토큰 없음, 문자 체계 서로소)는 _TIER_ABSTAIN(보류)로 통과시킨다 —
    "대조가 불가능하면 발동하지 않는다"는 이 모듈의 정책(1자 제목 출처 보류와 동류)이다.
    """
    na, ns = _norm_title(_core_title(asserted)), _norm_title(_core_title(source_title))
    if not na:
        return _TIER_ABSTAIN
    if na in ns or ns in na:
        return _TIER_CONTAINMENT
    sa, ss = _scripts(na), _scripts(ns)
    if sa and ss and not (sa & ss):
        return _TIER_ABSTAIN  # 문자 체계가 서로소 — 문자열 대조 불가, 보류
    tokens = _title_tokens(asserted)
    if not tokens:
        return _TIER_ABSTAIN
    hits = sum(1 for w in tokens if _norm_title(w) in ns)
    if hits / len(tokens) >= get_settings().title_token_overlap_min:
        return _TIER_TOKEN_OVERLAP
    return _TIER_NONE


def title_supported(asserted: str, source_title: str) -> bool:
    """본문이 주장한 제목이 출처 title에 의해 뒷받침되는지(관대 매칭).

    같은 책을 축약·부제 변형으로 쓴 정상 인용은 통과시키고, 전혀 다른 제목만 불일치로 본다.
    대조 불가(보류)도 통과다(오탐 방지).
    """
    tier = _support_tier(asserted, source_title)
    return tier == _TIER_ABSTAIN or tier >= _TIER_TOKEN_OVERLAP


def _source_field(source: dict, key: str) -> str:
    """출처 dict에서 필드를 뽑는다(flat 우선, 없으면 meta 안)."""
    return source.get(key) or (source.get("meta") or {}).get(key) or ""


def _is_edition_reference(asserted: str, source: dict) -> bool:
    """주장 문자열이 그 출처의 판본을 가리키는 통칭인지(출판사·전체 제목 문자열과 대조)."""
    alias = _norm_title(_EDITION_SUFFIX.sub("", asserted or ""))
    if len(alias) < 2:
        return False
    haystack = _norm_title(_source_field(source, "publisher")) + _norm_title(
        source.get("title", "")
    )
    return alias in haystack


# 라벨 볼드 — "**가격**: 12,600원", "**저자:** 손원평"처럼 **볼드 직후(또는 볼드 안 끝)에 콜론이
# 오면 그것은 값의 라벨이지 제목 주장이 아니다.** 한국어 카탈로그 마크업의 구조 규칙이라 단어
# 목록으로 자라지 않는다(예전 _TITLE_LABELS 25개 화이트리스트를 대체한다). 제목은 콜론을 달지
# 않는다: "**아몬드** — 손원평", "**아몬드** [1]", "**불편한 편의점**, 13,000원".
_LABEL_COLON = (":", "：")


def _is_label(match: re.Match, line: str) -> bool:
    """이 마커가 값의 라벨인지(볼드 직후 또는 볼드 안 끝에 콜론)."""
    raw = next((g for g in match.groups() if g), "") or ""
    if raw.rstrip().endswith(_LABEL_COLON):  # **가격:** 값
        return True
    tail = line[match.end() :].lstrip()
    return tail.startswith(_LABEL_COLON)  # **가격**: 값


def _clean_asserted(match: re.Match) -> str:
    """제목 마커 매치에서 제목 문자열을 뽑는다(끝의 인용 마커 [n] 제거)."""
    raw = next((g for g in match.groups() if g), "") or ""
    return MARKER_PATTERN.sub("", raw).strip().rstrip(":").strip()


def title_claims(scope: str, pool_titles: list[str], *, peers: bool = False) -> list[str]:
    """이 범위(줄·항목 블록)가 **책 제목으로 주장한** 문자열들을 뽑는다.

    마커 종류로 강도를 가른다: 『』·《》·헤딩은 한국어에서 작품명에만 쓰는 **제목 전용** 표기라
    그 자체로 제목 주장이다. 반면 **볼드**는 범용 강조 서식이라 라벨·안내 문구에도 붙으므로,
    두 구조 신호 중 하나가 있을 때만 제목 주장으로 본다:
      (a) **항목 머리**(불릿·번호·헤딩 직후)에 오는 볼드 — 카탈로그 항목 헤더의 관용 위치.
          문장 중간에 박힌 볼드는 산문 강조라 제외된다(위치가 곧 역할).
      (b) 이번 턴 상품 출처 제목과 포함관계로 대응 — 실재 책 제목임이 확인된 경우(엉뚱한 출처에
          매단 실재 제목 = 교차 인용 오매핑은 이 경로로 잡힌다).
    마크업 구조에 근거한 규칙이라 라벨 문구 목록으로 자라지 않는다.

    peers=True면 **모든 볼드를 제목 주장으로** 본다 — 상품 맥락의 **가격 있는 항목**에서만 쓴다.
    그 자리에서 볼드로 강조된 이름은 곧 상품 이름이고, 위치 규칙을 요구하면 지어낸 책이 문장
    중간에 숨거나("추천 도서는 **지어낸 책** … 15,120원") 진짜 책 뒤에 무임승차한다("**아몬드**
    다음으로 좋은 **고요한 밤의 서재**, 14,400원"). 라벨·수식 볼드의 오탐은 항목 단위 접지가
    받아낸다(단일 주장 항목은 출처 제목 등장·상품 인용으로 통과). 가격 없는 산문에는 쓰지 않는다.
    """
    claims: list[str] = []
    for line in scope.splitlines():
        head_end = _ITEM_HEAD.match(line).end()
        found: list[tuple[re.Match, str]] = []
        for match in _ASSERTED_TITLE.finditer(line):
            if _is_label(match, line):  # "**가격**: 12,600원" — 값의 라벨이지 제목이 아니다
                continue
            title = _clean_asserted(match)
            if len(title) < 2 or _PRICE_TOKEN.fullmatch(title):
                continue
            found.append((match, title))

        def _is_claim(match: re.Match, title: str) -> bool:
            if match.group(1) is None:  # 제목 전용 표기(『』·《》·헤딩)
                return True
            if match.start() == head_end:  # 항목 머리의 볼드
                return True
            return any(_support_tier(title, t) >= _TIER_CONTAINMENT for t in pool_titles)

        line_claims = found if peers else [(m, t) for m, t in found if _is_claim(m, t)]
        claims.extend(t for _, t in line_claims)
    return claims


def _product_sources(*source_lists: list[dict]) -> list[dict]:
    """여러 출처 목록에서 제목이 있는 상품 출처만 모은다."""
    return [
        s
        for sources in source_lists
        for s in sources
        if s.get("type") in PRODUCT_SOURCE_TYPES and s.get("title")
    ]


def unsupported_title_claims(
    text: str, product_sources: list[dict], *, peers: bool = False
) -> list[str]:
    """본문이 주장한 책 제목 중 **어느 상품 출처로도 뒷받침되지 않는** 것들을 돌려준다.

    "지어낸 책"의 단일 정의다 — 채팅 게이트와 매트릭스(pool 이탈 검사)가 같은 함수를 쓴다.
    접지는 주장 **하나하나**를 대조한다: 항목 안에 진짜 검색된 책이 한 권 섞여 있다고 해서 옆에
    붙은 지어낸 책까지 통과시키지 않는다(무임승차 차단). 판본 통칭("<출판사>판")은 새 제목 주장이
    아니라 그 출처를 가리키는 지시 표현이므로 접지로 인정한다.
    """
    titles = [s.get("title", "") for s in product_sources]
    return [
        claim
        for claim in title_claims(text, titles, peers=peers)
        if not any(title_supported(claim, t) for t in titles)
        and not any(_is_edition_reference(claim, s) for s in product_sources)
    ]


# ── 항목 접지 — 가격이 있는 항목은 이번 턴 상품 출처에 접지돼야 한다 ─────────


def _split_items(text: str) -> list[str]:
    """본문을 추천 항목(책) 블록으로 나눈다.

    최상위 불릿(col0)·헤딩·빈 줄에서 새 블록이 시작되고, 들여쓴 하위 줄은 직전 블록에 붙는다
    (한 책의 제목·저자·가격·설명·인용이 한 블록에 모이도록 — 오탐 방지 핵심).
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        starts_item = bool(_TOP_ITEM.match(line))
        is_blank = not stripped
        if (starts_item or is_blank) and current:
            blocks.append("\n".join(current))
            current = []
        if not is_blank:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def detect_unsourced_priced_item(
    text: str,
    cited_sources: list[dict],
    observed_sources: list[dict],
    *,
    product_context: bool = True,
) -> bool:
    """제목 주장과 가격이 함께 있는 항목의 제목이 상품 출처로 뒷받침되지 않으면 True.

    **게이트의 단일 규칙**: *제목 주장 + 가격이 한 블록에 있으면, 그 제목은 Yes24 상품 출처
    (PRODUCT_SOURCE_TYPES)로 뒷받침돼야 한다.*
      - 도서 어휘를 보지 않는다 → 만화·그림책·자기계발서·회고록 등 모든 부류를 균일하게 덮는다.
      - **기본값은 fail-closed다**(product_context=True): 호출부가 아무 말도 안 하면 상품 맥락으로
        보고 게이트를 건다. 분류가 실패해도 게이트가 꺼지지 않는다(원칙 4a — 환각 차단이 분류기
        정확도에 기대지 않는다). 호출부가 **웹 사실 질의임을 확신할 때만** product_context=False로
        면제한다: "**최저임금** 정책상 시급은 10,320원"과 "**나는 왜 불안할까** … 15,120원"은
        마크업 구조가 동일해, 턴 맥락 없이는 어떤 규칙으로도 가를 수 없다(둘 다 줄머리 볼드 +
        가격 + 웹 출처). 신호를 지우면 둘 중 한쪽을 반드시 잃는다 — 지우는 대신 실패 방향을
        안전한 쪽으로 고정한다.
      - **웹 출처 인용은 접지가 될 수 없다** → 상품 가격은 Yes24 출처로만 말한다(원칙 2). 인용을
        붙였다는 사실만으로 면제해 주지 않는다.
      - **제목 주장이 없는 순수 수치**(주가 296,000원·시급 9,860원)는 상품 주장이 아니므로 대상
        밖이다 — 이것이 비도서 수치를 어휘 없이 배제하는 지점이다.

    접지 범위는 그 항목이 **몇 권을 주장하는가**로 갈린다:
      - 두 권 이상을 주장한 항목은 **주장 하나하나**가 접지돼야 한다 — 진짜 검색된 책 옆에 지어낸
        책을 붙여 무임승차시키는 경로("**아몬드** 다음으로 좋은 **고요한 밤의 서재**, 14,400원")를
        막는다.
      - 한 권만 주장한 항목은 항목 단위 접지도 인정한다: 그 항목의 평문에 상품 출처 제목이
        등장하거나("**강력추천** 불편한 편의점 … 15,120원 [1]"), 항목이 상품 출처를 인용하면
        ("**가격**: 15,300원 [1]") 그 가격의 귀속이 분명하다. 무임승차가 성립하지 않는 형태다.
    """
    if not text or not product_context:
        return False
    product_sources = _product_sources(cited_sources, observed_sources)
    source_norms = [
        norm
        for norm in (_norm_title(_core_title(s.get("title", ""))) for s in product_sources)
        if len(norm) >= 2
    ]
    for block in _split_items(text):
        if not _PRICE_TOKEN.search(block):
            continue
        unsupported = unsupported_title_claims(block, product_sources, peers=True)
        if not unsupported:
            continue
        claims = title_claims(block, [s.get("title", "") for s in product_sources], peers=True)
        if len(claims) == 1:
            block_norm = _norm_title(block)
            item_grounded = any(norm in block_norm for norm in source_norms) or _cites_product(
                block, product_sources
            )
            if item_grounded:
                continue
        return True
    return False


def _cites_product(scope: str, product_sources: list[dict]) -> bool:
    """이 범위가 상품 출처를 인용[n]하는지 — 가격의 근거를 Yes24 출처로 밝힌 항목."""
    product_ids = {s.get("id") for s in product_sources}
    return any(n in product_ids for n in cited_ids(scope))


# ── 인용-제목 오매핑 판정 ────────────────────────────────────────────────────


def detect_title_mismap(
    text: str, sources: list[dict], observed_sources: list[dict] | None = None
) -> bool:
    """도서 인용의 주장 제목이 그 출처의 실제 title과 불일치(오매핑)하는지 전수 판정한다.

    각 줄에서 (제목 주장 + 같은 줄의 상품 출처 인용[n])을 찾아, 그 줄의 어떤 제목도 출처[n]의
    title을 뒷받침하지 못하고, 게다가 출처[n]의 실제 title이 답변 어디에도 등장하지 않으면
    오매핑으로 본다(두 겹의 보수 가드 A·B). 정직한 참조·웹 출처 인용·저자 배경작 언급은 이
    가드로 걸러져 오탐하지 않는다.

    **교차 인용(유사 제목 판본) 감지**: 인용 출처가 토큰 겹침 등급뿐인데 이번 턴의 다른 상품
    출처가 같은 주장 제목을 더 강한 포함관계 등급으로 뒷받침하면, 그 제목은 그 출처의 것이므로
    토큰 겹침을 지지로 인정하지 않는다. 같은 책의 축약·부제·판형 변형은 포함관계라 영향 없다.
    """
    if not text:
        return False
    id_to_source = {s.get("id"): s for s in sources if s.get("id") is not None}
    normalized_text = _norm_title(text)
    pool_titles = [s.get("title", "") for s in _product_sources(sources, observed_sources or [])]

    def _supports(title: str, source: dict) -> bool:
        tier = _support_tier(title, source.get("title", ""))
        if tier == _TIER_ABSTAIN or tier >= _TIER_CONTAINMENT:
            return True
        if tier == _TIER_TOKEN_OVERLAP:
            # 교차 인용 가드: 다른 상품 출처가 포함관계로 이 제목을 차지하면 겹침 지지는 무효.
            return not any(
                _support_tier(title, other) >= _TIER_CONTAINMENT for other in pool_titles
            )
        # 판본 통칭("<출판사>판")은 새 제목 주장이 아니라 이 출처를 가리키는 지시 표현이다.
        return _is_edition_reference(title, source)

    for line in text.splitlines():
        line_titles = title_claims(line, pool_titles)
        if not line_titles:
            continue
        product_ids = {
            n
            for n in cited_ids(line)
            if id_to_source.get(n, {}).get("type") in PRODUCT_SOURCE_TYPES
        }
        if not product_ids:
            continue
        # 한 줄이 여러 출처를 인용할 수 있다(가격은 [1], 서평은 [2]). 그러므로 오매핑은 "인용한
        # 출처 중 **어느 것도** 이 제목을 뒷받침하지 않을 때"다 — 인용 하나하나가 전부 제목과
        # 맞아야 한다고 보면 정상적인 복수 인용이 오매핑으로 오판된다.
        cited = [id_to_source[i] for i in product_ids]
        if any(_supports(t, source) for t in line_titles for source in cited):  # (A)
            continue
        # (B) 인용한 출처의 실제 제목이 답변 어딘가에 등장하면 정상(배경작 언급·서식 차이).
        #     1자 정규화 제목 출처는 우연 일치·비교 언급과 구별할 수 없어 판정을 보류한다.
        cores = [_norm_title(_core_title(s.get("title", ""))) for s in cited]
        if any(len(core) < 2 or core in normalized_text for core in cores):
            continue
        return True
    return False


# ── 평점 값 접지 ─────────────────────────────────────────────────────────────
#
# 값 대조 + 양성 귀속으로 판정한다:
#   ① 자사(Yes24) 상품 평점 주장인지 — (i) 절에 Yes24 표기 OR (ii) 절이 상품 출처[n] 인용 OR
#      (iii) 이번 턴 출처 0 **이면서 답변이 특정 상품을 제목으로 지목**. 그 외(식당·영화·타서점
#      평점, 척도 설명·일반 조언)는 대상이 아니다.
#   ② 주장 숫자가 이번 턴 출처 rating에 있으면 통과, 없는 값만 발동(지어낸 평점).
# 척도 선언("N점 만점")의 N은 점수 주장이 아니므로 값에서 제외한다.

_RATING_ANCHOR = re.compile(r"평점|별점")
_RATING_VALUE = re.compile(r"(?:평점|별점)\s*(?:은|는|이|가|을|를|:)?\s*(\d+(?:\.\d+)?)")
_MANJEOM_SCORE = re.compile(r"만점[^\d\n]{0,4}(\d+(?:\.\d+)?)")
_SCALE_MANJEOM = re.compile(r"(\d+)\s*점?\s*만점")
_SCALE_SLASH = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d{1,2})")
_STAR_RUN = re.compile(r"[★⭐]")
_YES24_MENTION = re.compile(r"yes24|예스24|예스이십사", re.IGNORECASE)
_CLAUSE_SPLIT = re.compile(r"[,，]")


def _declared_scale(clause: str) -> int | None:
    """절에서 선언된 평점 척도를 읽는다(없으면 None). "N점 만점"의 N, 또는 "M/N"의 N(분자 M이
    평점다운 수 ≤10일 때만 — 날짜 2026/07을 척도로 오인하지 않게)."""
    mj = _SCALE_MANJEOM.search(clause)
    if mj:
        return int(mj.group(1))
    sl = _SCALE_SLASH.search(clause)
    if sl and float(sl.group(1)) <= 10:
        return int(sl.group(2))
    return None


def _source_rating(source: dict) -> float | None:
    """출처 dict에서 평점 값을 뽑는다(flat 'rating' 우선, 없으면 meta.rating). 실패 시 None."""
    raw = source.get("rating")
    if raw is None:
        raw = (source.get("meta") or {}).get("rating")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _rating_grounded(claimed: float, source_ratings: list[float]) -> bool:
    """주장 평점이 출처 평점 중 하나와 대조되는지(표기 차이 허용, 정수 주장은 반올림 관대)."""
    tolerance = get_settings().rating_match_tolerance
    for r in source_ratings:
        if abs(claimed - r) < tolerance:
            return True
        if claimed == int(claimed) and int(claimed) in (int(r), int(r) + 1):
            return True
    return False


def _asserts_product(text: str) -> bool:
    """답변이 **특정 상품**을 제목으로 지목하고 있는지(제목 주장 존재).

    평점 값이 '어떤 책의 평점'인지는 이 지목으로만 특정된다. 지목이 없는 평점 서술은 특정 상품
    사실이 아니라 척도 설명·일반 조언("10점 만점 기준이에요")이라 대조할 대상 자체가 없다.
    """
    return bool(title_claims(text, []))


def detect_unsourced_rating_claim(
    text: str,
    cited_sources: list[dict],
    observed_sources: list[dict],
) -> bool:
    """자사 평점 값 주장이 이번 턴 출처의 rating과 대조되지 않으면(지어낸 평점) True."""
    if not text:
        return False
    source_ratings = [
        r
        for r in (_source_rating(s) for s in (*cited_sources, *observed_sources))
        if r is not None
    ]
    has_product = has_product_grounding(cited_sources) or has_product_grounding(observed_sources)
    zero_sources = not cited_sources and not observed_sources
    anchored = zero_sources and _asserts_product(text)
    id_to_type = {s.get("id"): s.get("type") for s in cited_sources}
    for line in text.splitlines():
        for clause in _CLAUSE_SPLIT.split(line):
            if not _RATING_ANCHOR.search(clause):
                continue
            clause_ids = cited_ids(clause)
            cites_product = any(id_to_type.get(i) in PRODUCT_SOURCE_TYPES for i in clause_ids)
            if not (_YES24_MENTION.search(clause) or cites_product or anchored):
                continue  # ① 자사 상품 평점 주장이 아니다
            scale = _declared_scale(clause)
            if scale is not None and scale != 10:
                continue  # 10점 척도가 아니면 값 대조 불가
            # ② 값 추출. 척도 선언의 숫자("10점 만점"의 10)는 점수 주장이 아니므로 제외한다.
            scale_spans = [m.span(1) for m in _SCALE_MANJEOM.finditer(clause)]

            def _is_scale_token(span: tuple[int, int], spans=scale_spans) -> bool:
                return any(start <= span[0] < end for start, end in spans)

            claimed = [
                float(m.group(1))
                for m in _MANJEOM_SCORE.finditer(clause)
                if not _is_scale_token(m.span(1))
            ]
            if not claimed:
                claimed = [
                    float(m.group(1))
                    for m in _RATING_VALUE.finditer(clause)
                    if not _is_scale_token(m.span(1))
                ]
            if claimed:
                for value in claimed:
                    if source_ratings:
                        if not _rating_grounded(value, source_ratings):
                            return True  # 대조 가능한데 없는 값 = 지어낸 평점
                    elif not has_product:
                        return True  # 대조할 출처 평점도 상품 출처도 없음 = 지어낸 평점
                    # else: 상품 출처는 있으나 rating 미파싱 → 검증 불가, 발동 안 함.
            elif _STAR_RUN.search(clause) and not source_ratings and not has_product:
                return True  # 별점 ★ 주장(숫자 없음) — 접지 전혀 없을 때만
    return False


def evaluate_product_answer(
    text: str,
    *,
    cited_sources: list[dict],
    observed_sources: list[dict],
    product_context: bool = True,
) -> str | None:
    """답변의 상품 주장이 근거에 어긋나는 사유를 반환한다(정상이면 None).

    - "mismap": 인용한 출처와 주장 제목이 불일치(cited-but-fabricated).
    - "unsourced": 가격·평점 주장이 이번 턴 도구 결과에 접지되지 않음.
    접지는 인용된 최종 출처 또는 이번 턴 관찰 출처로 인정한다(검색은 했으나 인용을 빠뜨린 경우까지
    통과시켜 오탐을 막는다). 사유가 있으면 호출부가 재확인으로 정정한다.

    판정은 질의 분류에 종속되지 않는다 — 상품 사실은 **어떤 맥락이라도** 이번 턴 도구 결과에만
    근거한다(원칙 4a). 범위는 답변의 구조(제목 주장 + 가격)가 정한다.
    """
    if detect_title_mismap(text, cited_sources, observed_sources):
        return "mismap"
    if detect_unsourced_rating_claim(text, cited_sources, observed_sources):
        return "unsourced"
    if detect_unsourced_priced_item(
        text, cited_sources, observed_sources, product_context=product_context
    ):
        return "unsourced"
    return None


# 무출처 정책 단정 감지 시 정책 보정 턴에 내리는 지시(2차 턴 user 메시지). yes24_fetch를 강제해
# (지시만으론 비결정적) Yes24 내부 정책 페이지를 열어 실제 규정만 인용과 함께 답하게 한다.
# 주제 중립: 특정 카테고리(반품·취소 등)를 나열하면 모델이 질문과 무관한 그 페이지로 새므로
# (실측: "배송비" 질문에 취소/교환/반품 페이지를 열어 반품 배송비만 답함), 카테고리를 열거하지
# 않고 "사용자가 물은 바로 그 주제"로 앵커한다. 이미 이번 대화에서 연 해당 페이지가 있으면 재사용.
POLICY_CORRECTION_DIRECTIVE = (
    "방금 답변은 Yes24 이용정책을 출처 없이 답했거나 질문 주제를 벗어났습니다. "
    "지금 yes24_fetch로 **사용자가 물은 바로 그 주제**의 Yes24 내부 정책·안내 페이지를 열어 "
    "(이미 이번 대화에서 그 주제의 페이지를 열었으면 그 url을 그대로 재사용), 그 페이지에 실제로 "
    "적힌 내용만 인용[n]과 함께 답하세요. 사용자가 묻지 않은 다른 카테고리(예: 배송비를 물었는데 "
    "반품·취소 규정)로 새지 말고, 페이지에 없는 기한·수치·조건은 지어내지 말며, 공감 서두 없이 "
    "곧바로 본론으로 답하세요."
)

# 정책 페이지 fetch까지 접지에 실패했을 때만 쓰는 최종 안전 안내(폴백). 규정을 지어내지 않고
# 정확한 확인 경로로 안내한다(변명·사과·내부 동작 언급 없이).
UNSOURCED_POLICY_NOTICE = (
    "정확한 환불·반품 규정은 상품과 주문 상태에 따라 달라질 수 있어요. "
    "Yes24 고객센터 또는 마이페이지 > 주문/배송 > 반품/교환 신청에서 확인해 주세요."
)


def has_policy_grounding(sources: list[dict]) -> bool:
    """주어진 출처 목록에 Yes24 정책 페이지 출처(POLICY_SOURCE_TYPES)가 하나라도 있는지."""
    return any(source.get("type") in POLICY_SOURCE_TYPES for source in sources)


def evaluate_policy_answer(
    *,
    cited_sources: list[dict],
    observed_sources: list[dict],
    policy_turn: bool = False,
) -> str | None:
    """정책 답변이 Yes24 정책 페이지에 접지됐는지 판정한다(정상이면 None).

    - "unsourced_policy": **정책 질의 턴**인데 Yes24 정책 페이지(notice) 접지가 전혀 없음.

    범위를 텍스트가 아니라 **구조**로 잡는다: 이 턴이 정책 질의인지는 상류 분류(intent=policy,
    또는 분류 불가로 안전 편입)가 알려주고, 이 모듈은 접지 여부만 본다. 예전엔 주제어 목록
    (반품·환불·교환·취소·배송비)과 규정 신호 목록(N일 이내·영업일·출고완료)의 동시 등장으로
    "정책 규정 단정"을 알아내려 했는데, 목록 밖 주제는 통째로 새고(포인트 소멸·무이자 할부 조건)
    책 제목의 우연한 일치는 오탐이 됐다. 정책 질의인지는 질의가 알고 있으므로 본문을 뒤질 이유가
    없다.

    접지는 인용된 최종 출처 또는 이번 턴 관찰 출처 중 정책 출처가 있으면 인정한다 — fetch는 했으나
    인용 마커를 빠뜨린 정상 답변을 파괴하지 않기 위함이다(마커 부재만으로는 발동하지 않는다).
    """
    if not policy_turn:
        return None
    grounded = has_policy_grounding(cited_sources) or has_policy_grounding(observed_sources)
    return None if grounded else "unsourced_policy"


# ── 결정 — 관측 사실로 "무엇을 할지" 하나를 고른다 ───────────────────────────

# 재확인(보정) 턴에 내리는 지시. 상품·정책 두 갈래이며, 그 외 사유는 상품 지시를 쓴다.
FOLLOWUP_DIRECTIVE = (
    "방금 답변이 사용자의 질문을 근거와 함께 답하지 못했습니다(도구를 쓰지 않았거나 결과가 "
    "질문을 덮지 못함). 지금 도구로 실제 확인해, 도구 결과에 있는 내용만 인용[n]과 함께 답하세요. "
    "책·상품을 찾는 질문이면 yes24_search로(검색어를 바꿔 넓히거나 핵심어만 남겨), 사실·정보를 "
    "묻는 질문이면 web_search로 확인하세요(사실 질문을 책 추천으로 바꾸지 말 것). 확인되지 않은 "
    "제목·저자·가격은 절대 쓰지 말고, 공감 서두 없이 곧바로 본론으로 답하세요."
)

# 미완결(도구 0회 + 인용 0) 재확인까지 실패했을 때의 안전 마감. 원답은 약속이나 추측이라 그대로
# 두면 답이 아닌 것이 최종 답으로 확정된다.
UNFULFILLED_NOTICE = (
    "지금은 확인이 잘 되지 않았어요. 한 번만 다시 물어봐 주시면 정확히 찾아 답해 드릴게요."
)


@dataclass(frozen=True)
class Gate:
    """게이트 발동 결과 — 호출부가 재확인·채택·폴백을 결정하는 데 필요한 전부.

    kind: "contradicted"(출처와 어긋남) | "missing"(접지 없음).
    destructive: 재확인이 실패했을 때 **원답을 버려야 하는가**. 환각이거나(contradicted) 도구를
        한 번도 안 부른 약속문(missing + 도구 0회)이면 True, 도구를 돌렸는데 못 찾은 정직한
        답이면 False(원답 유지 — 비파괴).
    force_tool: 재확인 턴에 강제할 도구(정책이면 yes24_fetch, 그 외 None).
    """

    kind: str
    reason: str
    directive: str
    status_detail: str
    destructive: bool
    force_tool: str | None = None

    @property
    def notice(self) -> str:
        """재확인까지 실패했을 때 내보낼 안전 안내(원답을 버려야 할 때만 쓰인다)."""
        if self.force_tool == "yes24_fetch":
            return UNSOURCED_POLICY_NOTICE
        if self.kind == "contradicted":
            return UNSOURCED_PRODUCT_NOTICE
        return UNFULFILLED_NOTICE


_SEARCH_TOOLS = frozenset({"yes24_search", "web_search", "yes24_browse"})


def evaluate(
    text: str,
    *,
    cited_sources: list[dict],
    observed_sources: list[dict],
    observed_tool_calls: list[dict],
    citation_count: int = 0,
    needs_grounding: bool = False,
    policy_turn: bool = False,
    product_context: bool = True,
) -> Gate | None:
    """이번 턴 답변을 판정해 Gate 하나를 돌려준다(정상이면 None).

    호출부는 **관측한 사실만** 넘긴다(실제 도구 호출 기록·유효 인용 수·질의 분류). 게이트가
    없었던 도구 호출을 지어내 자기 신호를 만들지 않는다.

    ① 출처와 어긋나는 주장(환각) → contradicted(파괴적 폴백).
    ② 접지가 필요한데 없음 → missing. 정책 턴의 notice 부재, 또는 접지 필요 질의의 인용 0.
       도구를 실제로 돌렸다면 **비파괴**다: 넓힌 재검색이 답을 찾으면 채택하고, 못 찾으면 원 답변
       (정직한 "못 찾음")을 그대로 둔다. 결과 0건은 "그 책이 없다"만이 아니라 "검색어가 나빴다"
       이기도 해서, 재확인이 실제로 답을 회수한다 — 대가는 지연뿐이므로 비파괴로 유지한다.
    """
    contradiction = evaluate_product_answer(
        text,
        cited_sources=cited_sources,
        observed_sources=observed_sources,
        product_context=product_context,
    )
    if contradiction is not None:
        return Gate(
            kind="contradicted",
            reason=contradiction,
            directive=CORRECTION_DIRECTIVE,
            status_detail="정확한 정보를 찾아 다시 확인하고 있어요",
            destructive=True,
            force_tool="yes24_fetch" if has_policy_grounding(observed_sources) else None,
        )

    policy_missing = evaluate_policy_answer(
        cited_sources=cited_sources,
        observed_sources=observed_sources,
        policy_turn=policy_turn,
    )
    searched = [c for c in observed_tool_calls if c.get("tool_name") in _SEARCH_TOOLS]
    if policy_missing is not None:
        return Gate(
            kind="missing",
            reason=policy_missing,
            directive=POLICY_CORRECTION_DIRECTIVE,
            status_detail="Yes24 정책 페이지에서 정확히 확인하고 있어요",
            destructive=not searched,
            force_tool="yes24_fetch",
        )

    # 접지가 필요한 질의이거나, **실제로 검색을 돌렸는데** 인용이 하나도 안 붙었으면 근거가 없다.
    # 후자는 분류와 무관한 순수 관측 사실이다 — 검색을 했다는 것 자체가 "이 답은 근거가 필요하다"는
    # 모델의 판단이므로, 분류가 web으로 봤더라도 인용 0이면 재확인 대상이다.
    if (needs_grounding or searched) and citation_count == 0:
        return Gate(
            kind="missing",
            reason="unfulfilled" if not searched else "shallow",
            directive=CORRECTION_DIRECTIVE if not searched else FOLLOWUP_DIRECTIVE,
            status_detail="실제로 확인해서 답해 드릴게요"
            if not searched
            else "검색 범위를 넓혀 다시 확인하고 있어요",
            destructive=not searched,
        )
    return None
