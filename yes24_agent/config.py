"""프로젝트 전역 설정.

URL·UA·타임아웃·모델명·상한값 등 하드코딩 금지 원칙에 따라 모든 조정 가능한 값은
이 모듈의 `Settings`에 필드로 정의한다. 시크릿(API 키 등)은 `.env`에서만 로드하며
코드에 직접 값을 넣지 않는다.
"""

import logging
import os
from functools import lru_cache

from google import genai
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수·`.env`에서 로드되는 애플리케이션 설정."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    # 자율 다단계 탐색용 상위 모델(사용자 승인, 비용·지연 감수). 미명시 시 preview로 떨어짐.
    # 실측: pro는 flash의 빈응답 회귀 없이 자율 보강(정책 질문에 스스로 검색)을 안정 수행.
    model_name: str = "gemini-2.5-pro"
    # pro 단일 경로의 고정 추론 예산. flash/pro 하이브리드 라우팅을 폐기하며 이 값으로 통일한다.
    # -1(동적)은 실측상 첫 토큰 ~10.8s로 512(~4.6s) 대비 2배+ 느려(모든 질의에 최대 추론)
    # "쉬운 건 빠르게" 실익이 없어 검증된 512로 고정(.env 조정 가능).
    thinking_budget: int = 512
    # 사고 요약 스트리밍(Gemini include_thoughts). 벤더 사고 구간(첫 3~5초)은 본문 파트가
    # 없어 화면이 비는데, 사고 요약 파트는 그 구간에 먼저 도착한다 — runner가 이를 진행
    # 타임라인(stage=thinking)으로 흘려 첫 응답 체감 침묵을 줄인다(LLM 실생성 텍스트,
    # 정적 라벨 아님). 본문 오염은 _event_text의 thought 필터가 그대로 막는다(원칙 4b).
    include_thoughts: bool = True
    # 사고 요약 라벨의 표시용 한국어 번역. 요약기는 벤더측이라 영어 고정(프롬프트·플래너·
    # 3세대 모델 전부 무효과 실측 — known-limitations.md 2026-07-23). 답변 생성 모델
    # 정책(pro 단일)과 무관한 **표시 유틸 전용** 경량 모델이며, 빈 문자열이면 번역 없이
    # 원문(영어)을 그대로 쓴다. 번역은 본류 무차단 병행이고 실패 시 원문 폴백(비파괴).
    thought_translation_model: str = "gemini-3.1-flash-lite"
    thought_translation_timeout_s: float = 5.0
    thought_translation_max_tokens: int = 80

    # 에러 구동 반응형 재시도: pro 경로가 Gemini 과부하/일시장애(429/5xx)로 첫 응답조차 내지
    # 못하면 같은 pro로 딱 1회 조용히 재시도한다. off면 곧장 정직 안내(error+done).
    error_fallback: bool = True
    max_llm_calls: int = 50  # ADK RunConfig 상한

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
    # 한 번의 yes24_search 호출에서 동시에 던질 검색 각도(쿼리) 수 상한. 탐색 각도 하나당
    # LLM 왕복을 1회씩 소모하던 직렬 구조가 추천 경로 지연의 최대 덩어리였다(2026-07-20 실측:
    # 검색만 5라운드 직렬). web_search_max_queries가 웹 검색에 하는 역할의 Yes24판 —
    # 컨텍스트·지연·Yes24 요청 폭발을 막는 천장이며, 그 대칭으로 같은 기본값을 쓴다.
    # 공유 Yes24Client의 동시성 Semaphore(http_concurrency=5) 안에 들어가는 폭이기도 하다.
    # 초과분은 조용히 버리지 않고 dropped_queries로 명시한다(fail-loud).
    yes24_search_max_queries: int = 4
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
    # 한 번의 web_search 호출에서 동시에 던질 검색 각도(쿼리) 수 상한. 퍼플렉시티식 질문 분해
    # (복합·시의성·비교 질문을 여러 각도로 쪼개 병렬 검색 후 종합)의 폭. fetch_many_max_items가
    # 상세 열람 배치에 하는 역할의 web_search판 — 컨텍스트·지연·벤더 요청 폭발을 막는 천장이며,
    # 초과분은 조용히 버리지 않고 dropped_queries로 명시한다(fail-loud).
    web_search_max_queries: int = 4
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
    # 로그인 쿠키 유효기간(초). 데모 접근 게이트라 재로그인 성가심을 줄이되 무한은 아니게 7일.
    access_cookie_max_age_s: int = 7 * 24 * 60 * 60
    # 로그인·admin 쿠키에 `Secure`를 붙일지. **기본 False가 의도**다 — 현재 배포(deploy-mq.sh)는
    # 리버스 프록시·TLS 없이 평문 http로 노출돼, Secure를 켜면 브라우저가 쿠키를 버려 로그인이
    # 통째로 깨진다. TLS 종단(프록시·ALB)이 생기면 env `COOKIE_SECURE=true`로 코드 수정 없이
    # 켠다. httponly·samesite는 평문에서도 안전해 플래그 없이 항상 켜져 있다.
    cookie_secure: bool = False
    # refine·selection 모두 채팅과 같은 pro(model_name)를 쓴다. refine은 추상·분위기형 추천에서
    # 취향을 좁히는 구체 검색씨앗(대표 저자·작품·하위장르)을 내야 하는데, flash는 추론 여지가
    # 있어도 이를 불안정하게 내(넓은 카테고리어 '한국 소설'로 흘러 베스트셀러·참고서 잡탕 풀)
    # pro가 필요하다(실측). refine·selection 각 1회뿐이라 16배 비용 가드는 성립하지 않는다.
    # 추론 예산은 채팅의 thinking_budget(512 — 짧은 답변 지연 튜닝값)과 분리한다: 16코드 차별화
    # 배정과 다각 검색축 설계는 조합 탐색이 커서, 동일 풀 A/B 실측(2026-07-20)으로 512는 축약
    # 수렴(selection unique 1~6·미러 빈발, refine 작가씨앗 쏠림 2/4)했고 2048은 selection
    # unique 4~8(미러 1/8), refine 유형 믹스 4/4로 안정됐다. 매트릭스당 2회뿐이라 비용은
    # 위와 같은 논리로 무시 가능하다.
    matrix_planning_thinking_budget: int = 2048
    # 매트릭스 공유검색 전 경량 쿼리 정제 on/off. 채팅은 에이전트가 "핵심 제목·장르·저자만"으로
    # 검색어를 성형하지만 매트릭스는 질문을 그대로 검색해, 자연어 문장("~비슷한 소설 추천해줘")이
    # Yes24 0건 → 16카드 전부 폴백하는 데모 품질 이슈가 있다. on이면 매트릭스당 정제 1회
    # (16× 아님, 위 주석대로 pro)로 수식어를 걷고 핵심 검색어를 뽑는다. 실패·빈 결과면 원 질문
    # 폴백(안전).
    matrix_query_refine: bool = True
    # 정제 결과의 상한(글자수·공백 토큰 수). 정상 검색어(제목·저자·장르 몇 단어)는 짧아, 둘 중
    # 하나라도 초과하면 모델이 검색어가 아니라 문장·설명을 냈다는 신호로 보고 원 질문으로 폴백한다.
    matrix_refine_max_chars: int = 40
    matrix_refine_max_words: int = 8
    # 풀 확대: refine이 서로 다른 의미 각도로 낼 수 있는 검색어 개수 상한. 같은 주제를 '교양/입문'
    # 각도와 '소설/에세이' 각도로 나눠 검색해 union+dedup하면 풀이 넓어져 16 페르소나가 갈라질
    # 선택지가 생긴다(rbti-feature-plan §3.2, 사용자 피드백: 16셀 수렴). 매트릭스당 이 수만큼 검색.
    matrix_retrieval_max_queries: int = 3
    # 공유 풀 목표 크기. 다각 검색·dedup·다양성가드 후 이 수까지 담는다. **16셀보다 충분히 커야
    # 한다** — 풀이 16보다 작으면 셀들이 같은 책을 고를 수밖에 없어 차별화가 구조적으로 불가능하고
    # (실측: 최종 풀 12 < 16셀), 회전·축가드 같은 대증요법이 그 부족을 메우려 쌓인다.
    matrix_pool_target_size: int = 40
    # 공유 풀 캐시 엔트리 상한(LRU-ish 만료). TTL만 있고 상한이 없으면 장수 프로세스에서 질문
    # 종류만큼 무한히 자란다.
    matrix_cache_max_entries: int = 64

    # 세션 영속
    session_db_url: str = "sqlite+aiosqlite:///./data/sessions.db"  # async 드라이버 접미사 필수

    # 운영자 데이터 조회(admin). 빈 문자열이면 **라우트 미등록**(404) — matrix_enabled와 같은
    # 패턴으로, 설정하지 않은 환경엔 admin이 존재조차 하지 않는다. 값은 env `ADMIN_PASSWORD`로
    # 주입하며, 채팅 로그인월(access_password)과 별도 비밀번호다(데모 접근 ≠ 운영 데이터 열람).
    admin_password: str = ""
    # 세션 목록 한 페이지 크기. 페이지당 세션 수만큼 이벤트 수·미리보기 조회가 따라붙어
    # (인덱스 조회지만) 왕복이 늘므로, 한 화면에 담기는 정도로 둔다.
    admin_page_size: int = 50
    # 본문 검색 시 events 스캔에서 거둘 세션 id 상한. LIKE는 인덱스를 못 타 전체 스캔이라
    # (실측 ~0.3s/16k행) 히트가 많을 때 IN 절이 무한히 커지지 않게 천장을 둔다.
    admin_search_max_sessions: int = 500
    # 목록 미리보기(첫 사용자 발화)를 찾으려 세션 앞에서 읽을 이벤트 수. 첫 이벤트가 사용자
    # 발화인 게 보통이라 몇 건이면 충분하다 — 세션 전체를 읽으면 목록 한 장이 수십 MB가 된다.
    admin_preview_scan_events: int = 3
    admin_preview_max_chars: int = 140
    # 상세 타임라인이 한 번에 싣는 이벤트 수 상한(초장기 세션의 응답 폭발 방지).
    admin_session_max_events: int = 300
    # 타임라인 part 하나의 본문 상한(문자). 도구 결과는 실측 최대 671KB라 상한 없이는 상세
    # 응답이 수 MB가 된다. 초과분은 조용히 버리지 않고 truncated·total_chars로 표시한다.
    admin_part_max_chars: int = 4000

    # 서버
    host: str = "0.0.0.0"
    port: int = 8010
    cors_origins: list[str] = ["http://localhost:3000"]  # `*`+credentials 조합 금지 — 명시 목록
    # 진행 status detail 상한(문자). 예고는 모델이 쓴 자유 텍스트라 길 수 있는데,
    # 진행 타임라인 한 줄은 짧아야 읽힌다. 문구를 만들지 않고 길이만 자른다.
    status_detail_max_chars: int = 120
    sse_timeout_s: float = 180.0
    app_name: str = "yes24-agent"
    # 요청 본문 상한(문자). ChatRequest.message·MatrixRequest.question에 pydantic max_length로
    # 걸어 초장문 입력을 422로 구조적으로 거절한다(키워드 탐지 아님) — 컨텍스트·토큰 폭발과
    # 악의적 대용량 페이로드를 입구에서 막는다. 정상 대화·질문은 수백 자라 넉넉한 천장이다.
    request_max_chars: int = 4000

    # 관측성(파일 로깅). log_file_path가 빈 문자열이면 stdout만(로컬 개발 기본), 값이 있으면
    # 그 경로에 RotatingFileHandler를 얹어 stdout+파일 이중 기록해 배포 후 사후 디버깅을 남긴다.
    # 크기·백업 수도 config로 둬 하드코딩을 피한다(원칙 6).
    log_file_path: str = ""
    log_max_bytes: int = 10 * 1024 * 1024  # 로그 파일 회전 임계 크기(바이트)
    log_backup_count: int = 5  # 회전 보관 백업 파일 수

    # 시크릿 (.env에서만 로드)
    gemini_api_key: str = ""
    perplexity_api_key: str = ""  # web_search(퍼플렉시티 /search)용 — Bearer 토큰
    tavily_api_key: str = ""  # web_fetch(Tavily /extract)용

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
# 여기 있는 이유: 소비자가 matrix(generate·retrieval·planning)이고 config가 그 공통 조상이라
# **여기가 제자리**다. matrix에 두면 다른 코어 모듈이 matrix를 import하는 역방향 의존이 생겨
# 계층이 뒤집힌다.
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
