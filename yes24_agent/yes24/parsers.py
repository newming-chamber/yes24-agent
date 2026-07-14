"""Yes24 검색 결과·상품 상세·섹션 목록 HTML 파서.

파싱 실패(HTML 구조 변경 등)를 빈 리스트/빈 값으로 위장하면 에이전트가 "결과 없음"으로
환각하게 되므로, 구조가 깨진 경우는 명시적으로 ParseError를 발생시켜 fail-loud한다.
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from yes24_agent.yes24.selectors import (
    BESTSELLER_ITEM,
    BESTSELLER_LIST_CONTAINER,
    CREMACLUB_AUTHOR,
    CREMACLUB_GOODS_NO_LINK,
    CREMACLUB_ITEM,
    CREMACLUB_LIST_CONTAINER,
    CREMACLUB_RANK,
    CREMACLUB_RATING,
    CREMACLUB_TITLE_LINK,
    ITEM_AUTHOR,
    ITEM_AUTHOR_TOGGLE,
    ITEM_GOODS_NO_ATTR,
    ITEM_IMAGE,
    ITEM_IMAGE_ATTR,
    ITEM_PRICE,
    ITEM_PUB_DATE,
    ITEM_PUBLISHER,
    ITEM_RANK,
    ITEM_RATING,
    ITEM_REVIEW_COUNT,
    ITEM_SALE_INDEX,
    ITEM_TITLE_LINK,
    LINK_NOISE_PATH_MARKERS,
    LINK_NOISE_SUBDOMAINS,
    LINK_PRODUCT_PATH_RE,
    NEWPRODUCT_ITEM,
    NEWPRODUCT_LIST_CONTAINER,
    NO_RESULTS_MARKER,
    PRODUCT_AUTHOR,
    PRODUCT_GOODS_NAME_JS_RE,
    PRODUCT_GOODS_NO_JS_RE,
    PRODUCT_INTRO,
    PRODUCT_IS_EBOOK_JS_RE,
    PRODUCT_PUB_DATE,
    PRODUCT_PUB_REVIEW,
    PRODUCT_PUBLISHER,
    PRODUCT_RATING,
    PRODUCT_REVIEW_WEEK_CONTAINER,
    PRODUCT_REVIEW_WEEK_FULL_TEXT,
    PRODUCT_REVIEW_WEEK_ITEM,
    PRODUCT_SALE_PRICE_JS_RE,
    PRODUCT_TEXTAREA_CONTENT,
    PRODUCT_TITLE,
    PRODUCT_TOC,
    SEARCH_ITEM,
    SEARCH_LIST_CONTAINER,
)
from yes24_agent.yes24.urls import absolutize, product_url

# extract_links()의 도메인 허용 판정 기준. www/cremaclub/event/ssl 등 모든
# *.yes24.com 서브도메인을 "내부"로 취급하고(개별 노이즈 서브도메인은
# LINK_NOISE_SUBDOMAINS로 별도 배제), 그 외 도메인은 전부 외부로 간주해 배제한다.
_YES24_ROOT_DOMAIN = "yes24.com"

# section 인자 → (목록 컨테이너, 아이템 셀렉터, 순위 마커 존재 여부).
# urls.BROWSE_SEED_URLS 키와 1:1로 대응한다. "cremaclub"는 마크업이 아예 달라
# 별도 파싱 함수(_parse_cremaclub_list)로 처리하므로 이 표에는 없다.
_SEARCH_STYLE_BROWSE_SECTIONS = {
    "bestseller": (BESTSELLER_LIST_CONTAINER, BESTSELLER_ITEM, True),
    "new": (NEWPRODUCT_LIST_CONTAINER, NEWPRODUCT_ITEM, False),
}

_WHITESPACE_RE = re.compile(r"\s+")


class ParseError(Exception):
    """검색 결과 HTML 파싱 실패. 메시지에 원인(어떤 셀렉터가 안 맞았는지)을 포함한다."""


def parse_search(html: str, *, base_url: str, limit: int = 10) -> list[dict]:
    """Yes24 검색 결과 HTML을 파싱해 상품 목록을 반환한다.

    각 아이템 dict 키: goods_no, title, url, author, publisher, pub_date, price, rating,
    image_url(표지, lazy-load `data-original`에서 추출 — 없으면 None).
    제목 또는 URL이 없는 아이템은 건너뛴다.

    아이템 컨테이너(`ul#yesSchList`) 자체가 없으면 기본적으로 HTML 구조 변경으로 보고
    ParseError를 발생시킨다. 단, Yes24는 검색 결과가 실제로 0건일 때 이 컨테이너 대신
    "결과가 없습니다" 안내 블록(NO_RESULTS_MARKER)을 렌더링하므로, 컨테이너가 없어도
    이 신호가 있으면 정상적인 빈 검색 결과로 보고 빈 리스트를 반환한다. 컨테이너는
    있지만 아이템이 0개인 경우도 마찬가지로 빈 리스트. 컨테이너와 아이템이 있는데도
    전부 파싱 실패하면 역시 ParseError(부분적 구조 변경 감지).
    """
    soup = BeautifulSoup(html, "lxml")

    if soup.select_one(SEARCH_LIST_CONTAINER) is None:
        if soup.select_one(NO_RESULTS_MARKER) is not None:
            return []
        raise ParseError(
            f"검색 결과 컨테이너({SEARCH_LIST_CONTAINER})와 "
            f"무결과 신호({NO_RESULTS_MARKER}) 모두 찾을 수 없음 — HTML 구조 변경 의심"
        )

    items = soup.select(SEARCH_ITEM)
    if not items:
        return []

    results: list[dict] = []
    for item in items:
        title_el = item.select_one(ITEM_TITLE_LINK)
        href = title_el.get("href") if title_el else None
        title = title_el.get_text(strip=True) if title_el else None
        if not title or not href:
            continue

        results.append(
            {
                "goods_no": item.get(ITEM_GOODS_NO_ATTR),
                "title": title,
                "url": absolutize(base_url, href),
                "author": _author_or_none(item),
                "publisher": _text_or_none(item.select_one(ITEM_PUBLISHER)),
                "pub_date": _text_or_none(item.select_one(ITEM_PUB_DATE)),
                "price": _parse_price(item.select_one(ITEM_PRICE)),
                "rating": _parse_rating(item.select_one(ITEM_RATING)),
                "sale_index": _parse_grouped_int(item.select_one(ITEM_SALE_INDEX)),
                "review_count": _parse_grouped_int(item.select_one(ITEM_REVIEW_COUNT)),
                "image_url": _image_url_or_none(item),
            }
        )
        if len(results) >= limit:
            break

    if not results:
        raise ParseError(
            f"아이템 {len(items)}개 중 제목/URL을 추출한 것이 0개 — HTML 구조 변경 의심"
        )

    return results


def parse_product(html: str, *, base_url: str) -> dict:
    """Yes24 상품 상세 페이지 HTML을 파싱해 상세 정보를 반환한다.

    반환 dict 키: goods_no, title, url, author, publisher, pub_date, price(int|None),
    rating(float|None), is_ebook(bool), intro, toc, pub_review, weekly_reviews(list[str]).
    텍스트 블록(intro/toc/pub_review)은 없으면 None, weekly_reviews는 없으면 빈 리스트.

    가격·goods_no·eBook 여부는 CSS가 아니라 페이지 인라인 <script>의 전역변수를
    정규식으로 추출한다 — 상세페이지 CSS 가격(em.yes_b)은 번들가·중고가까지 섞여
    나와 오염 위험이 크다(docs/m2-scout-report.md 참조).

    제목(CSS)도 없고 goods_no(JS 변수)도 없으면 상품 상세 페이지가 아니거나 구조가
    변경된 것으로 보고 ParseError를 발생시킨다.
    """
    soup = BeautifulSoup(html, "lxml")

    title = _text_or_none(soup.select_one(PRODUCT_TITLE))
    goods_no_match = re.search(PRODUCT_GOODS_NO_JS_RE, html)
    goods_no = goods_no_match.group(1) if goods_no_match else None

    if title is None and goods_no is None:
        raise ParseError(
            "제목(CSS)과 상품 식별 JS 변수(g_GoodsNo)를 모두 찾을 수 없음 — "
            "상품 상세 페이지가 아니거나 HTML 구조 변경 의심"
        )

    if title is None:
        name_match = re.search(PRODUCT_GOODS_NAME_JS_RE, html)
        title = name_match.group(1) if name_match else None

    is_ebook_match = re.search(PRODUCT_IS_EBOOK_JS_RE, html)
    is_ebook = is_ebook_match.group(1) == "Y" if is_ebook_match else False

    price_match = re.search(PRODUCT_SALE_PRICE_JS_RE, html)
    price = _parse_js_price(price_match.group(1)) if price_match else None

    return {
        "goods_no": goods_no,
        "title": title,
        "url": product_url(base_url, goods_no) if goods_no else None,
        "author": _text_or_none(soup.select_one(PRODUCT_AUTHOR)),
        "publisher": _text_or_none(soup.select_one(PRODUCT_PUBLISHER)),
        "pub_date": _text_or_none(soup.select_one(PRODUCT_PUB_DATE)),
        "price": price,
        "rating": _parse_rating(soup.select_one(PRODUCT_RATING)),
        "is_ebook": is_ebook,
        "intro": _extract_infoset_text(soup, PRODUCT_INTRO),
        "toc": _extract_infoset_text(soup, PRODUCT_TOC),
        "pub_review": _extract_infoset_text(soup, PRODUCT_PUB_REVIEW),
        "weekly_reviews": _extract_weekly_reviews(soup),
    }


def parse_browse_list(html: str, *, base_url: str, section: str, limit: int = 24) -> list[dict]:
    """Yes24 섹션 목록(베스트셀러/신간/크레마클럽 인기) HTML을 파싱한다.

    반환 dict 키: rank(int|None), goods_no, title, url(절대), author, publisher,
    price(int|None), rating(float|None). 아이템 단위로 제목 또는 goods_no가 없으면
    건너뛴다.

    section은 urls.BROWSE_SEED_URLS의 키와 1:1로 대응한다:
      - "bestseller": 검색 결과와 동일한 마크업 + 순위(rank) 마커.
      - "new": 검색 결과와 동일한 마크업, 순위는 항상 None(마커 없음).
      - "cremaclub": 별도 마크업(cremaclub.yes24.com). URL은 `/BookClub/Detail/{id}`가
        아니라 항상 product_url(base_url, goods_no)로 조립한 www.yes24.com 상품
        페이지를 반환한다. publisher·price는 이 섹션에 필드 자체가 없어 항상 None.

    지원하지 않는 section은 ValueError. 목록 컨테이너 자체가 없으면(무결과 신호도
    없으면) HTML 구조 변경으로 보고 ParseError(parse_search와 동일 원칙).
    """
    if section == "cremaclub":
        return _parse_cremaclub_list(html, base_url=base_url, limit=limit)

    try:
        container_selector, item_selector, has_rank = _SEARCH_STYLE_BROWSE_SECTIONS[section]
    except KeyError as exc:
        allowed = ", ".join([*sorted(_SEARCH_STYLE_BROWSE_SECTIONS), "cremaclub"])
        raise ValueError(f"지원하지 않는 section: {section!r} (허용값: {allowed})") from exc

    return _parse_search_style_browse_list(
        html,
        base_url=base_url,
        limit=limit,
        container_selector=container_selector,
        item_selector=item_selector,
        has_rank=has_rank,
    )


def _parse_search_style_browse_list(
    html: str,
    *,
    base_url: str,
    limit: int,
    container_selector: str,
    item_selector: str,
    has_rank: bool,
) -> list[dict]:
    """베스트셀러/신간처럼 검색 결과와 마크업이 동일한 섹션 목록을 파싱한다."""
    soup = BeautifulSoup(html, "lxml")

    if soup.select_one(container_selector) is None:
        if soup.select_one(NO_RESULTS_MARKER) is not None:
            return []
        raise ParseError(
            f"목록 컨테이너({container_selector})와 "
            f"무결과 신호({NO_RESULTS_MARKER}) 모두 찾을 수 없음 — HTML 구조 변경 의심"
        )

    items = soup.select(item_selector)
    if not items:
        return []

    results: list[dict] = []
    for item in items:
        title_el = item.select_one(ITEM_TITLE_LINK)
        href = title_el.get("href") if title_el else None
        title = title_el.get_text(strip=True) if title_el else None
        if not title or not href:
            continue

        rank = None
        if has_rank:
            rank_el = item.select_one(ITEM_RANK)
            rank = _parse_int(rank_el.get_text(strip=True)) if rank_el else None

        results.append(
            {
                "rank": rank,
                "goods_no": item.get(ITEM_GOODS_NO_ATTR),
                "title": title,
                "url": absolutize(base_url, href),
                "author": _author_or_none(item),
                "publisher": _text_or_none(item.select_one(ITEM_PUBLISHER)),
                "price": _parse_price(item.select_one(ITEM_PRICE)),
                "rating": _parse_rating(item.select_one(ITEM_RATING)),
            }
        )
        if len(results) >= limit:
            break

    if not results:
        raise ParseError(
            f"아이템 {len(items)}개 중 제목/URL을 추출한 것이 0개 — HTML 구조 변경 의심"
        )

    return results


def _parse_cremaclub_list(html: str, *, base_url: str, limit: int) -> list[dict]:
    """크레마클럽 인기(eBook 구독) 목록을 파싱한다. 검색/베스트셀러와 마크업이 다르다."""
    soup = BeautifulSoup(html, "lxml")

    if soup.select_one(CREMACLUB_LIST_CONTAINER) is None:
        if soup.select_one(NO_RESULTS_MARKER) is not None:
            return []
        raise ParseError(
            f"크레마클럽 목록 컨테이너({CREMACLUB_LIST_CONTAINER})와 "
            f"무결과 신호({NO_RESULTS_MARKER}) 모두 찾을 수 없음 — HTML 구조 변경 의심"
        )

    items = soup.select(CREMACLUB_ITEM)
    if not items:
        return []

    results: list[dict] = []
    for item in items:
        goods_no_el = item.select_one(CREMACLUB_GOODS_NO_LINK)
        goods_no = goods_no_el.get(ITEM_GOODS_NO_ATTR) if goods_no_el else None
        title_el = item.select_one(CREMACLUB_TITLE_LINK)
        title = title_el.get_text(strip=True) if title_el else None
        if not title or not goods_no:
            continue

        rank_el = item.select_one(CREMACLUB_RANK)
        rank = _parse_int(rank_el.get_text(strip=True)) if rank_el else None

        results.append(
            {
                "rank": rank,
                "goods_no": goods_no,
                "title": title,
                "url": product_url(base_url, goods_no),
                "author": _text_or_none(item.select_one(CREMACLUB_AUTHOR)),
                "publisher": None,
                "price": None,
                "rating": _parse_rating(item.select_one(CREMACLUB_RATING)),
            }
        )
        if len(results) >= limit:
            break

    if not results:
        raise ParseError(
            f"아이템 {len(items)}개 중 제목/goods_no를 추출한 것이 0개 — HTML 구조 변경 의심"
        )

    return results


def _parse_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def extract_links(
    html: str, *, base_url: str, limit: int = 20, page_url: str | None = None
) -> list[dict]:
    """페이지 안의 내부 링크를 추출해 에이전트가 페이지→페이지로 이동할 수 있게 한다.

    반환 항목 dict 키: title(앵커 텍스트, 공백 정규화), url(절대 URL), kind
    ("product" | "page"). 앵커 텍스트가 비어있는 링크는 건너뛴다.

    kind 판정:
      - "product": href 경로가 `/product/goods/{id}` 패턴(대소문자 무시).
      - "page": 그 외 yes24.com 계열(서브도메인 포함) 링크 중 노이즈로 걸러지지
        않은 것. 노이즈 판정 기준은 selectors.LINK_NOISE_SUBDOMAINS/
        LINK_NOISE_PATH_MARKERS 참고(로그인/회원/장바구니/마이페이지/캠페인/
        이벤트 페이지를 실측으로 확인해 배제).

    외부 도메인, `#`으로 시작하는 인페이지 앵커, `javascript:` 의사 링크는 전부
    제외한다. URL이 중복되면 문서에 먼저 등장한 것(=먼저 만난 비어있지 않은
    앵커 텍스트)만 남긴다.

    반환 순서: product 링크를 page 링크보다 앞에 배치한다. `page_url`(현재 열람 중인
    페이지의 URL)이 주어지면 page 링크 중 "현재 페이지 맥락의 하위 링크"(현재 페이지
    경로 자신 또는 그 하위 경로를 가리키는 링크)를 나머지 page 링크보다 앞으로 당긴다.
    고객센터 FAQ 메인처럼 글로벌 카테고리 메가메뉴가 수백 개 깔린 페이지에서, 정작
    그 페이지 고유의 유용한 하위 링크(예: `/Mall/Help/FAQ?faqGb=...` 정책 링크)가
    글로벌 네비에 밀려 limit 밖으로 잘려나가는 것을 막기 위함이다. 그 후 limit로 자른다.

    이 함수는 순수 함수다(설정값을 전혀 참조하지 않는다) — 도메인 허용 정책의
    최종 판단은 client가 하고, 여기서는 명백한 노이즈만 줄이고(도메인/경로 필터) 현재
    페이지 맥락의 신호를 앞세워 신호 대 잡음비를 높인다.
    """
    soup = BeautifulSoup(html, "lxml")

    seen_urls: set[str] = set()
    products: list[dict] = []
    pages: list[dict] = []

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue

        title = _normalize_whitespace(anchor.get_text(" ", strip=True))
        if not title:
            continue

        url = absolutize(base_url, href)
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()

        if not _is_yes24_domain(netloc) or netloc in LINK_NOISE_SUBDOMAINS:
            continue

        path_lower = parsed.path.lower()
        if any(marker in path_lower for marker in LINK_NOISE_PATH_MARKERS):
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        is_product = re.match(LINK_PRODUCT_PATH_RE, parsed.path) is not None
        entry = {"title": title, "url": url, "kind": "product" if is_product else "page"}
        (products if is_product else pages).append(entry)

    return (products + _context_first(pages, page_url))[:limit]


def _context_first(pages: list[dict], page_url: str | None) -> list[dict]:
    """page 링크를 현재 페이지 맥락의 하위 링크가 앞서도록 안정 정렬한다.

    page_url이 없으면 원래(DOM 등장) 순서를 그대로 유지한다. 있으면 현재 페이지
    경로(자기 자신 포함)의 하위를 가리키는 링크를 앞으로 당기되, 안정 정렬이라 각
    그룹 내부의 DOM 순서는 보존한다. FAQ 하위 정책 링크는 경로가 현재 페이지와
    같고 쿼리스트링만 다르므로(예: `/Mall/Help/FAQ?faqGb=34`) "자기 자신 경로"
    일치로 잡힌다.
    """
    if not pages or not page_url:
        return pages

    context_prefix = urlparse(page_url).path.lower().rstrip("/")
    if not context_prefix:  # 현재 페이지가 사이트 루트면 맥락 기준이 없다
        return pages

    def _in_context(entry: dict) -> bool:
        link_path = urlparse(entry["url"]).path.lower().rstrip("/")
        return link_path == context_prefix or link_path.startswith(context_prefix + "/")

    return sorted(pages, key=lambda entry: 0 if _in_context(entry) else 1)


def _is_yes24_domain(netloc: str) -> bool:
    """netloc이 yes24.com 자신 또는 그 서브도메인인지 판정한다.

    단순 `"yes24.com" in netloc` 방식은 `hansaeyes24.com`처럼 실제로는 무관한
    외부 도메인이 우연히 "yes24.com"을 부분 문자열로 포함하는 경우(goods_paper.html
    실측 확인) 오탐하므로, 반드시 도메인 끝부분(suffix) 일치로 판정해야 한다.
    """
    return netloc == _YES24_ROOT_DOMAIN or netloc.endswith("." + _YES24_ROOT_DOMAIN)


def _extract_infoset_text(soup: BeautifulSoup, container_selector: str) -> str | None:
    """책소개/목차/출판사리뷰 블록에서 텍스트를 추출한다.

    실제 본문은 `<textarea class="txtContentText" style="display:none;">` 안에
    HTML 태그가 이스케이프 없이 그대로 든 문자열로 들어있다 (페이지 JS가 나중에
    이 값을 innerHTML로 옮겨 렌더링). HTML 파서는 textarea 내용을 raw text로
    취급하므로, 꺼낸 문자열을 다시 한번 HTML로 파싱해 태그를 벗겨내야 한다.

    실측 결과 Yes24는 줄바꿈에 `<br/>`와 잘못된 종료 태그 `</br>`를 혼용한다
    (goods_paper.html은 `<br/>`, goods_ebook.html은 `</br>`). lxml은 `</br>`를
    빈 요소로 취급해 그냥 버리므로 재파싱 전 `<br/>`로 정규화해야 단어가 붙어
    나오지 않는다.
    """
    container = soup.select_one(container_selector)
    if container is None:
        return None

    textarea = container.select_one(PRODUCT_TEXTAREA_CONTENT)
    if textarea is None:
        return _text_or_none(container)

    raw = textarea.get_text().replace("</br>", "<br/>")
    text = BeautifulSoup(raw, "lxml").get_text(" ", strip=True)
    return _normalize_whitespace(text) or None


def _extract_weekly_reviews(soup: BeautifulSoup) -> list[str]:
    """"주간 우수리뷰"(SSR로 존재하는 유일한 회원리뷰) 전문을 추출한다.

    리뷰마다 잘린 미리보기(.crop)와 잘리지 않은 원문(.origin)이 함께 렌더되므로,
    원문 쪽만 선택해 "...더보기"로 잘린 텍스트를 반환하지 않도록 한다.
    """
    container = soup.select_one(PRODUCT_REVIEW_WEEK_CONTAINER)
    if container is None:
        return []

    reviews: list[str] = []
    for item in container.select(PRODUCT_REVIEW_WEEK_ITEM):
        full_el = item.select_one(PRODUCT_REVIEW_WEEK_FULL_TEXT)
        if full_el is None:
            continue
        text = _normalize_whitespace(full_el.get_text(" ", strip=True))
        if text:
            reviews.append(text)

    return reviews


def _normalize_whitespace(text: str) -> str:
    """줄바꿈·NBSP(\\xa0) 등을 일반 공백으로 접어 연속 공백을 하나로 정리한다."""
    return _WHITESPACE_RE.sub(" ", text.replace("\xa0", " ")).strip()


def _text_or_none(el) -> str | None:
    if el is None:
        return None
    text = el.get_text(" ", strip=True)
    return text or None


def _author_or_none(item) -> str | None:
    """저자 span에서 '정보 더 보기/감추기' 토글 UI(span.moreAuthArea)를 제거한 뒤 텍스트를 뽑는다.

    다중저자 책은 저자 span에 토글 라벨 + 숨김 저자 전체목록이 함께 렌더돼, 그대로 뽑으면
    "홍창숙 , 김경은 … 저 외 1명 정보 더 보기/감추기 홍창숙 김경은 …"처럼 UI 텍스트·중복
    이름이 섞인다(카드 표시 품질 버그). 토글 노드를 지운 뒤 저자만 남긴다.

    또 저자 사이 콤마는 별개 텍스트노드(", ")라 get_text(" ")가 "홍창숙 , 김경은"처럼 콤마 앞에
    공백을 넣는다 — 카드 표시 품질을 위해 콤마 주변 공백을 "이름, 이름"으로 정규화한다.
    """
    el = item.select_one(ITEM_AUTHOR)
    if el is None:
        return None
    for toggle in el.select(ITEM_AUTHOR_TOGGLE):
        toggle.decompose()
    text = el.get_text(" ", strip=True)
    text = re.sub(r"\s*,\s*", ", ", text)
    return text or None


def _image_url_or_none(item) -> str | None:
    """검색 결과 아이템의 표지 이미지 URL을 추출한다(없으면 None — 빈 성공 위장 금지).

    lazy-load라 실제 커버는 `src`(placeholder)가 아니라 `data-original`(ITEM_IMAGE_ATTR)에
    든다. URL은 HTML 속성값을 그대로 쓰며 파생 패턴을 조립하지 않는다.
    """
    img = item.select_one(ITEM_IMAGE)
    if img is None:
        return None
    url = (img.get(ITEM_IMAGE_ATTR) or "").strip()
    return url or None


def _parse_price(el) -> int | None:
    if el is None:
        return None
    cleaned = el.get_text(strip=True).replace(",", "")
    return int(cleaned) if cleaned.isdigit() else None


def _parse_js_price(raw: str) -> int | None:
    """PRODUCT_SALE_PRICE_JS_RE가 캡처한 문자열("15300.00" 등)을 정수로 변환한다.

    캡처 그룹 자체가 `[\\d.]+`라 점이 여러 개인 값("1.2.3")도 매칭될 수 있어
    `float()`가 ValueError를 던질 수 있다 — 형제 파서(_parse_int/_parse_rating)와
    동일하게 가드해 파싱 불가 시 예외 대신 None으로 degrade한다.
    """
    try:
        return int(float(raw))
    except ValueError:
        return None


def _parse_rating(el) -> float | None:
    if el is None:
        return None
    text = el.get_text(strip=True)
    try:
        return float(text)
    except ValueError:
        return None


def _parse_grouped_int(el) -> int | None:
    """판매지수·리뷰수처럼 라벨·쉼표가 섞인 정수를 뽑는다("판매지수 113,304"→113304).

    첫 숫자 뭉치(쉼표 포함)만 취해 앞뒤 라벨 텍스트("판매지수 "·"건")를 무시한다. 숫자가
    없으면 None(빈 성공 위장 금지 — 형제 파서와 동일 degrade 규약)."""
    if el is None:
        return None
    match = re.search(r"\d[\d,]*", el.get_text())
    if match is None:
        return None
    return int(match.group(0).replace(",", ""))
