"""프로젝트 전역 설정.

URL·UA·타임아웃·모델명·상한값 등 하드코딩 금지 원칙에 따라 모든 조정 가능한 값은
이 모듈의 `Settings`에 필드로 정의한다. 시크릿(API 키 등)은 `.env`에서만 로드하며
코드에 직접 값을 넣지 않는다.
"""

import logging
import os
from functools import lru_cache

from google import genai
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수·`.env`에서 로드되는 애플리케이션 설정."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    # 자율 다단계 탐색용 상위 모델(사용자 승인, 비용·지연 감수). 미명시 시 preview로 떨어짐.
    # 실측: pro는 flash의 빈응답 회귀 없이 자율 보강(정책 질문에 스스로 검색)을 안정 수행.
    model_name: str = "gemini-2.5-pro"
    # gemini-2.5-pro는 thinking 필수(budget 0은 400 에러). **유한값으로 상한을 건다** — -1(dynamic)
    # 은 모델이 생각을 얼마든 늘려 첫 토큰이 7~12초 뒤에야 나온다(성능 감사 실측: 같은 프롬프트에
    # tb=-1 10.8s vs tb=512 4.6s, 출력 길이는 거의 동일 880 vs 824자). 512는 다단계 계획에 충분한
    # 예산이면서 TTFT를 6~8초 줄인다. 품질 저하가 관측되면 이 값을 올린다(.env로 조정 가능).
    thinking_budget: int = 512
    # 하이브리드 라우팅용 경량 모델(잡담·단순질의·단일조회를 즉답). 실측: flash는
    # thinking_budget=0만 안정(-1 dynamic은 주가 등에서 빈응답 회귀). pro 전역의 20~40초
    # 지연을 단순 질의에서 없애는 것이 목적(redesign-decision.md 원칙 8).
    flash_model_name: str = "gemini-2.5-flash"
    flash_thinking_budget: int = 0
    # 질의 난도로 flash/pro를 고르는 하이브리드 라우팅 on/off. off면 model_name(pro) 전역.
    hybrid_routing: bool = True
    # 질의 분류기(intent·multistep·confidence) on/off. 키워드 버킷을 폐기하고 값싼 모델 1회
    # 구조화 출력으로 대체한다 — 부류를 '의미'로 판정해 표면 문자열(합성어·부분일치)에 걸리지
    # 않는다. off이거나 실패·저확신이면 안전한 쪽(pro + 게이트 적용)으로 폴백한다(키워드 부활 없음).
    query_classifier: bool = True
    # 분류 전용 모델. 비면 validator가 flash_model_name으로 채운다(단일 소스·드리프트 방지).
    classifier_model_name: str = ""
    classifier_timeout_s: float = 3.0  # 분류 호출 상한(초과 시 안전 폴백)
    # 분류 결과 메모리 캐시 크기(같은 질의 재입력·재시도 시 재호출 0). 프로세스 수명 동안 유지.
    classifier_cache_size: int = 512
    # 인용 무결성 게이트(product_gate)의 대조 임계.
    # 주장 제목의 토큰 중 이 비율 이상이 출처 제목에 있으면 같은 책으로 본다(축약·부제 변형 허용).
    # 오탐(정상 인용을 오매핑으로 오판) 방지를 위해 관대하게 과반으로 둔다.
    title_token_overlap_min: float = 0.5
    # 평점 값 대조 허용오차. 표기 차이(9.5 vs 9.50)를 같은 값으로 보되, 지어낸 값은 걸러낸다.
    rating_match_tolerance: float = 0.1

    # 에러 구동 반응형 모델 폴백: pro 경로가 Gemini 과부하/일시장애(429/5xx)로 첫 응답조차
    # 내지 못하면 flash로 딱 1회 조용히 폴백 재시도한다. 선제적 hybrid_routing과 직교한다
    # ─ 저건 질의 난도로 사전 선택, 이건 에러 후 반응. off면 기존처럼 곧장 정직 안내(error+done).
    error_fallback: bool = True
    # 인터스티셜 응대(ack) 채널: 도구를 호출하는 턴에서 첫 function_call 시점에 도구 전 공감·
    # 안내 preamble의 첫 문장(들)을 별도 `event: ack`로 즉시 흘려, 30~60s 무응대 대신 수 초 내
    # 첫 응대가 보이게 한다. 이 상한(문자)까지 문장 경계로 담고, **나머지는 버린다** — 도구 전
    # 발화는 진행 발화이지 본문이 아니므로 done.text에도 들어가지 않는다(원칙 4b).
    ack_max_chars: int = 120
    max_llm_calls: int = 50  # ADK RunConfig 상한
    # 조건부 standalone 질의 재작성(멀티턴 지시대명사 해소, architecture-blueprint.md P4).
    # 직전 턴이 있고 대명사/생략 신호가 있을 때만 좁은 flash 1회로 "그 책 목차"류를 명시
    # 질의로 풀어 검색·라우팅 입력에 쓴다(사용자에게 보이는 답변·인용 계약은 불변). 리스크
    # 높은 기능이라 기본 off로 dark-launch — 게이트가 단일턴·명시 질의를 건드리지 않아 off일
    # 땐 기존과 바이트 단위 동일. 라이브 A/B(해소율↑·지연 회귀0)로 검증 후 on 전환.
    standalone_rewrite: bool = False
    # 재작성 시 참조할 직전 대화 턴 수(user+assistant 합산 상한). 맥락은 최근 몇 턴이면 충분.
    standalone_rewrite_history_turns: int = 4
    standalone_rewrite_timeout_s: float = 4.0  # 재작성 flash 호출 상한(초과 시 원본 fallback)

    # Yes24 크롤링
    yes24_base_url: str = "https://www.yes24.com"
    user_agent: str = "Mozilla/5.0 (compatible; yes24-agent/0.1)"
    http_timeout_s: float = 15.0
    http_connect_timeout_s: float = 5.0
    http_concurrency: int = 5
    http_rps: float = 1.5
    http_max_retries: int = 2  # 429/5xx 지수 백오프 횟수
    http_backoff_base_s: float = 0.5  # 지수 백오프 기준 간격(backoff_base_s * 2**attempt)
    # 리다이렉트 홉 상한. 홉마다 도메인 검증을 통과해야 요청되므로(사전 차단) 상한은
    # 무한 루프·체인 폭주 방지용이다.
    http_max_redirects: int = 5
    # 인코딩 판별 실패 허용 상한. 어떤 인코딩으로도 strict 디코드가 안 되면 cp949
    # (errors="replace")로 폴백하는데, 그 결과의 대체 문자(U+FFFD) 비율이 이 값을 넘으면
    # 깨진 텍스트를 성공으로 반환하지 않고 Yes24FetchError로 끊는다("조용히 성공하는 실패"
    # 차단). 정상 페이지에도 특수문자 몇 개는 대체될 수 있어 0이 아닌 작은 여유를 둔다.
    http_max_replacement_char_ratio: float = 0.02
    # robots.txt가 Disallow한 경로(소문자 **경로 접두** 일치). Yes24 robots는 구경로 `/Goods/`와
    # `/member/`를 차단하고 현행 `/product/search`·`/product/goods`는 허용한다(2026-07-07 실측).
    # 링크 팔로우로 차단 경로가 흘러들 수 있으므로 client.get_text가 도메인 검증과 **같은 층에서**
    # 판정해 요청 자체를 막는다(도구별 필터는 우회 경로가 생긴다 — 게이트는 한 곳).
    yes24_disallowed_paths: list[str] = ["/goods/", "/member/"]
    search_result_limit: int = 10
    browse_result_limit: int = 10
    fetch_max_chars: int = 6000
    # yes24_fetch 결과에 싣는 페이지 내 이동 링크 후보 상한. FAQ 입구 같은 내비 허브는
    # 카테고리 메뉴가 40여 개라, 동적 정책 내비게이션(입구 fetch → links에서 카테고리 선택)이
    # 성립하려면 메뉴가 잘리지 않아야 한다(12였을 때 실측: 결제정보 이후 배송·반품·회원·포인트
    # 링크가 잘려 해당 질문이 "못 찾음"으로 샜다).
    fetch_links_limit: int = 48
    fetch_min_meaningful_chars: int = 300  # 이 미만이면 실질 본문 없음(빈 성공 위장 방지)
    # find 키워드가 상한 밖에서 발견돼 그 주변 창을 잘라 돌려줄 때, 키워드 앞에 함께 담을
    # 맥락 글자 수(리드 마진). 키워드 바로 앞 문장·제목이 함께 실려야 규정의 범위·조건이
    # 이해된다(예: "무이자 할부" 앞의 카드사 소제목). 창 크기 자체는 fetch_max_chars.
    fetch_find_lead_chars: int = 500
    # fetch_many 1회 호출에서 동시에 열 상세 페이지 수 상한. 컨텍스트·지연 폭발 방지 겸,
    # 공유 Yes24Client의 동시성 Semaphore(http_concurrency=5)와 정렬해 초과 요청이 쌓이지
    # 않게 한다. 초과 items는 이 상한까지만 처리한다(하드코딩 금지 — 원칙 6).
    fetch_many_max_items: int = 5

    # 웹 검색 (외부 원시 검색 — Perplexity /search). 상품 정보는 여전히 Yes24 출처만 인용 가능.
    # 퍼플렉시티 /search는 결과의 snippet 필드에 페이지 콘텐츠(추출 본문)를 직접 담아준다
    # (Tavily의 snippet/raw_content 이원 구조와 달리 단일 필드). 분량은 아래 토큰 예산으로
    # 조절한다 — snippet이 곧 "종합 재료". 더 긴 전문이 필요하면 web_fetch(Tavily /extract).
    web_search_max_results: int = 8  # /search body의 max_results (퍼플렉시티 상한 20)
    web_search_max_tokens_per_page: int = 1024  # 결과당 snippet 콘텐츠 분량 상한(토큰)
    web_search_max_tokens: int = 12000  # 전체 결과 합산 콘텐츠 예산(토큰 폭발 방지)
    # 결과당 snippet 로컬 하드 상한(문자). 위 토큰 예산은 벤더(퍼플렉시티)에 보내는 요청 힌트라
    # 벤더가 이를 초과 반환하면 대형 전문이 그대로 컨텍스트·지연에 노출된다 — 도구 결과가 우리
    # 손을 떠나기 전 마지막 방어선으로 문자 상한을 건다(fetch_max_chars가 fetch 본문에 하는 역할의
    # web_search판). 정상 종합 재료를 자르지 않도록 토큰 예산(≈1024토큰) 위로 넉넉히 둔 안전
    # 천장이며, 초과 시에만 발동해 문장 경계 근처에서 잘라내고 절단 표식을 남긴다.
    web_search_snippet_max_chars: int = 6000
    web_search_timeout_s: float = 10.0
    # web_fetch 본문 상한·리드 마진. Yes24 상세용 fetch_max_chars를 빌려 쓰면 자사 페이지 예산을
    # 바꿀 때 외부 문서 예산이 딸려 움직인다(무관한 두 결정의 커플링) — 별도 필드로 분리한다.
    # 절단 계약(truncated·total_chars·find)은 yes24_fetch와 동일하다(같은 함수를 공유).
    web_fetch_max_chars: int = 6000
    web_fetch_find_lead_chars: int = 500
    perplexity_search_url: str = "https://api.perplexity.ai/search"
    # 웹 열람(web_fetch)은 여전히 Tavily /extract 사용 — 특정 URL 전문 확보용.
    tavily_extract_url: str = "https://api.tavily.com/extract"

    # 16뷰 매트릭스 (RBTI 시뮬레이터, retrieve-once → fan-out-generate)
    # 공유 검색이 때릴 Yes24 검색 섹션. 매트릭스 풀은 **도서 섹션(domain=BOOK)**으로 상류에서
    # 제약한다 — 통합검색(ALL)이 비도서 상품(교구·보드게임)을 섞어 내면 하류에 필터를 겹겹이
    # 쌓아야 하고, 그 필터의 오탐이 풀을 16셀보다 작게 깎아 수렴을 부른다. 실측(4질의 × ALL/BOOK):
    # BOOK 응답은 마크업이 동일해 파서가 그대로 동작하고 author·pub_date가 전 항목에 있다
    # (비도서 0건) — 필터가 아니라 질의로 제약하는 편이 단순하고 견고하다.
    matrix_search_section: str = "book"
    # 검색 1건당 파싱할 후보 수. 채팅 도구(search_result_limit=10)는 에이전트가 읽을 목록이라
    # 짧지만, 매트릭스 풀은 16셀이 갈라질 재료라 한 페이지가 주는 만큼(24건) 다 받는다 —
    # 풀이 16보다 작으면 차별화가 구조적으로 불가능하다.
    matrix_pool_parse_limit: int = 24
    # 16 fan-out 생성의 동시 실행 상한(asyncio.Semaphore). 지연을 낮추되 Gemini flash 레이트리밋·
    # 로컬 부하 폭발을 막는 가드. flash는 도구 없이 짧게 생성하므로 8이면 16열을 2배치로 소화.
    matrix_generation_concurrency: int = 8
    # 매트릭스 생성 전용 모델. **항상 flash 고정**(비용 가드 — 16배 생성을 pro로 돌리지 않음).
    # 빈 문자열이면 아래 validator가 flash_model_name으로 채워(단일 소스), 모델명 드리프트를 막는다.
    matrix_generation_model: str = ""
    # flash는 thinking_budget=0만 안정적(실측: -1 dynamic 빈응답 회귀). 매트릭스도 동일 규약.
    matrix_generation_thinking_budget: int = 0
    # 질문별 공유 풀 캐시 TTL(초). 같은 질문 재렌더·축필터 조작 시 Yes24 재타격 없이 풀 재사용
    # (rbti-feature-plan §3.2-4). 짧게 두어 신선도를 지키되 데모 중 반복 렌더는 캐시로 흡수한다.
    matrix_cache_ttl_s: float = 300.0
    # RBTI 16뷰 매트릭스 배포 게이팅. 로컬 개발은 True(매트릭스 노출), 프로드는 env
    # `MATRIX_ENABLED=false`로 숨긴다("rbti 제외하고 띄우자"). False면 main.py가 /matrix·
    # /chat/matrix 라우트를 등록하지 않아 404가 되고(채팅 경로는 무영향), 프론트 네비 링크는
    # 클라이언트가 /matrix 404를 감지해 숨긴다(서버 플래그가 단일 진실).
    matrix_enabled: bool = True
    # 공유 패스워드 로그인월. 빈 문자열이면 **비활성**(로컬 개발 기본 — 월 없음), 값이 있으면
    # 활성화돼 미들웨어가 보호 경로(/ ·/matrix ·/chat/*)를 쿠키로 가린다. env `ACCESS_PASSWORD`로
    # 주입한다(하드코딩 대신 env). 진짜 인증이 아니라 데모 접근을 막는 단일 공유 비밀번호 게이트다.
    access_password: str = ""
    # 매트릭스 공유검색 전 경량 쿼리 정제 on/off. 채팅은 에이전트가 "핵심 제목·장르·저자만"으로
    # 검색어를 성형하지만 매트릭스는 질문을 그대로 검색해, 자연어 문장("~비슷한 소설 추천해줘")이
    # Yes24 0건 → 16카드 전부 폴백하는 데모 품질 이슈가 있다. on이면 매트릭스당 flash 1회
    # (16× 아님)로 수식어를 걷고 핵심 검색어를 뽑는다. 실패·빈 결과면 원 질문 폴백(안전).
    matrix_query_refine: bool = True
    # 정제 결과의 상한(글자수·공백 토큰 수). 정상 검색어(제목·저자·장르 몇 단어)는 짧아, 둘 중
    # 하나라도 초과하면 모델이 검색어가 아니라 문장·설명을 냈다는 신호로 보고 원 질문으로 폴백한다.
    matrix_refine_max_chars: int = 40
    matrix_refine_max_words: int = 8
    # 공유 풀 다양성 가드: 같은 시리즈/제목 접두(첫 토큰 정규화)당 풀에 담을 상한. 광의 검색어
    # (예: '과학')가 문제집·시리즈("수능특강 …")로 풀을 도배하면 16셀이 전부 같은 부류를 추천하게
    # 되므로, 첫 토큰이 같은 후보를 이 수만큼만 남겨 다양성을 확보한다(어떤 검색어에서도 작동).
    matrix_pool_max_per_series: int = 2
    # 풀 확대: refine이 서로 다른 의미 각도로 낼 수 있는 검색어 개수 상한. 같은 주제를 '교양/입문'
    # 각도와 '소설/에세이' 각도로 나눠 검색해 union+dedup하면 풀이 넓어져 16 페르소나가 갈라질
    # 선택지가 생긴다(rbti-feature-plan §3.2, 사용자 피드백: 16셀 수렴). 매트릭스당 이 수만큼 검색.
    matrix_retrieval_max_queries: int = 3
    # 공유 풀 목표 크기. 다각 검색·dedup·다양성가드 후 이 수까지 담는다. **16셀보다 충분히 커야
    # 한다** — 풀이 16보다 작으면 셀들이 같은 책을 고를 수밖에 없어 차별화가 구조적으로 불가능하고
    # (실측: 최종 풀 12 < 16셀), 회전·축가드 같은 대증요법이 그 부족을 메우려 쌓인다.
    matrix_pool_target_size: int = 40
    # 열별 후보 순서 로테이션 on/off. 16셀이 같은 풀의 '가장 위(가장 대중적)' 책으로 수렴하는
    # 것(리드북 중복·자카드 겹침)을 구조로 완화한다 — 열마다 후보를 다른 위치에서 시작해 렌더하면
    # (source_id는 불변) 모델의 primacy 편향이 열마다 다른 책을 앞세운다. product 풀에만 적용.
    matrix_pool_rotate: bool = True
    # 풀 강등(soft penalty) 계수. 어떤 페르소나에게도 좋은 단권 추천이 되기 어려운 출품(판촉
    # 브래킷 나열 제목·다권 세트/전집)을 **삭제하지 않고 순위만 뒤로 민다**. 하드 드롭이던 것을
    # 강등으로 바꾼 이유: 오탐의 대가가 "책 1권 영구 소멸"에서 "순위 몇 칸 하락"으로 줄고, 신호가
    # 소실되지 않아 풀이 얇을 때는 강등된 후보라도 16셀이 쓸 수 있다(누구도 복구할 수 없던 구조를
    # 없앤다). 값은 순위 비교 시 감점 가중치이며, 0이면 강등 없음.
    matrix_pool_noise_penalty: int = 1
    # 배제 엔티티(exclude) 적용 가드. 모델이 낸 배제어가 너무 짧거나(부분 문자열이 과하게 걸림)
    # 적용 시 후보의 이 비율을 넘게 지우면 **적용을 취소**한다 — exclude:["소설"] 한 방에 풀이
    # 증발하는 것을 막는다(우세-부류 판정과 같은 발상: 대다수를 지우는 규칙은 규칙이 틀린 것).
    matrix_exclude_min_chars: int = 2
    matrix_exclude_max_drop_ratio: float = 0.5
    # 공유 풀 캐시 엔트리 상한(LRU-ish 만료). TTL만 있고 상한이 없으면 장수 프로세스에서 질문
    # 종류만큼 무한히 자란다.
    matrix_cache_max_entries: int = 64
    # 게이트 발동 셀의 재생성 재시도 횟수. 셀 답이 게이트(풀 밖 책·무출처 상품사실)에 걸리면 곧장
    # 정직 폴백으로 dim 처리하는 대신, 같은 풀로 flash를 이 횟수만큼 더 생성해 본다(재검색 아님 —
    # Yes24 트래픽 0). 생성이 비결정적이라 두 번째 초안이 접지된 답을 낼 확률이 높아 셀 성공률이
    # 오른다. 발동 셀에만 들고 최대 이 횟수라 비용 가드는 유지. 0이면 재시도 없음(기존 동작).
    matrix_cell_retries: int = 1
    # D/B(깊이/넓이) 축 추천 구성 구조 가드의 권수 경계. 4축 부호 원리를 프롬프트 서술만으로
    # 지키지 못하는 축이 D/B다(4R 실측: 깊이 셀이 4~5권 나열·'넓은 시야' 서술로, 넓이 셀이
    # '깊이 있는 접근' 프레이밍으로 역행 — 부적합 7건 중 5건). 셀 프롬프트에 권수 경계를 구조
    # 신호로 명시한다: 깊이(D) 셀은 최대 depth_max_picks권만 골라 그만큼 깊게 상술, 넓이(B)
    # 셀은 최소 breadth_min_picks권 이상(풀이 허용하는 한)을 스펙트럼으로 조망. 축 정의
    # (깊이=소수 집중, 넓이=복수 조망)에서 도출되는 부류 규칙이며 특정 질문 대응이 아니다.
    matrix_depth_max_picks: int = 2
    matrix_breadth_min_picks: int = 3

    # 세션 영속
    session_db_url: str = "sqlite+aiosqlite:///./data/sessions.db"  # async 드라이버 접미사 필수

    # 서버
    host: str = "0.0.0.0"
    port: int = 8010
    cors_origins: list[str] = ["http://localhost:3000"]  # `*`+credentials 조합 금지 — 명시 목록
    sse_timeout_s: float = 180.0
    app_name: str = "yes24-agent"

    # 시크릿 (.env에서만 로드)
    gemini_api_key: str = ""
    perplexity_api_key: str = ""  # web_search(퍼플렉시티 /search)용 — Bearer 토큰
    tavily_api_key: str = ""  # web_fetch(Tavily /extract)용

    @model_validator(mode="after")
    def _default_matrix_model_to_flash(self) -> "Settings":
        """matrix_generation_model이 비면 flash_model_name으로 채운다(단일 소스).

        매트릭스 생성은 항상 flash 고정이므로 별도 모델명 리터럴을 두지 않고 flash_model_name을
        그대로 참조한다 — flash 모델을 바꾸면 매트릭스도 자동으로 따라가 드리프트가 없다. 명시
        오버라이드(.env)가 있으면 그 값을 존중한다.
        """
        if not self.matrix_generation_model:
            self.matrix_generation_model = self.flash_model_name
        return self

    @model_validator(mode="after")
    def _default_classifier_model_to_flash(self) -> "Settings":
        """classifier_model_name이 비면 flash_model_name으로 채운다(단일 소스).

        질의 분류는 값싼 모델 고정이므로 별도 모델명 리터럴을 두지 않는다 — flash 모델을 바꾸면
        분류기도 따라간다. 명시 오버라이드(.env)가 있으면 그 값을 존중한다.
        """
        if not self.classifier_model_name:
            self.classifier_model_name = self.flash_model_name
        return self


@lru_cache
def get_settings() -> Settings:
    """설정 싱글턴을 반환한다."""
    return Settings()


def ensure_google_api_key_env() -> str:
    """ADK가 기대하는 `GOOGLE_API_KEY` 환경변수를 설정하고 사용된 키를 반환한다.

    ADK는 `GOOGLE_API_KEY`를 우선 사용하므로, 최종적으로 `GOOGLE_API_KEY` 하나만
    남기고 `GEMINI_API_KEY`는 제거해 충돌을 방지한다. 키가 전혀 없어도 예외를
    던지지 않는다 — 서버 기동은 항상 가능해야 한다.
    """
    existing_google = os.environ.get("GOOGLE_API_KEY", "")
    if existing_google:
        existing_gemini = os.environ.get("GEMINI_API_KEY", "")
        if existing_gemini and existing_gemini != existing_google:
            logging.warning(
                "GOOGLE_API_KEY와 GEMINI_API_KEY가 모두 설정되어 있고 값이 다릅니다. "
                "GOOGLE_API_KEY를 우선 사용합니다."
            )
        return existing_google

    gemini_key = os.environ.get("GEMINI_API_KEY") or get_settings().gemini_api_key
    if gemini_key:
        os.environ["GOOGLE_API_KEY"] = gemini_key
    os.environ.pop("GEMINI_API_KEY", None)
    return gemini_key


# 공유 google.genai 클라이언트 싱글턴.
# 여기 있는 이유: 소비자가 코어(query_understanding)와 matrix(generate·retrieval) 양쪽이라
# **공통 조상인 config**가 제자리다. matrix에 두면 코어가 matrix를 import하는 역방향 의존이
#생겨(실제로 query_understanding이 지연 import로 우회하고 있었다) 계층이 뒤집힌다.
# ensure_google_api_key_env가 GOOGLE_API_KEY를 세팅하므로 genai.Client()가 인증된다.
# 테스트는 호출부에 스텁을 주입해 이 팩토리를 우회한다.
_genai_client: genai.Client | None = None


def get_genai_client() -> genai.Client:
    """공유 genai 클라이언트 싱글턴을 반환한다(최초 호출 시 생성·인증)."""
    global _genai_client
    if _genai_client is None:
        ensure_google_api_key_env()
        _genai_client = genai.Client()
    return _genai_client
