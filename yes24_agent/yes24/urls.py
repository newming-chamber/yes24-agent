"""Yes24 URL 조립 + 섹션 레지스트리.

하드코딩 금지 원칙에 따라 base_url·section 매핑값 등은 전부 호출자가 주입한다.
섹션(검색 domain·브라우즈 코너)은 **이 모듈의 표가 유일한 열거**다 — 도구·파서는 여기서
파생한다(같은 섹션을 여러 곳에 나열하면 하나만 늘려도 다른 쪽이 조용히 터진다).
"""

from urllib.parse import quote, urljoin

from yes24_agent.yes24.selectors import (
    BESTSELLER_ITEM,
    BESTSELLER_LIST_CONTAINER,
    CREMACLUB_ITEM,
    CREMACLUB_LIST_CONTAINER,
    NEWPRODUCT_ITEM,
    NEWPRODUCT_LIST_CONTAINER,
)

# section 파라미터 → Yes24 검색 domain 쿼리값 매핑. 검색 도구의 허용값도 이 표에서 파생한다.
_SECTION_DOMAIN = {
    "all": "ALL",
    "book": "BOOK",
}

# yes24_search가 받는 section 허용값(별도 열거 금지 — 이 표가 단일 출처).
SEARCH_SECTIONS = frozenset(_SECTION_DOMAIN)


def search_url(base_url: str, query: str, section: str = "all") -> str:
    """Yes24 검색 URL을 조립한다.

    Args:
        base_url: Yes24 오리진 (예: "https://www.yes24.com"). 끝에 "/"가 있어도 없어도 된다.
        query: 검색어. URL 인코딩은 이 함수가 처리한다.
        section: "all"(전체) 또는 "book"(도서). 그 외 값은 ValueError.

    Returns:
        `/product/search?domain=...&query=...` 형태의 완전한 검색 URL.
    """
    try:
        domain = _SECTION_DOMAIN[section]
    except KeyError as exc:
        allowed = ", ".join(sorted(_SECTION_DOMAIN))
        raise ValueError(f"지원하지 않는 section: {section!r} (허용값: {allowed})") from exc

    encoded_query = quote(query, safe="")
    base = base_url.rstrip("/")
    return f"{base}/product/search?domain={domain}&query={encoded_query}"


def absolutize(base_url: str, href: str) -> str:
    """상대 경로 href를 base_url 기준 절대 URL로 변환한다."""
    return urljoin(base_url, href)


def product_url(base_url: str, goods_no: str) -> str:
    """상품 상세 페이지 URL을 조립한다."""
    base = base_url.rstrip("/")
    return f"{base}/product/goods/{goods_no}"


# 정책/CS 시드 URL 맵. docs/m2-scout-report.md 라이브 조사 기준으로 확정.
# 클라이언트 도메인 검증 제약상 반드시 www.yes24.com 절대 URL이어야 한다.
#
# 아래 두 후보는 라이브로 확인했으나 제외했다 (2026-07-07 재확인):
#   - /notice/privacypolicy.aspx (개인정보처리방침): HTTP 200이지만 본문 전체 텍스트가
#     2,662자 중 헤더/네비/푸터 상용구뿐 — 실제 조항 본문은 이미지 또는 별도 JS 로드로
#     추정(동일 계열인 /notice/service.aspx와 같은 패턴).
#   - /notice/youthpolicy.aspx (청소년보호정책): 위와 동일한 패턴(2,660자, 상용구뿐).
# 빈 본문을 시드로 등록하면 yes24_fetch가 "성공"으로 위장한 빈 답을 반환하게 되므로
# 텍스트가 실제로 확인된 URL만 남긴다.
# 고객센터 FAQ 세부 페이지(faqGb/faqSubGb 쿼리)는 EUC-KR이지만 실제 정책 본문이 SSR로
# 렌더되어 yes24_fetch가 정상 추출한다(라이브 확인: 반품 "출고일로부터 10일 이내" 등).
# 정책 질문은 이 Yes24 내부 페이지로 답해야 하며 외부 web_search로 대신하지 않는다.
#
# 설계(2026-07-10, 사용자 방향): 세부 카테고리 URL을 정적 맵으로 나열하지 않고 **입구만**
# 시드로 둔다. FAQ 입구는 좌측 메뉴(카테고리 55개 링크)를 SSR로 렌더하고 yes24_fetch가
# links로 돌려주므로, 어느 카테고리로 들어갈지는 에이전트가 **그때그때 페이지에서 읽은
# 실제 링크로 판단**한다(따라가기 1~2회). 정적 맵은 Yes24 메뉴 개편 시 조용히 썩고,
# 부분 시드는 빠진 카테고리 질문(실측: 무이자 할부 카드)이 "못 찾음"으로 새는 문제가 있었다.
POLICY_SEED_URLS: dict[str, str] = {
    "공지사항": "https://www.yes24.com/mall/help/notice",
    "고객센터 FAQ 입구(전체 카테고리 메뉴)": "https://www.yes24.com/Mall/Help/FAQ",
}


# 섹션 브라우징 레지스트리 — **한 섹션 = 한 레코드**(URL·라벨·파싱 스펙).
# docs/browse-scout-report.md 라이브 조사 기준으로 확정.
# 도구(yes24_browse)의 허용 섹션, 진행 라벨, 파서의 셀렉터·마크업
# 종류가 전부 이 표에서 파생된다 — 섹션을 여러 곳에 나열하면 시드만 추가했을 때 파서가
# `ValueError: 지원하지 않는 section`을 도구 밖으로 던진다(도구는 예외를 던지지 않는다는
# 계약 파손).
#
#   markup: "search"    검색 결과와 동일한 ITEM_* 마크업(베스트셀러·신간).
#           "cremaclub" 마크업이 다른 별도 목록(li에 data-goods-no가 없고 가격 필드도 없음).
#   has_rank: 순위 마커가 렌더되는 목록인지(신간은 없음).
#
# "cremaclub"만 cremaclub.yes24.com 서브도메인 URL이다 — robots.txt가 `Allow: /BookClub/`로
# 명시 허용했고(정찰 확인), yes24.com 서브도메인이라 클라이언트 도메인 허용 정책도 통과한다.
BROWSE_SEED_URLS: dict[str, dict] = {
    "bestseller": {
        "url": (
            "https://www.yes24.com/product/category/bestseller?CategoryNumber=001&sumgb=06"
        ),
        "label": "베스트셀러(국내도서)",
        "markup": "search",
        "list_container": BESTSELLER_LIST_CONTAINER,
        "item": BESTSELLER_ITEM,
        "has_rank": True,
    },
    "new": {
        "url": "https://www.yes24.com/product/category/newproduct?categoryNumber=001",
        "label": "신간(국내도서)",
        "markup": "search",
        "list_container": NEWPRODUCT_LIST_CONTAINER,
        "item": NEWPRODUCT_ITEM,
        "has_rank": False,
    },
    "cremaclub": {
        "url": "https://cremaclub.yes24.com/BookClub/Best",
        "label": "크레마클럽 인기(eBook 구독)",
        "markup": "cremaclub",
        "list_container": CREMACLUB_LIST_CONTAINER,
        "item": CREMACLUB_ITEM,
        "has_rank": True,
    },
}
