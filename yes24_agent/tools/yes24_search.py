"""Yes24 검색 도구 — ADK FunctionTool로 노출되는 async 함수.

에이전트가 Yes24 상품 검색이 필요하다고 판단할 때 호출한다. HTML 조회·파싱 결과를
세션 state의 출처 레지스트리에 등록해 각 결과에 source_id를 부여하고, 인용에 쓸 수
있도록 반환 dict에 함께 담는다.

실패는 예외를 밖으로 던지지 않고 구조화된 error dict로 반환한다(fail-loud). ADK가
도구 예외를 삼키거나 RetryConfig가 개입하는 것을 피하고, 에이전트가 상태를 보고
사용자에게 알리거나 재검색을 결정하게 하기 위함이다.
"""

import logging

from google.adk.tools import ToolContext

from yes24_agent.config import Settings, get_settings
from yes24_agent.sources import now_checked_at, register_source
from yes24_agent.yes24.client import Yes24Client, Yes24FetchError
from yes24_agent.yes24.parsers import ParseError, parse_search, product_fields
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


async def yes24_search(query: str, section: str, tool_context: ToolContext) -> dict:
    """Yes24에서 도서·상품을 검색한다.

    가격·저자·출판사 등 실제 Yes24 데이터가 필요하거나, 책을 추천·비교·나열해야 할 때
    호출한다. 잡담·인사·이전 대화 후속질문처럼 검색이 불필요한 경우엔 호출하지 않는다.

    Args:
        query: 검색어. 핵심 키워드 위주로 짧게 구성한다(예: "채식주의자 한강",
            "파이썬 입문서"). 불필요한 조사·수식어는 빼고 제목·저자·주제어를 담는다.
        section: 검색 범위. "all"은 통합 검색(도서·음반·DVD 등 전체),
            "book"은 국내도서로 한정. 확실치 않으면 "all".

    Returns:
        성공 시 status="ok"와 results 목록(각 항목에 인용용 source_id 포함), 검색 시각
        checked_at, result_count를 담은 dict. 검색 결과가 없으면 results가 빈 목록이고
        result_count=0이다. 실패 시 status="error"와 error_type("fetch"|"parse"),
        message에 더해 result_count=0을 담은 dict.
    """
    settings = get_settings()

    # 허용값은 urls의 섹션 표에서 파생한다(도구가 따로 열거하지 않는다 — 표가 늘면 자동 반영).
    # 모르는 값이 오면 search_url의 ValueError가 도구 밖으로 새지 않도록 통합검색으로 폴백한다.
    if section not in SEARCH_SECTIONS:
        section = "all"

    url = search_url(settings.yes24_base_url, query, section)
    client = _get_client(settings)

    try:
        html = await client.get_text(url)
    except Yes24FetchError as exc:
        logger.info("yes24_search query=%r status=error error_type=fetch results=0", query)
        return {
            "status": "error",
            "error_type": "fetch",
            "message": f"Yes24 조회에 실패했습니다: {exc}",
            "result_count": 0,
        }

    try:
        parsed = parse_search(
            html, base_url=settings.yes24_base_url, limit=settings.search_result_limit
        )
    except ParseError as exc:
        logger.info("yes24_search query=%r status=error error_type=parse results=0", query)
        return {
            "status": "error",
            "error_type": "parse",
            "message": f"검색 결과를 해석하지 못했습니다: {exc}",
            "result_count": 0,
        }

    checked_at = now_checked_at()

    if not parsed:
        logger.info("yes24_search query=%r status=ok results=0", query)
        return {
            "status": "ok",
            "query": query,
            "results": [],
            "checked_at": checked_at,
            "message": "검색 결과 없음",
            "result_count": 0,
        }

    results: list[dict] = []
    for item in parsed:
        # 등록(meta)과 반환에 같은 필드 집합을 싣는다 — 도구별 선택 누락 불가(_product_fields).
        fields = product_fields(item)
        source_id = register_source(
            tool_context.state,
            title=item["title"],
            url=item["url"],
            source_type="search_result",
            snippet=item.get("author"),
            meta=fields,
        )
        results.append(
            {
                "source_id": source_id,
                "type": "search_result",
                "title": item["title"],
                "url": item["url"],
                **fields,
            }
        )

    logger.info("yes24_search query=%r status=ok results=%d", query, len(results))
    return {
        "status": "ok",
        "query": query,
        "results": results,
        "checked_at": checked_at,
        "result_count": len(results),
    }
