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
import logging

from google.adk.tools import ToolContext

from yes24_agent.config import Settings, get_settings
from yes24_agent.sources import now_checked_at, register_source
from yes24_agent.yes24.client import Yes24Client, Yes24FetchError
from yes24_agent.yes24.parsers import (
    ParseError,
    parse_search,
    product_fields,
)
from yes24_agent.yes24.urls import SEARCH_SECTIONS, search_url

logger = logging.getLogger(__name__)


# 모듈 레벨 공유 클라이언트 (lazy 싱글턴). Yes24Client는 스로틀·동시성 상태를
# 내부에 들고 있으므로 프로세스 전체가 하나의 인스턴스를 공유해야 예의 있는 트래픽이 된다.
_shared_client: Yes24Client | None = None


def _get_client(settings: Settings) -> Yes24Client:
    """공유 Yes24Client 싱글턴을 반환한다(최초 호출 시 생성)."""
    global _shared_client
    if _shared_client is None:
        _shared_client = Yes24Client.from_settings(settings)
    return _shared_client


async def aclose_shared_client() -> None:
    """공유 클라이언트를 정리한다(서버 shutdown 훅용). 미생성 상태면 무동작."""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


async def _search_one(
    query: str, section: str, client: Yes24Client, settings: Settings
) -> dict:
    """한 검색 각도로 Yes24 검색 HTML을 받아 **파싱 결과만** 돌려준다(등록 없음).

    출처 등록(register_source)은 여기서 하지 않는다 — 여러 각도를 gather로 동시 실행할 때
    등록을 병렬로 돌리면 source_id 부여에 레이스가 생기므로, 네트워크·파싱(순수 계산)만
    여기서 하고 등록은 호출부의 순차 루프에서 처리한다(레이스 0). 예상된 오류(조회·파싱)만
    잡아 구조화된 error dict로 반환하고, 예상 밖 예외는 삼키지 않고 그대로 올려보낸다.

    반환: {"query", "status": "ok", "parsed": [item...]} 또는
          {"query", "status": "error", "error_type": "fetch"|"parse", "message"}.
    """
    url = search_url(settings.yes24_base_url, query, section)
    try:
        html = await client.get_text(url)
    except Yes24FetchError as exc:
        logger.info("yes24_search query=%r status=error error_type=fetch results=0", query)
        return {
            "query": query,
            "status": "error",
            "error_type": "fetch",
            "message": f"Yes24 조회에 실패했습니다: {exc}",
        }

    try:
        parsed = parse_search(
            html, base_url=settings.yes24_base_url, limit=settings.search_result_limit
        )
    except ParseError as exc:
        logger.info("yes24_search query=%r status=error error_type=parse results=0", query)
        return {
            "query": query,
            "status": "error",
            "error_type": "parse",
            "message": f"검색 결과를 해석하지 못했습니다: {exc}",
        }

    return {"query": query, "status": "ok", "parsed": parsed}


async def yes24_search(
    queries: list[str], section: str, tool_context: ToolContext
) -> dict:
    """Yes24에서 도서·상품을 검색해 현재 가격·평점·저자·출판사 등 실제 데이터를 얻는다.

    당신은 Yes24의 현재 가격·평점을 알지 못한다 — 상품 사실은 아무리 유명한 책이어도 이 도구로
    확인한다(잡담·인사·이전 대화 후속질문처럼 검색이 불필요한 경우만 예외). Yes24 검색은 키워드
    색인이라 분위기·서술형 질의는 0건이거나 무관한 상품이 섞인다 — 무관한 결과는 인용하지 말고,
    확인할 제목·저자 앵커 각도들을 queries에 함께 담아 검증한다. 검색 결과 행에는 소개·줄거리가
    없다 — 책의 내용·줄거리 서술에는 yes24_fetch/fetch_many의 상세 근거가 필요하다.

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

    Returns:
        각도 중 하나라도 검색에 성공하면 status="ok"와 results 목록(모든 각도의 결과를
        상품 기준으로 병합·중복제거, 각 항목에 인용용 source_id와 어느 각도에서 나왔는지
        queries 포함), 각 각도의 성공/실패/결과 수를 담은 searches 요약, 검색 시각 checked_at,
        result_count를 담은 dict. 어느 각도에서도 상품을 찾지 못하면 results가 빈 목록이고
        result_count=0이다(검색은 성공했으나 결과가 없는 상태). 상한을 넘겨 검색하지 않은
        각도가 있으면 dropped_count·dropped_queries로 명시한다. 모든 각도가 실패했을 때만
        status="error"와 error_type("empty_query"|"fetch"|"parse"), message에 더해
        result_count=0을 담은 dict.
    """
    settings = get_settings()

    # 허용값은 urls의 섹션 표에서 파생한다(도구가 따로 열거하지 않는다 — 표가 늘면 자동 반영).
    # 모르는 값이 오면 search_url의 ValueError가 도구 밖으로 새지 않도록 통합검색으로 폴백한다.
    if section not in SEARCH_SECTIONS:
        section = "all"

    # 각도 계획: 문자열이 아니거나 빈 각도는 버리고, 같은 각도는 한 번만(중복 검색은 Yes24
    # 트래픽·컨텍스트 낭비), 상한까지만 검색한다. 단일 문자열로 잘못 넘어와도 관용 처리한다.
    if isinstance(queries, str):
        queries = [queries]
    requested = (
        [q.strip() for q in queries if isinstance(q, str) and q.strip()]
        if isinstance(queries, list)
        else []
    )

    planned: list[str] = []
    seen_queries: set[str] = set()
    dropped_queries: list[str] = []
    for q in requested:
        if q in seen_queries:
            continue
        if len(planned) >= settings.yes24_search_max_queries:
            dropped_queries.append(q)
            continue
        seen_queries.add(q)
        planned.append(q)

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
        *(_search_one(q, section, client, settings) for q in planned)
    )

    checked_at = now_checked_at()

    results: list[dict] = []
    key_to_index: dict[str, int] = {}  # 상품 키 → results 인덱스(각도 간 중복제거·병합용)
    searches: list[dict] = []  # 각도별 성공/실패/결과 수 요약(부분 실패·0건 fail-loud)
    for outcome in searched:
        query = outcome["query"]
        if outcome["status"] == "error":
            searches.append({
                "query": query,
                "status": "error",
                "error_type": outcome["error_type"],
                "result_count": 0,
            })
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
            )
            key_to_index[key] = len(results)
            results.append(
                {
                    "source_id": source_id,
                    "type": "search_result",
                    "title": item["title"],
                    "url": item["url"],
                    "checked_at": checked_at,
                    "queries": [query],
                    **fields,
                }
            )
        searches.append({"query": query, "status": "ok", "result_count": matched})

    ok_count = sum(1 for s in searches if s["status"] == "ok")
    if ok_count == 0:
        # 모든 각도가 실패 — 단일 각도 실패의 기존 계약(status=error·error_type·result_count=0)을
        # 그대로 유지해, 에이전트가 "못 찾음"이 아니라 조회/파싱 오류로 처리하게 한다.
        first = searched[0]
        logger.info(
            "yes24_search queries=%d status=error error_type=%s results=0",
            len(planned), first["error_type"],
        )
        return {
            "status": "error",
            "error_type": first["error_type"],
            "message": first["message"],
            "result_count": 0,
        }

    logger.info(
        "yes24_search queries=%d angles_ok=%d results=%d dropped=%d",
        len(planned), ok_count, len(results), len(dropped_queries),
    )
    response = {
        "status": "ok",
        "queries": planned,
        "results": results,
        "searches": searches,
        "checked_at": checked_at,
        "result_count": len(results),
    }
    if not results:
        # 검색은 됐으나 어느 각도에서도 상품이 없다 — 0건임을 본문으로도 명시한다.
        response["message"] = "검색 결과 없음"
    if dropped_queries:
        # 가법 필드: 드롭이 없으면 반환 형태는 단일/다중 각도 모두 이 키가 없다.
        response["dropped_count"] = len(dropped_queries)
        response["dropped_queries"] = dropped_queries
        response["message"] = (
            f"한 번에 검색할 수 있는 각도 상한({settings.yes24_search_max_queries}개)을 넘어 "
            f"{len(dropped_queries)}개 각도는 검색하지 않았습니다. "
            "필요하면 남은 각도로 한 번 더 호출하세요."
        )
    return response
