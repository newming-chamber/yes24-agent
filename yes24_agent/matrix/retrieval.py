"""C1 — 공유 검색 풀 조립(retrieve-once).

16 페르소나가 같은 질문에 필요로 하는 것은 **같은 사실·후보 책**이고 다른 것은 톤·선택·
프레이밍이다. 그래서 검색은 질문당 소수회(fanout)만 실행해 공유 후보 풀 + 공유 출처
레지스트리를 만들고, 생성 16회가 이 풀 하나를 나눠 쓴다(Yes24 트래픽 O(1)).

채팅 루프를 재사용하지 않고 원시 요소만 재사용한다:
- `search_url`·`parse_search`·`Yes24Client.get_text`: yes24_search 도구의 내부 부품.
- `register_source`(plain dict로 호출 — ToolContext 불필요, MutableMapping만 받음).
- 공유 클라이언트 싱글턴(`yes24_search._get_client`): 프로세스 전역 http_rps 스로틀을 공유해
  매트릭스 검색도 예의 있는 트래픽이 되게 한다(별도 클라이언트를 만들면 스로틀이 분리됨).

파싱 0/조회 실패는 빈 성공으로 위장하지 않고 status로 명시한다("empty"/"error"). 생성 단계는
status!="ok"면 genai를 호출하지 않고 16열 모두 정직 폴백으로 처리한다(비용 가드).
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError

from yes24_agent.config import Settings
from yes24_agent.matrix.genai_runtime import get_genai_client
from yes24_agent.sources import get_sources, register_source
from yes24_agent.tools._pubstatus import pub_status
from yes24_agent.tools.web_search import _get_client as _get_web_client
from yes24_agent.tools.web_search import _truncate_snippet
from yes24_agent.tools.yes24_search import _get_client
from yes24_agent.yes24.client import Yes24FetchError
from yes24_agent.yes24.parsers import ParseError, parse_search
from yes24_agent.yes24.urls import search_url

logger = logging.getLogger(__name__)

# KST(UTC+9). checked_at 타임스탬프용(도구 checked_at과 동일 규약).
_KST = timezone(timedelta(hours=9))

# 캐시 키 정규화용 연속 공백 축약 패턴.
_WHITESPACE_RE = re.compile(r"\s+")

# fanout > 1일 때 풀을 넓힐 섹션 변형 순서. 같은 질문을 반복 검색하면 결과가 동일해
# 무의미하므로, LLM 없이 결정론으로 풀을 넓히는 방법으로 섹션(통합→국내도서)을 순회한다.
# fanout=1은 통합검색 1회. fanout이 변형 수를 넘으면 있는 만큼만 실행한다.
_SECTION_VARIANTS: tuple[str, ...] = ("all", "book")

# 정제 프롬프트. 핵심은 **추출이 아니라 "검색 의도 번역(translation)"**이다 — 원문에 없는
# 단어라도 의도를 더 잘 담으면 생성하고(예: '과학적인 책' → '과학 교양', 문제집 아닌 교양서
# 의도 주입), 서로 다른 의미 각도로 1~N개 검색어를 내 풀을 넓힌다. intent 분류(product/web/
# none)도 함께 낸다 — 지금은 도서(product) 경로만 쓰지만 향후 web/none 라우팅에 재사용한다.
# 사례별 패치가 아니라 일반 원칙으로 기술한다. 도구 없이 flash 1회(JSON 구조화 출력).
_REFINE_SYSTEM = """사용자 질문을 분석해 (1) 의도 분류와 (2) 검색어를 JSON으로 내세요.

intent 분류:
- "product": 책·상품·도서를 추천/정보/구매하려는 질문. **막연한 도서 요청('무슨 책 읽을까',
  '책 추천해줘', '읽을 거 없나')도 product입니다** — 장르가 없어도 책을 원하는 것입니다.
- "web": 시사·사실·실시간 정보 질문(뉴스·스포츠 결과·주가·날씨·인물 근황 등).
- "none": 책·정보를 원하지 않는 순수 잡담·감정 토로·의견·인사만.

queries(검색어) 규칙 — **의도를 최대한 담는 번역**이지 단순 추출이 아닙니다. Yes24는 작가·제목·
주제 색인이라, 질문의 서술어를 그대로 넣기보다 **좋은 책이 실제로 달고 있을 작가·작품·주제어**로
옮겨야 좋은 후보가 걸립니다:
- 원문 단어에 얽매이지 말고 검색 의도를 가장 잘 담는 검색어를 만드세요. 원문에 없는 단어라도
  의도를 더 잘 담으면 넣으세요(예: '과학적인 책 추천' → '과학 교양' — 문제집이 아닌 교양서 의도).
- **작가·작품·화풍·사조를 언급하면 아는 지식을 동원해 유사 작가·대표작의 '이름'을 검색어로
  삼으세요.** '~비슷한'·'~같은 작가'·'~풍' 같은 서술어는 그 말이 제목에 박힌 주변부 책만 걸리므로,
  대신 그 작가와 결이 닮은 **다른 작가들 이름**이나 그 사조의 **대표 작품·대표 작가**를 떠올려
  검색어로 내세요. **특히 '비슷한/같은 계열'을 물으면 참조된 작가 본인뿐 아니라 그와 결이 닮은
  다른 작가 최소 한 명의 이름을 반드시 검색어에 넣으세요**(예: 'A 작가 비슷한 추리소설' → 'A',
  그리고 A와 닮은 실제 작가 'B', 거기에 장르 대표어 하나). 장르·주제만 있고 특정 작가가 없어도,
  그 분야의 **대표 작가·대표작 한둘**을 각도로 섞으면 대중적 후보가 풍부해집니다
  (예: 'SF 소설' 각도에 더해 그 분야를 대표하는 실제 작가·작품명 한 각도).
  이 지식은 **검색어를 만드는 데만** 씁니다 —
  실제 책의 존재·제목·가격·평점 등 검증 가능한 사실은 오직 검색 결과로만 확인하고, 여기서 떠올린
  이름을 사실로 단정하지 않습니다.
- **주관적 평가·경험 수식어는 빼고 주제·장르·분야의 대표어로 옮기세요.** '재미있는'·'흥미로운'·
  '쉬운'·'감동적인' 같은 수식어를 그대로 넣으면 그 단어가 박힌 광고성·주변부 제목만 걸립니다
  (예: '재미있는 역사책' → '세계사'·'교양 세계사'·'역사 에세이'). 재미·감정의 뉘앙스는 뒤 생성
  단계가 페르소나로 살립니다.
- **인기·수준·시의를 뜻하는 말('베스트셀러'·'유명한'·'입문'·'초보'·'요즘'·'추천')을 이미 구체적인
  주제·장르 뒤에 덧붙이지 마세요.** 그런 말은 대중적 좋은 책 제목엔 없어 검색을 오염시킬 뿐이고,
  인기·난이도는 검색이 아니라 뒤 단계(판매지수 순위화·페르소나)가 반영합니다 —
  순수 주제·장르·작가어만 남기세요
  (예: 'SF 입문 소설' → 'SF 소설'·'과학소설'과 대표 작가명; '입문'을 뺌).
- **단 하나의 광의 단어(소설/과학/책)로 과축약하지 마세요.** 주제·분야가 있으면 2단어 이상으로.
- **풀을 넓히려면 서로 다른 각도로 최대 {max_queries}개**를 내세요. 넓은 풀일수록 16 유형이
  다른 책을 고를 여지가 커집니다:
  · 주제·장르가 뚜렷하면 **같은 갈래 안에서** 여러 각도(주제어·대표 작가·인접 하위장르)로 넓히세요
    (예: '재테크 책' → '재테크'·'돈 공부'·'경제 상식'). **요청한 갈래를 벗어나지 마세요**(비문학
    '역사책'에 '소설'을, '그림책'에 '입시'를 섞지 말 것 — 엉뚱한 우세 도서가 풀을 오염시킵니다).
  · 장르·주제가 없는 막연한 요청만 **서로 다른 장르로** 넓히세요(예: '무슨 책 읽을까' → '소설'·
    '에세이'·'인문').
- intent가 "none"이면 queries는 빈 배열로. "web"이면 검색엔진용 핵심어로.
- 각 검색어는 짧게(설명·따옴표·문장 없이 단어만 공백 구분)."""


@dataclass(frozen=True)
class RefineResult:
    """정제 결과 — 의도 분류 + 다각 검색어.

    intent ∈ {"product","web","none"}. queries는 서로 다른 의미 각도의 검색어(product/web),
    none이면 빈 리스트. 현재 build_shared_pool은 product 경로(yes24)만 쓰고 intent는 향후
    web/none 라우팅(Batch2)에서 분기한다.
    """

    intent: str
    queries: list[str]


# intent 허용값. 그 외/누락이면 product로 폴백(기존 도서 경로 무회귀).
_VALID_INTENTS = frozenset({"product", "web", "none"})

# JSON 구조화 출력 스키마.
_REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["product", "web", "none"]},
        "queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent", "queries"],
}


@dataclass(frozen=True)
class SharedPool:
    """16 생성이 공유하는 후보 풀 + 출처 레지스트리(불변 스냅샷).

    - question: 원 질문(캐시 키·프롬프트에 사용).
    - candidates: 후보 dict 목록. product면 상품 필드(title·author·price·pub_status…),
      web면 웹 결과(title·url·snippet·last_updated). none이면 빈 리스트.
    - sources: register_source로 누적된 공유 출처 레지스트리(인용 검증·done payload 재료).
    - checked_at: 검색 시각(KST). 가격·목록·신선도의 기준 시점 표기에 사용.
    - status: "ok"(생성 가능) | "empty"(정상 조회했으나 0건) | "error"(조회 실패).
    - kind: "product"(Yes24 도서 풀) | "web"(웹 사실 풀) | "none"(검색 불필요 잡담). 생성
      프롬프트·게이트가 이 kind로 분기한다(product만 풀-confine 게이트 적용).
    """

    question: str
    candidates: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    checked_at: str = ""
    status: str = "empty"
    kind: str = "product"


# 질문 정규화 키 → (등록 시각[monotonic], SharedPool). status="ok" 풀만 캐시한다
# (empty/error는 일시 실패일 수 있어 캐시하면 TTL 동안 재시도를 막으므로 캐시하지 않음).
_pool_cache: dict[str, tuple[float, SharedPool]] = {}


def _cache_key(question: str) -> str:
    """캐시 조회용 정규화 키 — 앞뒤 공백 제거, 연속 공백 축약, 소문자화."""
    return _WHITESPACE_RE.sub(" ", question.strip()).lower()


def clear_pool_cache() -> None:
    """공유 풀 캐시를 비운다(테스트·운영 리셋용)."""
    _pool_cache.clear()


def _sections_for_fanout(fanout: int) -> list[str]:
    """fanout 횟수만큼 섹션 변형을 고른다(최소 1, 변형 수 상한)."""
    count = max(1, min(fanout, len(_SECTION_VARIANTS)))
    return list(_SECTION_VARIANTS[:count])


# 시리즈/제목 접두 그룹 키에서 떼어낼 선행 잡음(브래킷 접두 등)과 토큰 경계.
_SERIES_KEY_STRIP = re.compile(r"[\[\](){}<>《》『』«»\"'`.,·/]+")


def _series_key(title: str) -> str:
    """제목의 시리즈/접두 그룹 키 — 첫 유의미 토큰의 정규화 형태.

    "수능특강 과학탐구 물리학Ⅰ"·"수능특강 생명과학"은 첫 토큰 '수능특강'으로 묶여 다양성
    가드가 캡을 건다. 서로 다른 책("과학을 보다"·"과학의 위로")은 첫 토큰(과학을·과학의)이
    달라 묶이지 않으므로 정상 후보를 과도하게 지우지 않는다(첫 토큰 완전일치만 그룹).
    """
    cleaned = _SERIES_KEY_STRIP.sub(" ", title or "")
    tokens = cleaned.split()
    return tokens[0].lower() if tokens else ""


def _diversify(items: list[dict], max_per_series: int) -> list[dict]:
    """같은 시리즈 접두(첫 토큰)당 max_per_series개까지만 남긴다(등장 순서 보존).

    광의 검색어가 문제집·시리즈로 풀을 도배하는 것을 구조로 막는다 — 어떤 검색어에서도 작동한다.
    max_per_series<=0이면 가드 미적용(원본 그대로).
    """
    if max_per_series <= 0:
        return items
    counts: dict[str, int] = {}
    kept: list[dict] = []
    for item in items:
        key = _series_key(item.get("title", ""))
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= max_per_series:
            kept.append(item)
    return kept


# 공유 풀 노이즈 필터 — Yes24 키워드 검색이 광의 한국어 질의에 섞어 내는, 어떤 페르소나에게도
# 좋은 추천이 될 수 없는 후보 유형을 매트릭스 풀에서 걷어낸다. 세 부류 모두 질의 무관 보편
# 노이즈이며(특정 주제/'역사' 케이스 패치가 아님), 실효 후보 수를 높여 16 페르소나가 갈라질
# 여지를 확보한다(계측: 분산은 실효 후보 수에 정비례).
#   ① 외국어 원서: 가나(일본어)가 있거나 한자가 한글 이상(중국어/한문).
#   ② 광고 스팸 제목: 판촉 브래킷([…증정]·[무료배송] 등)이 2개 이상(제목이 광고 나열).
#   ③ 다권 전집/세트: '전 N권'·'N권 세트'·'전집' — 매트릭스는 단권 비교라 세트는 부적합.
# 판촉어는 도서 도메인 상수(섹션 변형·시리즈 접두 잡음과 같은 층의 모듈 상수).
_AD_PROMO_TOKENS: tuple[str, ...] = (
    "증정", "사은품", "무료배송", "최신간", "정품", "할인", "이벤트",
)
_BRACKET_GROUP = re.compile(r"[\[\(【][^\]\)】]*[\]\)】]")
_BOXSET_RE = re.compile(r"전\s?\d+\s?권|\d+\s?권\s?세트|전집|\(\s?전\s?\d+")


def _is_foreign_title(title: str) -> bool:
    """제목이 외국어(일본어/중국어) 원서인지 — 가나가 있거나 한자가 한글 이상."""
    hangul = han = kana = 0
    for ch in title:
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        if "HANGUL" in name:
            hangul += 1
        elif "CJK UNIFIED" in name:
            han += 1
        elif "HIRAGANA" in name or "KATAKANA" in name:
            kana += 1
    return kana > 0 or (han > 0 and han >= hangul)


def _is_ad_spam_title(title: str) -> bool:
    """제목이 판촉 브래킷 나열(광고 스팸)인지 — 판촉어 담은 브래킷 그룹이 2개 이상."""
    promo = sum(
        1
        for group in _BRACKET_GROUP.findall(title)
        if any(tok in group for tok in _AD_PROMO_TOKENS)
    )
    return promo >= 2


def _is_noise(title: str) -> bool:
    """후보 제목이 매트릭스 풀 노이즈(외국어 원서·광고 스팸·다권 전집)인지."""
    if not title:
        return False
    return (
        _is_foreign_title(title)
        or _is_ad_spam_title(title)
        or bool(_BOXSET_RE.search(title))
    )


def _denoise(items: list[dict], enabled: bool) -> list[dict]:
    """풀 노이즈 후보를 걷어낸다(등장 순서 보존). enabled=False면 원본 그대로."""
    if not enabled:
        return items
    return [item for item in items if not _is_noise(item.get("title", ""))]


# 읽기 적합성 필터 — 독서(소설·에세이 등 읽을거리) 목적 질문에, 키워드 검색이 섞어 내는
# **참고·학습·수험·사전 부류**(그 장르 '읽을 책'이 아니라 그 주제를 '공부·연습하는 도구책')를
# 걷어낸다. 부류 규칙이며 특정 서명 나열이 아니다(작법·문제집·워크북·회화·사전 등 도구책 마커).
# 단 사용자가 실제로 그런 자료를 원하면(글쓰기 작법·어학 공부·시험 준비 질문) 걸러선 안 되므로,
# **질문에 그 학습·도구 의도 신호가 없을 때만** 적용한다(질문-인지형). _is_noise(질의 무관)와
# 달리 질문 맥락에 의존하므로 별도 함수로 둔다.
# '회화'는 어학(영어회화)과 미술(회화)이 모호해 미술 도서를 오탐할 수 있어 title 마커에서 뺀다
# (어학 교재는 문법·단어장·독학 마커로도 잡힌다). cue에는 남겨 미술·어학 질문이 필터를 끄게 한다.
_INSTRUCTIONAL_TITLE_TOKENS: tuple[str, ...] = (
    "작법", "문제집", "워크북", "단어장", "기출", "모의고사", "수험",
    "사전", "필기시험", "실기시험", "자격증",
)
# 질문에 이 신호가 있으면 사용자가 도구·학습 자료를 원하는 것 → 위 필터를 끈다(부류 신호).
# '입문'·'초보'는 초심 독자를 뜻할 뿐 교재 요구가 아니므로 신호에서 제외한다(오작동 방지).
_INSTRUCTIONAL_QUESTION_CUES: tuple[str, ...] = (
    "작법", "글쓰기", "공부", "학습", "시험", "자격", "문법", "회화", "사전",
    "문제", "독학", "교재", "쓰는 법", "쓰고 싶", "배우",
)


def _is_instructional_title(title: str) -> bool:
    """제목이 학습·수험·사전 부류(도구책) 마커를 담고 있는지."""
    return any(tok in title for tok in _INSTRUCTIONAL_TITLE_TOKENS)


def _question_wants_instructional(question: str) -> bool:
    """질문이 학습·도구 자료를 원하는 신호(작법·공부·시험 등)를 담고 있는지."""
    return any(cue in question for cue in _INSTRUCTIONAL_QUESTION_CUES)


def _filter_offtopic(items: list[dict], question: str, enabled: bool) -> list[dict]:
    """독서 목적 질문에 섞인 도구책(참고·학습·수험·사전)을 걷어낸다(등장 순서 보존).

    질문 자체가 그런 자료를 원하면(작법·공부·시험 신호) 필터를 끈다 — 글쓰기 작법서·어학 교재를
    원하는 질문에서 그 책을 지우지 않도록. enabled=False거나 질문이 학습 의도면 원본 그대로."""
    if not enabled or _question_wants_instructional(question):
        return items
    return [item for item in items if not _is_instructional_title(item.get("title", ""))]


# 에디션 변형 dedup — 같은 책의 판형/장정 변형이 별개 URL로 풀에 중복 유입되는 것을 접는다.
# 아래는 제목 괄호/브래킷 안에서 **판형·장정·개정 부류**를 가리키는 부분 토큰이다(특정 제목
# 나열이 아니라 부류 규칙 — _AD_PROMO_TOKENS·시리즈 접두 잡음과 같은 층의 도서 도메인 상수).
# 부분 일치라 "큰글자도서"·"큰글씨책"이 '큰글자'·'큰글씨'로, "개정증보판"이 '개정'으로 잡힌다.
_EDITION_MODIFIER_TOKENS: tuple[str, ...] = (
    "큰글자", "큰글씨", "리커버", "특별판", "한정판", "양장", "반양장", "무선",
    "개정", "증보", "합본", "보급판", "문고판", "에디션", "완전판", "스페셜",
)


def _edition_key(title: str) -> str:
    """제목의 에디션 정규화 키 — 판형/장정 수식어 괄호를 걷어낸 정규화 형태.

    제목의 괄호/브래킷 그룹 중 판형·장정 부류 토큰(_EDITION_MODIFIER_TOKENS)을 담은 것만
    제거한다("채식주의자(개정판)"·"오늘부터 채식주의 (큰글자도서)" → 부가어 그룹 제거).
    판형과 무관한 서술 괄호("… (영어 원서)"·"… 휴고상")는 남겨, 서로 다른 책이 잘못 합쳐지지
    않게 한다(제거는 판형 토큰이 들어간 그룹에 한함). 남은 텍스트를 소문자·공백정규화한다."""
    stripped = _BRACKET_GROUP.sub(
        lambda m: "" if any(tok in m.group(0) for tok in _EDITION_MODIFIER_TOKENS) else m.group(0),
        title or "",
    )
    return _WHITESPACE_RE.sub(" ", stripped).strip().lower()


def _has_edition_modifier(title: str) -> bool:
    """제목에 판형/장정 수식어 괄호가 붙어 있는지(기본판 판정용 — 없는 쪽이 기본판)."""
    return any(
        any(tok in group for tok in _EDITION_MODIFIER_TOKENS)
        for group in _BRACKET_GROUP.findall(title or "")
    )


def _dedup_editions(items: list[dict], enabled: bool) -> list[dict]:
    """같은 책의 판형/장정 변형을 1종으로 접는다(대표는 기본판 우선, 동급이면 판매지수).

    같은 _edition_key끼리 묶고 각 그룹에서 대표 하나만 남긴다 — ① 부가어 없는 기본판을
    우선하고(카드가 "(큰글자도서) 28,000원"처럼 부가판으로 뜨는 것 방지), ② 동급이면
    판매지수(대중성)가 큰 쪽. 대표는 그룹 첫 등장 위치에 놓아 이후 순위화 전 등장 순서를
    보존한다. enabled=False면 원본 그대로. 대표는 **선택**일 뿐 제목을 재작성하지 않는다
    (기본판이 풀에 없으면 있던 변형을 그대로 유지 — 없는 기본판을 지어내지 않음)."""
    if not enabled:
        return items

    def _better(candidate: dict, current: dict) -> bool:
        """candidate가 current보다 대표로 더 적합한지 — 기본판 우선, 그다음 판매지수."""
        cand_base = not _has_edition_modifier(candidate.get("title", ""))
        cur_base = not _has_edition_modifier(current.get("title", ""))
        if cand_base != cur_base:
            return cand_base
        return (candidate.get("sale_index") or -1) > (current.get("sale_index") or -1)

    order: list[str] = []  # 그룹 첫 등장 순서 보존
    rep: dict[str, dict] = {}
    for item in items:
        key = _edition_key(item.get("title", ""))
        if key not in rep:
            rep[key] = item
            order.append(key)
        elif _better(item, rep[key]):
            rep[key] = item
    return [rep[key] for key in order]


def _rank_by_popularity(items: list[dict], enabled: bool) -> list[dict]:
    """판매지수 내림차순 안정정렬(값 None은 최하위). enabled=False면 원본 그대로.

    다각 검색 union의 쿼리-순서 편향을 걷어, 절대 판매지수가 큰 대중적·매력 후보를 앞세운다.
    안정정렬이라 동점(같은 판매지수·None 다수)은 등장 순서를 보존한다."""
    if not enabled:
        return items
    return sorted(items, key=lambda item: item.get("sale_index") or -1, reverse=True)


def _valid_query(q: object, settings: Settings) -> bool:
    """검색어 하나가 유효한지 — 문자열·비어있지 않음·글자수/토큰수 상한 이내.

    상한 초과는 검색어가 아니라 문장·설명을 낸 신호로 보고 폐기한다(상한은 config 필드).
    """
    if not isinstance(q, str):
        return False
    q = q.strip()
    if not q:
        return False
    if len(q) > settings.matrix_refine_max_chars:
        return False
    return len(q.split()) <= settings.matrix_refine_max_words


async def _refine_query(
    question: str, settings: Settings, genai_client: genai.Client | None = None
) -> RefineResult | None:
    """질문을 flash 1회로 {intent, queries}로 번역한다(JSON 구조화 출력).

    매트릭스당 1회만 호출한다(16 fan-out과 무관 — 공유검색 전단계). 실패·파싱 불가면 None을
    반환해 호출부가 원 질문으로 폴백한다. intent 누락·비허용값은 product로, queries는 유효
    검색어만 max_queries개까지 남긴다. genai 클라이언트는 여기서 지연 해소한다(테스트가 이
    함수만 스텁하면 클라이언트 생성 없음).
    """
    client = genai_client or get_genai_client()
    system = _REFINE_SYSTEM.format(max_queries=settings.matrix_retrieval_max_queries)
    try:
        config = types.GenerateContentConfig(
            system_instruction=system,
            thinking_config=types.ThinkingConfig(
                thinking_budget=settings.matrix_generation_thinking_budget
            ),
            response_mime_type="application/json",
            response_schema=_REFINE_SCHEMA,
        )
        response = await client.aio.models.generate_content(
            model=settings.matrix_generation_model,
            contents=question,
            config=config,
        )
    except APIError as exc:
        logger.info("matrix 쿼리 정제 실패(원 질문으로 폴백): %s", exc)
        return None

    raw = (response.text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.info("matrix 정제 JSON 파싱 실패(원 질문으로 폴백): %s", exc)
        return None
    if not isinstance(data, dict):
        return None

    intent = data.get("intent")
    if intent not in _VALID_INTENTS:
        intent = "product"
    raw_queries = data.get("queries")
    if not isinstance(raw_queries, list):
        raw_queries = []
    queries: list[str] = []
    for q in raw_queries:
        if _valid_query(q, settings) and q.strip() not in queries:
            queries.append(q.strip())
        if len(queries) >= settings.matrix_retrieval_max_queries:
            break
    return RefineResult(intent=intent, queries=queries)


async def _build_product_pool(
    question: str,
    search_queries: list[str],
    settings: Settings,
    checked_at: str,
    effective_fanout: int,
) -> SharedPool:
    """Yes24 도서 풀(kind=product) — 다각 검색×섹션 union→dedup→다양성가드→목표크기 절단."""
    raw_items: list[dict] = []
    seen_urls: set[str] = set()
    saw_error = False
    client = _get_client(settings)

    for query in search_queries:
        for section in _sections_for_fanout(effective_fanout):
            url = search_url(settings.yes24_base_url, query, section)
            try:
                html = await client.get_text(url)
            except Yes24FetchError as exc:
                logger.info("matrix fetch 실패 q=%r sec=%s: %s", query, section, exc)
                saw_error = True
                continue
            try:
                parsed = parse_search(
                    html, base_url=settings.yes24_base_url, limit=settings.search_result_limit
                )
            except ParseError as exc:
                logger.info("matrix parse 실패 q=%r sec=%s: %s", query, section, exc)
                saw_error = True
                continue

            for item in parsed:
                item_url = item.get("url")
                if not item_url or item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                raw_items.append(item)

    # 정제 파이프라인: 노이즈 → 오프토픽 → 에디션 dedup → 판매지수 재순위화 → 다양성 가드 → 절단.
    # ① 노이즈(외국어 원서·광고스팸·전집)를 **먼저** 걷어 뒤 단계가 잡음에 낭비되지 않게 한다.
    # ② 독서 목적 질문에 섞인 도구책(참고·학습·수험·사전)을 걷는다(질문이 그런 자료를 원하면 통과).
    # ③ 같은 책의 판형 변형(개정판·큰글자도서 등)을 1종으로 접어 중복 출처·부가판 대표를 없앤다.
    # ④ 다각 검색 union의 쿼리-순서 편향을 판매지수로 재정렬해 대중적·매력 후보를 앞세운다
    #    (기본 검색은 쿼리별 인기순이나 union이 그 순서를 섞음). ⑤ 시리즈 도배를 캡하고 ⑥ 목표
    #    크기로 절단 — 순위화가 앞서므로 절단이 매력 있는 후보를 남긴다. 등록(register_source)은
    #    최종 순서로 맨 마지막(source_id는 이 순서로 부여).
    denoised = _denoise(raw_items, settings.matrix_pool_filter_noise)
    noise_dropped = len(raw_items) - len(denoised)
    if noise_dropped > 0:
        logger.info("matrix 풀 노이즈 제거: %d건", noise_dropped)
    on_topic = _filter_offtopic(denoised, question, settings.matrix_pool_filter_offtopic)
    offtopic_dropped = len(denoised) - len(on_topic)
    if offtopic_dropped > 0:
        logger.info("matrix 풀 오프토픽(도구책) 제거: %d건", offtopic_dropped)
    deduped = _dedup_editions(on_topic, settings.matrix_pool_dedup_editions)
    edition_dropped = len(denoised) - len(deduped)
    if edition_dropped > 0:
        logger.info("matrix 풀 에디션 dedup: %d건", edition_dropped)
    ranked = _rank_by_popularity(deduped, settings.matrix_pool_rank_by_popularity)
    diversified = _diversify(ranked, settings.matrix_pool_max_per_series)[
        : settings.matrix_pool_target_size
    ]
    dropped = len(deduped) - len(diversified)
    if dropped > 0:
        logger.info("matrix 풀 정제: %d건 제거, 최종 %d", dropped, len(diversified))

    state: dict = {}  # register_source 누적용 plain dict(MutableMapping)
    candidates: list[dict] = []
    for item in diversified:
        source_id = register_source(
            state,
            title=item["title"],
            url=item["url"],
            source_type="search_result",
            snippet=item.get("author"),
            # image_url은 W2 표지 UI가 col done.sources[].meta에서 읽는다(채팅 경로와 동일 가법).
            meta={
                "price": item.get("price"),
                "goods_no": item.get("goods_no"),
                "image_url": item.get("image_url"),
            },
        )
        candidate = {
            "source_id": source_id,
            "title": item["title"],
            "url": item["url"],
            "author": item.get("author"),
            "publisher": item.get("publisher"),
            "pub_date": item.get("pub_date"),
            "price": item.get("price"),
            "rating": item.get("rating"),
            # 판매지수는 순위화·검증에 쓰이는 내부 신호다 — 프롬프트 사실 목록엔 렌더하지 않는다
            # (책값·평점 같은 인용 대상 사실이 아니라 대중성 메타).
            # 후보 dict엔 남겨 계측·테스트에 쓴다.
            "sale_index": item.get("sale_index"),
        }
        pstatus = pub_status(item.get("pub_date"))
        if pstatus is not None:
            candidate["pub_status"] = pstatus
        candidates.append(candidate)

    status = "ok" if candidates else ("error" if saw_error else "empty")
    return SharedPool(
        question=question,
        candidates=candidates,
        sources=get_sources(state),
        checked_at=checked_at,
        status=status,
        kind="product",
    )


async def _build_web_pool(
    question: str, search_queries: list[str], settings: Settings, checked_at: str
) -> SharedPool:
    """웹 사실 풀(kind=web) — 퍼플렉시티 /search 원시결과를 url-유일 union. type="web" 출처.

    web_search 도구의 공유 httpx 클라이언트·snippet 절단을 재사용한다(도구는 안 건드림).
    상품 정보(가격·구매)는 여기서 오지 않는다 — 웹 출처는 상품 접지가 아니라 사실 근거다.
    """
    if not settings.perplexity_api_key:
        logger.info("matrix web 풀: 퍼플렉시티 미설정 → error")
        return SharedPool(question, [], [], checked_at, status="error", kind="web")

    client = _get_web_client(settings)
    headers = {"Authorization": f"Bearer {settings.perplexity_api_key}"}
    raw_items: list[dict] = []
    seen_urls: set[str] = set()
    saw_error = False

    for query in search_queries:
        payload = {
            "query": query,
            "max_results": settings.web_search_max_results,
            "max_tokens_per_page": settings.web_search_max_tokens_per_page,
            "max_tokens": settings.web_search_max_tokens,
        }
        try:
            response = await client.post(
                settings.perplexity_search_url, json=payload, headers=headers
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError(f"응답이 JSON 객체가 아닙니다: {type(data).__name__}")
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("matrix web 검색 실패 q=%r: %s", query, exc)
            saw_error = True
            continue
        for item in data.get("results") or []:
            item_url = item.get("url")
            if not item_url or item_url in seen_urls:
                continue
            seen_urls.add(item_url)
            raw_items.append(item)

    raw_items = raw_items[: settings.matrix_pool_target_size]
    state: dict = {}
    candidates: list[dict] = []
    for item in raw_items:
        title = item.get("title") or item.get("url")
        snippet = _truncate_snippet(item.get("snippet"), settings.web_search_snippet_max_chars)
        last_updated = item.get("last_updated") or item.get("date")
        source_id = register_source(
            state,
            title=title,
            url=item["url"],
            source_type="web",
            snippet=snippet,
            meta={"last_updated": last_updated},
        )
        candidates.append(
            {
                "source_id": source_id,
                "type": "web",
                "title": title,
                "url": item["url"],
                "snippet": snippet,
                "last_updated": last_updated,
            }
        )

    status = "ok" if candidates else ("error" if saw_error else "empty")
    return SharedPool(
        question=question,
        candidates=candidates,
        sources=get_sources(state),
        checked_at=checked_at,
        status=status,
        kind="web",
    )


async def build_shared_pool(
    question: str,
    settings: Settings,
    *,
    fanout: int | None = None,
    genai_client: genai.Client | None = None,
) -> SharedPool:
    """질문에 대한 공유 풀을 조립한다(캐시 우선, retrieve-once + 의도 라우팅).

    정제(matrix_query_refine on)가 질문을 {intent, queries}로 번역한다:
    - product: Yes24 도서 풀(다각 검색×섹션 union·다양성가드·목표크기). 기존 도서 경로.
    - web: 퍼플렉시티 웹 사실 풀(type=web 출처). 16 페르소나가 같은 사실을 관점·말투로 해석.
    - none: 빈 풀(kind=none) — 16 페르소나가 각자 화법으로 즉답(무인용, 무출처 상품 사실은 금지).
    풀은 정제 검색어로 채우되 SharedPool.question은 **원 질문**을 유지한다. status="ok" 풀만
    TTL 캐시에 저장한다. 정제 off/실패면 product 경로로 원 질문을 검색한다(기존 도서 경로 무회귀).
    """
    effective_fanout = fanout if fanout is not None else settings.matrix_retrieval_fanout
    key = _cache_key(question)
    now = time.monotonic()

    cached = _pool_cache.get(key)
    if cached is not None and (now - cached[0]) < settings.matrix_cache_ttl_s:
        logger.info("matrix pool cache hit question=%r", question)
        return cached[1]

    # 정제: intent + 다각 검색어. off/실패면 product·원 질문(무회귀).
    intent = "product"
    search_queries = [question]
    if settings.matrix_query_refine:
        refined = await _refine_query(question, settings, genai_client)
        if refined:
            intent = refined.intent
            if refined.queries:
                search_queries = refined.queries
            logger.info(
                "matrix 정제 question=%r intent=%s queries=%r", question, intent, search_queries
            )

    checked_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M")

    if intent == "none":
        pool = SharedPool(question, [], [], checked_at, status="ok", kind="none")
    elif intent == "web":
        pool = await _build_web_pool(question, search_queries, settings, checked_at)
    else:
        pool = await _build_product_pool(
            question, search_queries, settings, checked_at, effective_fanout
        )

    logger.info(
        "matrix pool built question=%r kind=%s status=%s candidates=%d",
        question,
        pool.kind,
        pool.status,
        len(pool.candidates),
    )
    if pool.status == "ok":
        _pool_cache[key] = (now, pool)
    return pool
