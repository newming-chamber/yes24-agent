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

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError

from yes24_agent.config import Settings, get_genai_client
from yes24_agent.sources import get_sources, register_source
from yes24_agent.tools.web_search import _get_client as _get_web_client
from yes24_agent.tools.web_search import _truncate_snippet
from yes24_agent.tools.yes24_search import _get_client
from yes24_agent.yes24.client import Yes24FetchError
from yes24_agent.yes24.parsers import ParseError, parse_search, product_fields
from yes24_agent.yes24.urls import search_url

logger = logging.getLogger(__name__)

# KST(UTC+9). checked_at 타임스탬프용(도구 checked_at과 동일 규약).
_KST = timezone(timedelta(hours=9))

# 캐시 키 정규화용 연속 공백 축약 패턴.
_WHITESPACE_RE = re.compile(r"\s+")

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
  검색어로 내세요. **특히 '비슷한/같은 계열'을 물으면 참조된 작가 말고 그와 결이 닮은 다른 작가
  최소 한 명의 이름을 검색어에 넣으세요**(예: 'A 작가 비슷한 추리소설' → A와 닮은 실제 작가 'B',
  'C', 거기에 장르 대표어 하나). 장르·주제만 있고 특정 작가가 없어도 그 분야의 **대표 작가·대표작
  한둘**을 각도로 섞으면 대중적 후보가 풍부해집니다(예: 'SF 소설' + 그 분야 대표 실제 작가·작품명).
- **작가 이름은 그 자체로(또는 '작가명 소설') 단독 검색어로 내고 뒤에 장르어를 붙이지 마세요.**
  '편혜영 스릴러'처럼 작가명+장르를 붙이면 제목에 그 장르어가 박힌 책만 걸려 정작 그 작가의 소설이
  안 잡힙니다 — '편혜영' 또는 '편혜영 소설'로 내야 그 작가의 실제 작품이 걸립니다. 장르 대표어는
  작가명과 **별개 각도**로 따로 내세요.
- **사용자가 어떤 작가·시리즈를 "이미 다 읽었다/봤다"거나 "빼고·말고·제외"라고 하면, 그 이름을
  검색어로 쓰지 말고 `exclude` 배열에 넣으세요**(예: '정유정 다 읽었어, 비슷한 스릴러' → exclude:
  ["정유정"], queries는 정유정과 닮은 다른 작가들·장르 대표어). 배제 대상을 검색하면 이미 읽은
  책이 그대로 추천되어 요청을 정면으로 어깁니다.
- 이 지식은 **검색어를 만드는 데만** 씁니다 — 실제 책의 존재·제목·가격·평점 등 검증 가능한
  사실은 오직 검색 결과로만 확인하고, 여기서 떠올린 이름을 사실로 단정하지 않습니다.
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
  · 주제가 좁은 니치 분야면 같은 단어를 어순만 바꿔 반복하지 말고, 갈래는 지키되 **인접 각도**로
    벌리세요 — 대상(누구를 위한)·방법·형식(어떻게 다루는)·그 분야 대표 저자·고전을 서로 다른
    검색어로. 좁은 주제일수록 각도가 달라야 서로 다른 후보가 걸립니다.
  · 장르·주제가 없는 막연한 요청만 **서로 다른 장르로** 넓히세요(예: '무슨 책 읽을까' → '소설'·
    '에세이'·'인문').
- intent가 "none"이면 queries는 빈 배열로. "web"이면 검색엔진용 핵심어로.
- 각 검색어는 짧게(설명·따옴표·문장 없이 단어만 공백 구분)."""


@dataclass(frozen=True)
class RefineResult:
    """정제 결과 — 의도 분류 + 다각 검색어 + 배제 엔티티.

    intent ∈ {"product","web","none"}. queries는 서로 다른 의미 각도의 검색어(product/web),
    none이면 빈 리스트. exclude는 사용자가 "이미 읽음/빼고/제외"로 명시한 저자·시리즈명(예:
    "정유정 다 읽었어" → ["정유정"])으로, 풀 선별에서 그 저자 후보를 걷어낸다.
    """

    intent: str
    queries: list[str]
    exclude: list[str] = field(default_factory=list)


# intent 허용값. 그 외/누락이면 product로 폴백(기존 도서 경로 무회귀).
_VALID_INTENTS = frozenset({"product", "web", "none"})

# JSON 구조화 출력 스키마.
_REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["product", "web", "none"]},
        "queries": {"type": "array", "items": {"type": "string"}},
        "exclude": {"type": "array", "items": {"type": "string"}},
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


def _cache_get(key: str, settings: Settings, now: float) -> SharedPool | None:
    """만료되지 않은 캐시 풀을 반환한다(만료 엔트리는 조회 김에 청소)."""
    ttl = settings.matrix_cache_ttl_s
    for stale in [k for k, (at, _) in _pool_cache.items() if now - at >= ttl]:
        del _pool_cache[stale]
    entry = _pool_cache.get(key)
    return entry[1] if entry else None


def _cache_put(key: str, pool: SharedPool, settings: Settings, now: float) -> None:
    """풀을 캐시에 넣는다. 상한을 넘으면 가장 오래된 엔트리부터 밀어낸다(무한 성장 방지)."""
    _pool_cache[key] = (now, pool)
    while len(_pool_cache) > settings.matrix_cache_max_entries:
        oldest = min(_pool_cache, key=lambda k: _pool_cache[k][0])
        del _pool_cache[oldest]


# 시리즈 그룹 키에서 떼어낼 선행 잡음(브래킷 접두 등)과 토큰 경계.
_SERIES_KEY_STRIP = re.compile(r"[\[\](){}<>《》『』«»\"'`.,·/]+")


def _series_key(item: dict) -> tuple[str, str]:
    """후보의 시리즈 그룹 키 — (출판사, 제목 첫 유의미 토큰).

    시리즈는 "같은 출판사가 같은 이름으로 내는 묶음"이다("수능특강 과학탐구"·"수능특강 생명
    과학"). 첫 토큰만 보면 출판사가 다른 별개 도서("과학 콘서트"·"과학 혁명의 구조")가 같은
    시리즈로 오인돼 다양성 캡에 잘리므로, 출판사를 키에 포함해 그 오병합을 없앤다.
    """
    cleaned = _SERIES_KEY_STRIP.sub(" ", item.get("title") or "")
    tokens = cleaned.split()
    first = tokens[0].lower() if tokens else ""
    return (item.get("publisher") or "").strip().lower(), first


def _diversify(items: list[dict], max_per_series: int) -> list[dict]:
    """같은 시리즈(출판사+제목 첫 토큰)당 max_per_series개까지만 남긴다(등장 순서 보존).

    광의 검색어가 문제집·시리즈로 풀을 도배하는 것을 구조로 막는다 — 어떤 검색어에서도 작동한다.
    max_per_series<=0이면 가드 미적용(원본 그대로).
    """
    if max_per_series <= 0:
        return items
    counts: dict[tuple[str, str], int] = {}
    kept: list[dict] = []
    for item in items:
        key = _series_key(item)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= max_per_series:
            kept.append(item)
    return kept


# 풀 강등(soft penalty) — **삭제가 아니라 순위 강등**이다.
#
# 도서 섹션(domain=BOOK)으로 상류를 제약해도 검색은 여전히 단권 추천에 부적합한 출품을 섞어
# 낸다(실측: '재미있는 역사책' BOOK 응답 상위가 '전 68권' 전집·판촉 브래킷 제목). 이들을
# **지우지 않고 뒤로 미는** 이유: 오탐의 대가가 "책 1권 영구 소멸"에서 "순위 몇 칸 하락"으로
# 줄고, 신호가 소실되지 않아 풀이 얇을 땐 16셀이 강등된 후보라도 쓸 수 있다(한 번 걸러진 후보를
# 누구도 복구할 수 없던 구조를 없앤다).
#
# 감점은 **제목의 구조 신호**만 본다(판촉 어휘 목록 없이):
#   ① 브래킷 그룹 과잉 — 정상 도서 제목의 괄호는 많아야 하나(판형·부제)다. 둘 이상은 제목이
#      아니라 광고 나열이라는 구조 신호이며, 초과분 개수만큼 비례 감점한다(임계·매직넘버 없음).
#   ② 다권 세트/전집 — 매트릭스는 단권 비교라 묶음 상품은 카드 재료로 부적합('전 N권'·'N권
#      세트'·'전집'. 수량 단위+묶음어는 도서 유통의 묶음 상품 어휘 부류다).
_BRACKET_GROUP = re.compile(r"[\[\(【][^\]\)】]*[\]\)】]")
_BOXSET_RE = re.compile(r"전\s?\d+\s?권|\d+\s?[권종]\s?(?:세트|패키지)|전집|세트\s*$")


def _demerits(title: str) -> int:
    """제목의 구조적 감점 수 — 브래킷 그룹 초과분 + 다권 세트 신호(0이면 감점 없음)."""
    extra_brackets = max(0, len(_BRACKET_GROUP.findall(title or "")) - 1)
    return extra_brackets + (1 if _BOXSET_RE.search(title or "") else 0)


def _rank(items: list[dict], settings: Settings) -> list[dict]:
    """풀을 카드 재료로서의 적합도 순으로 정렬한다(안정정렬 — 동급은 등장 순서 보존).

    키는 세 단계다: ① 구조 감점이 적은 후보 먼저(_demerits × 강등 계수) — 판촉 나열·전집을
    삭제하는 대신 뒤로 민다. ② 판매지수가 **있는** 후보 먼저 — 결측(실측 37%)을 최하위 값으로
    강등하면 Yes24 검색의 원 인기순 정보가 통째로 소실돼 절단에 잘려나간다. ③ 판매지수 큰 순
    (대중성). 결측끼리는 안정정렬이 원 등장 순위(=Yes24 인기순)를 그대로 보존한다.
    """
    weight = settings.matrix_pool_noise_penalty

    def key(item: dict) -> tuple[int, bool, int]:
        sale = item.get("sale_index")
        return (-weight * _demerits(item.get("title", "")), sale is not None, sale or 0)

    return sorted(items, key=key, reverse=True)


def _matches_excluded(item: dict, exclude: list[str]) -> bool:
    """후보의 저자 또는 제목이 배제 엔티티(이미 읽은 저자·시리즈) 중 하나와 겹치는지.

    사용자가 "정유정 다 읽었어"라 하면 exclude=["정유정"]로 정유정 저자/제목 후보를 걷는다.
    부분 문자열 대조(저자 표기 '정유정 저'·병기 포함, 제목에 시리즈명 박힌 경우 모두 포괄)."""
    hay = f"{item.get('author') or ''} {item.get('title') or ''}"
    return any(name in hay for name in exclude)


def _filter_excluded(items: list[dict], exclude: list[str], settings: Settings) -> list[dict]:
    """배제 엔티티(이미 읽음/제외 저자·시리즈)에 걸리는 후보를 걷어낸다(등장 순서 보존).

    exclude는 모델이 낸 자유 문자열이라 무검증 부분일치는 위험하다 — 너무 짧은 배제어는 아무
    제목에나 걸리고, 장르어(예: "소설")가 오면 풀이 통째로 증발한다. 두 겹으로 막는다:
    최소 길이 미만은 무시하고, 적용 결과가 후보의 과반(설정 비율 초과)을 지우면 **적용을
    취소**한다 — 대다수를 지우는 배제는 배제어가 틀린 것이다(우세-부류 판정과 같은 발상).
    """
    names = [
        name.strip()
        for name in exclude
        if len(name.strip()) >= settings.matrix_exclude_min_chars
    ]
    if not names or not items:
        return items
    kept = [item for item in items if not _matches_excluded(item, names)]
    if len(items) - len(kept) > len(items) * settings.matrix_exclude_max_drop_ratio:
        logger.info("matrix 배제엔티티 적용 취소(후보 과반 소멸): exclude=%r", names)
        return items
    return kept


# 에디션 변형 dedup — 같은 책의 판형/장정 변형("채식주의자"·"채식주의자(개정판)"·"채식주의자
# (큰글자도서)")이 별개 URL이라 url-dedup을 통과해 풀에 중복 유입되는 것을 접는다.
#
# 판형 수식어 목록은 두지 않는다 — **코어 제목**(부제·괄호·시리즈 라벨 앞까지)이 이미 그 부가
# 텍스트를 잘라내므로, "같은 저자 + 같은 코어 제목"이면 같은 책이다. 목록 없이 부류 전체를 덮는다.
#
# 코어 제목 경계 — 이 구분자 앞까지가 '책 본제목'이고 뒤는 부제·시리즈 라벨·원제 병기·부록
# 안내 등 부가 텍스트다.
_CORE_TITLE_BOUNDARY = re.compile(r"\s[-–—]\s|\s?[\(\[【]|★|\s외\s|[:：/·]")
# 제목 맨 앞의 브래킷 접두("[중고] …"·"(개정판) …"). 코어 추출 전에 떼지 않으면 경계가 첫
# 글자에 걸려 코어가 빈 문자열이 되고, 저자-코어 병합 경로가 통째로 무력해진다.
_LEADING_BRACKETS = re.compile(r"^\s*(?:[\[\(【][^\]\)】]*[\]\)】]\s*)+")
# 저자 표기에서 떼어낼 역할어·병기(원제 한자/영문 괄호). 같은 저자의 다른 표기("김춘광 저"·
# "김춘광 (金春光) 저")를 한 키로 모으기 위한 정규화.
_AUTHOR_ROLE_STRIP = re.compile(r"(저|글|지음|엮음|옮김|그림|편|역|등저|외)\b")
# 판형이 아니라 **다른 판본/상품**을 뜻하는 구별 마커. 이게 있으면 저자-코어 대조 병합을 막아
# 원서↔번역·낱권↔합본·세트 같은 진짜 다른 상품이 잘못 합쳐지지 않게 한다(오병합 방지).
_DISTINGUISHING_MARKER = re.compile(
    r"원서|원문|영문판|영어판|일문판|중문판|세트|전집|합본|상권|하권|\d+\s*권"
)


def _norm_author(author: str | None) -> str:
    """저자 표기 정규화 — 첫 저자만, 한자/영문 병기·역할어 제거, 소문자·공백정리.

    "김춘광 저"·"김춘광 (金春光) 저"를 같은 "김춘광"으로 모으되, 동명이서(異書)를 가르는 데
    쓰이므로 다저자는 첫 저자로 대표한다(편집·선집 구분은 별 문제 — 서로 다른 책이면 코어제목
    또는 저자가 어차피 다르다)."""
    if not author:
        return ""
    first = re.split(r"[/,]", author, maxsplit=1)[0]
    first = _BRACKET_GROUP.sub(" ", first)  # (金春光) 등 병기 제거
    first = _AUTHOR_ROLE_STRIP.sub(" ", first)
    return _WHITESPACE_RE.sub(" ", first).strip().lower()


def _core_title(title: str) -> str:
    """책 본제목(부제·시리즈·원제 병기·부록 안내 앞까지)을 정규화해 반환한다.

    선행 브래킷 접두를 먼저 떼어(그러지 않으면 코어가 비어버린다) 경계까지를 코어로 삼고,
    공백·대소문자를 정규화한다. 비교는 공백 무시(같은 책의 붙여쓰기 변형을 흡수)."""
    text = _LEADING_BRACKETS.sub("", title or "")
    boundary = _CORE_TITLE_BOUNDARY.search(text)
    core = text[: boundary.start()] if boundary else text
    return _WHITESPACE_RE.sub("", core).strip().lower()


def _dedup_key(item: dict) -> tuple[str, str]:
    """후보의 동일도서 그룹 키 — (정규화 저자, 코어 제목).

    저자와 코어 제목이 둘 다 잡히고 구별 마커(원서·세트·낱권 등 **다른 상품**을 뜻하는 표기)가
    없을 때만 병합 대상이다. 그 밖(저자 없음·코어 없음·구별 마커)은 **제목 전문**을 키로 삼는다 —
    완전히 같은 제목이 아니면 병합하지 않는다는 뜻이며, 별개 도서를 잘못 접는 위험을 0으로 둔다.
    """
    title = item.get("title", "")
    author = _norm_author(item.get("author"))
    core = _core_title(title)
    if author and len(core) >= 2 and not _DISTINGUISHING_MARKER.search(title):
        return author, core
    return "", _WHITESPACE_RE.sub("", title).lower()


def _dedup_editions(items: list[dict]) -> list[dict]:
    """같은 책의 변형(판형·부제·시리즈 라벨·원제 병기)을 1종으로 접는다(첫 등장 위치 보존).

    그룹 대표는 판매지수(대중성)가 큰 쪽, 동급이면 부가 텍스트가 적은(짧은) 제목 — 카드가
    "(큰글자도서) 28,000원"·"… 외 단편 17작품 ★ 부록…" 같은 부가판 제목으로 뜨는 것을 막는다.
    대표는 **선택**일 뿐 제목을 재작성하지 않는다(없는 제목을 지어내지 않음).
    """

    def _better(candidate: dict, current: dict) -> bool:
        cand_sale = candidate.get("sale_index") or -1
        cur_sale = current.get("sale_index") or -1
        if cand_sale != cur_sale:
            return cand_sale > cur_sale
        return len(candidate.get("title", "")) < len(current.get("title", ""))

    reps: dict[tuple[str, str], dict] = {}
    for item in items:
        key = _dedup_key(item)
        current = reps.get(key)
        if current is None or _better(item, current):
            reps[key] = item
    return list(reps.values())


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
    raw_exclude = data.get("exclude")
    exclude: list[str] = []
    if isinstance(raw_exclude, list):
        for e in raw_exclude:
            if isinstance(e, str) and e.strip() and e.strip() not in exclude:
                exclude.append(e.strip())
    return RefineResult(intent=intent, queries=queries, exclude=exclude)


async def _search_once(query: str, settings: Settings) -> list[dict] | None:
    """검색어 하나로 도서 섹션을 조회·파싱한다. 조회/파싱 실패는 None(빈 성공으로 위장 금지).

    공유 클라이언트 싱글턴을 쓰므로 여러 검색을 동시에 발사해도 요청 예의는 클라이언트가
    지킨다(프로세스 전역 http_rps 최소간격 + 동시성 Semaphore).
    """
    url = search_url(settings.yes24_base_url, query, settings.matrix_search_section)
    try:
        html = await _get_client(settings).get_text(url)
    except Yes24FetchError as exc:
        logger.info("matrix fetch 실패 q=%r: %s", query, exc)
        return None
    try:
        return parse_search(
            html, base_url=settings.yes24_base_url, limit=settings.matrix_pool_parse_limit
        )
    except ParseError as exc:
        logger.info("matrix parse 실패 q=%r: %s", query, exc)
        return None


async def _build_product_pool(
    question: str,
    search_queries: list[str],
    settings: Settings,
    checked_at: str,
    exclude: list[str] | None = None,
) -> SharedPool:
    """Yes24 도서 풀(kind=product) — 도서 섹션 다각 검색 union→dedup→순위화→다양성→절단.

    다각 검색은 **동시에 발사한다**(asyncio.gather). 검색어끼리 의존이 없는데 순차로 기다리면
    풀 빌드 지연이 검색어 수에 선형으로 늘어난다. Yes24 검색은 한 페이지 24건이 상한이라 풀을
    더 키우는 유일한 수단이 질의 수를 늘리는 것이므로, 여기서 동시화해 두면 질의 증가 비용이
    사실상 사라진다. 요청 예의(RPS 최소간격·동시성 상한)는 공유 클라이언트가 관리한다.
    gather는 입력 순서대로 결과를 돌려주므로 후보의 등장 순서(=검색어 순서)는 결정론이다 —
    순위화의 안정정렬이 이 순서를 tiebreak로 쓰므로 풀 구성이 실행마다 흔들리지 않는다.
    """
    results = await asyncio.gather(*(_search_once(q, settings) for q in search_queries))
    saw_error = any(parsed is None for parsed in results)

    raw_items: list[dict] = []
    seen_urls: set[str] = set()
    for parsed in results:
        for item in parsed or ():
            item_url = item.get("url")
            if not item_url or item_url in seen_urls:
                continue
            seen_urls.add(item_url)
            raw_items.append(item)

    # 정제 파이프라인 — **제약은 상류(도서 섹션 검색)에, 판단은 순위에**. 하류에서 텍스트로
    # 걷어내는 필터를 겹겹이 쌓지 않는다(그 오탐이 풀을 16셀보다 작게 깎아 수렴을 부른다).
    # ① 사용자가 "이미 읽음/제외"로 명시한 저자·시리즈만 걷는다(유일한 하드 드롭 — 사용자가
    #    명시적으로 요청한 배제이고, 과반이 사라지면 그마저 취소된다).
    # ② 같은 책의 판형·부제 변형을 1종으로 접는다(중복 출처·부가판 대표 제거).
    # ③ 카드 적합도 순위화 — 구조 감점(판촉 나열·전집)은 삭제가 아닌 강등, 그 다음 판매지수.
    # ④ 시리즈 도배를 캡하고 ⑤ 목표 크기로 절단(순위화가 앞서므로 절단이 좋은 후보를 남긴다).
    # 등록(register_source)은 최종 순서로 맨 마지막 — source_id는 이 순서로 부여된다.
    included = _filter_excluded(raw_items, exclude or [], settings)
    deduped = _dedup_editions(included)
    ranked = _rank(deduped, settings)
    diversified = _diversify(ranked, settings.matrix_pool_max_per_series)[
        : settings.matrix_pool_target_size
    ]
    logger.info(
        "matrix 풀 정제: raw=%d exclude→%d dedup→%d 최종=%d",
        len(raw_items), len(included), len(deduped), len(diversified),
    )

    # 출처(meta)와 후보 dict에 **같은 필드 집합**을 싣는다 — 채팅 도구와 동일하게
    # product_fields 하나로 조립해(단일 소스), 매트릭스만 필드를 빠뜨리는 일이 없게 한다.
    # 이 후보 레코드가 그대로 셀의 picks로 SSE에 실리므로(프론트가 산문을 재파싱하지 않도록),
    # 표지(image_url)·가격·평점이 여기서 누락되면 카드 UI가 통째로 빈다.
    state: dict = {}  # register_source 누적용 plain dict(MutableMapping)
    candidates: list[dict] = []
    for item in diversified:
        fields = product_fields(item)
        source_id = register_source(
            state,
            title=item["title"],
            url=item["url"],
            source_type="search_result",
            snippet=item.get("author"),
            meta=fields,
        )
        candidates.append(
            {"source_id": source_id, "title": item["title"], "url": item["url"], **fields}
        )

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
    genai_client: genai.Client | None = None,
) -> SharedPool:
    """질문에 대한 공유 풀을 조립한다(캐시 우선, retrieve-once + 의도 라우팅).

    정제(matrix_query_refine on)가 질문을 {intent, queries}로 번역한다:
    - product: Yes24 도서 풀(도서 섹션 다각 검색·다양성가드·목표크기).
    - web: 퍼플렉시티 웹 사실 풀(type=web 출처). 16 페르소나가 같은 사실을 관점·말투로 해석.
    - none: 빈 풀(kind=none) — 16 페르소나가 각자 화법으로 즉답(무인용, 무출처 상품 사실은 금지).
    풀은 정제 검색어로 채우되 SharedPool.question은 **원 질문**을 유지한다. status="ok" 풀만
    캐시한다.

    정제가 켜져 있는데 **실패**하면 의도를 모르는 상태다 — 그때 원 질문을 도서 검색으로 보내면
    잡담("기분이 꿀꿀해")이 도서 질의로 강등돼 16셀 전부 "책을 찾지 못했어요"가 된다. 의도 없이
    추측하지 말고 정직한 일시 오류로 마감한다(파싱 0건을 빈 성공으로 위장하지 않는 원칙 7과 동형).
    정제가 꺼져 있으면 원 질문을 그대로 도서 검색한다(정제 없는 운영 모드의 정의된 동작).
    """
    key = _cache_key(question)
    now = time.monotonic()

    cached = _cache_get(key, settings, now)
    if cached is not None:
        logger.info("matrix pool cache hit question=%r", question)
        return cached

    checked_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M")

    intent = "product"
    search_queries = [question]
    exclude: list[str] = []
    if settings.matrix_query_refine:
        refined = await _refine_query(question, settings, genai_client)
        if refined is None:
            logger.info("matrix 정제 실패 → 정직한 일시 오류 question=%r", question)
            return SharedPool(question, [], [], checked_at, status="error", kind="none")
        intent = refined.intent
        if refined.queries:
            search_queries = refined.queries
        exclude = refined.exclude
        logger.info(
            "matrix 정제 question=%r intent=%s queries=%r exclude=%r",
            question, intent, search_queries, exclude,
        )

    if intent == "none":
        pool = SharedPool(question, [], [], checked_at, status="ok", kind="none")
    elif intent == "web":
        pool = await _build_web_pool(question, search_queries, settings, checked_at)
    else:
        pool = await _build_product_pool(
            question, search_queries, settings, checked_at, exclude=exclude
        )

    logger.info(
        "matrix pool built question=%r kind=%s status=%s candidates=%d",
        question,
        pool.kind,
        pool.status,
        len(pool.candidates),
    )
    if pool.status == "ok":
        _cache_put(key, pool, settings, now)
    return pool
