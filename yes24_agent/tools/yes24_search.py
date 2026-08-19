"""Yes24 검색 도구 — ADK FunctionTool로 노출되는 async 함수.

에이전트가 Yes24 상품 검색이 필요하다고 판단할 때 호출한다. HTML 조회·파싱 결과를
세션 state의 출처 레지스트리에 등록해 각 결과에 source_id를 부여하고, 인용에 쓸 수
있도록 반환 dict에 함께 담는다.

**멀티쿼리 병렬 검색**: 이 도구는 한 번에 여러 검색 각도(queries)를 받아 asyncio.gather로
동시에 검색하고 결과를 합쳐 돌려준다. 하나의 탐색 각도마다 LLM 왕복을 1회씩 소모하던
구조가 지연의 최대 덩어리였다(2026-07-20 실측: 추천 질의 1회에서 검색만 5라운드 직렬,
라운드당 모델 대기 3.5~11.1s). web_search·fetch_many가 이미 채택한 것과 같은 구조 —
모델이 N번 나눠 호출해 주길 기대(비결정적)하는 대신 N개 각도를 한 리스트로 받아 코드가
병렬 실행을 보장한다. 단일 각도 질문은 원소 하나짜리 리스트로 그대로 처리된다(단일 검색과
동일 지연). 동시 실행분도 공유 Yes24Client의 Semaphore(http_concurrency)+rps 페이싱을
그대로 지나므로 Yes24 예의(동시성·요청률)는 불변이다.

정확성 설계(레이스 0): 네트워크·파싱만 동시 실행하고, 출처 등록(register_source·id 부여)은
**순차 루프**로 처리한다 — 단일 tool_context.state에 대한 등록이 await 없이 순차라 source_id가
유일·단조로 부여된다(web_search·fetch_many와 동일 규약). 같은 상품이 여러 각도에서 걸리면
한 번만 등록하고 어느 각도에서 나왔는지(queries)를 합쳐 교차 확증 신호로 남긴다.

실패는 예외를 밖으로 던지지 않고 구조화된 error dict로 반환한다(fail-loud). ADK가
도구 예외를 삼키거나 RetryConfig가 개입하는 것을 피하고, 에이전트가 상태를 보고
사용자에게 알리거나 재검색을 결정하게 하기 위함이다. 부분 실패·0건 각도는 성공 결과와 함께
각 각도의 상태를 searches로 노출한다 — 빈 성공으로 위장하지 않는다.
"""

import asyncio
import contextlib
import contextvars
import logging

from google.adk.tools import ToolContext

from yes24_agent.config import Settings, get_settings
from yes24_agent.sources import cite_marker, now_checked_at, register_source
from yes24_agent.tools._planning import (
    angle_error_summary,
    dropped_queries_message,
    plan_queries,
)
from yes24_agent.yes24.client import Yes24Client, Yes24FetchError, Yes24TextCache
from yes24_agent.yes24.parsers import (
    ParseError,
    parse_search,
    product_fields,
)
from yes24_agent.yes24.urls import SEARCH_ORDERS, SEARCH_SECTIONS, WIDEST_SECTION, search_url

logger = logging.getLogger(__name__)


# 모듈 레벨 공유 클라이언트 (lazy 싱글턴). Yes24Client는 스로틀·동시성 상태를
# 내부에 들고 있으므로 프로세스 전체가 하나의 인스턴스를 공유해야 예의 있는 트래픽이 된다.
_shared_client: Yes24Client | None = None

# 매트릭스 경로 전용 고처리량 클라이언트(lazy 싱글턴, 채팅 클라이언트와 별도 스로틀 상태).
# 채팅 단일 경로는 _shared_client(rps=1.5)를 그대로 쓰고, 매트릭스 셀만 아래 contextvar가
# 켜진 자기 태스크 컨텍스트에서 이 클라이언트(matrix_http_rps/concurrency)를 집어 든다.
_matrix_client: Yes24Client | None = None

# 두 클라이언트가 **공유**하는 Yes24 HTTP 텍스트 캐시(lazy 싱글턴). 매트릭스 16셀 버스트의
# 동일 URL 중복 fetch가 표적이며, 채팅↔매트릭스 사이에서도 TTL 내 관측을 재사용한다.
# TTL·용량·한계(checked_at 표류)는 config yes24_cache_* 주석 참조.
_shared_cache: Yes24TextCache | None = None


def _get_cache(settings: Settings) -> Yes24TextCache:
    """공유 텍스트 캐시 싱글턴을 반환한다(최초 호출 시 생성)."""
    global _shared_cache
    if _shared_cache is None:
        _shared_cache = Yes24TextCache(
            ttl_s=settings.yes24_cache_ttl_s,
            max_entries=settings.yes24_cache_max_entries,
        )
    return _shared_cache

# "고처리량 경로인가" 신호. 매트릭스 셀 태스크가 자기 컨텍스트 사본에서만 True로 켜므로
# (matrix_runner._run_cell), 동시에 도는 채팅 요청 태스크는 영향받지 않는다. asyncio.gather로
# 갈라지는 도구 하위 태스크는 켠 시점 이후의 컨텍스트를 복사해 이 값을 물려받는다.
_high_throughput: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "yes24_high_throughput", default=False
)


@contextlib.contextmanager
def high_throughput_client():
    """이 with 블록(및 그 안에서 파생되는 하위 태스크) 동안 Yes24 요청을 고처리량 매트릭스
    클라이언트로 라우팅한다. 매트릭스 셀 태스크가 감싸 쓰며, 채팅 요청 태스크는 무영향이다.
    """
    token = _high_throughput.set(True)
    try:
        yield
    finally:
        _high_throughput.reset(token)


def _get_client(settings: Settings) -> Yes24Client:
    """이 태스크 컨텍스트에 맞는 Yes24Client 싱글턴을 반환한다(최초 호출 시 생성).

    고처리량 컨텍스트(매트릭스 셀)면 matrix_http_* 예산의 전용 클라이언트를, 아니면 채팅
    공유 클라이언트를 쓴다. 두 경로는 스로틀·세마포어 상태가 분리돼 서로를 굶기지 않는다.
    """
    global _shared_client, _matrix_client
    if _high_throughput.get():
        if _matrix_client is None:
            _matrix_client = Yes24Client.from_settings(
                settings,
                concurrency=settings.matrix_http_concurrency,
                rps=settings.matrix_http_rps,
                cache=_get_cache(settings),
            )
        return _matrix_client
    if _shared_client is None:
        _shared_client = Yes24Client.from_settings(settings, cache=_get_cache(settings))
    return _shared_client


async def aclose_shared_client() -> None:
    """공유·매트릭스 클라이언트를 정리한다(서버 shutdown 훅용). 미생성분은 무동작."""
    global _shared_client, _matrix_client, _shared_cache
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None
    if _matrix_client is not None:
        await _matrix_client.aclose()
        _matrix_client = None
    _shared_cache = None


async def _search_one(
    query: str, section: str, order: str, author_no: str, client: Yes24Client, settings: Settings
) -> dict:
    """한 검색 각도로 Yes24 검색 HTML을 받아 **파싱 결과만** 돌려준다(등록 없음).

    출처 등록(register_source)은 여기서 하지 않는다 — 여러 각도를 gather로 동시 실행할 때
    등록을 병렬로 돌리면 source_id 부여에 레이스가 생기므로, 네트워크·파싱(순수 계산)만
    여기서 하고 등록은 호출부의 순차 루프에서 처리한다(레이스 0). 예상된 오류(조회·파싱)만
    잡아 구조화된 error dict로 반환하고, 예상 밖 예외는 삼키지 않고 그대로 올려보낸다.

    반환: {"query", "section", "status": "ok", "parsed": [item...]} 또는
          {"query", "section", "status": "error", "error_type": "fetch"|"parse", "message"}.
    `section`은 **실제로 검색한 범위**다 — 호출부가 요청 범위와 대조해 넓히기 여부를 판정하고
    searches 요약에도 그대로 싣는다(별도 장부를 만들지 않는다).
    """
    url = search_url(settings.yes24_base_url, query, section, order, author_no)
    try:
        html = await client.get_text(url)
    except Yes24FetchError as exc:
        logger.info(f"yes24_search query={query!r} status=error error_type=fetch results=0")
        return {
            "query": query,
            "section": section,
            "status": "error",
            "error_type": "fetch",
            "message": f"Yes24 조회에 실패했습니다: {exc}",
        }

    try:
        parsed = parse_search(
            html, base_url=settings.yes24_base_url, limit=settings.search_result_limit
        )
    except ParseError as exc:
        logger.info(f"yes24_search query={query!r} status=error error_type=parse results=0")
        return {
            "query": query,
            "section": section,
            "status": "error",
            "error_type": "parse",
            "message": f"검색 결과를 해석하지 못했습니다: {exc}",
        }

    return {"query": query, "section": section, "status": "ok", "parsed": parsed}


async def yes24_search(
    queries: list[str],
    section: str,
    tool_context: ToolContext,
    order: str = "",
    author_no: str = "",
) -> dict:
    """Yes24에서 도서·상품을 검색해 현재 가격·평점·저자·출판사 등 실제 데이터를 얻는다.

    당신은 Yes24의 현재 가격·평점을 알지 못한다 — 상품 사실은 아무리 유명한 책이어도 이 도구로
    확인한다(잡담·인사·이전 대화 후속질문처럼 검색이 불필요한 경우만 예외). Yes24 검색은 키워드
    색인이라 분위기·서술형 질의는 0건이거나 무관한 상품이 섞인다 — 무관한 결과는 인용하지 말고,
    확인할 제목·저자 앵커 각도들을 queries에 함께 담아 검증한다. 검색 결과 행은 목록 수준의
    메타데이터일 뿐이라 값이 없는 필드(null)와 행에 아예 없는 정보가 많다 — 그런 값은 관측되지
    않은 것이므로 추정하지 말고, 답변이나 후보 선별에 필요하면 yes24_fetch/fetch_many로 상세를
    읽어 확인한다.

    질문에 서로 독립적인 탐색 각도가 여럿 있으면 **그 각도들을 한 번에 queries에 함께 담는다**
    — 각도들은 동시에 검색되므로 여러 각도를 나눠 호출할 때보다 훨씬 빠르다. 단일 각도
    질문은 원소 하나만 전달한다. 같은 의도를 표현만 바꾼 중복 각도는 만들지 않고, 서로 다른
    대상이나 조건을 확인하는 각도만 담는다.

    Args:
        queries: 검색 각도 리스트. 각 원소는 독립적으로 찾아볼 검색어이며, 핵심 키워드
            위주로 짧게 구성한다 — 불필요한 조사·수식어는 빼고 제목·저자·주제어를 담는다.
            상한을 넘는 각도는 dropped_queries로 알리고 검색하지 않는다.
        section: 검색 범위(모든 각도에 공통 적용). "all"은 통합 검색(도서·음반·DVD 등 전체),
            "book"은 국내도서로 한정. 확실치 않으면 "all".
        order: 정렬(모든 각도에 공통 적용). 기본 ""는 인기도순 — 잘 팔리는 책이 앞이라
            **최신작이 목록 밖에 밀려날 수 있다**. 출간 시점이 답의 근거가 되는 질문은
            "recent"(신상품순)로 검색해야 출간일 내림차순의 실제 최신 목록이 근거가 된다.
            단 신상품순은 관련도 필터가 풀려 검색어만 겹치는 무관 상품(동명이인의 신간
            포함)이 섞이므로, 결과의 author·author_no·pub_date·kind로 대상을 확인하고
            무관한 행은 무시한다. kind는 그 행이 어떤 종류의 상품인지 말하는 사이트
            라벨이다 — 책에 대한 사실은 kind가 책인 행에만 근거한다. 연도를 검색어에
            넣는 방식("작가명 2026")은 키워드 색인이라 0건이 되기 쉽다 — 대신 이 정렬을 쓴다.
        author_no: 저자 스코프(모든 각도에 공통 적용). 검색 결과 행의 author_no 필드가
            그 저자의 동일성 키다 — 지정하면 검색어와 무관하게 **그 저자의 책만** 나온다.
            같은 이름의 다른 저자 책이 결과에 섞이거나(결과의 author_no 값이 여러 개로
            갈리면 동명이인이다), 신상품순 목록이 무관 저자의 신간으로 덮일 때, 관측한
            author_no로 다시 검색해 확정한다. **키의 채집처가 중요하다**: author_no는
            그 저자임이 이미 확인되는 행(대표작·아는 작품이 보이는 기본 인기도순 결과)에서
            관측한다 — 신상품순 목록은 그 저자의 행이 통째로 밀려나고 동명이인 행만 남을
            수 있어, 이름만 보고 거기서 집은 author_no는 다른 사람의 번호일 수 있다.
            같은 이유로 **신상품순 목록에 그 저자의 책이 안 보인다고 "신간 없음"으로
            단정하지 말 것** — 인기도순에서 확보한 author_no로 좁혀 재확인한다. 관측된
            값만 쓰고 지어내지 않는다. author_no를 쓸 때 검색어는 사이트가 무시하므로
            각도는 하나만 담는다.

    Returns:
        각도 중 하나라도 검색에 성공하면 status="ok"와 results 목록(모든 각도의 결과를
        상품 기준으로 병합·중복제거, 각 항목에 인용용 source_id와 어느 각도에서 나왔는지
        queries 포함), 각 각도의 성공/실패/결과 수를 담은 searches 요약, 검색 시각 checked_at,
        result_count를 담은 dict. 어느 각도에서도 상품을 찾지 못하면 results가 빈 목록이고
        result_count=0이다(검색은 성공했으나 결과가 없는 상태). 섹션을 한정했는데 0건인
        각도는 통합 검색으로 한 번 더 자동 재검색되며, 그 항목은 searches에 expanded_from으로
        표시된다(원 각도의 0건 항목도 함께 남는다). 상한을 넘겨 검색하지 않은
        각도가 있으면 dropped_count·dropped_queries로 명시한다. 모든 각도가 실패했을 때만
        status="error"와 error_type("empty_query"|"fetch"|"parse"), message에 더해
        result_count=0을 담은 dict.
    """
    settings = get_settings()

    # 허용값은 urls의 섹션·정렬 표에서 파생한다(도구가 따로 열거하지 않는다 — 표가 늘면 자동
    # 반영). 모르는 값이 오면 search_url의 ValueError가 도구 밖으로 새지 않도록 각각 최광역
    # 범위·기본 정렬로 폴백한다.
    if section not in SEARCH_SECTIONS:
        section = WIDEST_SECTION
    if order not in SEARCH_ORDERS:
        order = ""

    # 각도 계획(관용 변환·중복 제거·상한 cap)은 web_search와 공용 헬퍼를 쓴다.
    planned, dropped_queries = plan_queries(queries, settings.yes24_search_max_queries)

    if not planned:
        # 유효한 검색 각도가 하나도 없다 — 빈 성공으로 위장하지 않고 명시적 실패.
        logger.info("yes24_search status=error error_type=empty_query")
        return {
            "status": "error",
            "error_type": "empty_query",
            "message": "검색할 유효한 검색어가 없습니다",
            "result_count": 0,
        }

    client = _get_client(settings)

    # 네트워크·파싱만 동시 실행한다(각도별 병렬). 등록은 아래 순차 루프에서 — 레이스 0.
    # _search_one이 예상 오류를 이미 error dict로 삼키므로 예상 밖 예외만 gather 밖으로 올라온다.
    searched = await asyncio.gather(
        *(_search_one(q, section, order, author_no, client, settings) for q in planned)
    )

    # 섹션 한정 검색의 0건은 "없다"가 아니라 **범위 밖**일 수 있다 — 국내도서 한정은 영어판·
    # 수입서를 구조적으로 배제하므로, 정확한 질의여도 0건이 나오고 모델은 그대로 오부정한다
    # (2026-08-12 실측: "한강 The Vegetarian 영어판" section=book 0건 → all 8건).
    # 그 0건 각도만 최광역 범위로 한 번 더 검색해 병합한다. 조회·파싱 실패 각도는 대상이
    # 아니고(범위 문제가 아니라 실패다), 이미 최광역이면 넓힐 곳이 없다 — 1단 한정이다.
    if section != WIDEST_SECTION:
        widen = [o["query"] for o in searched if o["status"] == "ok" and not o["parsed"]]
        if widen:
            logger.info(
                f"yes24_search section={section} 0건 각도 {len(widen)}개를 "
                f"section={WIDEST_SECTION}로 넓혀 재검색합니다: {widen}"
            )
            searched = [
                *searched,
                *await asyncio.gather(
                    *(
                        _search_one(q, WIDEST_SECTION, order, author_no, client, settings)
                        for q in widen
                    )
                ),
            ]

    checked_at = now_checked_at()

    results: list[dict] = []
    key_to_index: dict[str, int] = {}  # 상품 키 → results 인덱스(각도 간 중복제거·병합용)
    searches: list[dict] = []  # 각도별 성공/실패/결과 수 요약(부분 실패·0건 fail-loud)
    for outcome in searched:
        query = outcome["query"]
        # 넓혀 재검색한 각도는 원 각도의 0건 항목과 나란히 남기고, 어느 범위로 넓혔는지를
        # 가법 필드로만 구분한다 — 넓히기가 없으면 요약 형태가 종전과 그대로 같다.
        widened = (
            {"section": outcome["section"], "expanded_from": section}
            if outcome["section"] != section
            else {}
        )
        if outcome["status"] == "error":
            searches.append({**angle_error_summary(query, outcome["error_type"]), **widened})
            continue
        matched = 0
        for item in outcome["parsed"]:
            # 등록(meta)과 반환에 같은 필드 집합을 싣는다 — 도구별 선택 누락 불가(product_fields).
            fields = product_fields(item)
            # 같은 상품의 동일성은 goods_no가 정본이다(검색 결과 URL은 같은 상품이어도
            # 각도별로 파라미터가 붙을 수 있다). goods_no가 없는 항목은 url로 갈음한다.
            key = fields.get("goods_no") or item["url"]
            matched += 1
            existing = key_to_index.get(key)
            if existing is not None:
                # 같은 상품이 다른 각도에서도 걸렸다 — 재등록하지 않고 어느 각도에서 나왔는지만
                # 합쳐 교차 확증 신호로 남긴다(source_id 중복 방지).
                if query not in results[existing]["queries"]:
                    results[existing]["queries"].append(query)
                continue
            source_id = register_source(
                tool_context.state,
                title=item["title"],
                url=item["url"],
                source_type="search_result",
                snippet=item.get("author"),
                checked_at=checked_at,
                meta=fields,
                invocation_id=getattr(tool_context, "invocation_id", None),
            )
            key_to_index[key] = len(results)
            results.append(
                {
                    "source_id": source_id,
                    "cite_as": cite_marker(source_id),
                    "type": "search_result",
                    "title": item["title"],
                    "url": item["url"],
                    "checked_at": checked_at,
                    "queries": [query],
                    **fields,
                }
            )
        searches.append({"query": query, "status": "ok", "result_count": matched, **widened})

    ok_count = sum(1 for s in searches if s["status"] == "ok")
    if ok_count == 0:
        # 모든 각도가 실패 — 단일 각도 실패의 기존 계약(status=error·error_type·result_count=0)을
        # 그대로 유지해, 에이전트가 "못 찾음"이 아니라 조회/파싱 오류로 처리하게 한다.
        first = searched[0]
        logger.info(
            f"yes24_search queries={len(planned)} status=error "
            f"error_type={first['error_type']} results=0"
        )
        return {
            "status": "error",
            "error_type": first["error_type"],
            "message": first["message"],
            "result_count": 0,
        }

    logger.info(
        f"yes24_search queries={len(planned)} angles_ok={ok_count} "
        f"results={len(results)} dropped={len(dropped_queries)}"
    )
    response = {
        "status": "ok",
        "queries": planned,
        "results": results,
        "searches": searches,
        "checked_at": checked_at,
        "result_count": len(results),
    }
    if order:
        # 가법 필드: 기본 정렬이면 반환 형태는 종전과 같다. 모든 각도 공통이라 한 번만 싣는다
        # — 모델이 "이 결과 순서 = 출간일 내림차순"임을 알고 최신작 판정의 근거로 삼는다.
        response["order"] = order
    if author_no:
        # 가법 필드: 이 결과 집합이 저자 스코프임을 명시한다 — 모델이 "이 목록 = 그 저자의
        # 전작"을 전제로 최신작·전작 여부를 판정할 수 있다.
        response["author_no"] = author_no
    if not results:
        # 검색은 됐으나 어느 각도에서도 상품이 없다 — 0건임을 본문으로도 명시한다.
        response["message"] = "검색 결과 없음"
    if dropped_queries:
        # 가법 필드: 드롭이 없으면 반환 형태는 단일/다중 각도 모두 이 키가 없다.
        response["dropped_count"] = len(dropped_queries)
        response["dropped_queries"] = dropped_queries
        response["message"] = dropped_queries_message(
            settings.yes24_search_max_queries, len(dropped_queries)
        )
    return response
