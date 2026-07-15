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

**퍼플렉시티식 멀티쿼리 병렬 검색**: 이 도구는 한 번에 여러 검색 각도(queries)를 받아
asyncio.gather로 **동시에** 검색하고 결과를 합쳐 돌려준다. 복합·시의성·비교 질문을 하나의
좁은 쿼리로 뭉개는 대신 서로 다른 각도로 분해해 폭넓게 수집한 뒤(질문 분해 → 병렬 검색 →
원시 결과를 에이전트가 직접 종합) 답하기 위함이다. 이는 fetch_many가 여러 상세 열람을 한 번의
LLM 왕복으로 병렬화하는 것과 같은 구조 — 모델이 N번 도구를 나눠 호출하길 기대(비결정적)하는
대신, N개 각도를 한 리스트로 받아 코드가 병렬 실행을 보장한다. 단순·단일 각도 질문은 원소
하나짜리 리스트로 그대로 처리된다(단일 검색과 동일 지연).

정확성 설계(레이스 0): 네트워크(/search POST)만 동시 실행하고, 출처 등록(register_source·
id 부여)은 **순차 루프**로 처리한다 — 단일 tool_context.state에 대한 등록이 await 없이 순차라
source_id가 유일·단조로 부여된다(fetch_many와 동일 규약, 병렬 도구 유실 방지). 같은 url이 여러
각도에서 걸리면 한 번만 등록하고 어느 각도에서 나왔는지(queries)를 합쳐 교차 확증 신호로 남긴다.

각 결과를 세션 state의 출처 레지스트리에 등록해 source_id를 부여하고, 인용에 쓸 수 있도록
반환 dict에 담는다. 실패는 예외를 밖으로 던지지 않고 구조화된 error dict로 반환한다(부분 실패는
성공 결과와 함께 각 각도의 상태를 searches로 fail-loud 노출 — 빈 성공으로 위장하지 않는다).
"""

import asyncio
import logging

import httpx
from google.adk.tools import ToolContext

from yes24_agent.config import Settings, get_settings
from yes24_agent.sources import now_checked_at, register_source
from yes24_agent.tools.yes24_fetch import truncate

logger = logging.getLogger(__name__)


def _truncate_snippet(snippet: str | None, max_chars: int) -> str | None:
    """snippet을 max_chars로 절단한다(None이면 그대로) — 절단 로직은 yes24_fetch와 공유.

    절단 자체(상한에서 자르고 끝 공백 정리 후 표식)는 세 도구가 같아야 하므로 yes24_fetch의
    truncate를 그대로 쓰고, 여기서는 snippet이 None일 수 있다는 관용만 감싼다."""
    if not isinstance(snippet, str):
        return snippet
    return truncate(snippet, max_chars)

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


async def _search_one(
    query: str, client: httpx.AsyncClient, headers: dict, settings: Settings
) -> dict:
    """한 검색 각도(query)로 Perplexity /search를 호출해 **원시 결과만** 돌려준다(등록 없음).

    출처 등록(register_source)은 여기서 하지 않는다 — 여러 각도를 gather로 동시 실행할 때
    등록을 병렬로 돌리면 source_id 부여에 레이스가 생기므로, 네트워크만 여기서 하고 등록은
    호출부의 순차 루프에서 처리한다(레이스 0). 예상된 오류(전송·HTTP·JSON)만 잡아 구조화된
    error dict로 반환하고, 예상 밖 예외는 삼키지 않고 그대로 올려보낸다(fail-loud).

    반환: {"query", "status": "ok", "raw": [원시 item...]} 또는
          {"query", "status": "error", "error_type": "fetch", "message"}.
    """
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
            "query": query,
            "status": "error",
            "error_type": "fetch",
            "message": f"웹 검색 요청에 실패했습니다: {exc}",
        }
    return {"query": query, "status": "ok", "raw": data.get("results") or []}


async def web_search(queries: list[str], tool_context: ToolContext) -> dict:
    """웹에서 원시 검색 결과(제목·URL·스니펫) 목록을 가져온다(여러 각도를 한 번에 병렬 검색).

    Yes24로 답할 수 없는 외부·최신 정보(뉴스·스포츠·주가·날씨·시사·인물·상식 등)가
    필요할 때 쓴다. 각 결과의 snippet에는 해당 페이지에서 추출한 본문이 담기므로, 여러
    결과의 snippet을 종합해 그대로 답한다. snippet만으로 부족해 특정 페이지의 더 긴 전문이
    필요하면 그 url을 web_fetch에 넣어 읽는다. 각 결과의 last_updated(최종 갱신일)를 보고
    시의성 질문에선 최신 결과를 우선한다. 가격·재고·구매 링크 같은 상품 정보를 얻는 용도가
    아니다(그것은 Yes24 검색으로). 잡담이나 Yes24로 충분한 질문엔 쓰지 않는다.

    **여러 각도를 한 번에**: 복합·시의성·비교 질문은 하나의 좁은 쿼리로 뭉개지 말고, 서로
    다른 각도의 검색어 여러 개를 queries 리스트에 담아 한 번에 넘긴다 — 각도들은 동시에 검색돼
    한 번의 지연으로 폭넓게 수집되고, 에이전트가 그 원시 결과를 교차 종합한다(퍼플렉시티식
    질문 분해 → 병렬 검색 → 직접 종합). 예: "월드컵 4강 결과와 다음 상대·일정" →
    ["월드컵 4강 경기 결과", "대한민국 대표팀 다음 경기 상대", "월드컵 남은 경기 일정"].
    단순·단일 각도 질문은 원소 하나짜리 리스트로 넘기면 된다(예: ["삼성전자 주가"]).

    Args:
        queries: 검색 각도(검색어) 리스트. 각 원소는 알고 싶은 내용을 담은 자연스러운
            질문·키워드다. 복합 질문은 3~4개의 서로 다른 각도로 분해해 담고(상한을 넘는
            각도는 dropped_queries로 알린 뒤 처리에서 제외), 단순 질문은 원소 하나만 담는다.

    Returns:
        성공 시 status="ok"와 results 목록(모든 각도의 결과를 url 기준으로 병합·중복제거,
        각 결과에 인용용 source_id·title·url·snippet·last_updated와 어느 각도에서 나왔는지
        queries), 각 각도의 성공/실패를 담은 searches 요약, 검색 시각 checked_at을 담은 dict.
        snippet은 스니펫이 아니라 페이지 콘텐츠(추출 본문)라 그 자체로 종합 재료가 된다.
        상한을 넘겨 검색하지 않은 각도가 있으면 dropped_count·dropped_queries로 명시한다.
        성공·실패 모두 result_count를 함께 담는다. 모든 각도가 실패했을 때만 status="error"와
        error_type("not_configured"|"fetch"), message에 더해 result_count=0을 담은 dict.
    """
    settings = get_settings()

    if not settings.perplexity_api_key:
        logger.info("web_search status=error error_type=not_configured")
        return {
            "status": "error",
            "error_type": "not_configured",
            "message": "웹 검색이 설정되지 않았습니다",
            "result_count": 0,
        }

    # 각도 계획: 문자열이 아니거나 빈 각도는 버리고, 같은 각도는 한 번만(중복 검색은 벤더
    # 트래픽·컨텍스트 낭비), 상한까지만 검색한다. 단일 문자열로 잘못 넘어와도 관용 처리한다.
    if isinstance(queries, str):
        queries = [queries]
    requested = [q.strip() for q in queries if isinstance(q, str) and q.strip()] \
        if isinstance(queries, list) else []

    planned: list[str] = []
    seen_queries: set[str] = set()
    dropped_queries: list[str] = []
    for q in requested:
        if q in seen_queries:
            continue
        if len(planned) >= settings.web_search_max_queries:
            dropped_queries.append(q)
            continue
        seen_queries.add(q)
        planned.append(q)

    if not planned:
        # 유효한 검색 각도가 하나도 없다 — 빈 성공으로 위장하지 않고 명시적 실패.
        logger.info("web_search status=error error_type=empty_query")
        return {
            "status": "error",
            "error_type": "empty_query",
            "message": "검색할 유효한 검색어가 없습니다",
            "result_count": 0,
        }

    client = _get_client(settings)
    # 퍼플렉시티는 Bearer 헤더 인증(바디 api_key 아님). 헤더는 요청마다 넘겨 공유 클라이언트를
    # 인증 중립으로 유지한다. snippet 콘텐츠 분량은 토큰 예산으로 조절(snippet이 종합 재료).
    headers = {"Authorization": f"Bearer {settings.perplexity_api_key}"}

    # 네트워크(/search POST)만 동시 실행한다(각도별 병렬). 등록은 아래 순차 루프에서 — 레이스 0.
    # _search_one이 예상 오류를 이미 error dict로 삼키므로 예상 밖 예외만 gather 밖으로 올라온다.
    searched = await asyncio.gather(
        *(_search_one(q, client, headers, settings) for q in planned)
    )

    checked_at = now_checked_at()

    results: list[dict] = []
    url_to_index: dict[str, int] = {}  # url → results 인덱스(각도 간 중복제거·교차확증 병합용)
    searches: list[dict] = []  # 각도별 성공/실패 요약(부분 실패 fail-loud)
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
        for item in outcome["raw"]:
            url = item.get("url")
            if not url:
                continue
            matched += 1
            existing = url_to_index.get(url)
            if existing is not None:
                # 같은 url이 다른 각도에서도 걸렸다 — 재등록하지 않고 어느 각도에서 나왔는지만
                # 합쳐 교차 확증 신호로 남긴다(source_id 중복 방지).
                if query not in results[existing]["queries"]:
                    results[existing]["queries"].append(query)
                continue
            title = item.get("title") or url
            # snippet 로컬 하드 상한(벤더 토큰 예산 초과분 방어). 등록·반환 모두 절단본으로
            # 통일해 세션 출처와 도구 결과의 snippet이 어긋나지 않게 한다.
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
            url_to_index[url] = len(results)
            results.append(
                {
                    "source_id": source_id,
                    "type": "web",
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "last_updated": last_updated,
                    "queries": [query],
                }
            )
        searches.append({"query": query, "status": "ok", "result_count": matched})

    ok_count = sum(1 for s in searches if s["status"] == "ok")
    if ok_count == 0:
        # 모든 각도가 실패 — 단일 각도 실패의 기존 계약(status=error·error_type=fetch·
        # result_count=0)을 그대로 유지해, 에이전트가 "못 찾음"이 아니라 일시 오류로 처리하게 한다.
        logger.info("web_search queries=%d status=error error_type=fetch", len(planned))
        return {
            "status": "error",
            "error_type": "fetch",
            "message": searched[0]["message"],
            "result_count": 0,
        }

    logger.info(
        "web_search queries=%d angles_ok=%d results=%d dropped=%d",
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
    if dropped_queries:
        # 가법 필드: 드롭이 없으면 반환 형태는 단일/다중 각도 모두 이 키가 없다.
        response["dropped_count"] = len(dropped_queries)
        response["dropped_queries"] = dropped_queries
        response["message"] = (
            f"한 번에 검색할 수 있는 각도 상한({settings.web_search_max_queries}개)을 넘어 "
            f"{len(dropped_queries)}개 각도는 검색하지 않았습니다. "
            "필요하면 남은 각도로 한 번 더 호출하세요."
        )
    return response
