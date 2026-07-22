"""C1 — 공유 검색 풀 조립(retrieve-once).

16 페르소나가 같은 질문에 필요로 하는 것은 **같은 사실·후보 책**이고 다른 것은 톤·선택·
프레이밍이다. 그래서 검색은 질문당 소수회(fanout)만 실행해 공유 후보 풀 + 공유 출처
레지스트리를 만들고, 서버 렌더링 16뷰가 이 풀 하나를 나눠 쓴다(Yes24 트래픽 O(1)).

채팅 루프를 재사용하지 않고 원시 요소만 재사용한다:
- `search_url`·`parse_search`·`Yes24Client.get_text`: yes24_search 도구의 내부 부품.
- `register_source`(plain dict로 호출 — ToolContext 불필요, MutableMapping만 받음).
- 공유 클라이언트 싱글턴(`yes24_search._get_client`): 프로세스 전역 http_rps 스로틀을 공유해
  매트릭스 검색도 예의 있는 트래픽이 되게 한다(별도 클라이언트를 만들면 스로틀이 분리됨).

파싱 0/조회 실패는 빈 성공으로 위장하지 않고 status로 명시한다("empty"/"error"). 렌더 단계는
status!="ok"면 16열 모두 정직 폴백으로 처리한다.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from dataclasses import dataclass, field, replace

from google import genai

from yes24_agent.config import Settings, get_genai_client
from yes24_agent.evidence_segments import build_field_evidence_segments
from yes24_agent.matrix.planning import PlannedPick, matrix_codes, plan_selection, refine_query
from yes24_agent.product_selection import (
    PRODUCT_RATIONALE_FIELDS,
    ProductConstraint,
    product_constraints_satisfied,
    product_evidence_fields,
)
from yes24_agent.sources import SOURCES_STATE_KEY, get_sources, now_checked_at, register_source
from yes24_agent.tools.yes24_fetch import build_result_from_html
from yes24_agent.tools.yes24_search import _get_client
from yes24_agent.yes24.client import Yes24FetchError
from yes24_agent.yes24.parsers import ParseError, parse_search, product_fields
from yes24_agent.yes24.urls import search_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SharedPool:
    """16 생성이 공유하는 후보 풀 + 출처 레지스트리(불변 스냅샷).

    - question: 원 질문(캐시 키·프롬프트에 사용).
    - candidates: 상품 후보 dict 목록(title·author·price·pub_date…).
    - sources: register_source로 누적된 공유 출처 레지스트리(인용 검증·done payload 재료).
    - checked_at: 검색 시각(KST). 가격·목록·신선도의 기준 시점 표기에 사용.
    - status: "ok"(선택 가능) | "empty"(검색 0건) |
      "no_match"(상세 조건 일치 후보 없음) | "error"(조회 실패).
    """

    question: str
    candidates: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    checked_at: str = ""
    status: str = "empty"
    picks: dict[str, tuple[PlannedPick, ...]] = field(default_factory=dict)
    expected_constraints: tuple[ProductConstraint, ...] = ()
    requested_count: int = 1
    models: dict[str, str] = field(default_factory=dict)


# 질문 정규화 키 → (등록 시각[monotonic], SharedPool). status="ok" 풀만 캐시한다
# (empty/error는 일시 실패일 수 있어 캐시하면 TTL 동안 재시도를 막으므로 캐시하지 않음).
_pool_cache: dict[str, tuple[float, SharedPool]] = {}


def clear_pool_cache() -> None:
    """공유 풀 캐시를 비운다(테스트 격리·운영 리셋용). 전역 _pool_cache는 살아있으므로
    캐시 hit/miss가 테스트 간 새지 않도록 이 공개 훅으로 리셋한다."""
    _pool_cache.clear()


def _cache_key(question: str) -> str:
    """캐시 조회용 정규화 키 — 앞뒤 공백 제거, 연속 공백 축약, 소문자화."""
    return " ".join(question.split()).lower()


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


# 캐시 키·저자 정규화용 연속 공백 축약, 저자 병기(한자/영문 괄호) 제거 패턴.
_WHITESPACE_RE = re.compile(r"\s+")
_BRACKET_GROUP = re.compile(r"[\[\(【][^\]\)】]*[\]\)】]")

# 에디션 변형 dedup — 같은 책의 판형/장정 변형("채식주의자"·"채식주의자(개정판)"·"채식주의자
# (큰글자도서)")이 별개 URL이라 url-dedup을 통과해 풀에 중복 유입되는 것을 접는다.
#
# 판형 수식어(개정판·큰글자도서 등)는 별도 목록으로 두지 않는다 — **코어 제목**(부제·괄호·시리즈
# 라벨 앞까지)이 이미 그 부가 텍스트를 잘라내므로, "같은 저자 + 같은 코어 제목"이면 같은 책이다.
# 단 아래 _AUTHOR_ROLE_STRIP·_DISTINGUISHING_MARKER의 열거(저자 역할어·다른 판본 표지)는 판형이
# 아니라 오병합 방지용 전수조사 기반 열거이며, 신규 항목 발견 시 추가한다.
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


def _assemble_product_pool(
    question: str,
    items: list[dict],
    settings: Settings,
    checked_at: str,
    *,
    saw_error: bool,
    models: dict[str, str] | None = None,
    expected_constraints: tuple[ProductConstraint, ...] = (),
) -> SharedPool:
    """URL로 합쳐진 검색 후보를 source_id가 있는 공유 상품 풀로 조립한다."""
    selected = items[: settings.matrix_pool_target_size]
    state: dict = {}
    candidates: list[dict] = []
    for item in selected:
        fields = product_fields(item)
        source_id = register_source(
            state,
            title=item["title"],
            url=item["url"],
            source_type="search_result",
            snippet=item.get("author"),
            checked_at=checked_at,
            meta=fields,
        )
        candidates.append(
            {
                "source_id": source_id,
                "title": item["title"],
                "url": item["url"],
                "_retrieval_axes": tuple(item.get("_retrieval_axes") or ()),
                **fields,
            }
        )
    status = "ok" if candidates else ("error" if saw_error else "empty")
    return SharedPool(
        question=question,
        candidates=candidates,
        sources=get_sources(state),
        checked_at=checked_at,
        status=status,
        expected_constraints=expected_constraints,
        models=dict(models or {}),
    )


async def _build_product_pool(
    question: str,
    search_queries: list[str],
    settings: Settings,
    checked_at: str,
) -> SharedPool:
    """Yes24 도서 풀 — 다각 검색 결과를 URL로 합쳐 순위화한다.

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
    by_url: dict[str, dict] = {}
    for row in itertools.zip_longest(*(parsed or () for parsed in results)):
        for axis_index, item in enumerate(row):
            if item is None:
                continue
            item_url = item.get("url")
            if not item_url:
                continue
            existing = by_url.get(item_url)
            if existing is not None:
                existing["_retrieval_axes"] = tuple(
                    dict.fromkeys((*existing["_retrieval_axes"], axis_index))
                )
                continue
            observed = {**item, "_retrieval_axes": (axis_index,)}
            by_url[item_url] = observed
            raw_items.append(observed)

    # 같은 책의 판형·부제·시리즈 변형(별개 URL이라 url-dedup을 통과)을 1종으로 접는다 —
    # target_size 절단 직전에 수행해 중복 판본이 풀의 유효 폭을 좀먹지 않게 한다.
    deduped = _dedup_editions(raw_items)
    logger.info(
        "matrix 풀 정제: raw=%d dedup→%d 최종=%d",
        len(raw_items),
        len(deduped),
        min(len(deduped), settings.matrix_pool_target_size),
    )
    return _assemble_product_pool(
        question,
        deduped,
        settings,
        checked_at,
        saw_error=saw_error,
    )


def _known_violation(
    candidate: dict, constraints: tuple[ProductConstraint, ...]
) -> bool:
    """검색 단계에서 이미 관측된 숫자 필드가 조건을 어겼는지 판정한다.

    관측 없는 필드(예: 검색 목록에 없는 page_count)는 위반이 아니다 — 상세 fetch가 확정한다.
    상세 예산은 유한하므로, 이미 조건을 어긴 게 확실한 후보(가격·평점 초과 등)에 예산을 쓰면
    eligible 상세 후보가 그만큼 줄어 합리적 제약 질의가 0/16 폴백으로 새기 쉽다.
    """
    for constraint in constraints:
        observed = candidate.get(constraint.field.value)
        if (
            isinstance(observed, (int, float))
            and not isinstance(observed, bool)
            and not product_constraints_satisfied(candidate, (constraint,))
        ):
            return True
    return False


def _detail_source_ids(candidates: list[dict], limit: int) -> tuple[int, ...]:
    """검색 축마다 상위 후보를 번갈아 골라 단일 결과 순서의 상세 편향을 막는다."""
    queues: dict[int, list[int]] = {}
    for candidate in candidates:
        source_id = candidate.get("source_id")
        if not isinstance(source_id, int) or isinstance(source_id, bool):
            continue
        for axis in candidate.get("_retrieval_axes") or ():
            if isinstance(axis, int) and not isinstance(axis, bool):
                queues.setdefault(axis, []).append(source_id)

    selected: list[int] = []
    seen: set[int] = set()
    while len(selected) < limit:
        progressed = False
        for queue in queues.values():
            while queue and queue[0] in seen:
                queue.pop(0)
            if not queue:
                continue
            source_id = queue.pop(0)
            seen.add(source_id)
            selected.append(source_id)
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return tuple(selected)


class _StateContext:
    def __init__(self, state: dict):
        self.state = state


async def _fetch_selected_details(
    pool: SharedPool,
    settings: Settings,
    detail_source_ids: tuple[int, ...],
) -> tuple[SharedPool, tuple[int, ...]]:
    """축별로 배정된 후보를 병렬 열람하고 동일 source_id를 상세 근거로 갱신한다."""
    by_id = {candidate["source_id"]: candidate for candidate in pool.candidates}
    selected = [
        by_id[source_id]
        for source_id in detail_source_ids
        if source_id in by_id
    ]
    if not selected:
        return pool, ()

    client = _get_client(settings)
    fetched = await asyncio.gather(
        *(client.get_text(candidate["url"]) for candidate in selected),
        return_exceptions=True,
    )
    state = {"sources": list(pool.sources)}
    context = _StateContext(state)
    updated = {candidate["source_id"]: dict(candidate) for candidate in pool.candidates}
    successful_ids: list[int] = []
    for candidate, html in zip(selected, fetched):
        if isinstance(html, BaseException):
            logger.info("matrix 선택 상세 조회 실패 source_id=%s: %s", candidate["source_id"], html)
            continue
        result = build_result_from_html(html, candidate["url"], settings, context)
        if result.get("status") != "ok" or result.get("type") != "book_detail":
            continue
        source_id = result["source_id"]
        detail = {
            key: value
            for key, value in result.items()
            if key not in {"status", "source_id", "links"}
        }
        updated[source_id] = {**updated[source_id], **detail, "source_id": source_id}
        state[SOURCES_STATE_KEY] = [
            {
                **source,
                "_evidence_fields": sorted(product_evidence_fields(result)),
                "_evidence_segments": build_field_evidence_segments(
                    result, PRODUCT_RATIONALE_FIELDS
                ),
            }
            if source["id"] == source_id
            else source
            for source in state[SOURCES_STATE_KEY]
        ]
        successful_ids.append(source_id)

    return (
        SharedPool(
            question=pool.question,
            candidates=list(updated.values()),
            sources=get_sources(state),
            checked_at=pool.checked_at,
            status=pool.status,
            expected_constraints=pool.expected_constraints,
            requested_count=pool.requested_count,
            models=pool.models,
        ),
        tuple(successful_ids),
    )


async def build_shared_pool(
    question: str,
    settings: Settings,
    *,
    genai_client: genai.Client | None = None,
) -> SharedPool:
    """원 질문을 보존하며 Yes24 검색·상세 근거로 RBTI 공유 풀을 조립한다."""
    key = _cache_key(question)
    now = time.monotonic()

    cached = _cache_get(key, settings, now)
    if cached is not None:
        logger.info("matrix pool cache hit question=%r", question)
        return cached

    checked_at = now_checked_at()

    if not settings.matrix_query_refine:
        logger.info("matrix 구조화 검색 계획 비활성 → 정직한 일시 오류 question=%r", question)
        return SharedPool(question, [], [], checked_at, status="error")

    search_queries = [question]
    expected_constraints: tuple[ProductConstraint, ...] = ()
    refined = await refine_query(question, settings, genai_client)
    if refined is None:
        logger.info("matrix 정제 실패 → 정직한 일시 오류 question=%r", question)
        return SharedPool(question, [], [], checked_at, status="error")
    if refined.queries:
        search_queries = refined.queries
    expected_constraints = refined.constraints
    logger.info(
        "matrix 정제 question=%r queries=%r constraints=%s",
        question,
        search_queries,
        [constraint.model_dump(mode="json") for constraint in expected_constraints],
    )

    pool = await _build_product_pool(question, search_queries, settings, checked_at)
    pool = replace(
        pool,
        expected_constraints=expected_constraints,
        requested_count=refined.requested_count,
    )

    pool = replace(pool, models={**pool.models, "research": settings.model_name})

    if pool.status == "ok":
        planner_client = genai_client or get_genai_client()
        # 상세 후보 예산은 채팅 도구의 배치 상한이 아니라 Matrix 출력 구조와 요청 수량에서
        # 파생한다. 후보가 그보다 적으면 실제 후보 수가 자연스럽게 상한이 된다.
        detail_budget = min(
            len(pool.candidates),
            max(len(matrix_codes()), refined.requested_count),
        )
        # 조건이 있으면 검색 관측만으로 이미 위반이 확실한 후보를 뒤로 미룬다(안정 정렬 —
        # 같은 그룹 안의 축 배분·등장 순서는 보존). 관측 불가 필드는 미루지 않는다.
        detail_candidates = sorted(
            pool.candidates,
            key=lambda candidate: _known_violation(candidate, pool.expected_constraints),
        )
        detail_source_ids = _detail_source_ids(
            detail_candidates,
            detail_budget,
        )
        pool, successful_detail_ids = await _fetch_selected_details(
            pool, settings, detail_source_ids
        )
        if not successful_detail_ids:
            logger.info("matrix 상세 근거 확보 실패 question=%r", question)
            pool = replace(pool, status="error", picks={})
        else:
            candidates_by_id = {candidate["source_id"]: candidate for candidate in pool.candidates}
            eligible_detail_ids = tuple(
                source_id
                for source_id in successful_detail_ids
                if product_constraints_satisfied(
                    candidates_by_id[source_id], pool.expected_constraints
                )
            )
            if not eligible_detail_ids:
                logger.info("matrix 숫자 조건 일치 상세 후보 없음 question=%r", question)
                pool = replace(pool, status="no_match", picks={})
                logger.info(
                    "matrix pool built question=%r status=%s candidates=%d",
                    question,
                    pool.status,
                    len(pool.candidates),
                )
                return pool
            pool = replace(pool, models={**pool.models, "selection": settings.model_name})
            selection = await plan_selection(
                pool.question,
                pool.candidates,
                pool.sources,
                settings,
                planner_client,
                eligible_detail_ids,
                tuple(zip(refined.queries, refined.query_axes)),
                refined.requested_count,
            )
            if selection is None:
                logger.info("matrix 상세 근거 선택 실패 question=%r", question)
                pool = replace(pool, status="error", picks={})
            elif not selection.picks:
                logger.info("matrix 상세 조건 일치 후보 없음 question=%r", question)
                pool = replace(pool, status="no_match", picks={})
            else:
                pool = replace(pool, picks=selection.picks)

    logger.info(
        "matrix pool built question=%r status=%s candidates=%d",
        question,
        pool.status,
        len(pool.candidates),
    )
    if pool.status == "ok" and set(pool.picks) == set(matrix_codes()):
        _cache_put(key, pool, settings, now)
    return pool
