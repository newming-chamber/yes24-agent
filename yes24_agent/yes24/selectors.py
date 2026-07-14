"""Yes24 검색 결과·상품 상세·섹션 목록 페이지 CSS 셀렉터 상수.

HTML 구조가 바뀌면 이 파일만 수정하면 되도록, 파싱에 쓰이는 셀렉터를 한곳에 격리한다.
검색 결과 값은 ``tests/fixtures/search_sample.html``(실제 검색 HTML, 상품 24개)로,
상품 상세 값은 ``tests/fixtures/goods_paper.html``/``goods_ebook.html``
(docs/m2-scout-report.md 정찰 기준)로, 섹션 목록 값은
``tests/fixtures/bestseller_domestic_sample.html``/``newproduct_sample.html``/
``cremaclub_best_sample.html``(docs/browse-scout-report.md 정찰 기준)로 검증했다.
"""

# 검색 결과 아이템 컨테이너 (없으면 HTML 구조 변경으로 간주 — ParseError 트리거)
SEARCH_LIST_CONTAINER = "ul#yesSchList"

# 아이템 자체. goods_no는 li의 data 속성으로 붙는다.
SEARCH_ITEM = f"{SEARCH_LIST_CONTAINER} li[data-goods-no]"
ITEM_GOODS_NO_ATTR = "data-goods-no"

# 제목 + 상세 링크(href는 "/product/goods/{id}" 상대경로)
ITEM_TITLE_LINK = "a.gd_name"

# 표지 이미지. lazy-load라 실제 커버 URL은 `src`(Noimg 플레이스홀더)가 아니라 `data-original`
# 속성에 든다(실측: 24개 상품 전부 data-original에 image 서브도메인의 goods 커버 경로, src는
# placeholder). URL은 HTML 속성값을 그대로 추출하며 파생 패턴을 하드코딩하지 않는다.
ITEM_IMAGE = "img.lazy"
ITEM_IMAGE_ATTR = "data-original"

ITEM_AUTHOR = "span.authPub.info_auth"
# 다중저자 책의 저자 span에는 "정보 더 보기/감추기" 토글 + 숨김(펼침) 저자 전체목록이
# `span.moreAuthArea`로 함께 들어 있다(실측: goods 136301221). 저자 텍스트 추출 전 이 UI
# 노드를 제거하지 않으면 "홍창숙 , 김경은 … 저 외 1명 정보 더 보기/감추기 홍창숙 김경은 …"
# 처럼 토글 라벨과 중복 이름이 섞인다.
ITEM_AUTHOR_TOGGLE = ".moreAuthArea"
ITEM_PUBLISHER = "span.authPub.info_pub"
ITEM_PUB_DATE = "span.authPub.info_date"

# 가격과 평점이 둘 다 `em.yes_b`를 재사용하므로 컨테이너로 구분해야 한다.
# `.info_price` 안에는 쿠폰 적용가(`.yCoupon` 안의 `strong.txt_num`)가 추가로 나올 수
# 있어, '>' 자식 결합자로 최상위 판매가(strong.txt_num)만 선택해 쿠폰가 오염을 막는다.
ITEM_PRICE = ".info_price > strong.txt_num em.yes_b"
ITEM_RATING = ".rating_grade em.yes_b"

# 대중성/매력 신호(매트릭스 풀 재순위화용). 둘 다 검색 결과 HTML에 SSR로 박혀 있다.
#   - 판매지수(`span.saleNum`, 텍스트 "판매지수 113,304"): Yes24의 판매 기반 인기 집계.
#     매트릭스 다각 검색 union 풀을 이 값으로 순위화해 대중적·매력 있는 후보를 앞세운다
#     (기본 검색 정렬이 이미 인기순이나, 여러 쿼리 결과를 병합하면 그 순서가 섞여 minor
#     후보가 앞줄을 차지하던 문제 — union을 절대 판매지수로 재정렬해 해소).
#   - 회원리뷰 수(`span.rating_rvCount` 안 `em.txC_blue`, 텍스트 "1,789"): 보조 인기 신호.
# 값이 없는 후보(세트·신간 등)는 None으로 degrade(빈 성공 위장 금지 — 파서 형제 규약).
ITEM_SALE_INDEX = "span.saleNum"
ITEM_REVIEW_COUNT = "span.rating_rvCount em.txC_blue"

# 검색 결과 0건(HTML 구조 파손이 아닌 진짜 "결과 없음") 신호.
# 실측(tests/fixtures/search_empty.html, "보라색코끼리의은하수여행기xyz" 무결과 쿼리)
# 기준: `ul#yesSchList` 컨테이너 자체가 없는 대신 `div.noData`가 나타난다.
# 단, 페이지에는 검색과 무관한 "최근 본 상품 없음" 위젯(`div#yRGoodsNoData.noData`,
# 검색 결과와 무관하게 항상 숨김 상태로 존재)도 같은 클래스를 쓰므로 id로 제외한다.
NO_RESULTS_MARKER = "div.noData:not(#yRGoodsNoData)"


# ============================================================
# 상품 상세 페이지(product) 셀렉터
# ============================================================

# 제목. 페이지 안에 사이드 고정 탭 헤더(.gd_tabName)에도 동일 클래스(h2.gd_name)가
# 중복 렌더되므로, 반드시 `.gd_titArea`로 스코프해야 유일하게 걸린다.
PRODUCT_TITLE = ".gd_titArea .gd_name"
PRODUCT_AUTHOR = ".gd_auth"
PRODUCT_PUBLISHER = ".gd_pub"
PRODUCT_PUB_DATE = ".gd_date"
PRODUCT_RATING = ".gd_lnkRate em.yes_b"

# 가격·goods_no·eBook 여부는 상세페이지 인라인 <script> 전역변수를 정규식으로 추출한다.
# CSS 방식(예: em.yes_b)은 상세페이지에서 "함께 사면 좋은 상품" 번들가·중고가까지
# 섞여 나와 오염 위험이 크다(docs/m2-scout-report.md 참조). JS 변수는 항상 SSR로
# 박혀 있어 정규식 추출이 더 안전하다.
PRODUCT_GOODS_NO_JS_RE = r"g_GoodsNo\s*=\s*'([^']*)'"
PRODUCT_GOODS_NAME_JS_RE = r"g_GoodsName\s*=\s*'([^']*)'"
PRODUCT_IS_EBOOK_JS_RE = r"g_isEbook\s*=\s*'([YN])'"
PRODUCT_SALE_PRICE_JS_RE = r"g_GoodsSalePrice\s*=\s*([\d.]+)"

# 책소개/목차/출판사리뷰 블록. 실제 본문은
# `<textarea class="txtContentText" style="display:none;">` 안에 HTML 태그가
# 이스케이프 없이 그대로 든 문자열로 들어있다(페이지 JS가 나중에 이 값을 innerHTML로
# 옮겨 렌더링). 정적 파싱에서는 이 textarea의 텍스트를 꺼낸 뒤 다시 한번 HTML로
# 파싱해 태그를 벗겨내야 한다.
PRODUCT_INTRO = "#infoset_introduce"
PRODUCT_TOC = "#infoset_toc"
# 오타 주의: Yes24 실제 HTML의 id가 "pubReivew"다 (Review 아님, Yes24 자체 오타).
PRODUCT_PUB_REVIEW = "#infoset_pubReivew"
PRODUCT_TEXTAREA_CONTENT = "textarea.txtContentText"

# 회원리뷰는 "주간 우수리뷰"만 SSR로 존재한다(1~2건, 전체 리뷰 목록은 AJAX).
# 리뷰마다 잘린 미리보기(.crop, "...더보기")와 잘리지 않은 원문(.origin)이 함께
# 렌더되므로, 잘리지 않은 origin 쪽만 선택해야 온전한 리뷰 텍스트를 얻는다.
PRODUCT_REVIEW_WEEK_CONTAINER = "#infoset_reviewWeek"
PRODUCT_REVIEW_WEEK_ITEM = ".reviewInfoGrp"
PRODUCT_REVIEW_WEEK_FULL_TEXT = ".reviewInfoBot.origin .review_cont"


# ============================================================
# 섹션 목록(browse) 셀렉터 — 베스트셀러 / 신간 / 크레마클럽 인기
# ============================================================

# 베스트셀러: 검색 결과 페이지와 마크업이 사실상 동일해 ITEM_* 상수를 그대로 재사용
# 가능하다. 순위 마커(em.ico.rank, 텍스트가 1~24 숫자)만 추가로 붙는다.
BESTSELLER_LIST_CONTAINER = "ul#yesBestList"
BESTSELLER_ITEM = f"{BESTSELLER_LIST_CONTAINER} li[data-goods-no]"
ITEM_RANK = "em.ico.rank"

# 신간: 순위 마커가 없다는 점만 빼면 베스트셀러와 동일한 마크업(ITEM_* 재사용).
NEWPRODUCT_LIST_CONTAINER = "ul#yesNewList"
NEWPRODUCT_ITEM = f"{NEWPRODUCT_LIST_CONTAINER} li[data-goods-no]"

# 크레마클럽 인기(eBook 구독 서비스)는 검색/베스트셀러/신간과 마크업이 다르다:
#   - li 자체에는 data-goods-no가 없다. 대신 "내서재에 추가" 버튼
#     (a.btn_addBC)의 data-goods-no 속성에서 뽑아야 한다.
#   - 상세 링크(a.gd_name의 href)는 `/BookClub/Detail/{id}`라 구매 가능한
#     상품 페이지가 아니다 — URL은 항상 product_url(base_url, goods_no)로 별도
#     조립해야 한다("BookClub/Detail 링크 말고 구매 가능한 상품 페이지로").
#   - 출판사/출간일 필드 자체가 이 페이지에 없고, 가격 정보도 전혀 없다
#     (구독형 eBook 서비스라 개별 판매가를 표시하지 않음) — 항상 None.
CREMACLUB_LIST_CONTAINER = "ul#ulBestBookClubGoods"
CREMACLUB_ITEM = f"{CREMACLUB_LIST_CONTAINER} li"
CREMACLUB_GOODS_NO_LINK = "a.btn_addBC[data-goods-no]"
CREMACLUB_TITLE_LINK = "a.gd_name"
CREMACLUB_RANK = "div.info_row.info_rank em"
CREMACLUB_AUTHOR = "span.authPub.info_auth"
CREMACLUB_RATING = ".rating_grade em.yes_b"


# ============================================================
# 링크 추출(extract_links) 상수 — M6 링크 팔로우
# ============================================================

# 상품 상세 링크 판별 패턴(경로만 대상, 쿼리스트링 제외하고 매칭).
# 실측 결과 대소문자가 페이지마다 혼용된다 — 검색/베스트셀러/신간은 소문자
# "/product/goods/{id}"인데, 크레마클럽 리뷰 건수 링크는
# "/Product/Goods/{id}?ReviewYn=Y"로 대문자 혼용. 반드시 대소문자 무시로 매칭해야 한다.
LINK_PRODUCT_PATH_RE = r"(?i)^/product/goods/\d+"

# yes24.com 계열 서브도메인이지만 콘텐츠 탐색에 쓸모없는 노이즈로 실측 확인된 것들
# (goods_paper.html/bestseller_domestic_sample.html의 모든 <a href> 전수 조사 기준).
#   - event.yes24.com: docs/browse-scout-report.md 정찰에서 이미 "페이지마다 완전히
#     다른 프로모션 템플릿"이라 공통 목록 셀렉터가 없다고 확인됨 — 예측 불가능한
#     마케팅 랜딩 페이지라 에이전트가 따라가도 유의미한 도서 콘텐츠를 못 얻는다.
#   - ssl.yes24.com: 실측 결과 이 서브도메인의 모든 링크가 장바구니(Cart/Cart)·
#     주문내역(MyPageOrderList/MyPageOrderClaimList)뿐이었다 — 전부 로그인 필요한
#     계정/거래 페이지.
LINK_NOISE_SUBDOMAINS = frozenset({"event.yes24.com", "ssl.yes24.com"})

# 경로에 아래 문자열이 포함되면(소문자 비교) 로그인/회원/장바구니/마이페이지/캠페인 등
# 콘텐츠와 무관한 페이지로 보고 제외한다. goods_paper.html 실측 기준.
#   - "/member/"    : /Member/FTGoMyBlog.aspx, /Member/FTMypageMain.aspx,
#                      /Member/Join/Accept.aspx 등
#   - "/templates/" : /Templates/FTLogin.aspx(로그인), FTMyAccount_*(포인트/쿠폰/
#                      기프트카드), FTCusMain.aspx(고객센터 — docs/m2-scout-report.md에서
#                      이미 robots 차단으로 확인된 경로) 등 계정 관리 템플릿 모음
#   - "/cart/"      : ssl. 서브도메인 배제로 이미 걸러지지만 방어적으로 중복 체크
#   - "/mypage"     : 대소문자 섞인 MyPageOrderList/MyPageOrderClaimList 등 대비
#   - "/campaign/"  : /campaign/00_corp/..., /campaign/01_Book/yesOnly/... 등
#                      프로모션 캠페인 페이지(event.yes24.com과 같은 성격의 노이즈)
LINK_NOISE_PATH_MARKERS = ("/member/", "/templates/", "/cart/", "/mypage", "/campaign/")
