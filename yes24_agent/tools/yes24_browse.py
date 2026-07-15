"""Yes24 코너 둘러보기 도구 — ADK FunctionTool로 노출되는 async 함수.

베스트셀러·신간·크레마클럽 인기처럼 검색어가 아니라 "코너 자체"를 열람하는 도구.
자사 실시간 목록이라 랭킹·신간 질문에는 웹 검색보다 이 도구가 가장 정확하다.

yes24_search와 마찬가지로 결과를 세션 state의 출처 레지스트리에 등록해 source_id를
부여하고, 인용에 쓸 수 있도록 반환 dict에 담는다. 실패는 예외를 밖으로 던지지 않고
구조화된 error dict로 반환한다(fail-loud).
"""

import logging
from datetime import datetime, timedelta, timezone

from google.adk.tools import ToolContext

from yes24_agent.config import get_settings
from yes24_agent.sources import register_source
from yes24_agent.tools.yes24_search import _get_client
from yes24_agent.yes24.client import Yes24FetchError
from yes24_agent.yes24.parsers import ParseError, parse_browse_list, product_fields
from yes24_agent.yes24.urls import BROWSE_SEED_URLS

logger = logging.getLogger(__name__)

# KST(UTC+9). checked_at 타임스탬프용.
_KST = timezone(timedelta(hours=9))


async def yes24_browse(section: str, tool_context: ToolContext) -> dict:
    """Yes24의 특정 코너(목록)를 직접 열람한다.

    검색어가 아니라 코너 전체를 랭킹·목록으로 보고 싶을 때 쓴다. 베스트셀러 순위,
    새로 나온 책, 크레마클럽 구독 인기처럼 "요즘 잘 나가는 책"류 질문에 적합하다.

    Args:
        section: 열람할 코너 코드. 셋 중 하나:
            "bestseller"(국내도서 베스트셀러 랭킹), "new"(새로 나온 국내도서),
            "cremaclub"(크레마클럽 eBook 구독 인기).

    Returns:
        성공 시 status="ok"와 section·section_label, results 목록(각 항목에 인용용
        source_id와 순위 rank 포함), 검색 시각 checked_at, result_count를 담은 dict.
        잘못된 section은 status="error", error_type="invalid_section". 그 외 실패는
        error_type("fetch"|"parse"). 모든 실패 응답은 result_count=0을 함께 담는다.
    """
    settings = get_settings()

    seed = BROWSE_SEED_URLS.get(section)
    if seed is None:
        valid = ", ".join(BROWSE_SEED_URLS)
        logger.info("yes24_browse section=%r status=error error_type=invalid_section", section)
        return {
            "status": "error",
            "error_type": "invalid_section",
            "message": f"유효한 섹션: {valid}",
            "result_count": 0,
        }

    client = _get_client(settings)

    try:
        html = await client.get_text(seed["url"])
    except Yes24FetchError as exc:
        logger.info("yes24_browse section=%r status=error error_type=fetch", section)
        return {
            "status": "error",
            "error_type": "fetch",
            "message": f"Yes24 코너 조회에 실패했습니다: {exc}",
            "result_count": 0,
        }

    try:
        parsed = parse_browse_list(
            html,
            base_url=settings.yes24_base_url,
            section=section,
            limit=settings.browse_result_limit,
        )
    except ParseError as exc:
        logger.info("yes24_browse section=%r status=error error_type=parse", section)
        return {
            "status": "error",
            "error_type": "parse",
            "message": f"코너 목록을 해석하지 못했습니다: {exc}",
            "result_count": 0,
        }

    checked_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M")

    results: list[dict] = []
    for item in parsed:
        # 검색·상세와 같은 필드 집합(_product_fields) + 이 도구 고유의 rank.
        fields = product_fields(item)
        source_id = register_source(
            tool_context.state,
            title=item["title"],
            url=item["url"],
            source_type="browse",
            snippet=item.get("author"),
            meta={**fields, "rank": item.get("rank")},
        )
        results.append(
            {
                "source_id": source_id,
                "type": "browse",
                "rank": item.get("rank"),
                "title": item["title"],
                "url": item["url"],
                **fields,
            }
        )

    logger.info("yes24_browse section=%r status=ok results=%d", section, len(results))
    return {
        "status": "ok",
        "section": section,
        "section_label": seed["label"],
        "results": results,
        "checked_at": checked_at,
        "result_count": len(results),
        # 코너/랭킹 조회라 목록이 있으면 그 자체가 답 — 재검색 불필요.
    }
