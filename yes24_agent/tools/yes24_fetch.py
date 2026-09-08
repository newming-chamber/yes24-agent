"""Yes24 페이지 열람 도구 — ADK FunctionTool로 노출되는 async 함수.

에이전트가 검색만으로 부족한 상세 내용(줄거리·목차·출판사 서평·주간리뷰)이나 공지
페이지 본문을 읽어야 할 때 호출한다. yes24_search와 동일하게 결과를 세션 state의
출처 레지스트리에 등록해 source_id를 부여하고, 인용에 쓸 수 있도록 반환 dict에 담는다.

실패는 예외를 밖으로 던지지 않고 구조화된 error dict로 반환한다(fail-loud). 특히
본문이 상용구뿐인(이미지 배너 위주) 페이지는 "빈 성공"으로 위장하지 않고
error_type="empty"로 정직하게 반환한다.
"""

import asyncio
import logging
from typing import NamedTuple

from bs4 import BeautifulSoup
from google.adk.tools import ToolContext

from yes24_agent.config import get_settings
from yes24_agent.sources import DETAIL_FIDELITY, cite_marker, now_checked_at, register_source
from yes24_agent.tools._text import (
    window_around_find,
)
from yes24_agent.tools.yes24_search import get_client
from yes24_agent.yes24.client import Yes24FetchError
from yes24_agent.yes24.parsers import (
    ParseError,
    extract_faq_entries,
    extract_links,
    parse_product,
    product_fields,
)
from yes24_agent.yes24.selectors import GOODS_PATH

logger = logging.getLogger(__name__)

# 범용 텍스트 추출 시 제거할 태그(스크립트·스타일 등 비본문).
_NOISE_TAGS = ("script", "style", "noscript", "template")

# 실질 본문 판정 임계값은 config(fetch_min_meaningful_chars)에서 주입한다.


def _render_faq_entries(entries: list[dict[str, str]]) -> str:
    return "\n\n".join(
        f"질문: {entry['question']}\n답변: {entry['answer']}" for entry in entries
    )


def _select_complete_faq_entries(
    entries: list[dict[str, str]], max_chars: int, find: str | None
) -> tuple[list[dict[str, str]], str, bool]:
    """FAQ entry 경계를 깨지 않고 반환 예산 안의 연속 entry를 고른다."""
    full_text = _render_faq_entries(entries)
    find_found = bool(find) and find.casefold() in full_text.casefold()
    if len(full_text) <= max_chars:
        return entries, full_text, find_found

    start = 0
    if find and find_found:
        needle = find.casefold()
        start = next(
            index
            for index, entry in enumerate(entries)
            if needle in _render_faq_entries([entry]).casefold()
        )

    selected: list[dict[str, str]] = []
    selected_chars = 0
    for entry in entries[start:]:
        entry_text = _render_faq_entries([entry])
        added_chars = len(entry_text) + (2 if selected else 0)
        if selected_chars + added_chars > max_chars:
            break
        selected.append(entry)
        selected_chars += added_chars
    return selected, _render_faq_entries(selected), find_found


async def yes24_fetch(
    url: str, title: str, tool_context: ToolContext, find: str | None = None
) -> dict:
    """Yes24 페이지의 본문(상품 상세·공지)을 열람한다.

    Args:
        url: 열람할 Yes24 페이지의 절대 URL. 상품 상세를 보려면 yes24_search 결과의
            url을 그대로 전달한다.
        title: 열람 대상의 제목(진행 상태 표시용). yes24_search 결과의 제목을 그대로
            넣는다. 공지 등 제목을 모르면 페이지 성격을 짧게 적는다.
        find: (선택) 본문에서 찾는 정보의 핵심 키워드. 긴 페이지는 앞부분만 잘려
            오는데(truncated=True), 찾는 내용이 그 안에 없으면 이 키워드로 다시
            호출하면 **키워드가 나오는 위치부터** 본문을 잘라 돌려준다.

    Returns:
        성공 시 status="ok"와 인용용 source_id, 본문 내용을 담은 dict. 상품 상세는
        type="book_detail"로 줄거리·목차·출판사 서평·주간리뷰를, 공지 페이지는
        type="notice"로 text를 담는다. 본문이 상한보다 길어 잘렸으면 truncated=True와
        total_chars(전체 길이)가 함께 온다.
        상품 상세의 other_formats는 이 페이지가 함께 렌더한 **다른 판형**(eBook·중고 등)의
        판형명·판매가·url이다(이 페이지에서 관측한 값이라 이 source_id로 인용한다).
        함께 오는 links는 이 페이지에서 더 볼 수 있는 다른 Yes24 페이지 후보
        목록이다(아직 열지 않은 페이지 — 인용 대상이 아니며, 필요하면 그 url로 다시
        yes24_fetch를 호출해 이어서 열람할 수 있다). 실패 시 status="error"와
        error_type("fetch"|"parse"|"empty"), message.
    """
    # title은 runner의 진행 상태 라벨용으로만 쓴다. 반환 dict의 title은 항상 페이지
    # 파싱 결과를 우선하며, LLM이 준 이 값으로 덮어쓰지 않는다.
    del title

    settings = get_settings()
    client = get_client(settings)

    try:
        html = await client.get_text(url)
    except Yes24FetchError as exc:
        logger.info(f"yes24_fetch url={url!r} status=error error_type=fetch")
        return {
            "status": "error",
            "error_type": "fetch",
            "message": f"Yes24 페이지 조회에 실패했습니다: {exc}",
        }

    # **두 단계로 가른 이유(H17 오프로드)**: 파싱(상세 400KB에 ~70ms)은 순수 계산이라 워커
    # 스레드로 내리고, 등록은 **이벤트 루프에서 await 없이** 실행해 source_id의 원자·단조를
    # 지킨다. 종전엔 이 근거가 둘을 한 번에 도는 동기 편의 함수(build_result_from_html)의
    # 독스트링에 살았는데, 오늘 fetch·fetch_many가 둘 다 두 단계를 직접 부르게 되면서 그
    # 함수의 호출자가 하나도 남지 않아 삭제했다(2026-08-31 적대 감사) — 근거는 여기로 옮긴다.
    page = await asyncio.to_thread(parse_page, html, url, settings, find)
    return register_page(page, tool_context)


def parse_page(html: str, url: str, settings, find: str | None = None) -> dict:
    """HTML → 등록 전 중간 레코드(순수 계산, 스레드 안전 — state를 만지지 않는다).

    find는 범용 어포던스로, 상세(book_detail)·공지(notice) 양쪽에 적용된다 — 상세에서는
    키워드를 포함한 블록을 예산 우선순위 앞으로 당기고, 공지에서는 키워드 주변 창을 잘라준다.
    반환은 `{"type": "book_detail"|"notice", ...}` 중간 레코드 또는 status="error" dict다.
    """
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
    if GOODS_PATH in url.lower():
        return _parse_product_page(
            html,
            url,
            settings.yes24_base_url,
            settings.fetch_max_chars,
            settings.fetch_find_lead_chars,
            links,
            find=find,
        )
    return _parse_generic_page(
        html,
        url,
        settings.fetch_max_chars,
        settings.fetch_min_meaningful_chars,
        settings.fetch_find_lead_chars,
        links,
        find=find,
    )


def register_page(page: dict, tool_context: ToolContext) -> dict:
    """parse_page 중간 레코드를 세션 출처 레지스트리에 등록하고 도구 응답을 조립한다.

    이벤트 루프에서 await 없이 호출한다(fetch_many는 순차 루프) — 등록 id의 원자·단조 계약.
    """
    if page.get("status") == "error":
        return page
    return _REGISTRARS[page["type"]](page, tool_context)


def _parse_product_page(
    html: str,
    url: str,
    base_url: str,
    max_chars: int,
    lead_chars: int,
    links: list[dict],
    find: str | None = None,
) -> dict:
    """상품 상세 페이지를 파싱해 book_detail 중간 레코드를 만든다(순수 계산).

    상세 본문(줄거리·목차·서평)을 합쳐 max_chars 예산으로 담되, 예산을 넘으면
    notice와 동일하게 truncated=True·total_chars를 가법으로 명시한다("짧은 상세"로
    위장 금지). find 키워드가 주어지면 그 키워드를 포함한 블록을 예산 우선순위 앞으로
    당겨(그리고 그 블록 안에서 키워드 주변 창으로) 잘린 뒤쪽 블록의 내용도 한 번의
    재호출로 읽히게 한다.
    """
    try:
        product = parse_product(html, base_url=base_url)
    except ParseError as exc:
        logger.info(f"yes24_fetch url={url!r} status=error error_type=parse")
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

    # 설명 블록이 하나도 없는 상품(굿즈·문구 등)은 페이지에 설명 본문 자체가 없다. 이때 빈
    # 문자열을 근거(snippet)로 등록하면 모델에 남는 신호가 "열어봤다"뿐이라, 열어보고도 못 본
    # 내용을 파라메트릭으로 채운다(실측 2026-08-03: 책갈피·독서대 답변의 '특징'이 전부 창작,
    # 출처 snippet 길이 0). 없음을 값으로 위장하지 않고 None으로 두어 아래 message로 명시한다.
    content = (
        "\n\n".join(block for block in (intro, toc, pub_review, *weekly_reviews) if block) or None
    )
    return {
        "type": "book_detail",
        "url": url,
        # parse_product는 title이 None이 아님을 보장하지 않으므로 인용 라벨용 방어값을 둔다.
        "title": product.get("title") or "제목 미상",
        # 검색·브라우즈와 같은 필드 집합(product_fields) — 상세만 연 턴에서도 게이트가 대조할
        # 접지 필드(publisher·rating·sale_price·pub_date…)를 빠짐없이 싣는다.
        "fields": product_fields(product),
        "content": content,
        "intro": intro,
        "toc": toc,
        "pub_review": pub_review,
        "weekly_reviews": weekly_reviews,
        "info_tables": product.get("info_tables"),
        # 상품 상세의 page 링크는 전 페이지 공통 GNB(국내도서·카테고리 트리 …)라 정보가 0이다
        # (실측 48건 중 35건). 공지·목록 페이지에서는 같은 kind가 정책 내비의 근간이므로
        # 공지 경로는 전부 유지한다 — 걸러내는 기준은 페이지 유형이지 링크 문구가 아니다.
        "links": [link for link in links if link.get("kind") == "product"],
        "trunc": trunc,
        "find": find,
    }


def _register_product(page: dict, tool_context: ToolContext) -> dict:
    """book_detail 중간 레코드 → 출처 등록 + 도구 응답."""
    checked_at = now_checked_at()
    url, title, fields, content = page["url"], page["title"], page["fields"], page["content"]
    trunc: _DetailTrunc = page["trunc"]
    source_id = register_source(
        tool_context.state,
        title=title,
        url=url,
        source_type="book_detail",
        # 상세 관측이라 같은 URL의 목록 요약보다 우선한다(코어는 이 숫자만 비교한다).
        fidelity=DETAIL_FIDELITY,
        snippet=content,
        checked_at=checked_at,
        meta=fields,
        invocation_id=getattr(tool_context, "invocation_id", None),
    )
    logger.info(
        f"yes24_fetch url={url!r} status=ok type=book_detail total={trunc.total_chars} "
        f"truncated={trunc.truncated} detail_text={content is not None} find={page['find']!r}"
    )
    detail = {
        "status": "ok",
        "source_id": source_id,
        "cite_as": cite_marker(source_id),
        "title": title,
        "url": url,
        "type": "book_detail",
        # 관측 충실도를 **응답에도** 싣는다. 병렬 도구 유실로 세션 레지스트리가 이 출처를
        # 잃으면 러너의 화해·복구는 이 응답만 갖고 레코드를 되세우는데, 여기에 충실도가
        # 없으면 상세가 요약으로 저장돼 다음 목록 관측에 격하된다. 값은 register_source에
        # 넘긴 것과 같은 상수이며, 코어는 이 숫자의 크기만 비교한다(T1: 도구가 선언).
        "fidelity": DETAIL_FIDELITY,
        **fields,
        # snippet은 블록들의 연접이라 모델에겐 중복이지만, **공개 출처 DTO의 근거 본문**이다.
        # done.sources는 세션 레지스트리가 아니라 이 도구 응답에서 만들어지므로
        # (_sources_from_response), 여기서 빼면 출처 카드·인용 검증·QA 판정이 근거를 잃는다 —
        # 2026-07-21 정본 하네스에서 evidence_faithfulness가 4 → 0~2로 급락해 실측됐다.
        # 페이로드 절감은 evidence_segments·GNB 링크 제거만으로 충분하다.
        "snippet": content,
        "intro": page["intro"],
        "toc": page["toc"],
        "pub_review": page["pub_review"],
        "weekly_reviews": page["weekly_reviews"],
        "links": page["links"],
        "checked_at": checked_at,
    }
    if page["info_tables"]:
        # 가법 필드: 페이지의 정보 테이블(강연 정보의 모집기간·모집마감 상태, 상품별 배송비
        # 등 — selectors.PRODUCT_INFO_TABLES 주석)을 {캡션: {라벨: 값}} 그대로 싣는다.
        # 표가 없는 상품은 키 자체가 없다(관측 불가 표시 규약).
        detail["info_tables"] = page["info_tables"]
    if content is None:
        # 페이지는 열렸고 가격·평점 등 구조 필드는 실제로 관측됐으므로 status=error로 버리지
        # 않는다(그러면 정당한 가격 인용까지 사라진다). 대신 없는 것을 없다고 명시한다 —
        # notice 경로의 error_type="empty"와 같은 정신이되, 관측된 사실은 살린 형태다.
        detail["message"] = (
            "이 상품 페이지에는 설명 본문(책소개·목차·출판사 서평·주간 우수리뷰)이 없습니다. "
            "이 상품에 대해 관측된 내용은 함께 실린 상품 정보 필드가 전부이며, 재질·디자인·"
            "용도·사용감 등 서술은 이 페이지에서 확인되지 않았습니다."
        )
    if trunc.truncated:
        # 가법 필드: 잘리지 않은 상세의 반환 형태는 기존과 동일하다.
        detail["truncated"] = True
        detail["total_chars"] = trunc.total_chars
    if page["find"]:
        detail["find_found"] = trunc.find_found
    return detail


def _parse_generic_page(
    html: str,
    url: str,
    max_chars: int,
    min_meaningful_chars: int,
    lead_chars: int,
    links: list[dict],
    find: str | None = None,
) -> dict:
    """공지 등 비상품 페이지에서 범용 본문 텍스트를 추출해 notice 중간 레코드를 만든다(순수 계산).

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
    faq_entries = extract_faq_entries(soup)
    text = (
        _render_faq_entries(faq_entries)
        if faq_entries
        else _normalize_whitespace(body.get_text(" ", strip=True))
    )

    if len(text) < min_meaningful_chars:
        logger.info(f"yes24_fetch url={url!r} status=error error_type=empty chars={len(text)}")
        return {
            "status": "error",
            "error_type": "empty",
            "message": (
                "이 페이지에서 읽을 수 있는 본문을 찾지 못했습니다 "
                "(이미지 배너 위주이거나 별도 로딩되는 내용일 수 있음)."
            ),
        }

    total_chars = len(text)
    if faq_entries:
        selected_entries, window, find_found = _select_complete_faq_entries(
            faq_entries, max_chars, find
        )
        if not selected_entries:
            logger.info(
                f"yes24_fetch url={url!r} status=error error_type=content_too_large "
                f"total={total_chars}"
            )
            return {
                "status": "error",
                "error_type": "content_too_large",
                "message": "완전한 FAQ 질문·답변 항목이 본문 반환 상한을 초과했습니다.",
            }
    else:
        window, find_found = window_around_find(text, max_chars, find, lead_chars)

    return {
        "type": "notice",
        "url": url,
        "title": title,
        "window": window,
        "links": links,
        "trunc": _DetailTrunc(total_chars > max_chars, total_chars, find_found),
        "find": find,
    }


def _register_generic(page: dict, tool_context: ToolContext) -> dict:
    """notice 중간 레코드 → 출처 등록 + 도구 응답."""
    checked_at = now_checked_at()
    url, title, window = page["url"], page["title"], page["window"]
    trunc: _DetailTrunc = page["trunc"]
    source_id = register_source(
        tool_context.state,
        title=title,
        url=url,
        source_type="notice",
        snippet=window,
        checked_at=checked_at,
        invocation_id=getattr(tool_context, "invocation_id", None),
    )

    logger.info(
        f"yes24_fetch url={url!r} status=ok type=notice chars={len(window)} "
        f"total={trunc.total_chars} find={page['find']!r}"
    )
    result = {
        "status": "ok",
        "source_id": source_id,
        "cite_as": cite_marker(source_id),
        "title": title,
        "url": url,
        "type": "notice",
        "text": window,
        # text와 같은 내용이지만 공개 출처 DTO가 근거로 읽는 필드는 snippet이다(위 book_detail
        # 주석과 같은 이유 — done.sources는 이 도구 응답에서 조립된다).
        "snippet": window,
        "links": page["links"],
        "checked_at": checked_at,
    }
    if trunc.truncated:
        # 가법 필드: 잘리지 않은 페이지의 반환 형태는 기존과 동일하다.
        result["truncated"] = True
        result["total_chars"] = trunc.total_chars
    if page["find"]:
        result["find_found"] = trunc.find_found
    return result


# 중간 레코드 type → 등록기. 페이지 유형이 늘면 이 표만 는다(if 분기 없음).
_REGISTRARS = {"book_detail": _register_product, "notice": _register_generic}


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
    emitted = False
    for kind, text in named:
        separator_chars = len("\n\n") if emitted else 0
        taken, block_remaining = _take_block(
            text,
            max(0, remaining - separator_chars),
            find,
            lead_chars,
        )
        if taken:
            remaining = block_remaining
            emitted = True
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
    text: str, remaining: int, find: str | None = None, lead_chars: int = 0
) -> tuple[str | None, int]:
    """남은 예산 안에서 블록을 담는다. 초과 시 절단 표시를 붙이고 예산을 소진한다.

    블록이 예산을 넘고 find 키워드가 그 안(예산 밖 위치)에 있으면 앞 절단 대신 키워드
    주변 창으로 잘라, 잘린 블록에서도 찾는 규정이 살아남게 한다.
    """
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
