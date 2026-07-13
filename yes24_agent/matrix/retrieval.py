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

queries(검색어) 규칙 — **의도를 최대한 담는 번역**이지 단순 추출이 아닙니다:
- 원문 단어에 얽매이지 말고 검색 의도를 가장 잘 담는 검색어를 만드세요. 원문에 없는 단어라도
  의도를 더 잘 담으면 넣으세요(예: '과학적인 책 추천' → '과학 교양' — 문제집이 아닌 교양서 의도).
- **단 하나의 광의 단어(소설/과학/책)로 과축약하지 마세요.** 주제·분야가 있으면 2단어 이상으로.
- **풀을 넓히려면 서로 다른 각도로 최대 {max_queries}개**를 내세요. 넓은 풀일수록 16 유형이
  다른 책을 고를 여지가 커집니다:
  · 주제·장르가 뚜렷하면 **같은 갈래 안에서** 여러 각도로 넓히세요(예: '재테크 입문서' → '재테크
    입문', '돈 공부', '경제 상식'). **요청한 갈래를 벗어나지 마세요**(비문학 '역사책'에 '소설'을,
    '그림책'에 '입시'를 섞지 말 것 — 엉뚱한 우세 도서가 풀을 오염시킵니다).
  · 장르·주제가 없는 막연한 요청은 **서로 다른 장르로** 넓히세요(예: '무슨 책 읽을까' →
    '베스트셀러', '소설', '에세이').
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

    # 다양성 가드(시리즈 도배 차단) → 목표 크기 절단. 살아남은 아이템만 register_source로
    # 등록한다(sources·candidates 정렬 유지 — 등록은 dedup·절단 후).
    diversified = _diversify(raw_items, settings.matrix_pool_max_per_series)[
        : settings.matrix_pool_target_size
    ]
    dropped = len(raw_items) - len(diversified)
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
