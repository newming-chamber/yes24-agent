"""프로젝트 전역 설정.

URL·UA·타임아웃·모델명·상한값 등 하드코딩 금지 원칙에 따라 모든 조정 가능한 값은
이 모듈의 `Settings`에 필드로 정의한다. 시크릿(API 키 등)은 `.env`에서만 로드하며
코드에 직접 값을 넣지 않는다.
"""

import logging
import os
from functools import lru_cache
from typing import Literal

from google import genai
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수·`.env`에서 로드되는 애플리케이션 설정."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    # 자율 다단계 탐색용 상위 모델(사용자 승인, 비용·지연 감수). 미명시 시 preview로 떨어짐.
    # 실측: pro는 flash의 빈응답 회귀 없이 자율 보강(정책 질문에 스스로 검색)을 안정 수행.
    # 2026-07-28 기본값 flash 전환(사용자 결정 — 속도 우선): 완료 기준 시변 11.2s·환율 6.3s
    # (pro 18~30s·10s), 채팅 품질 7승1패 동급 실측. pro는 드롭다운에 유지. 매트릭스는 채팅
    # 선택 모델을 따르므로 다양성 검증 시엔 드롭다운에서 pro 선택 권장(flash 다양성 붕괴 실측).
    model_name: str = "gemini-3.6-flash"
    # 사용자가 UI에서 선택할 수 있는 Gemini 모델(라벨→모델ID). 요청의 model 필드는 이
    # 화이트리스트의 **값**만 허용하고(임의 모델 문자열 차단), 없거나 무효면 model_name으로
    # 폴백한다. 자동 라우팅이 아니라 명시 선택이라 단일 경로 원칙과 상충하지 않는다.
    # 벤치: pro 9/12·3.5-flash 10/12 동급, 단순질의 flash 2~3배 빠름(2026-07-24).
    selectable_models: dict[str, str] = {
        "Gemini 2.5 Pro": "gemini-2.5-pro",
        "Gemini 3.5 Flash": "gemini-3.5-flash",
        "Gemini 3.6 Flash": "gemini-3.6-flash",
    }
    # 첫 모델 턴 도구 호출 강제(ANY). **커플링**: True로 켜면 reply_directly 도구가 탈출구로
    # 반드시 함께 있어야 한다 — 스위치·_force_tool_first_turn·reply_directly는 한 세트다.
    # 기본 False = Claude Code식 자율(AUTO 상시) —
    # 2026-07-29 채점 채택: 첫 delta 6~30s → 2.5~4.0s, 내레이션 확률적(30~50%) → 도구턴
    # 8/8 결정적, 4a 유혹 배터리(유명책 가격·평점·판매지수) 전수 무인용 0건 + 라이브 대조
    # 일치. ANY의 보호는 입구(도구 강제)에서 출구(validate_citations 인용 검증)로 이동해도
    # 유지됨이 실증됐다. True로 되돌리면 예전 ANY 가드(느리지만 사전 차단)로 복귀.
    force_first_turn_tool: bool = False
    # 추론 예산. -1 = 동적(모델이 질의별로 사고량을 스스로 결정). 과거 512 고정의 근거였던
    # "-1은 첫 토큰 ~10.8s" 실측은 사고 요약 스트리밍 도입 후 재현되지 않는다(2026-07-28
    # A/B: 첫 반응 -1·512 모두 3.2~3.8s 동일). 총시간은 쉬운 질문에서 -1이 우세(평균 5.9s
    # vs 512는 9.3s — 512 쪽 23.8s 사고 폭주 스파이크 포함, -1은 스파이크 없음)이고 어려운
    # 추천 질의는 동일(~20s, 지배 변수는 예산이 아니라 라운드 수·샘플링 변동). 고정 상한은
    # 근거 소멸로 삭제, .env `THINKING_BUDGET`으로 여전히 조정 가능.
    thinking_budget: int = -1
    # 사고 요약 스트리밍(Gemini include_thoughts). 벤더 사고 구간(첫 3~5초)은 본문 파트가
    # 없어 화면이 비는데, 사고 요약 파트는 그 구간에 먼저 도착한다 — runner가 이를 진행
    # 타임라인(stage=thinking)으로 흘려 첫 응답 체감 침묵을 줄인다(LLM 실생성 텍스트,
    # 정적 라벨 아님). 본문 오염은 _event_text의 thought 필터가 그대로 막는다(원칙 4b).
    include_thoughts: bool = True
    # 사고 요약 라벨의 표시용 한국어 번역. 요약기는 벤더측이라 영어 고정(프롬프트·플래너·
    # 3세대 모델 전부 무효과 실측 — known-limitations.md 2026-07-23). 답변 생성 모델
    # (`model_name`)과 무관한 **표시 유틸 전용** 경량 모델이며, 빈 문자열이면 번역 없이
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
    # 매트릭스 경로 전용 Yes24 처리량. 매트릭스는 채팅 파이프라인을 16 페르소나로 **동시**
    # 실행하는 개발 확인 화면이라, 전역 rps=1.5의 단일 throttle_lock이 16셀의 Yes24 요청을
    # 0.667초 간격으로 직렬화해 총 벽시계를 단일 채팅의 3배+로 끌어올렸다(2026-07-24 실측:
    # rps만 상향해도 89→60초, concurrency는 throttle 뒤에 가려 단독 효과 0). 매트릭스
    # 셀에서만(contextvar) 이 값으로 클라이언트를 띄워, rps 인공 직렬화 대신 concurrency
    # 세마포어가 정중함 경계가 되게 한다(≤16 동시 연결 = 대형 상용 사이트 허용 dev 버스트).
    # 채팅 단일 경로는 http_rps/http_concurrency 그대로 — 채팅 벽시계는 throttle을 사실상
    # 안 밟아 상향 전후 동일했다(실측 27초 불변). 하드코딩 금지(원칙 6) 준수 config 필드.
    # 주의: 이 값의 정당성은 "매트릭스=개발 확인 화면, 간헐 버스트" 전제에 걸려 있다.
    # 매트릭스·채팅이 같은 egress IP를 쓰므로, 매트릭스가 운영 노출·상시 자동 실행으로
    # 승격되면 버스트가 IP 제재를 부르고 채팅이 연대 피해를 입는다 — 그때 이 값 재심사.
    matrix_http_concurrency: int = 16
    matrix_http_rps: float = 16.0
    http_max_retries: int = 2  # 429/5xx 지수 백오프 횟수
    http_backoff_base_s: float = 0.5  # 지수 백오프 기준 간격(backoff_base_s * 2**attempt)
    # 200-위장 서버 오류 리다이렉트 신호(2026-07-27 실측): Yes24는 장애 시 5xx 대신
    # 302 → error_500.html?aspxerrorpath=<원경로> → 200을 돌려줘 상태코드 기반 재시도를
    # 통째로 우회한다. 리다이렉트 대상 query에 이 파라미터가 있으면 5xx와 동급의 재시도
    # 대상으로 취급한다(ASP.NET 표준 오류 페이지 신호). 빈 문자열이면 판정 비활성.
    yes24_error_redirect_param: str = "aspxerrorpath"
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
    # Yes24 HTTP 짧은 TTL 캐시 + single-flight(client.Yes24TextCache). 매트릭스 16셀이
    # 같은 질문으로 거의 같은 URL(베스트셀러 목록·상품 상세)을 동시 중복 요청하는 버스트가
    # 표적 — TTL 내 재요청은 fetch 없이 즉답, 동시 요청은 키당 1회만 fetch. 성공 응답만
    # 캐시하고 예외·차단은 캐시하지 않는다. 0이면 캐시 완전 비활성(롤백 레버).
    # 기본 90s 근거: 매트릭스 셀 중앙값 40~79s·전체 47~112초라 한 실행의 버스트 창을 덮고,
    # web_prefetch_ttl_s=90과 같은 신선도 철학(분 단위 이상 늘리지 말 것)을 공유한다.
    # **한계(checked_at 정직성)**: 도구 계층은 checked_at을 도구 실행 시각으로 찍으므로,
    # 캐시 서빙분은 실제 관측(fetch)이 최대 TTL만큼 과거다 — "지금 확인" 단정이 TTL만큼
    # 표류할 수 있다. TTL은 고정 만료(접근 연장 없음)라 표류 상한 = 이 값. 랭킹·가격
    # 시변성이 문제 되면 이 값을 줄이거나 0으로 끈다.
    yes24_cache_ttl_s: float = 90.0
    # 캐시 엔트리 수 상한(LRU 퇴출, 무한 증식 방지). 엔트리가 **디코딩된 HTML 전문**이라
    # 메모리 상한 ≈ 페이지 크기 × 상한이다 — fixture 실측 페이지가 183~598KB(평균 327KB)라
    # 64엔트리는 대략 **20~75MB**(str 디코딩 후 기준). 같은 박스에 세션 DB가 이미 GB 단위로
    # 있으므로 여유를 크게 잡을 이유가 없다.
    # 64인 근거: 매트릭스 한 실행이 실제로 만지는 고유 URL이 실측 10~12개다(weekend 1런에서
    # 16셀이 인용한 수 — 인용은 fetch의 하한이라 실제는 더 많지만 자릿수는 같다). 64면 실행
    # 1회 + 동시 채팅을 덮고도 남는다. 처음 128로 잡았다가 독립 감사 지적으로 절반으로 줄였다
    # (표적 효과는 그대로이고 최악 메모리만 반감 — 2026-08-03).
    yes24_cache_max_entries: int = 64
    search_result_limit: int = 10
    # 한 번의 yes24_search 호출에서 동시에 던질 검색 각도(쿼리) 수 상한. 탐색 각도 하나당
    # LLM 왕복을 1회씩 소모하던 직렬 구조가 추천 경로 지연의 최대 덩어리였다(2026-07-20 실측:
    # 검색만 5라운드 직렬). web_search_max_queries가 웹 검색에 하는 역할의 Yes24판 —
    # 컨텍스트·지연·Yes24 요청 폭발을 막는 천장이며, 그 대칭으로 같은 기본값을 쓴다.
    # 공유 Yes24Client의 동시성 Semaphore(http_concurrency=5) 안에 들어가는 폭이기도 하다.
    # 초과분은 조용히 버리지 않고 dropped_queries로 명시한다(fail-loud).
    yes24_search_max_queries: int = 4
    browse_result_limit: int = 10
    # yes24_browse 결과에 싣는 카테고리 내비(이름·번호) 상한. 페이지의 카테고리 트리는
    # 144개+라 전부 실으면 도구 결과가 비대해진다 — 상위·중분류가 앞서 렌더되므로 문서
    # 순서 상위만 담아도 분야 좁히기(소설/경제 등)는 충분하다.
    browse_categories_limit: int = 60
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

    # 웹 검색 백엔드 스위치(2026-07-28, 사용자 결정): "grounding" = Gemini google_search
    # 그라운딩을 도구 내부의 **별도 요청**으로 실행(빌트인 도구는 함수 선언과 같은 요청에
    # 혼용 금지 — 400 실측). 전환 근거: 시변 수치에서 검색 스니펫=크롤 캐시 한계 실측
    # (삼전 폭락일: 그라운딩은 분 단위 정확 224,000, 스니펫·Tavily는 7~20% 낡음).
    # "perplexity" = 기존 원시 검색 경로(임시 비활 — 코드 유지, 이 값으로 즉시 복귀).
    # 도메인 필터(domains)가 지정된 호출은 그라운딩에 구조적 필터가 없어 항상 퍼플렉시티
    # 경로로 처리한다(능력 기반 라우팅 — 콘텐츠 분기 아님).
    web_search_backend: Literal["grounding", "perplexity"] = "grounding"
    # 그라운딩 서브콜 모델·타임아웃. 서브콜은 "검색해 출처별 근거를 모아오는" 유틸이라 깊은
    # 추론이 불필요하다(종합은 메인 에이전트 몫) — flash-lite A/B 실측(2026-07-28): 3.1s vs
    # 3.5-flash 12~55s, 출처 수·당일 시황 정확도 동급. thought_translation과 같은 경량 유틸
    # 모델 관례.
    web_grounding_model: str = "gemini-3.1-flash-lite"
    web_grounding_timeout_s: float = 40.0
    # 웹 출처 카드용 페이지 <title> 경량 fetch 상한(표시 보강 — 실패 시 도메인 폴백).
    web_title_fetch_timeout_s: float = 3.0
    # 웹 선제 실행(prefetch) — TTFT 실측(2026-07-28: 첫 본문 11.8s = 사고1 3.8 + 도구 2.8 +
    # 사고2 5.2 직렬)에서 도구 구간을 사고1과 병렬화하는 순수 지연 최적화. 턴 시작 시 경량
    # 모델이 "웹 최신 정보가 필요한가"만 판단(모델 판단 — 키워드 분류 아님)해 그라운딩
    # 서브콜을 미리 시작하고, 이번 턴 첫 web_search가 그 결과를 서빙받는다. 힌트 오판·
    # 실패·미소비 전부 정상 경로 폴백이라 답 내용·도구 선택에는 영향이 없다(web_search.py).
    web_prefetch_enabled: bool = True
    # 프리페치 결과 공유 캐시 TTL. 매트릭스 16셀이 같은 질문을 동시에 돌려도 힌트·서브콜이
    # 1회가 되는 공유 창구다. 시변 수치의 신선도 하한이기도 하므로 분 단위 이상 늘리지 말 것.
    web_prefetch_ttl_s: float = 90.0
    # 힌트 판정(불리언 1개) 상한. 이 시간 안에 판정이 안 오면 프리페치를 포기한다 — 힌트가
    # 그라운딩 타임아웃(40s)을 물려받으면 최악 경로에서 도구가 힌트 완료까지 기다리게 된다.
    web_prefetch_hint_timeout_s: float = 5.0

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

    # 16뷰 매트릭스 (RBTI 시뮬레이터). 채팅 파이프라인(run_agent_stream)을 16 페르소나로
    # 그대로 병렬 실행한다 — 전용 검색·선택 엔진이 없어 매트릭스만의 설정은 matrix_enabled뿐이다.
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
    # 소스 변경 시 uvicorn 자동 재기동(로컬 개발 편의). 배포 컨테이너에선 켜지 않는다 —
    # 리로드 워커는 신호 처리·성능 특성이 달라 운영 경로가 아니다.
    dev_reload: bool = False
    cors_origins: list[str] = ["http://localhost:3000"]  # `*`+credentials 조합 금지 — 명시 목록
    # 진행 status detail 상한(문자). 사고 요약 라벨·검색 각도는 모델이 쓴 자유 텍스트라
    # 길 수 있는데, 진행 타임라인 한 줄은 짧아야 읽힌다. 문구를 만들지 않고 길이만 자른다.
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
