"""Yes24 페이지 열람 도구 — ADK FunctionTool로 노출되는 async 함수.

에이전트가 검색만으로 부족한 상세 내용(줄거리·목차·출판사 서평·주간리뷰)이나 공지
페이지 본문을 읽어야 할 때 호출한다. yes24_search와 동일하게 결과를 세션 state의
출처 레지스트리에 등록해 source_id를 부여하고, 인용에 쓸 수 있도록 반환 dict에 담는다.

실패는 예외를 밖으로 던지지 않고 구조화된 error dict로 반환한다(fail-loud). 특히
본문이 상용구뿐인(이미지 배너 위주) 페이지는 "빈 성공"으로 위장하지 않고
error_type="empty"로 정직하게 반환한다.
"""

import logging
from typing import NamedTuple

from bs4 import BeautifulSoup
from google.adk.tools import ToolContext

from yes24_agent.config import get_settings
from yes24_agent.sources import now_checked_at, register_source
from yes24_agent.tools.yes24_search import _get_client
from yes24_agent.yes24.client import Yes24FetchError
from yes24_agent.yes24.parsers import ParseError, extract_links, parse_product, product_fields

logger = logging.getLogger(__name__)


# 상품 상세 페이지 경로 식별자.
_GOODS_PATH = "/product/goods/"

# 범용 텍스트 추출 시 제거할 태그(스크립트·스타일 등 비본문).
_NOISE_TAGS = ("script", "style", "noscript", "template")

# 실질 본문 판정 임계값은 config(fetch_min_meaningful_chars)에서 주입한다.

# 절단 표시 접미사·중간 시작 표시 접두사.
TRUNCATION_SUFFIX = "…(이하 생략)"
OMITTED_PREFIX = "(앞부분 생략)… "


def truncate(text: str, max_chars: int) -> str:
    """text를 max_chars로 절단하고 절단 표시를 붙인다. 이미 짧으면 그대로 반환."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + TRUNCATION_SUFFIX


def window_around_find(
    text: str, max_chars: int, find: str | None, lead_chars: int
) -> tuple[str, bool]:
    """본문에서 반환할 창을 고른다. 반환: (창 텍스트, find 발견 여부).

    find가 없거나 본문이 상한 이내면 앞에서부터 자른다. find가 있고 상한 밖 위치에서
    발견되면 그 위치 lead_chars 앞에서 시작하는 창을 잘라 키워드 앞 맥락(소제목·조건
    문장)이 함께 담기게 한다. 못 찾으면 앞부분 창으로 폴백한다.
    """
    if not find or len(text) <= max_chars:
        return truncate(text, max_chars), bool(find) and find.lower() in text.lower()

    pos = text.lower().find(find.lower())
    if pos < 0:
        return truncate(text, max_chars), False
    if pos < max_chars:
        return truncate(text, max_chars), True

    start = max(0, pos - lead_chars)
    window = text[start : start + max_chars].strip()
    prefix = OMITTED_PREFIX if start > 0 else ""
    suffix = TRUNCATION_SUFFIX if start + max_chars < len(text) else ""
    return f"{prefix}{window}{suffix}", True


# 외부 페이지 열람(web_fetch)도 이 두 함수를 그대로 import해 **같은 절단 계약**을 갖는다
# (도구마다 다른 절단 규칙을 배우게 하지 않는다). 주 소비자가 여기라 여기 둔다.


async def yes24_fetch(
    url: str, title: str, tool_context: ToolContext, find: str | None = None
) -> dict:
    """Yes24 페이지의 본문을 열람한다.

    검색 결과만으로 부족할 때, 상품 상세(줄거리·목차·출판사 서평·주간리뷰)나 공지
    페이지의 실제 본문을 읽어오는 도구다. 보통 yes24_search가 반환한 결과의 url을
    그대로 넣어 그 책의 상세 내용을 확인할 때 쓴다.

    Args:
        url: 열람할 Yes24 페이지의 절대 URL. 상품 상세를 보려면 yes24_search 결과의
            url을 그대로 전달한다. 정책·주문·결제·배송 관련은 공지사항 URL을 넣는다.
        title: 열람 대상의 제목(진행 상태 표시용). yes24_search 결과의 제목을 그대로
            넣는다. 공지 등 제목을 모르면 페이지 성격을 짧게 적는다(예: "공지사항").
        find: (선택) 본문에서 찾는 정보의 핵심 키워드(예: "무이자"). 긴 페이지는 앞부분만
            잘려 오는데(truncated=True), 찾는 내용이 그 안에 없으면 이 키워드로 다시
            호출하면 **키워드가 나오는 위치부터** 본문을 잘라 돌려준다. FAQ처럼 여러
            주제가 한 페이지에 있는 긴 문서에서 특정 규정을 찾을 때 쓴다.

    Returns:
        성공 시 status="ok"와 인용용 source_id, 본문 내용을 담은 dict. 상품 상세는
        type="book_detail"로 줄거리·목차 등을, 공지 페이지는 type="notice"로 text를
        담는다. 본문이 상한보다 길어 잘렸으면 truncated=True와 total_chars(전체 길이)가
        함께 온다 — 찾는 내용이 안 보이면 find 키워드로 재호출해 뒷부분을 읽는다.
        함께 오는 links는 이 페이지에서 더 볼 수 있는 다른 Yes24 페이지 후보
        목록이다(아직 열지 않은 페이지 — 인용 대상이 아니며, 필요하면 그 url로 다시
        yes24_fetch를 호출해 이어서 열람할 수 있다). 실패 시 status="error"와
        error_type("fetch"|"parse"|"empty"), message.
    """
    # title은 runner의 진행 상태 라벨용으로만 쓴다. 반환 dict의 title은 항상 페이지
    # 파싱 결과를 우선하며, LLM이 준 이 값으로 덮어쓰지 않는다.
    del title

    settings = get_settings()
    client = _get_client(settings)

    try:
        html = await client.get_text(url)
    except Yes24FetchError as exc:
        logger.info("yes24_fetch url=%r status=error error_type=fetch", url)
        return {
            "status": "error",
            "error_type": "fetch",
            "message": f"Yes24 페이지 조회에 실패했습니다: {exc}",
        }

    return build_result_from_html(html, url, settings, tool_context, find=find)


def build_result_from_html(
    html: str, url: str, settings, tool_context: ToolContext, find: str | None = None
) -> dict:
    """이미 받아온 HTML을 파싱해 fetch 결과 dict를 조립한다(get_text 이후 전 로직).

    yes24_fetch(단건, 네트워크는 위에서)와 fetch_many(다건, 네트워크는 gather로 선행)가
    공유하는 순수 파싱·등록 계층이다. register_source(출처 등록)는 이 함수 안에서 이뤄지므로,
    fetch_many는 이 함수를 **순차 루프**로만 호출해 출처 id의 원자성·단조성을 지킨다.
    find는 범용 어포던스로, 상세(book_detail)·공지(notice) 양쪽에 적용된다 — 상세에서는
    키워드를 포함한 블록을 예산 우선순위 앞으로 당기고, 공지에서는 키워드 주변 창을 잘라준다.
    """
    checked_at = now_checked_at()

    links = extract_links(
        html,
        base_url=settings.yes24_base_url,
        limit=settings.fetch_links_limit,
        page_url=url,
        # client가 거절할 수집 금지 경로는 애초에 후보로 내놓지 않는다(같은 규칙 주입).
        disallowed_paths=tuple(settings.yes24_disallowed_paths),
    )

    # 경로 판별은 대소문자 무시 — Yes24가 상품 링크를 /Product/Goods/(대문자)로도
    # 내보내며(크레마클럽 목록 등), 링크 팔로우로 그런 url이 오면 상세로 인식돼야 한다.
    if _GOODS_PATH in url.lower():
        return _fetch_product(
            html,
            url,
            checked_at,
            settings.fetch_max_chars,
            settings.fetch_find_lead_chars,
            links,
            tool_context,
            find=find,
        )
    return _fetch_generic(
        html,
        url,
        checked_at,
        settings.fetch_max_chars,
        settings.fetch_min_meaningful_chars,
        settings.fetch_find_lead_chars,
        links,
        tool_context,
        find=find,
    )


def _fetch_product(
    html: str,
    url: str,
    checked_at: str,
    max_chars: int,
    lead_chars: int,
    links: list[dict],
    tool_context: ToolContext,
    find: str | None = None,
) -> dict:
    """상품 상세 페이지를 파싱해 book_detail 결과를 조립한다.

    상세 본문(줄거리·목차·서평)을 합쳐 max_chars 예산으로 담되, 예산을 넘으면
    notice와 동일하게 truncated=True·total_chars를 가법으로 명시한다("짧은 상세"로
    위장 금지). find 키워드가 주어지면 그 키워드를 포함한 블록을 예산 우선순위 앞으로
    당겨(그리고 그 블록 안에서 키워드 주변 창으로) 잘린 뒤쪽 블록의 내용도 한 번의
    재호출로 읽히게 한다.
    """
    try:
        product = parse_product(html, base_url=get_settings().yes24_base_url)
    except ParseError as exc:
        logger.info("yes24_fetch url=%r status=error error_type=parse", url)
        return {
            "status": "error",
            "error_type": "parse",
            "message": f"상품 상세를 해석하지 못했습니다: {exc}",
        }

    intro, toc, pub_review, weekly_reviews, trunc = _truncate_detail_blocks(
        product.get("intro"),
        product.get("toc"),
        product.get("pub_review"),
        product.get("weekly_reviews") or [],
        max_chars,
        find=find,
        lead_chars=lead_chars,
    )

    # parse_product는 title이 None이 아님을 보장하지 않으므로 인용 라벨용 방어값을 둔다.
    title = product.get("title") or "제목 미상"

    # 검색·브라우즈와 같은 필드 집합(_product_fields) — 상세만 연 턴에서도 게이트가 대조할
    # 접지 필드(publisher·rating·price·pub_status…)를 빠짐없이 싣는다.
    fields = product_fields(product)
    source_id = register_source(
        tool_context.state,
        title=title,
        url=url,
        source_type="book_detail",
        snippet=intro,
        meta=fields,
    )

    logger.info(
        "yes24_fetch url=%r status=ok type=book_detail total=%d truncated=%s find=%r",
        url, trunc.total_chars, trunc.truncated, find,
    )
    detail = {
        "status": "ok",
        "source_id": source_id,
        "title": title,
        "url": url,
        "type": "book_detail",
        **fields,
        "is_ebook": product.get("is_ebook"),
        "intro": intro,
        "toc": toc,
        "pub_review": pub_review,
        "weekly_reviews": weekly_reviews,
        "links": links,
        "checked_at": checked_at,
    }
    if trunc.truncated:
        # 가법 필드: 잘리지 않은 상세의 반환 형태는 기존과 동일하다.
        detail["truncated"] = True
        detail["total_chars"] = trunc.total_chars
    if find:
        detail["find_found"] = trunc.find_found
    return detail


def _fetch_generic(
    html: str,
    url: str,
    checked_at: str,
    max_chars: int,
    min_meaningful_chars: int,
    lead_chars: int,
    links: list[dict],
    tool_context: ToolContext,
    find: str | None = None,
) -> dict:
    """공지 등 비상품 페이지에서 범용 본문 텍스트를 추출한다.

    본문이 max_chars보다 길면 잘라 담되 **truncated=True·total_chars를 명시**해
    "짧은 페이지였음"으로 위장하지 않는다(빈 성공 위장 금지와 같은 정신 — 실측:
    FAQ 결제정보 페이지 13.7K자에서 무이자 규정이 6K 상한 밖에 있어 답이 유실됐다).
    find 키워드가 주어지면 그 첫 등장 위치 조금 앞에서부터 창을 잘라, 에이전트가
    잘린 뒷부분의 특정 정보를 추가 fetch 한 번으로 읽을 수 있게 한다.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    title = _text_or_none(soup.title) or url
    body = soup.body if soup.body is not None else soup
    text = _normalize_whitespace(body.get_text(" ", strip=True))

    if len(text) < min_meaningful_chars:
        logger.info("yes24_fetch url=%r status=error error_type=empty chars=%d", url, len(text))
        return {
            "status": "error",
            "error_type": "empty",
            "message": (
                "이 페이지에서 읽을 수 있는 본문을 찾지 못했습니다 "
                "(이미지 배너 위주이거나 별도 로딩되는 내용일 수 있음)."
            ),
        }

    total_chars = len(text)
    window, find_found = window_around_find(text, max_chars, find, lead_chars)

    source_id = register_source(
        tool_context.state,
        title=title,
        url=url,
        source_type="notice",
        snippet=None,
        meta=None,
    )

    logger.info(
        "yes24_fetch url=%r status=ok type=notice chars=%d total=%d find=%r",
        url, len(window), total_chars, find,
    )
    result = {
        "status": "ok",
        "source_id": source_id,
        "title": title,
        "url": url,
        "type": "notice",
        "text": window,
        "links": links,
        "checked_at": checked_at,
    }
    if total_chars > max_chars:
        # 가법 필드: 잘리지 않은 페이지의 반환 형태는 기존과 동일하다.
        result["truncated"] = True
        result["total_chars"] = total_chars
    if find:
        result["find_found"] = find_found
    return result


class _DetailTrunc(NamedTuple):
    """상세 블록 절단 결과 메타 — 반환 dict에 가법으로 실릴 값."""

    truncated: bool
    total_chars: int
    find_found: bool


# 상세 블록의 기본 예산 우선순위(위에서부터 채운다).
_DETAIL_BLOCK_ORDER = ("intro", "toc", "pub_review", "weekly")


def _truncate_detail_blocks(
    intro: str | None,
    toc: str | None,
    pub_review: str | None,
    weekly_reviews: list[str],
    max_chars: int,
    find: str | None = None,
    lead_chars: int = 0,
) -> tuple[str | None, str | None, str | None, list[str], _DetailTrunc]:
    """상세 텍스트 블록 합계가 max_chars를 넘으면 우선순위 순으로 담다가 절단한다.

    기본 우선순위: intro → toc → pub_review → weekly_reviews. 예산을 초과하는 블록은
    그 블록 안에서 절단(절단 표시 부착)하고, 이후 블록은 버린다. find가 주어지면 그
    키워드를 포함한 블록을 **예산 우선순위 앞으로 당겨**(안정 정렬 — 동순위는 기본 순서
    유지) 잘려나가지 않게 하고, 그 블록이 예산을 넘으면 키워드 주변 창으로 잘라준다.
    합계가 상한을 넘었는지(truncated)·전체 길이·find 발견 여부를 메타로 함께 돌려준다.
    """
    # (kind, text) 배열로 펼친다 — weekly는 개별 리뷰 항목으로 나열해 각자 예산 경쟁.
    named: list[tuple[str, str]] = [
        (kind, text)
        for kind, text in (("intro", intro), ("toc", toc), ("pub_review", pub_review))
        if text
    ]
    named += [("weekly", r) for r in weekly_reviews if r]

    total_chars = sum(len(text) for _, text in named)
    find_lower = find.lower() if find else None
    find_found = bool(find_lower) and any(find_lower in text.lower() for _, text in named)

    order = {kind: i for i, kind in enumerate(_DETAIL_BLOCK_ORDER)}
    if find_lower:
        # 안정 정렬: 키워드 포함 블록(0)을 미포함(1)보다 앞으로. 그 안에서는 기본 순서 유지.
        named.sort(key=lambda kt: (0 if find_lower in kt[1].lower() else 1, order[kt[0]]))

    intro_out = toc_out = pub_review_out = None
    weekly_out: list[str] = []
    remaining = max_chars
    for kind, text in named:
        taken, remaining = _take_block(text, remaining, find, lead_chars)
        if kind == "intro":
            intro_out = taken
        elif kind == "toc":
            toc_out = taken
        elif kind == "pub_review":
            pub_review_out = taken
        elif taken:
            weekly_out.append(taken)

    trunc = _DetailTrunc(
        truncated=total_chars > max_chars, total_chars=total_chars, find_found=find_found
    )
    return intro_out, toc_out, pub_review_out, weekly_out, trunc


def _take_block(
    text: str | None, remaining: int, find: str | None = None, lead_chars: int = 0
) -> tuple[str | None, int]:
    """남은 예산 안에서 블록을 담는다. 초과 시 절단 표시를 붙이고 예산을 소진한다.

    블록이 예산을 넘고 find 키워드가 그 안(예산 밖 위치)에 있으면 앞 절단 대신 키워드
    주변 창으로 잘라, 잘린 블록에서도 찾는 규정이 살아남게 한다.
    """
    if not text:
        return text, remaining
    if remaining <= 0:
        return None, 0
    if len(text) <= remaining:
        return text, remaining - len(text)
    window, _ = window_around_find(text, remaining, find, lead_chars)
    return window, 0


def _normalize_whitespace(text: str) -> str:
    """연속 공백·개행을 단일 공백으로 정규화한다."""
    return " ".join(text.split())


def _text_or_none(el) -> str | None:
    if el is None:
        return None
    text = el.get_text(strip=True)
    return text or None
