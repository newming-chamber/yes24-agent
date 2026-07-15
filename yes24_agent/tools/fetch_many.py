"""여러 Yes24 상세 페이지를 한 번에 여는 배치 열람 도구 — ADK FunctionTool.

배경(지연): 추천·비교에서 여러 후보의 상세(줄거리·목차·서평)를 확인하려면 지금까지 각 책을
yes24_fetch로 하나씩 열어야 했고, fetch마다 별도 LLM 왕복이 붙어 지연이 누적됐다(N권 → N턴).
fetch_many는 이 N개 열람을 **한 번의 도구 호출**로 받아 네트워크(get_text)만 asyncio.gather로
동시에 실행한다 — LLM 왕복이 1회로 줄고(핵심 win), 공유 Yes24Client의 Semaphore(http_concurrency)
+rps 페이싱이 Yes24 예의(동시성·요청률)를 그대로 지킨다.

정확성 설계(레이스 0): register_source(출처 등록·id 부여)는 동시에 돌리지 않는다. 네트워크만
gather로 동시 실행하고, 파싱·등록은 **순차 루프**(build_result_from_html)로 처리한다 — 단일
tool_context.state에 대한 등록이 await 없이 순차라 source_id가 유일·단조로 부여되고, 기존
_reconcile_sources 정합을 그대로 유지한다(병렬 도구 유실 방지 로직 불변).

부분 실패 fail-loud: 어떤 url이 실패해도(네트워크 오류) 나머지는 정상 결과로 반환하고, 실패분은
error 표식으로 섞어 반환한다 — 빈 성공으로 위장하지 않는다. 네트워크 오류는 특정 예외
(Yes24FetchError)만 per-item error로 처리하고, 파싱 실패는 build_result_from_html이 이미
구조화된 error dict로 돌려준다. 예상 밖 예외는 삼키지 않고 그대로 올려보낸다(broad except 금지).
"""

import asyncio
import logging

from google.adk.tools import ToolContext

from yes24_agent.config import get_settings
from yes24_agent.sources import now_checked_at
from yes24_agent.tools.yes24_fetch import build_result_from_html
from yes24_agent.tools.yes24_search import _get_client
from yes24_agent.yes24.client import Yes24FetchError

logger = logging.getLogger(__name__)



async def fetch_many(items: list[dict], tool_context: ToolContext) -> dict:
    """여러 Yes24 상세 페이지를 한 번에 열어 각 본문을 함께 가져온다.

    여러 책의 상세(줄거리·목차·서평)를 확인해야 할 때, 각 책을 yes24_fetch로 하나씩 여는 대신
    이 도구에 목록을 한 번에 넘긴다. 페이지들은 동시에 열려 여러 권을 열어도 한 번의 지연으로
    끝난다. 보통 yes24_search 결과의 url·제목을 그대로 담아 넘긴다(같은 책 제목을 다시 검색하지
    말 것 — 이미 url이 있다).

    Args:
        items: 열람할 페이지 목록. 각 항목은 {"url": 절대 URL(yes24_search 결과의 url을 그대로),
            "title": 제목(진행 상태 표시용, yes24_search 결과 제목)} 형태의 dict. 상한을 넘는
            항목은 처리하지 않는다.

    Returns:
        status="ok"와 results 목록을 담은 dict. results의 각 항목은 yes24_fetch와 동일한 형태다
        — 성공은 source_id·title·type(book_detail/notice)·본문(intro/toc/text 등), 실패는
        status="error"·error_type("fetch"|"parse"|"empty"|"invalid_url")·message. 성공 항목만
        인용 대상(source_id)이 된다. 상한을 넘겨 열지 않은 항목이 있으면 dropped_count·
        dropped_urls·message로 **무엇을 안 열었는지 명시**한다 — 그 책들이 필요하면 남은
        url로 한 번 더 호출한다(조용히 사라지지 않는다).
    """
    settings = get_settings()
    client = _get_client(settings)
    max_items = settings.fetch_many_max_items

    requested = list(items) if isinstance(items, list) else []

    # 계획 수립: 상한까지만 열고, 같은 url은 한 번만 연다(같은 페이지 → 같은 결과라 재요청은
    # Yes24 트래픽·컨텍스트 낭비). 상한 밖 항목은 **버리되 버렸다고 알린다**(fail-loud —
    # 조용한 드롭은 "안 열린 책"을 "없는 책"으로 오인하게 만든다).
    plan: list[tuple[dict, str | None]] = []
    seen_urls: set[str] = set()
    dropped_urls: list[str] = []
    duplicate_count = 0

    for item in requested:
        url = item.get("url") if isinstance(item, dict) else None
        if url and url in seen_urls:
            duplicate_count += 1
            continue
        if len(plan) >= max_items:
            dropped_urls.append(url or "(url 없음)")
            continue
        if url:
            seen_urls.add(url)
        plan.append((item, url))

    valid_urls = [url for _, url in plan if url]

    # 네트워크(get_text)만 동시 실행한다. return_exceptions=True로 개별 실패를 값으로 받아
    # 부분 실패를 fail-loud로 처리한다(등록은 아래 순차 루프에서 — 레이스 0).
    gathered = await asyncio.gather(
        *(client.get_text(url) for url in valid_urls), return_exceptions=True
    )
    gathered_iter = iter(gathered)

    results: list[dict] = []
    for _item, url in plan:
        if not url:
            results.append({
                "status": "error",
                "error_type": "invalid_url",
                "message": "item에 url이 없습니다",
            })
            continue
        outcome = next(gathered_iter)
        if isinstance(outcome, Yes24FetchError):
            logger.info("fetch_many url=%r status=error error_type=fetch", url)
            results.append({
                "status": "error",
                "error_type": "fetch",
                "url": url,
                "message": f"Yes24 페이지 조회에 실패했습니다: {outcome}",
            })
        elif isinstance(outcome, BaseException):
            # 예상 밖 예외는 삼키지 않는다(fail-loud) — 버그를 빈 성공으로 감추지 않는다.
            raise outcome
        else:
            # 순차 호출: register_source가 await 없이 하나씩 실행돼 id 원자·단조.
            results.append(build_result_from_html(outcome, url, settings, tool_context))

    checked_at = now_checked_at()
    ok = sum(1 for r in results if r.get("status") != "error")
    logger.info(
        "fetch_many requested=%d opened=%d ok=%d dropped=%d duplicate=%d",
        len(requested), len(results), ok, len(dropped_urls), duplicate_count,
    )

    response = {
        "status": "ok",
        "results": results,
        "checked_at": checked_at,
        "requested_count": len(requested),
        "result_count": len(results),
    }
    if dropped_urls:
        # 가법 필드: 드롭이 없으면 반환 형태는 기존과 동일하다.
        response["dropped_count"] = len(dropped_urls)
        response["dropped_urls"] = dropped_urls
        response["message"] = (
            f"한 번에 열 수 있는 상한({max_items}건)을 넘어 {len(dropped_urls)}건은 열지 "
            "않았습니다. 그 책들이 필요하면 남은 url로 한 번 더 호출하세요."
        )
    if duplicate_count:
        response["duplicate_count"] = duplicate_count
    return response
