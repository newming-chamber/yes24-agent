"""웹 검색 도구 — ADK FunctionTool로 노출되는 async 함수(Perplexity /search 직접 호출).

**역할 분리**: 이 도구는 Yes24 밖의 외부·최신 정보(뉴스·스포츠·주가·시사·인물·상식 등)를
확보하기 위한 것이다. 가격·구매·재고 등 상품 정보의 근거로는 쓰지 않는다 — 그것은
여전히 yes24_search/yes24_fetch(Yes24 출처)만 담당한다.

Perplexity /search는 요약(answer)이 아니라 **원시 검색 결과**(제목·URL·스니펫)를 준다.
각 결과의 snippet 필드에 페이지 콘텐츠(추출 본문, max_tokens_per_page로 분량 조절)가
직접 담기므로 Tavily의 snippet/raw_content 이원 구조가 단일 필드로 통합된다 — snippet이
곧 종합 재료다. 에이전트가 여러 결과를 직접 종합해 답하며, snippet보다 더 긴 전문이
필요하면 그 url을 web_fetch(Tavily /extract)로 읽는다. 각 결과에는 신선도 신호 last_updated를
함께 실어 시의성 질문에서 최신 우선에 쓸 수 있게 한다.

각 결과를 세션 state의 출처 레지스트리에 등록해 source_id를 부여하고, 인용에 쓸 수 있도록
반환 dict에 담는다. 실패는 예외를 밖으로 던지지 않고 구조화된 error dict로 반환한다.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from google.adk.tools import ToolContext

from yes24_agent.config import Settings, get_settings
from yes24_agent.sources import register_source
from yes24_agent.tools._followup import needs_search_followup

logger = logging.getLogger(__name__)

# KST(UTC+9). checked_at 타임스탬프용.
_KST = timezone(timedelta(hours=9))

# 절단 표시 접미사(web_fetch·yes24_fetch와 동일 표기 — 세 도구의 절단 신호를 일치시킨다).
_TRUNCATION_SUFFIX = "…(이하 생략)"


def _truncate_snippet(snippet: str | None, max_chars: int) -> str | None:
    """snippet을 max_chars로 절단하고 절단 표식을 붙인다(상한 이하·None이면 그대로).

    snippet은 없을 수(None) 있으므로 문자열일 때만 절단한다. 절단은 fetch 계열 _truncate와
    같은 방식(문자 상한에서 자르고 끝 공백 정리 후 표식)으로, 인용·source_id·title 등 메타는
    건드리지 않고 본문(snippet)만 줄인다.
    """
    if not isinstance(snippet, str) or len(snippet) <= max_chars:
        return snippet
    return snippet[:max_chars].rstrip() + _TRUNCATION_SUFFIX

# Yes24 클라이언트와 별개인 범용 HTTP 클라이언트(도메인 제약 없음). 모듈 lazy 싱글턴.
# web_fetch도 이 클라이언트를 재사용한다(둘 다 외부 API 호출 — aclose 일원화). 두 도구가
# 서로 다른 인증(퍼플렉시티 Bearer 헤더 vs Tavily 바디 api_key)을 쓰므로, 클라이언트에는
# 기본 인증 헤더를 두지 않고 각 요청에서 헤더를 넘겨 키가 다른 API로 새지 않게 한다.
_shared_client: httpx.AsyncClient | None = None


def _get_client(settings: Settings) -> httpx.AsyncClient:
    """외부 API 호출용 공유 httpx 클라이언트 싱글턴을 반환한다(최초 호출 시 생성)."""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=settings.web_search_timeout_s)
    return _shared_client


async def aclose_shared_client() -> None:
    """웹 검색·열람 공유 클라이언트를 정리한다(서버 shutdown 훅용). 미생성 상태면 무동작."""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


async def web_search(query: str, tool_context: ToolContext) -> dict:
    """웹에서 원시 검색 결과(제목·URL·스니펫) 목록을 가져온다.

    Yes24로 답할 수 없는 외부·최신 정보(뉴스·스포츠·주가·날씨·시사·인물·상식 등)가
    필요할 때 쓴다. 각 결과의 snippet에는 해당 페이지에서 추출한 본문이 담기므로, 여러
    결과의 snippet을 종합해 그대로 답한다. snippet만으로 부족해 특정 페이지의 더 긴 전문이
    필요하면 그 url을 web_fetch에 넣어 읽는다. 각 결과의 last_updated(최종 갱신일)를 보고
    시의성 질문에선 최신 결과를 우선한다. 가격·재고·구매 링크 같은 상품 정보를 얻는 용도가
    아니다(그것은 Yes24 검색으로). 잡담이나 Yes24로 충분한 질문엔 쓰지 않는다.

    Args:
        query: 검색어. 알고 싶은 내용을 자연스러운 질문·키워드로 구성한다
            (예: "월드컵 한국 최근 경기 결과", "삼성전자 주가").

    Returns:
        성공 시 status="ok"와 results 목록(각 결과에 인용용 source_id·title·url·snippet·
        last_updated), 검색 시각 checked_at을 담은 dict. snippet은 스니펫이 아니라 페이지
        콘텐츠(추출 본문)라 그 자체로 종합 재료가 된다. 결과가 없으면 results가 빈 목록.
        성공·실패 모두 충분성 힌트(result_count·needs_followup)를 함께 담는다. 실패 시
        status="error"와 error_type("not_configured"|"fetch"), message에 더해
        result_count=0·needs_followup=True(재시도 신호)를 담은 dict.
    """
    settings = get_settings()

    if not settings.perplexity_api_key:
        logger.info("web_search query=%r status=error error_type=not_configured", query)
        return {
            "status": "error",
            "error_type": "not_configured",
            "message": "웹 검색이 설정되지 않았습니다",
            "result_count": 0,
            "needs_followup": True,
        }

    client = _get_client(settings)
    # 퍼플렉시티는 Bearer 헤더 인증(바디 api_key 아님). 헤더는 요청마다 넘겨 공유 클라이언트를
    # 인증 중립으로 유지한다. snippet 콘텐츠 분량은 토큰 예산으로 조절(snippet이 종합 재료).
    headers = {"Authorization": f"Bearer {settings.perplexity_api_key}"}
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
            # 유효 JSON이지만 객체가 아닌 본문(배열·null·스칼라)이면 data.get()이
            # AttributeError로 도구 밖 탈출한다 — 여기서 막아 fetch 에러로 처리.
            raise ValueError(f"응답이 JSON 객체가 아닙니다: {type(data).__name__}")
    except (httpx.HTTPError, ValueError) as exc:
        # httpx.HTTPError: 타임아웃·전송 오류·raise_for_status의 HTTPStatusError 포함.
        # ValueError: 응답이 JSON이 아니거나(response.json()) JSON 객체가 아닐 때.
        logger.info("web_search query=%r status=error error_type=fetch", query)
        return {
            "status": "error",
            "error_type": "fetch",
            "message": f"웹 검색 요청에 실패했습니다: {exc}",
            "result_count": 0,
            "needs_followup": True,
        }

    checked_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M")

    results: list[dict] = []
    for item in data.get("results") or []:
        url = item.get("url")
        if not url:
            continue
        title = item.get("title") or url
        # snippet 로컬 하드 상한(벤더 토큰 예산 초과분 방어). 등록·반환 모두 절단본으로 통일해
        # 세션 출처와 도구 결과의 snippet이 어긋나지 않게 한다.
        snippet = _truncate_snippet(
            item.get("snippet"), settings.web_search_snippet_max_chars
        )
        # 최종 갱신일(신선도 신호). 없으면 발행일(date)로 보완, 둘 다 없으면 None.
        last_updated = item.get("last_updated") or item.get("date")
        source_id = register_source(
            tool_context.state,
            title=title,
            url=url,
            source_type="web",
            snippet=snippet,
        )
        results.append(
            {
                "source_id": source_id,
                "type": "web",
                "title": title,
                "url": url,
                "snippet": snippet,
                "last_updated": last_updated,
            }
        )

    # 관련성 근거 텍스트는 제목+스니펫(스니펫이 없으면 제목만) — 웹은 제목이 짧아
    # 스니펫까지 봐야 핵심 토큰 커버를 제대로 판정한다.
    followup_texts = [f"{r['title']} {r.get('snippet') or ''}" for r in results]
    needs_followup = needs_search_followup(query, followup_texts, len(results))
    logger.info("web_search query=%r status=ok results=%d", query, len(results))
    return {
        "status": "ok",
        "query": query,
        "results": results,
        "checked_at": checked_at,
        "result_count": len(results),
        "needs_followup": needs_followup,
    }
