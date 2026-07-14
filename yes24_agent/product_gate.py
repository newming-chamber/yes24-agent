"""무출처 상품 사실 게이트 — 이번 턴 Yes24 상품 출처 없이 지어낸 책 상품 사실을 차단한다.

배경(실측 확정): 감정·상담·추천 맥락("우울한데 읽을 책", "직장 상사가 불편해요")에서 루트
에이전트가 yes24_search를 호출하지 않고 구체적인 책 제목·저자·가격(예: 15,120원)을 지어내는
환각이 비결정적으로 발생한다. 프롬프트("무출처 상품 금지")로는 안 잡혀(pro가 이 맥락에서
간헐 무시), 코드 제어흐름으로 물리화한다(재설계 결정 P0 "충분성/무출처 게이트를 코드로").

이 모듈은 세 가지를 결정론으로 판정한다:
1. **상품 사실 패턴**(detect_unsourced_product_claim): 답변 본문이 책 상품을 사실로 주장하는가.
   - T1(가격형): 가격 토큰("N원")이 도서 추천 맥락(저자·역자·장르 표기) 안에 있다.
   - T2(목록형): 책 제목 마커(**볼드**·《》·『』)와 저자 표기가 함께 있다(가격이 없어도).
   - T3(제목괄호+가격): 『제목』·《제목》 뒤에 가격이 오면 저자 어휘가 없어도 책값 주장.
   비도서 수치("주가 296,000원", "최저시급")는 도서 맥락 신호가 없어 배제된다.
2. **Yes24 상품 접지**(has_product_grounding): 답변을 뒷받침할 Yes24 상품 출처(검색·상세·
   브라우징 결과)가 실제로 있는가. 웹 출처(type=web)·정책 공지(type=notice)는 상품 접지가 아니다.
3. **인용-제목 오매핑**(detect_title_mismap): 답변이 "『제목』 … 가격 [n]"으로 주장한 책 제목이
   그 출처[n]의 실제 title과 일치하는가. 재검색·브라우징이 무관한 결과(예: 블라인드북)를 받았는데
   모델이 원하는 제목을 지어 그 source_id에 매핑하면, [n] 마커는 있어 "무출처(sources=0)" 판정은
   우회하지만 제목이 출처와 불일치한다 → cited-but-fabricated 환각. 이를 구조로 잡는다.

게이트는 (무출처 상품 주장) 또는 (인용-제목 오매핑)일 때 발동한다. runner는 발동 시 재검색으로
정정하고(escalate), 재검색도 실패하면 안전 안내로 폴백해 확정 답변에 환각이 남지 않게 한다.

이 모듈은 순수 함수 계층으로, 다른 프로젝트 모듈(config 등)을 import하지 않는다.
"""

import re

# Yes24 '상품' 출처로 인정하는 source type 집합. yes24_search=search_result,
# yes24_fetch(도서)=book_detail, yes24_browse=browse. 웹 검색·열람(web)과 정책
# 공지(notice)는 상품 가격·목록의 근거가 될 수 없으므로 상품 접지에서 제외한다.
PRODUCT_SOURCE_TYPES = frozenset({"search_result", "book_detail", "browse"})

# 가격 토큰: "15,120원", "16920 원" 등. 숫자로 시작해 쉼표를 포함할 수 있는 수 + '원'.
# 이 토큰만으로는 책값인지 알 수 없어(주가·시급·칼로리도 '원') 반드시 _BOOK_CONTEXT와 함께 본다.
_PRICE_TOKEN = re.compile(r"\d[\d,]*\s*원")

# 저자·역자 표기(도서 상품 고유 메타). "OO 저"는 한글 이름 + 공백 + '저' 뒤에 경계가
# 오는 형태만 잡아 "저가(低價)"·"저는" 같은 우연한 부분일치를 배제한다(주가 답변 '저가' 오탐 방지).
_AUTHOR_MARK = re.compile(
    r"저자|지은이|글쓴이|엮은이|옮긴이|옮김|(?<![가-힣])역자|[가-힣]\s저(?=[\s/|)·,.]|$)"
)

# 도서 추천 맥락 신호. 가격 토큰을 '책값'으로 판정하기 위한 결정론 가드 — 저자·역자·장르·출판
# 어휘 또는 '책'·저자류 호칭(스님·시인)이 있어야 한다. 비도서 수치(주가·시급·환율)는 이 신호가
# 없어 미발동. 한글 합성어 substring 누수 방지(rev-d1): '책'은 '정책·대책'의 substring이라 앞에
# 한글이 없을 때만('이 책'), '도서'는 '도서관·도서실' 앞이면 제외, '역자'는 앞에 한글이 있으면
# 제외(번역자·통역자·반역자 균일 배제, '역자:'·문두 '역자'는 유지).
# 느슨한 '작가'(영화 각본가·감독 '황동혁 작가')는 뺀다 — 진짜 책은 '소설·에세이·저/지음'으로 잡힘.
_BOOK_CONTEXT = re.compile(
    r"저자|지은이|글쓴이|엮은이|옮긴이|옮김|(?<![가-힣])역자|출판사|"
    r"에세이|소설|시집|산문|장편|단편|도서(?!관|실)|(?<![가-힣])책|스님|시인|지음|펴냄|"
    r"[가-힣]\s저(?=[\s/|)·,.]|$)"
)

# 도서 제목 마커: **볼드**, 《제목》, 『제목』 (2자 이상 내용). 목록형 추천에서 책 제목을
# 강조하는 관용. 저자 표기와 함께 나타나면 카탈로그식 상품 목록으로 본다.
_TITLE_MARKER = re.compile(r"\*\*[^*\n]{2,}\*\*|《[^》\n]{2,}》|『[^』\n]{2,}』")

# 책 제목 전용 겹낫표·화살괄호(『』·《》). 한국어에서 도서·작품명에만 쓰는 문장부호라
# **볼드보다 강한 도서 신호**다. 가격 토큰과 함께 나타나면 저자 표기 어휘가 없어도
# "『제목』 … N원" = 책값 주장으로 본다(비도서 '원'은 이 괄호가 붙지 않아 배제됨).
_BOOK_TITLE_BRACKET = re.compile(r"《[^》\n]{2,}》|『[^』\n]{2,}』")

# 무출처/오매핑 감지 시 모델에 내리는 재검색 지시(2차 턴 user 메시지). 직전 답변에서 지어낸
# 정보를 실제 도구로 확인해 인용과 함께 다시 답하게 한다 — 되물음 대신 실제 답을 회수한다.
# **질문 유형에 맞는 도구로 라우팅**하도록 명시: 책·상품 추천이면 yes24_search로, 사실·정보
# 질문(최저임금·법률·시세 등)이면 web_search로. 사실 질문을 책 추천으로 치환하지 않는다(P1).
CORRECTION_DIRECTIVE = (
    "방금 답변에는 실제로 확인하지 않은 정보(책 제목·저자·가격 등)가 섞여 있었습니다. "
    "사용자의 원래 질문에 맞는 도구로 지금 다시 확인해, 도구 결과에 실제로 있는 내용만 "
    "인용[n]과 함께 답하세요. 책·상품을 추천·안내하는 질문이면 yes24_search로 검색하고, "
    "사실·정보를 묻는 질문(최저임금·법률·시세·뉴스 등)이면 web_search로 확인해 정보 자체로 "
    "답하세요(사실 질문을 책 추천으로 바꾸지 말 것). 확인되지 않은 제목·저자·가격은 절대 쓰지 "
    "말고, 공감 서두 없이 곧바로 본론으로 답하세요."
)

# 재검색까지 접지에 실패했을 때만 쓰는 최종 안전 안내(폴백). 내부 자기수정 과정("방금 안내에
# 확인되지 않은 정보가…")은 사용자에게 노출하지 않고, 자연스럽게 취향을 한 줄 물어 정확한 추천으로
# 잇는다(변명·사과·내부 동작 언급 없이).
UNSOURCED_PRODUCT_NOTICE = (
    "찾으시는 책의 결(감정·상황·주제·좋아하는 장르)을 한 줄만 더 알려주시면, "
    "딱 맞는 책을 Yes24에서 정확히 찾아 추천해 드릴게요."
)


def detect_unsourced_product_claim(text: str) -> bool:
    """답변 본문이 책 상품을 사실로 주장하는지(가격·제목+저자) 결정론으로 판정한다.

    T1(가격형: 가격+도서맥락)·T2(목록형: 제목마커+저자표기)·T3(제목괄호+가격) 중 하나라도
    만족하면 True. 접지 여부와 무관한 순수 텍스트 판정으로, 게이트 발동은 호출부가
    접지(has_product_grounding)와 결합해 정한다. (평점 주장은 맥락 결합이 오탐이 많아 텍스트
    판정이 아닌 값 대조로 분리한다 — detect_unsourced_rating_claim.)
    """
    if not text:
        return False
    t1 = bool(_PRICE_TOKEN.search(text) and _BOOK_CONTEXT.search(text))
    t2 = bool(_TITLE_MARKER.search(text) and _AUTHOR_MARK.search(text))
    # T3: 『제목』·《제목》 뒤에 가격이 오면 저자 어휘가 없어도 책값 주장. "『불안이라는 위안』
    # … 16,020원"처럼 T1(도서맥락 키워드 부재)·T2(저자표기 부재)를 빠져나가던 누출을 막는다.
    t3 = bool(_BOOK_TITLE_BRACKET.search(text) and _PRICE_TOKEN.search(text))
    return t1 or t2 or t3


def has_product_grounding(sources: list[dict]) -> bool:
    """주어진 출처 목록에 Yes24 상품 출처(PRODUCT_SOURCE_TYPES)가 하나라도 있는지."""
    return any(source.get("type") in PRODUCT_SOURCE_TYPES for source in sources)


# ── 항목별(책 단위) 접지 검증 — 지어낸 책 감지 ───────────────────────────────
#
# 배경(실측 D1): 추천이 "일부는 접지(실제 검색된 책), 일부는 지어낸 책"으로 섞이면,
# has_product_grounding는 접지된 책 때문에 True가 돼 **답변 전체**의 무출처 검사를 끈다.
# 그 틈으로 지어낸 책+저자+가격이 통과한다(부분접지 구멍). 이를 **항목(책) 단위 접지**로 좁힌다 —
# 각 책 블록에 (cited+observed) 상품 출처 title이 나타나는지 대조해, 어느 출처도 없으면 지어낸 책.
#
# 오탐 0 최우선(rev-d1 1·2라운드 실측 반영):
# (1) **저자 표기(_AUTHOR_NOTATION)가 있는 블록만** 본다. D1 실제 실패모드가 "지어낸 책+저자+정가"
#     3종 세트(crema QA)라, 저자 표기 요구는 검출력을 거의 안 잃으면서 비도서 수치를 구조적으로
#     배제한다. _BOOK_CONTEXT는 '정책'→'책'·'도서관'→'도서' substring 누수로 FP-2를 냈고, 바로
#     '주가·시급' web 답변(FP-A)도 저자 표기가 없어 함께 배제된다. 바른 '작가'는 '각본가·영화
#     작가' 등 비도서 인물에도 붙어 제외한다("이름+저/지음/저자:" 직접 표기만).
# (2) 접지(둘 중 하나면 통과): (a) 블록 평문에 출처 title이 등장(정규화 부분일치) — 강조(**볼드**)가
#     제목 아닌 문구를 감싸도 실제 제목이 있으면 통과(FP-B). (b) 주장 제목이 출처 title에
#     _title_supported로 매칭(양방향·토큰겹침) — 답변이 출처 제목을 축약해도 통과(『나미야』↔
#     나미야 잡화점의 기적, FP-1). 검색된 책은 미인용이라도 이 접지로 통과(미인용 정상 인정).
# (3) 접지 안 되고 주장 제목이 뽑히면(볼드/괄호) 지어낸 책 → 발동. 제목 못 뽑는 산문·대명사는 보류.
# 최상위 불릿/헤딩/빈 줄로 항목을 나누고 들여쓴 하위 불릿(저자·가격)은 부모 항목에 묶는다.

# 최상위(들여쓰기 없는) 불릿·헤딩만 새 항목 시작으로 본다. 하위 불릿은 앞에 공백이 있어 미매칭.
_TOP_BULLET = re.compile(r"[*\-•·]\s")
_HEADING = re.compile(r"#{1,6}\s")
# 카탈로그 저자 표기(발동 필수 요건). "이름+저/저자:/지은이/지음/옮김/역자" 직접 표기만 — 비도서
# 수치(주가·시급·'정책'·'도서관')와 느슨한 '작가'(각본가·영화작가)를 배제한다. _BOOK_CONTEXT의
# substring 누수(FP-2)를 피하려 도서 어휘(책·도서·소설)는 넣지 않는다.
_AUTHOR_NOTATION = re.compile(
    r"[가-힣]\s?저(?=[\s/|)·,.]|$)|저자|지은이|글쓴이|엮은이|옮긴이|옮김|(?<![가-힣])역자|지음|엮음"
)


def _split_items(text: str) -> list[str]:
    """본문을 추천 항목(책) 블록으로 나눈다.

    최상위 불릿(col0)·헤딩·빈 줄에서 새 블록이 시작되고, 들여쓴 하위 줄은 직전 블록에 붙는다
    (한 책의 제목·저자·가격·설명·인용이 한 블록에 모이도록 — 오탐 방지 핵심).
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        starts_item = bool(_TOP_BULLET.match(line) or _HEADING.match(stripped))
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
) -> bool:
    """가격+저자 표기 항목이 (cited+observed) 상품 출처에 접지되지 않고 제목만 지어냈으면 True.

    항목 단위 접지로 부분접지 구멍(D1)을 메운다. 저자 표기(_AUTHOR_NOTATION)가 있는 책 항목만
    보므로 비도서 수치(주가·시급·정책)는 배제된다. 접지는 (a)블록 평문에 출처 title 등장 또는
    (b)주장 제목이 출처 title에 _title_supported로 매칭(축약 허용) 중 하나면 인정한다. 검색된 책은
    미인용이라도 이 접지로 통과하고, 어느 쪽도 아니면서 주장 제목이 뽑히면 지어낸 책으로 본다.
    제목을 못 뽑는 산문·대명사 항목은 보류한다(오탐 방지).
    """
    if not text:
        return False
    source_titles = [
        source.get("title", "")
        for source in (*cited_sources, *observed_sources)
        if source.get("type") in PRODUCT_SOURCE_TYPES and source.get("title")
    ]
    source_norms = [
        norm for norm in (_norm_title(_core_title(t)) for t in source_titles) if len(norm) >= 2
    ]
    for block in _split_items(text):
        if not (_PRICE_TOKEN.search(block) and _AUTHOR_NOTATION.search(block)):
            continue
        titles = [
            _clean_asserted(match)
            for match in _ASSERTED_TITLE.finditer(block)
            if _is_title_candidate(_clean_asserted(match))
        ]
        block_norm = _norm_title(block)
        # 접지: (a) 출처 title이 블록에 등장 OR (b) 주장 제목이 출처 title에 매칭(축약 허용).
        grounded = any(norm in block_norm for norm in source_norms) or any(
            _title_supported(title, source_title)
            for title in titles
            for source_title in source_titles
        )
        # 접지 안 됐고 주장 제목이 뽑히면 지어낸 책. 제목 못 뽑는 산문·대명사는 보류.
        if not grounded and titles:
            return True
    return False


# ── 인용-제목 오매핑 판정 ────────────────────────────────────────────────────
#
# "제목 [n]" 주장의 제목이 출처[n]의 실제 title과 맞는지 **모든 도서 인용에 전수 검증**한다
# (특정 경로·가격 유무와 무관). 재검색·브라우징이 무관한 결과를 받았는데 모델이 원하는 제목을
# 지어 그 source_id에 매핑하면 [n] 마커가 "무출처(sources=0)" 판정을 우회하므로, 제목 대조로 잡는다.
#
# 오탐(정상 인용을 오매핑으로 오판)이 최우선 금지 사항이라 두 겹의 보수 가드를 둔다:
# (A) **같은 줄 연관** — 제목 마커와 [n]이 같은 줄에 있을 때만 그 제목의 주장으로 본다. 목록 항목
#     헤더("* **제목** [n]")는 같은 줄이지만, 본문 산문에 인용만 흩어진 정직한 참조(제목 주장
#     아님)나 다른 항목의 [n]을 오귀속하지 않는다.
# (B) **출처 제목 본문 존재** — 출처[n]의 실제 title이 답변 어딘가에 나오면(축약·부제 변형 포함)
#     정상 인용으로 통과시킨다. 저자의 다른 작품을 배경으로 언급(『고령화 가족』의 작가…[n], 여기서
#     [n]은 실제 추천 도서 '아코디언'을 가리킴)해도, 실제 도서명이 본문에 있으므로 오탐하지 않는다.
# 제목 일치는 축약·부제 변형(같은 책의 부제를 줄여 씀)을 관대히 인정한다(부분일치·토큰 과반 겹침).

# 책 제목 마커: **볼드**·《》·『』 또는 마크다운 헤딩(### 제목). 목록형 추천에서 각 책 제목을
# 이 중 하나로 강조하는 관용. group 1~3=볼드/괄호, group 4=헤딩 본문. 줄 단위로 적용한다.
_ASSERTED_TITLE = re.compile(
    r"\*\*([^*\n]{2,})\*\*|《([^》\n]{2,})》|『([^』\n]{2,})』|^#{1,6}\s+(.+?)\s*$",
)
# 인용 마커 [n].
_CITATION_MARKER = re.compile(r"\[(\d+)\]")
# 제목 마커 안에 와도 '제목'이 아닌 라벨·소제목(저자/가격/줄거리 등). 제목 후보에서 제외해
# 라벨 볼드를 책 제목으로 오인하지 않는다.
_TITLE_LABELS = frozenset({
    "저자", "지은이", "글쓴이", "엮은이", "옮긴이", "옮김", "역자", "작가",
    "출판사", "가격", "정가", "판매가", "소개", "책소개", "줄거리", "목차",
    "평점", "추천", "특징", "구성", "내용", "제목", "부제", "출간", "출간일", "시리즈",
})


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


# 제목 토큰 겹침 최소 비율 — 주장 제목의 토큰 중 이만큼 이상이 출처 title에 있으면 같은 책으로
# 본다. 축약·부제 변형(정상 인용)은 통과시키고 전혀 다른 제목만 불일치로 잡는 관대 임계로,
# 오탐(정상 인용을 오매핑으로 오판) 방지를 위해 과반(0.5)으로 넉넉히 둔다.
_TITLE_TOKEN_OVERLAP_MIN = 0.5


def _clean_asserted(match: re.Match) -> str:
    """제목 마커 매치에서 제목 문자열을 뽑는다(끝의 인용 마커 [n] 제거)."""
    raw = next((g for g in match.groups() if g), "") or ""
    return _CITATION_MARKER.sub("", raw).strip().rstrip(":").strip()


def _is_title_candidate(text: str) -> bool:
    """제목 후보인지 — 라벨·가격·너무 짧은 문자열은 제외."""
    if not text or len(text) < 2 or text in _TITLE_LABELS:
        return False
    return not _PRICE_TOKEN.fullmatch(text)


def _title_supported(asserted: str, source_title: str) -> bool:
    """본문이 주장한 제목(asserted)이 출처 title에 의해 뒷받침되는지(관대 매칭).

    같은 책을 축약·부제 변형으로 쓴 정상 인용은 통과시키고(핵심 제목 부분일치 또는 토큰
    과반 겹침), 전혀 다른 책 제목만 불일치로 본다. 오탐 방지를 위해 임계를 넉넉히 둔다.
    """
    na, ns = _norm_title(_core_title(asserted)), _norm_title(_core_title(source_title))
    if not na:
        return True  # 비교 불가한 빈 제목은 통과(오탐 방지)
    if na in ns or ns in na:
        return True
    tokens = _title_tokens(asserted)
    if not tokens:
        return True
    hits = sum(1 for w in tokens if _norm_title(w) in ns)
    return hits / len(tokens) >= _TITLE_TOKEN_OVERLAP_MIN


def detect_title_mismap(text: str, sources: list[dict]) -> bool:
    """도서 인용의 주장 제목이 그 출처의 실제 title과 불일치(오매핑)하는지 전수 판정한다.

    각 줄에서 (제목 마커 + 같은 줄의 상품 출처 인용[n])을 찾아, 그 줄의 어떤 제목도 출처[n].
    title을 뒷받침하지 못하고(_title_supported=False), 게다가 출처[n]의 실제 title이 답변
    어디에도 등장하지 않으면 오매핑으로 본다(가드 A·B는 위 주석 참고). 정직한 참조(제목
    주장 없이 인용만)·웹 출처 인용·저자 배경작 언급은 이 가드로 걸러져 오탐하지 않는다.
    """
    if not text:
        return False
    id_to_source = {s.get("id"): s for s in sources if s.get("id") is not None}
    normalized_text = _norm_title(text)
    for line in text.splitlines():
        line_titles = [
            _clean_asserted(m)
            for m in _ASSERTED_TITLE.finditer(line)
            if _is_title_candidate(_clean_asserted(m))
        ]
        if not line_titles:
            continue
        product_ids = {
            n
            for n in (int(x) for x in _CITATION_MARKER.findall(line))
            if id_to_source.get(n, {}).get("type") in PRODUCT_SOURCE_TYPES
        }
        for source_id in product_ids:
            source_title = id_to_source[source_id].get("title", "")
            # (A) 같은 줄 제목 중 하나라도 출처와 일치하면 정상.
            if any(_title_supported(t, source_title) for t in line_titles):
                continue
            # 1자 정규화 제목 출처는 오매핑 판정이 신뢰 불가하다 — 접지(가드 B)의 '텍스트 등장'도
            # 우연일 수 있고("흰색"의 '흰"), 같은 줄 비교 언급(『채식주의자』처럼)이 가드 A를
            # 실패시키면 오매핑으로 오판된다(rev-d1 실측: '흰'·'봄'·'시'·'말' 1자 제목). 오탐 0
            # 우선으로 1자 제목 출처는 mismap 검사를 보류한다(2자+ 제목의 오매핑 검출은 불변).
            source_core = _norm_title(_core_title(source_title))
            if len(source_core) < 2:
                continue
            # (B) 출처의 실제 제목이 답변 어딘가에 등장하면 정상(배경작 언급·서식 차이 등).
            if source_core in normalized_text:
                continue
            return True
    return False


# ── 평점 값 대조(value grounding) — 지어낸 평점 감지 ─────────────────────────
#
# 배경(rev-t4): "평점 토큰+도서맥락" 정규식 결합(T4)은 오탐 7종을 냈고, 이어 외부 평점을
# 키워드 블랙리스트로 면제하던 방식도 성장형 목록(알라딘·교보·구글맵·'관객' 누락)이라 폐기했다.
# detect_title_mismap과 같은 원리의 **값 대조** + **양성 귀속**으로 확정한다:
#   ① 자사(Yes24) 평점 주장인지 양성 판정 — (i) 줄에 Yes24 표기 OR (ii) 줄이 상품 출처[n] 인용
#      OR (iii) 이번 턴 출처 0(순수 무접지). 그 외(식당·영화·타서점 평점)는 대상 아님.
#   ② 주장 숫자가 이번 턴 출처 rating에 있으면 통과(마커 없어도 참), 없는 값만 발동(지어낸 평점).
# 척도: "N점 만점에 M"·"M/N"에서 척도 N을 읽어 10점 척도가 아니면(5점 만점 등) 값 대조 불가로
# 건너뛰고, 척도 토큰의 N을 값으로 오인하지 않도록 '만점에 M'의 M(실제 점수)을 우선 캡처한다.

# 평점/별점 주장 앵커. 이 단어가 있어야 '평점 값 주장'으로 본다(장식 ★·'3점 슛'·배점·'회화 30점'
# 은 앵커가 없어 배제).
_RATING_ANCHOR = re.compile(r"평점|별점")
# 앵커 뒤 평점 값: "평점 9.8점", "평점은 9.5", "별점 4.3". 조사·콜론·공백을 건너뛰고 숫자를 딴다.
_RATING_VALUE = re.compile(r"(?:평점|별점)\s*(?:은|는|이|가|을|를|:)?\s*(\d+(?:\.\d+)?)")
# "만점 …M" 실제 점수 — 척도 접두 뒤의 값. 조사 나열이 아니라 "만점 뒤 근접(≤4자) 숫자"의
# 일반 규칙으로, "만점에 M"·"만점에서 M"·"만점 기준 M"을 모두 잡고 척도 N을 값으로 오인하지 않는다.
_MANJEOM_SCORE = re.compile(r"만점[^\d\n]{0,4}(\d+(?:\.\d+)?)")
# 척도 선언 — "10점 만점"(N). 10점 척도가 아니면 값 대조 불가로 건너뛴다.
_SCALE_MANJEOM = re.compile(r"(\d+)\s*점?\s*만점")
# 슬래시 척도 — "M/N". 날짜(2026/07)를 척도로 오인하지 않도록 분자 M을 함께 잡아, M이 평점다운
# 수(≤10)일 때만 척도로 본다(호출부에서 검사). 척도 N은 1~2자리로 제한.
_SCALE_SLASH = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d{1,2})")
# 별점 스타 문자(앵커가 있는 줄에서만 평점 주장으로 본다).
_STAR_RUN = re.compile(r"[★⭐]")
# 자사(Yes24) 표기 — 양성 귀속 신호 (i).
_YES24_MENTION = re.compile(r"yes24|예스24|예스이십사", re.IGNORECASE)
# 절 분리(쉼표) — 귀속을 줄이 아니라 절 단위로 좁혀 혼재 오귀속을 막는다("알라딘 평점 …, Yes24
# 판매 1위" 같은 문장에서 평점 절에 Yes24가 없으면 대상 아님).
_CLAUSE_SPLIT = re.compile(r"[,，]")


def _declared_scale(clause: str) -> int | None:
    """절에서 선언된 평점 척도를 읽는다(없으면 None). "N점 만점"의 N, 또는 "M/N"의 N(단 분자
    M이 평점다운 수 ≤10일 때만 — 날짜 2026/07을 척도로 오인하지 않게)."""
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
    """주장 평점이 출처 평점 중 하나와 대조되는지(정규화: 9.5==9.50, 정수 주장은 반올림 관대).

    오탐 0 우선: 관대하게 접지 인정(미스는 허용, 헛발동은 금지). 정수 주장(9점)은 출처값의
    내림·올림과 맞으면 접지로 본다(9.4를 '9점'으로 옮긴 정상 서술을 헛발동하지 않게).
    """
    for r in source_ratings:
        if abs(claimed - r) < 0.1:  # 9.5 vs 9.50
            return True
        if claimed == int(claimed) and int(claimed) in (int(r), int(r) + 1):
            return True
    return False


def detect_unsourced_rating_claim(
    text: str,
    cited_sources: list[dict],
    observed_sources: list[dict],
) -> bool:
    """자사 평점 값 주장이 이번 턴 출처의 rating과 대조되지 않으면(지어낸 평점) True.

    맥락 어휘가 아니라 값 대조 + 양성 귀속으로 판정한다(위 주석 ①②). 자사 평점 주장으로 판정된
    줄만 값 대조하고, 주장 숫자가 출처 rating에 있으면 통과, 없는 값만 발동한다. 대조할 출처
    평점이 하나도 없어도, 상품 출처가 있으면(rating 미파싱) 검증 불가로 발동하지 않고, 출처가
    아예 없을(무접지) 때만 지어낸 평점으로 본다.
    """
    if not text:
        return False
    source_ratings = [
        r
        for r in (_source_rating(s) for s in (*cited_sources, *observed_sources))
        if r is not None
    ]
    has_product = has_product_grounding(cited_sources) or has_product_grounding(observed_sources)
    zero_sources = not cited_sources and not observed_sources
    id_to_type = {s.get("id"): s.get("type") for s in cited_sources}
    for line in text.splitlines():
        for clause in _CLAUSE_SPLIT.split(line):
            if not _RATING_ANCHOR.search(clause):
                continue
            # ① 양성 귀속(절 단위): 자사 평점 주장인지. (i) 절에 Yes24 표기 / (ii) 절이 상품 출처[n]
            #    인용 / (iii) 이번 턴 출처 0. 셋 다 아니면(식당·영화·타서점 평점) 대상 아님.
            clause_ids = {int(n) for n in _CITATION_MARKER.findall(clause)}
            cites_product = any(id_to_type.get(i) in PRODUCT_SOURCE_TYPES for i in clause_ids)
            if not (_YES24_MENTION.search(clause) or cites_product or zero_sources):
                continue
            # 척도가 10점이 아니면(5점 만점 등) 10점 척도 출처와 값 대조 불가 → 건너뛴다.
            scale = _declared_scale(clause)
            if scale is not None and scale != 10:
                continue
            # ② 값 추출: "만점 …M"이면 실제 점수 M을(척도 N 오캡처 방지), 아니면 앵커 뒤 숫자.
            claimed = [float(m.group(1)) for m in _MANJEOM_SCORE.finditer(clause)]
            if not claimed:
                claimed = [float(m.group(1)) for m in _RATING_VALUE.finditer(clause)]
            if claimed:
                for value in claimed:
                    if source_ratings:
                        if not _rating_grounded(value, source_ratings):
                            return True  # 대조 가능한데 없는 값 = 지어낸 평점
                    elif not has_product:
                        return True  # 대조할 출처 평점도 상품 출처도 없음(무접지) = 지어낸 평점
                    # else: 상품 출처는 있으나 rating 미파싱 → 검증 불가, 발동 안 함.
            elif _STAR_RUN.search(clause) and not source_ratings and not has_product:
                # 별점 ★ 주장(숫자 없음)은 척도 대조 불가 — 출처·상품 접지 전혀 없을 때만 발동.
                return True
    return False


def evaluate_product_answer(
    text: str,
    *,
    cited_sources: list[dict],
    observed_sources: list[dict],
) -> str | None:
    """답변의 상품 주장이 근거에 어긋나는 사유를 반환한다(정상이면 None).

    - "mismap": 상품 주장 블록의 제목이 인용한 출처 title과 불일치(cited-but-fabricated).
    - "unsourced": 책 상품 사실(가격·저자·제목) 무접지, 또는 자사 평점 주장의 값이 이번 턴 출처
      rating과 대조되지 않음(지어낸 평점 — 값 접지 실패).
    접지는 인용된 최종 출처 또는 이번 턴 관찰 출처 중 상품 출처가 있으면 인정한다(검색은 했으나
    인용을 빠뜨린 경우까지 통과시켜 오탐을 막는다). 사유가 있으면 runner가 재검색으로 정정한다.
    """
    if detect_title_mismap(text, cited_sources):
        return "mismap"
    # 평점 값 대조: 자사 평점 주장 숫자가 이번 턴 출처 rating에 없으면 지어낸 평점(값 접지 실패).
    if detect_unsourced_rating_claim(text, cited_sources, observed_sources):
        return "unsourced"
    # 항목 단위 접지(부분접지 구멍 D1): 책+가격 항목의 제목이 (cited+observed) 상품 출처 어디에도
    # 없으면 지어낸 책으로 본다. detect_title_mismap(인용된 책의 제목 대조)과 아래 전체접지 검사
    # (무접지) 사이의 틈 — '일부만 접지된 추천에 섞인 지어낸 책' — 을 메운다. 검색된 책은 미인용
    # 이라도 observed에 있어 통과하므로 오탐이 없다.
    if detect_unsourced_priced_item(text, cited_sources, observed_sources):
        return "unsourced"
    grounded = has_product_grounding(cited_sources) or has_product_grounding(observed_sources)
    if not grounded and detect_unsourced_product_claim(text):
        return "unsourced"
    return None
