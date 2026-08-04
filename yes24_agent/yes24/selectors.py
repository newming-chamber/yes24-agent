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

# 상품 분류·판형 라벨. 제목 링크 **바로 앞**에 사이트가 직접 렌더한다(실측 어휘: "[도서]"
# "[eBook]" "[외서]" "[중고]" "[LP]" "[CD]" "[만화]" "[문구/GIFT]" "[오디오북]"
# "[직수입일서]"). 제목 문자열에는 판형이 들어 있지 않으므로(a.gd_name·상세 h2.gd_name·
# g_GoodsName 모두 순수 상품명 — 2026-08-04 실측), 목록에서 전자책 여부를 관측할 수 있는
# 유일한 신호가 이 라벨이다(상세는 JS 변수 g_isEbook). 목록 아이템 전체에 렌더되므로
# 라벨이 있는데 값이 eBook이 아니면 "전자책 아님"을 관측한 것이고, 라벨 자체가 없는
# 마크업(크레마클럽)에서는 관측 불가다 — 그 구분은 _item_fields의 키 생략이 표현한다.
ITEM_FORMAT_LABEL = "span.gd_res"
ITEM_EBOOK_LABEL = "[eBook]"

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
# 뽑히는 값은 **할인 적용 판매가**다(같은 블록의 취소선 `span.txt_num.dash em.yes_m`가
# 정가이며 뽑지 않는다) — 상세페이지 g_GoodsSalePrice와 같은 의미라 필드명도 sale_price다.
ITEM_SALE_PRICE = ".info_price > strong.txt_num em.yes_b"
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

# 종이책 쪽수는 품목정보 표의 이름/값 행에 있다. 목록 페이지에는 이 표가 없으므로 상세
# 열람에서만 채우고, 해당 행이 없는 eBook은 None으로 둔다.
PRODUCT_SPECIFICATION_ROWS = "#infoset_specific table.tb_vertical tr"
PRODUCT_SPECIFICATION_LABEL = "th[scope=row]"
PRODUCT_SPECIFICATION_VALUE = "td"
PRODUCT_PAGE_COUNT_FIELD = "쪽수"

# 가격·goods_no·eBook 여부는 상세페이지 인라인 <script> 전역변수를 정규식으로 추출한다.
# CSS 방식(예: em.yes_b)은 상세페이지에서 "함께 사면 좋은 상품" 번들가·중고가까지
# 섞여 나와 오염 위험이 크다(docs/m2-scout-report.md 참조). JS 변수는 항상 SSR로
# 박혀 있어 정규식 추출이 더 안전하다.
PRODUCT_GOODS_NO_JS_RE = r"g_GoodsNo\s*=\s*'([^']*)'"
PRODUCT_GOODS_NAME_JS_RE = r"g_GoodsName\s*=\s*'([^']*)'"
PRODUCT_IS_EBOOK_JS_RE = r"g_isEbook\s*=\s*'([YN])'"
PRODUCT_SALE_PRICE_JS_RE = r"g_GoodsSalePrice\s*=\s*([\d.]+)"

# 다른 판형 안내 위젯("eBook 12,000원 이동", "중고상품"). 같은 작품의 **다른 판형과 그
# 판매가**를 담으며, 값의 임자는 이 페이지의 상품이 아니라 링크가 가리키는 다른 상품이다.
# 컨테이너로 잡는 이유: 이 값을 범용 관련상품 링크로 흘리면 앵커 텍스트에 금액만 남고
# 임자가 사라져, 모델이 열람 중인 상품의 출처에 그 금액을 건다(실측 2026-08-03 —
# links[0].title="eBook 12,000원 이동"을 종이책 출처 [1]에 인용). 가격은 판형 링크마다
# em.txC_blue 하나로, 없는 판형(중고상품)도 있으므로 없으면 None으로 둔다.
PRODUCT_FORMAT_CONTAINER = "#divFormatInfo"
PRODUCT_FORMAT_LINK = "a.formatLnk"
PRODUCT_FORMAT_PRICE = "em.txC_blue"

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

# 상품 상세 경로 조각 — 조립(urls.product_url)·페이지 유형 판정(yes24_fetch)·링크 판별
# (아래 정규식)의 단일 출처. 무의존 최하층인 이 모듈에 둔다(urls는 이미 selectors를 import).
GOODS_PATH = "/product/goods/"

# 상품 상세 링크 판별 패턴(경로만 대상, 쿼리스트링 제외하고 매칭).
# 실측 결과 대소문자가 페이지마다 혼용된다 — 검색/베스트셀러/신간은 소문자
# "/product/goods/{id}"인데, 크레마클럽 리뷰 건수 링크는
# "/Product/Goods/{id}?ReviewYn=Y"로 대문자 혼용. 반드시 대소문자 무시로 매칭해야 한다.
LINK_PRODUCT_PATH_RE = rf"(?i)^{GOODS_PATH}\d+"

# yes24.com 계열 서브도메인이지만 콘텐츠 탐색에 쓸모없는 노이즈로 실측 확인된 것들
# (goods_paper.html/bestseller_domestic_sample.html의 모든 <a href> 전수 조사 기준).
#   - event.yes24.com: docs/browse-scout-report.md 정찰에서 이미 "페이지마다 완전히
#     다른 프로모션 템플릿"이라 공통 목록 셀렉터가 없다고 확인됨 — 예측 불가능한
#     마케팅 랜딩 페이지라 에이전트가 따라가도 유의미한 도서 콘텐츠를 못 얻는다.
#   - ssl.yes24.com: 실측 결과 이 서브도메인의 모든 링크가 장바구니(Cart/Cart)·
#     주문내역(MyPageOrderList/MyPageOrderClaimList)뿐이었다 — 전부 로그인 필요한
#     계정/거래 페이지.
LINK_NOISE_SUBDOMAINS = frozenset({"event.yes24.com", "ssl.yes24.com"})

# 경로에 아래 문자열이 포함되면(소문자 비교) 콘텐츠와 무관한 페이지로 보고 제외한다.
# goods_paper.html 실측 기준. **이것은 안전장치가 아니라 신호 대 잡음비 필터다** — 수집 금지
# (robots) 판정은 config(yes24_disallowed_paths) → client.get_text 단일 게이트가 한다.
# 그래서 robots·서브도메인 배제로 이미 막히는 항목(/member/·/cart/)은 여기서 지웠다.
#
# 남은 목록이 값을 버는지 실측(goods_paper.html, links 상한 48): 이 필터를 제거하면 상품
# 카테고리 링크 8개가 밀려나고 그 자리를 로그인·장바구니·마이페이지·포인트/쿠폰 링크 8개가
# 채운다(계정·거래 페이지라 에이전트가 따라가도 얻을 콘텐츠가 없다). 상한 안에서 실제
# 콘텐츠를 잃는 순손실이라 유지한다 — 사례를 덧붙여 자라는 목록이 아니라, 전수 조사로 확인된
# 계정/거래 경로 3종이다(늘어나면 그때는 구조 신호로 대체할 신호로 본다).
#   - "/templates/" : /Templates/FTLogin.aspx(로그인), FTMyAccount_*(포인트/쿠폰/
#                      기프트카드), FTCusMain.aspx(고객센터) 등 계정 관리 템플릿 모음
#   - "/mypage"     : 대소문자 섞인 MyPageOrderList/MyPageOrderClaimList 등 대비
#   - "/campaign/"  : /campaign/00_corp/..., /campaign/01_Book/yesOnly/... 등
#                      프로모션 캠페인 페이지(event.yes24.com과 같은 성격의 노이즈)
LINK_NOISE_PATH_MARKERS = ("/templates/", "/mypage", "/campaign/")

# 고객센터 FAQ 목록. 파서(extract_faq_entries)는 선언된 **모든** 목록을 순회해 entry를
# 합친다 — 첫 목록만 쓰던 조기 return은 #faqTop10List에만 있던 일반도서 반품 정책을
# 버렸다(e8247a4에서 삭제, 파서 docstring이 정본).
FAQ_ENTRY_LISTS = ("#faqCateList", "#faqTop10List")
FAQ_ENTRY = "dl.yesToggleDl"
FAQ_QUESTION = "dt a"
FAQ_QUESTION_DECORATION = "em.bgYUI"
FAQ_ANSWER = "dd .csCView_cont"

# 목록 페이지 내비의 카테고리 링크 href 패턴. Yes24는 카테고리 트리를
# `/Product/Category/Display/{번호}`(대소문자 혼재) 링크로 페이지에 싣는다 —
# parse_category_links가 이 패턴으로 분야 이름·번호를 추출한다(번호 정적 맵 금지).
CATEGORY_DISPLAY_HREF_RE = r"(?i)/category/display/(\d+)"
