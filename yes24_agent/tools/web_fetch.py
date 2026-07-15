"""웹 페이지 열람 도구 — ADK FunctionTool로 노출되는 async 함수(Tavily /extract 호출).

web_search가 준 스니펫만으로 부족할 때, 특정 외부 페이지의 전문을 읽어오는 도구다.
Tavily /extract가 대신 페이지를 가져오므로(우리가 직접 요청하지 않음) SSRF·도메인 화이트리스트
검증이 필요 없다 — url이 http(s)인지만 확인한다. yes24_fetch(Yes24 전용, 도메인 검증 있음)와
별개다.

읽어온 본문을 세션 state의 출처 레지스트리에 등록해 source_id를 부여하고, 인용에 쓸 수 있도록
반환 dict에 담는다. 실패는 예외를 밖으로 던지지 않고 구조화된 error dict로 반환한다.
"""

import logging
from urllib.parse import urlparse

import httpx
from google.adk.tools import ToolContext

from yes24_agent.config import get_settings
from yes24_agent.sources import now_checked_at, register_source
from yes24_agent.tools.web_search import _get_client
from yes24_agent.tools.yes24_fetch import window_around_find

logger = logging.getLogger(__name__)


# 절단·창 선택은 yes24_fetch의 함수를 그대로 쓴다(자사·외부 페이지가 같은 절단 계약).


async def web_fetch(url: str, tool_context: ToolContext, find: str | None = None) -> dict:
    """외부 웹 페이지의 전문을 읽어온다.

    web_search 결과의 스니펫만으로 부족할 때, 특정 페이지 url을 넣어 본문 전체를 확보한다.
    보통 web_search가 반환한 결과의 url을 그대로 전달한다. Yes24 상품·정책 페이지는 이 도구가
    아니라 yes24_fetch로 읽는다.

    Args:
        url: 읽을 외부 페이지의 절대 URL(http/https). web_search 결과의 url을 그대로 넣는다.
        find: (선택) 본문에서 찾는 정보의 핵심 키워드. 긴 페이지는 앞부분만 잘려 오는데
            (truncated=True), 찾는 내용이 그 안에 없으면 이 키워드로 다시 호출하면
            **키워드가 나오는 위치부터** 본문을 잘라 돌려준다(yes24_fetch와 동일).

    Returns:
        성공 시 status="ok"와 인용용 source_id, title, text(본문, 상한 초과 시 절단),
        type="web", checked_at을 담은 dict. 본문이 상한보다 길어 잘렸으면 truncated=True와
        total_chars(전체 길이)가 함께 온다 — 찾는 내용이 안 보이면 find 키워드로 재호출해
        뒷부분을 읽는다. 실패 시 status="error"와 error_type
        ("invalid_url"|"not_configured"|"empty"|"fetch"), message를 담은 dict.
    """
    settings = get_settings()

    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        logger.info("web_fetch url=%r status=error error_type=invalid_url", url)
        return {
            "status": "error",
            "error_type": "invalid_url",
            "message": "http/https URL만 열람할 수 있습니다",
        }

    if not settings.tavily_api_key:
        logger.info("web_fetch url=%r status=error error_type=not_configured", url)
        return {
            "status": "error",
            "error_type": "not_configured",
            "message": "웹 열람이 설정되지 않았습니다",
        }

    client = _get_client(settings)
    payload = {"api_key": settings.tavily_api_key, "urls": [url]}

    try:
        response = await client.post(settings.tavily_extract_url, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"응답이 JSON 객체가 아닙니다: {type(data).__name__}")
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("web_fetch url=%r status=error error_type=fetch", url)
        return {
            "status": "error",
            "error_type": "fetch",
            "message": f"웹 페이지 열람에 실패했습니다: {exc}",
        }

    results = data.get("results") or []
    raw_content = results[0].get("raw_content") if results else None
    if not raw_content:
        # 추출 실패(failed_results)거나 본문이 비어 있음 — 빈 성공 위장 금지.
        logger.info("web_fetch url=%r status=error error_type=empty", url)
        return {
            "status": "error",
            "error_type": "empty",
            "message": "이 페이지에서 읽을 수 있는 본문을 추출하지 못했습니다",
        }

    title = results[0].get("title") or url
    total_chars = len(raw_content)
    # yes24_fetch와 **같은 절단 계약**: 잘랐으면 잘랐다고 알리고(truncated·total_chars),
    # find로 뒷부분을 다시 읽을 수 있게 한다(도구마다 다른 규칙 금지).
    text, find_found = window_around_find(
        raw_content, settings.web_fetch_max_chars, find, settings.web_fetch_find_lead_chars
    )
    checked_at = now_checked_at()

    source_id = register_source(
        tool_context.state,
        title=title,
        url=url,
        source_type="web",
        snippet=None,
    )

    logger.info(
        "web_fetch url=%r status=ok chars=%d total=%d find=%r",
        url, len(text), total_chars, find,
    )
    result = {
        "status": "ok",
        "source_id": source_id,
        "type": "web",
        "title": title,
        "url": url,
        "text": text,
        "checked_at": checked_at,
    }
    if total_chars > settings.web_fetch_max_chars:
        # 가법 필드: 잘리지 않은 페이지의 반환 형태는 기존과 동일하다.
        result["truncated"] = True
        result["total_chars"] = total_chars
    if find:
        result["find_found"] = find_found
    return result
