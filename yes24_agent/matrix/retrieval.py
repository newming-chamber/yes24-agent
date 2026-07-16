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
import time
from dataclasses import dataclass, field, replace

from google import genai

from yes24_agent.config import Settings, get_genai_client
from yes24_agent.matrix.planning import PlannedPick, matrix_codes, plan_selection, refine_query
from yes24_agent.product_selection import (
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

    logger.info(
        "matrix 풀 정제: raw=%d 최종=%d",
        len(raw_items),
        min(len(raw_items), settings.matrix_pool_target_size),
    )
    return _assemble_product_pool(
        question,
        raw_items,
        settings,
        checked_at,
        saw_error=saw_error,
    )


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
                "_evidence_segments": result.get("evidence_segments", []),
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
    research_model: str | None = None
    refined = await refine_query(question, settings, genai_client)
    research_model = settings.matrix_generation_model
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

    if research_model:
        pool = replace(pool, models={**pool.models, "research": research_model})

    if pool.status == "ok":
        planner_client = genai_client or get_genai_client()
        # 상세 후보 예산은 채팅 도구의 배치 상한이 아니라 Matrix 출력 구조와 요청 수량에서
        # 파생한다. 후보가 그보다 적으면 실제 후보 수가 자연스럽게 상한이 된다.
        detail_budget = min(
            len(pool.candidates),
            max(len(matrix_codes()), refined.requested_count),
        )
        detail_source_ids = _detail_source_ids(
            pool.candidates,
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
