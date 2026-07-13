"""프로젝트 전역 설정.

URL·UA·타임아웃·모델명·상한값 등 하드코딩 금지 원칙에 따라 모든 조정 가능한 값은
이 모듈의 `Settings`에 필드로 정의한다. 시크릿(API 키 등)은 `.env`에서만 로드하며
코드에 직접 값을 넣지 않는다.
"""

import logging
import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수·`.env`에서 로드되는 애플리케이션 설정."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    # 자율 다단계 탐색용 상위 모델(사용자 승인, 비용·지연 감수). 미명시 시 preview로 떨어짐.
    # 실측: pro는 flash의 빈응답 회귀 없이 자율 보강(정책 질문에 스스로 검색)을 안정 수행.
    model_name: str = "gemini-2.5-pro"
    # gemini-2.5-pro는 thinking 필수(budget 0은 400 에러). -1=dynamic으로 복잡도별 자율 판단.
    thinking_budget: int = -1
    # 하이브리드 라우팅용 경량 모델(잡담·단순질의·단일조회를 즉답). 실측: flash는
    # thinking_budget=0만 안정(-1 dynamic은 주가 등에서 빈응답 회귀). pro 전역의 20~40초
    # 지연을 단순 질의에서 없애는 것이 목적(redesign-decision.md 원칙 8).
    flash_model_name: str = "gemini-2.5-flash"
    flash_thinking_budget: int = 0
    # 질의 난도로 flash/pro를 고르는 하이브리드 라우팅 on/off. off면 model_name(pro) 전역.
    hybrid_routing: bool = True
    # 에러 구동 반응형 모델 폴백: pro 경로가 Gemini 과부하/일시장애(429/5xx)로 첫 응답조차
    # 내지 못하면 flash로 딱 1회 조용히 폴백 재시도한다. 선제적 hybrid_routing과 직교한다
    # ─ 저건 질의 난도로 사전 선택, 이건 에러 후 반응. off면 기존처럼 곧장 정직 안내(error+done).
    error_fallback: bool = True
    # 인터스티셜 응대(ack) 채널: 도구를 호출하는 턴에서 첫 function_call 시점에 도구 전 공감·
    # 안내 preamble의 첫 문장(들)을 별도 `event: ack`로 즉시 흘려, 30~60s 무응대 대신 수 초 내
    # 첫 응대가 보이게 한다. 이 상한(문자)까지 담되 문장 경계로 자른다(상한 초과분은 기존 홀드).
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
    # 도구 라운드 이후 텍스트를 '도구 사이 내레이션'과 '최종 답변'으로 가르는 버퍼 임계(글자).
    # 도구를 부르는 턴의 내레이션은 짧고(실측: inter-tool 발화 한두 문장 50~80자), 최종 답변은
    # 길다. 버퍼가 이 임계를 넘으면 최종 답변으로 판정해 그 시점부터 라이브 토큰 스트리밍을
    # 시작하고, 임계 미만에서 function_call이 닫으면 내레이션으로 판정해 ack로 보낸다(본문 제외).
    # 200이면 오분류가 드물고, 드문 오판(임계 초과 후 도구 도착)은 transient 노출만 남되 done.text
    # 에선 제외된다(runner). 스트리밍 체감이 이 제품의 핵심 UX라 도구 턴 토큰 스트리밍을 지킨다.
    body_stream_threshold_chars: int = 200

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
    perplexity_search_url: str = "https://api.perplexity.ai/search"
    # 웹 열람(web_fetch)은 여전히 Tavily /extract 사용 — 특정 URL 전문 확보용.
    tavily_extract_url: str = "https://api.tavily.com/extract"

    # 16뷰 매트릭스 (RBTI 시뮬레이터, retrieve-once → fan-out-generate)
    # 공유 검색 팬아웃 횟수. 16 페르소나가 같은 질문의 같은 사실을 필요로 하므로 검색은
    # 질문당 소수회만 실행해 Yes24 트래픽을 O(1)로 유지한다(rbti-feature-plan §3.2). 기본 1은
    # 단일 통합검색; 2 이상이면 섹션 변형(all→book)으로 풀을 넓힌다(비용 대 다양성 트레이드오프).
    matrix_retrieval_fanout: int = 1
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
    # 공유 풀 목표 크기. 다각 검색·dedup·다양성가드 후 이 수까지 담는다(10권÷16페르소나는 겹침
    # 불가피 → 20~30으로 넓혀 페르소나별 선택이 갈라질 공간 확보). 검색 결과가 적으면 있는 만큼.
    matrix_pool_target_size: int = 24
    # 열별 후보 순서 로테이션 on/off. 16셀이 같은 풀의 '가장 위(가장 대중적)' 책으로 수렴하는
    # 것(리드북 중복·자카드 겹침)을 구조로 완화한다 — 열마다 후보를 다른 위치에서 시작해 렌더하면
    # (source_id는 불변) 모델의 primacy 편향이 열마다 다른 책을 앞세운다. product 풀에만 적용.
    matrix_pool_rotate: bool = True

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
