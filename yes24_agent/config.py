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
from pydantic import model_validator
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
    # 값에 "/"가 있으면 LiteLLM 경로("provider/model" — ADK·litellm 관례)로 해석돼
    # create_agent가 LiteLlm 어댑터로 감싼다. 벤더 키워드 목록 없이 ID 형태가 신호다.
    selectable_models: dict[str, str] = {
        "Gemini 2.5 Pro": "gemini-2.5-pro",
        "Gemini 3.5 Flash": "gemini-3.5-flash",
        "Gemini 3.6 Flash": "gemini-3.6-flash",
        "Gemini 3.7 Flash": "gemini-3.7-flash",
        # responses/ 접두는 litellm의 responses API 브리지 — chat/completions의
        # "function tools + reasoning 병용 불가" 제약이 없어 reasoning을 켠 채 도구를 쓴다.
        # 2026-08-12 도그푸딩 실측: reasoning 끈 chat 경로는 도구 회피(추천 3/3 무도구 창작).
        "GPT 5.6 Luna": "openai/responses/gpt-5.6-luna",
    }
    # 활성 도구셋(toolsets.TOOLSETS 키 부분집합). 도구 등록·프롬프트 fragment 활성·프론트
    # 브랜딩이 여기서 파생된다. 순서는 레지스트리 선언 순서가 정본(이 리스트는 켜고 끄기만).
    # 미등록 이름·빈 목록은 기동 시 ValueError(fail-loud — toolsets.resolve_app).
    # 타입이 list[str]인 이유: config는 레지스트리를 모른다(Literal 금지 — 계층 역전 방지).
    # env: ENABLED_TOOLSETS='["yes24","web"]'
    enabled_toolsets: list[str] = ["yes24", "web"]
    # 정체성·브랜딩 페르소나(toolsets.PERSONAS 키). 프롬프트 정체성 fragment와 프론트
    # 문안(제목·인사·예시 칩)이 파생된다.
    agent_persona: str = "yes24"
    # 추론 예산. -1 = 동적(모델이 질의별로 사고량을 스스로 결정). 과거 512 고정의 근거였던
    # "-1은 첫 토큰 ~10.8s" 실측은 사고 요약 스트리밍 도입 후 재현되지 않는다(2026-07-28
    # A/B: 첫 반응 -1·512 모두 3.2~3.8s 동일). 총시간은 쉬운 질문에서 -1이 우세(평균 5.9s
    # vs 512는 9.3s — 512 쪽 23.8s 사고 폭주 스파이크 포함, -1은 스파이크 없음)이고 어려운
    # 추천 질의는 동일(~20s, 지배 변수는 예산이 아니라 라운드 수·샘플링 변동). 고정 상한은
    # 근거 소멸로 삭제, .env `THINKING_BUDGET`으로 여전히 조정 가능.
    thinking_budget: int = -1
    # LiteLLM 경로(openai/*) 전용 reasoning 강도. gpt-5.6 계열은 chat/completions에서
    # function tools + reasoning_effort 병용을 400으로 거부하지만(2026-08-12 라이브 실측),
    # responses API 브리지(모델ID의 responses/ 접두)에서는 병용이 된다. 재현 프로브
    # (도구 미호출 추천 창작) 실측: 'none' 3/3 실패 → 'low' 1/3 → 'medium' 0/5, 지연은
    # medium에서도 8~15s로 준수. 빈 문자열이면 미전달(벤더 기본).
    # 2026-08-21 high 승격(사용자 결정): 세션 무결률 high 18/18 vs medium 14/18(08-18
    # 배터리) + 페어 실측 3건에서 속도 비용이 상시가 아님(7.5/45.3/50.5s vs medium
    # 8.4/49.1/26.4s — 추천 턴 지연의 지배 변수는 effort가 아니라 조사 라운드 수,
    # medium도 49s 관측). Luna는 드롭다운 선택지라 기본(flash) 사용자 무영향.
    litellm_reasoning_effort: str = "high"
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
    # 턴 부가 정보(meta) 추출 — 추천 이유·세션 제목(enrichment.py). 본문 생성과 무관한
    # "이미 쓰인 답변의 구조화"라 깊은 추론이 불필요하다 — thought_translation과 같은 경량
    # 유틸 모델 관례. 빈 문자열이면 파이프라인 전체 비활성(구조적 off 스위치).
    enrichment_model: str = "gemini-3.1-flash-lite"
    # done 이후 부가 채널의 상한 — 이 시간을 넘기면 meta 없이 스트림을 닫는다(비파괴).
    enrichment_timeout_s: float = 10.0
    # 추천 이유 여러 건 + 제목의 JSON 출력 예산. 이유는 한 줄 요약이라 넉넉한 천장이다.
    enrichment_max_output_tokens: int = 1024
    # meta 추천 항목 수 상한(출구에서 초과 꼬리 제거). crema-ai의 카드 5권 계약 이식 —
    # 프론트 카드 줄과 1:1이 보장되는 폭이다. 정렬은 본문 인용 등장 순서(출구 검증이 소유).
    enrichment_max_recommendations: int = 5
    # 세션 제목 표시 상한(문자). 목록 한 줄에 담기는 길이 — status_detail_max_chars와는
    # 다른 축이라 따로 둔다(우연히 비슷해도 서로 따라가면 안 된다).
    session_title_max_chars: int = 60
    # 검색결과 AI 오버뷰(POST /overview) — 에이전트 루프 없는 단발 요약
    # (docs/overview-design-judgment.md LEAN-SHOT). 빈 문자열이면 라우트 미등록
    # (구조적 off 스위치 — enrichment_model·matrix_enabled 관례).
    # 경량 유틸 관례(thought_translation·enrichment 동일). 폴백 사다리: 3.6-flash 승격 →
    # Luna 배터리 재평가(G5) — 판정은 출시 게이트 배터리(로드맵 7)가 한다.
    overview_model: str = "gemini-3.1-flash-lite"
    # 생성 데드라인(초) — **첫 유효 마커 방류 전까지만** 적용된다(2026-08-27 의미 정련).
    # 방류가 시작된 스트림은 데드라인을 넘어도 완주한다(스트림 도중 회수 금지 — H1 홀드
    # "전량 방류 or 전량 폐기" 원칙과 정합: 보였다 지워지는 UX가 최악). 따라서 이 값의
    # 의미는 "화면에 아무것도 못 보여준 채 사용자를 기다리게 하는 최대 시간"이고, 무인용·
    # 상세 보강 재시도 체인(전부 방류 전)도 이 하나를 나눠 쓴다. 10→5: 오버뷰 속도 최우선
    # (2026-08-26 사용자 원칙) — 콜드 해부(어린이 영어 그림책 15.1s)에서 방류 전 체인이
    # 지연의 본체였고, 5초 뒤에도 마커가 없으면 접힘(degraded="timeout")이 정직하다.
    overview_timeout_s: float = 5.0
    # 검색 구간의 **누적 벽시계 상한**(초) — 1차 2각도 + 보강 재검색(0건·저신호·무인용)이
    # 함께 쓴다. overview_timeout_s 재사용 금지 근거는 축이 다르다는 것이다: 그 값은 "화면에
    # 아무것도 못 보여준 채 기다리게 하는 **생성** 시간"이고, 검색은 LLM이 시작조차 하기 전의
    # 상류 왕복이다. 두 축을 한 값으로 묶으면 한쪽 지연이 다른 쪽 예산을 굶긴다.
    # 신설 근거(2026-08-28 라이브 계측, '토익 교재 추천' 콜드): 1차 검색 9.97s + 저신호 보강
    # 2.33s = 12.30s가 **아무 상한 없이** 흘렀고, 생성은 그 뒤 온전한 5.0s를 받아 2.19s에
    # 끝났는데도 첫 delta가 13.43s였다 — 데드라인이 굶은 게 아니라 검색 구간에 상한이 아예
    # 없던 것이 지연의 본체다(같은 배치의 나머지 4질의는 검색 1.46~2.02s).
    # 6.0s 근거: 실측 콜드 검색 합의 상단(1차 ~2.0s + 보강 ~2.3s ≈ 4.3s)의 1.4배 여유.
    # 소진 시 거동은 **보강 생략**이다(보강은 실패 사유가 아니다 — 기존 0건 폴백 철학):
    # 1차 결과 그대로 생성으로 넘어간다. 2026-08-28 리뷰로 **1차 기본 각도도 이 예산 안**에
    # 들어왔다: 종전엔 그 각도만 무상한이라 http_timeout_s×(http_max_retries+1)+백오프
    # (≈46s)까지 SSE가 열린 채 화면이 비었고, "예산 지도에 무상한 구간 없음"·"화면에 아무
    # 것도 못 보여주는 최대 시간 5.0s"라는 두 표기가 실질과 어긋났다. "취소 = 무답"이라는
    # 종전 논거는 병렬 RECENT 각도가 같은 질의의 행을 이미 물어온다는 사실로 반박된다 —
    # 예산을 넘긴 각도만 끊고 도착한 각도로 답하며, 양쪽 다 넘겨야 검색 단계 실패(degraded
    # parse_error, error_type=search_timeout)로 정직하게 접는다(46초 대기 역시 무답이다).
    # 0 이하 = 보강 즉시 생략(1차 각도만 — 롤백 레버).
    overview_search_budget_s: float = 6.0
    # 접힌 요약이 정본 — 글로벌 AI 오버뷰 실측 100~300단어·출력 200~400토큰 근거(리서치).
    # 400→600 (2026-08-27 절단 실측): 종전 400은 실제 본문 길이 분포의 **상단에 걸쳐**
    # 무음 절단을 냈다 — 저장 행 재생 진단(5질의 각 1콜)에서 자연 종료 출력은 243·244·
    # 277·357·401토큰이었고, 401짜리('그림책 추천')만 cap 400에 finish=MAX_TOKENS로 문장
    # 중간("…최신 창작동화로 만나")에서 끊겼다. 같은 입력·같은 seed로 cap만 600/800으로
    # 올리면 둘 다 finish=STOP·401토큰·본문 바이트 동일(끊긴 문장만 완결)이라, 상한은
    # 목표가 아니라 천장임이 실측으로 확인된다 — 총 소요도 2.05s→2.05s/2.02s로 무변화다
    # (상한을 올려도 모델이 더 길게 쓰지 않으므로 지연 순증이 없다). 600 = 관측 최대
    # (401)의 약 1.5배 여유 — 800과 결과가 같아 더 올릴 근거가 없다.
    overview_max_output_tokens: int = 600
    # ── 오버뷰 캐시 TTL 4종(2026-08-28 역할 3분할 + G2) ──────────────────────────────
    # 종전엔 저장소 하나(_cache)가 **수명이 다른 세 역할**을 겸했다: ① 진행 중 생성의
    # single-flight ② 완성 응답 서빙 ③ 이어가기 씨앗. TTL은 언제나 셋 중 **최댓값**에
    # 끌려가므로 3600 승격 근거의 절반이 ③(오버뷰 본 뒤 한참 있다 이어가기)이었고, 그
    # 결과 응답 신선도가 핸드오프 창의 인질이 됐다(TTL 축소 = 이어가기 404 회귀).
    # 역할을 나눈 지금 각 TTL은 **자기 축의 근거만으로** 정한다.
    #
    # ② 완성 응답 서빙 TTL(0 이하 = 응답 캐시 끔 — 롤백 레버. single-flight ①은 신선도
    # 축이 아니라 계속 동작해 동시 동일 질의는 여전히 LLM 1콜로 수렴한다).
    # 3600→300 근거: 이 값의 정직성 한계는 **checked_at 표기**다 — 오버뷰 카드·칩은 관측
    # 시각을 그대로 보여주므로, 서빙 창은 "그 표기를 보고도 사용자가 지금 값이라 여길
    # 창"을 넘지 않아야 한다. 1시간 전 관측을 검색 결과 **상단 요약**으로 계속 내놓는 것은
    # 그 한계 밖이고(가격·순위·품절은 그 사이에 바뀐다), 300s는 SERP를 다시 눌러 보는
    # 통상 재조회 간격을 덮으면서 표기가 "방금"으로 읽히는 창이다.
    # 축소의 비용(재생성 빈도)은 G2 행 캐시가 상쇄한다 — 응답 미스의 바닥이 콜드 3.84s에서
    # 행 히트 1.07s(첫 delta)로 내려가, 종전 TTL 축소가 곧 콜드 회귀이던 등식이 깨졌다.
    overview_response_ttl_s: float = 300.0
    # G2 병합 검색 결과(행) 캐시 TTL. 키는 (정규화 질의, section, 검색 예산)이고 값은
    # **병합 완료 결과 + 출처 레지스트리**다(파싱 행 하나가 아니라 — 파싱은 각도당
    # 50~100ms뿐이고 남는 1.1s는 보강 플래너 콜 + 2차 각도 병합이라, 병합 후를 담아야
    # 효과가 난다). 0 이하 = 행 캐시 끔(롤백 레버 — 매 생성이 콜드 검색).
    # 600 = 응답 TTL의 2배. 파생 근거는 **응답 만료 직후의 재생성**이 G2가 잡아야 할
    # 바로 그 경우라는 것이다: 행 TTL ≥ 2 × 응답 TTL이면 응답이 만료되는 시점의 행은
    # 반드시 아직 살아 있다(같은 질의의 첫 만료 재생성이 항상 행 히트). 신선도 정직성은
    # 응답과 같은 근거를 쓴다 — 행은 자기 checked_at을 달고 다니고, 그 표기가 그대로
    # 카드로 나간다.
    overview_rows_ttl_s: float = 600.0
    # ③ 이어가기 씨앗(본문 + 내부 id 출처 레지스트리, 약 10KB) TTL. 응답보다 긴 이유는
    # 축이 다르기 때문이다: 씨앗은 **서빙되는 답이 아니라** 사용자가 이미 화면에서 보고
    # 있는 답의 핸드오프다. 그 답의 낡음은 이미 화면에 있고(응답 TTL이 통제한 사실),
    # 여기서 미스가 나면 얻는 것은 신선도가 아니라 404 폴백(새 대화 + 질의 재전송 =
    # 방금 본 답을 잃는다)이다. 1800s = 탭을 열어 둔 채 자리를 비웠다 돌아오는 창.
    # 0 이하 = 이어가기 끔(항상 404 폴백 — 롤백 레버).
    overview_seed_ttl_s: float = 1800.0
    # degraded 판정(음성) 캐시 TTL. 종전엔 degraded가 어디에도 남지 않아 같은 질의의
    # 재조회가 매번 콜드 검색 + LLM + 예산을 다시 태웠다(5종 중 no_citations·irrelevant·
    # timeout은 예산을 소모한다). 캐시하는 값은 **기존 degraded 값 그대로**이고 새 분류를
    # 만들지 않으며, 값별 분기도 두지 않는다(사례 분기 금지) — 하나의 TTL을 5종 중 가장
    # 일시적인 원인(timeout·Yes24 일시 지연)에 맞춰 짧게 잡는 것이 그 대체다.
    # 60s = 반복 조회·봇 연타는 덮고, 일시적 원인은 그 안에 걷힌다. 0 이하 = 음성 캐시 끔.
    overview_negative_ttl_s: float = 60.0
    # 오버뷰 캐시 저장소별 엔트리 상한(LRU 축출 — Yes24TextCache와 같은 계약). 종전
    # _cache는 **무계**였고 일일 예산이 그 증식을 막지 못했다: degraded 엔트리는 예산을
    # 소모하지 않으므로 유니크 질의 봇 1만발이면 엔트리 1만(수백 MB)이 TTL 동안 상주한다.
    # 256 = 일일 생성 예산(300)과 같은 눈금 — 예산이 유계하는 저장소(응답·씨앗)는 애초에
    # 그 이상 쌓일 수 없고, 예산이 유계하지 **않는** 저장소(음성·행)에 같은 천장을 준다.
    # 상방 메모리 = 4저장소 × 256 × (응답 ~20KB + 씨앗 ~10KB + 행 ~60KB) 상당 ≈ 수십 MB.
    overview_cache_max_entries: int = 256

    @model_validator(mode="after")
    def _lock_overview_response_not_staler_than_rows(self) -> "Settings":
        """불변식 **응답 TTL ≤ 행 TTL**을 설정 조립 시점에 잠근다(주석이 아니라 코드로).

        두 TTL이 어긋나면 캐시가 스스로 모순된다: 응답을 10분 서빙하면서 그 응답을 만든
        행은 5분이면 낡았다고 버리는 구성은, "행이 낡았다"는 판단과 "그 행으로 쓴 답은
        아직 신선하다"는 판단을 동시에 주장한다. 신선도의 정직성은 늘 **더 상류**에서
        정해지므로 응답이 행보다 오래 살 수 없다.
        행 캐시가 꺼진 구성(행 TTL 0 이하)에선 모든 생성이 라이브 행에서 나오므로 상류
        신선도의 상한이 없다 — 불변식이 공허참이라 검사하지 않는다.
        """
        if 0 < self.overview_rows_ttl_s < self.overview_response_ttl_s:
            raise ValueError(
                "overview_response_ttl_s는 overview_rows_ttl_s를 넘을 수 없습니다"
                f"(응답 {self.overview_response_ttl_s}s > 행 {self.overview_rows_ttl_s}s) — "
                "응답이 그 근거 행보다 오래 서빙되면 신선도 표기가 모순된다."
            )
        return self
    # KST 일일 LLM 생성 상한(G3 — 봇 유니크 질의 = 비용 폭탄 벡터 봉쇄). 0 이하 = 상한
    # 비활성. 300 ≈ $0.24/일 상방(요청당 ~$0.0008 추정). 캐시 히트는 소진 후에도 정상 서빙.
    overview_daily_budget: int = 300
    # 프롬프트에 동봉하는 파싱 상품 행 상한(컨텍스트·토큰 천장). 설계 원안 §2는 "≤10건"
    # 이었으나 캠페인 웨이브2 실측(2026-08-26)으로 14 확대: 후보 풀 확대(각도당 24행·
    # 2각도)의 효과가 컷 10에서 소멸했다 — 해리포터 전집 SKU(기본 rank20·세트각도 rank6)
    # 가 3/3런 프롬프트 탈락. 토큰 순증은 행당 ~60~100t × 4행으로 W2 프롬프트 다이어트
    # (입력 27.7% 절감) 여유 내다. 첫 delta 영향은 배터리 계측이 판정 정본.
    # 웨이브4 A/B(2026-08-26, 같은 배터리 27케이스 순차 계측)로 14 확정: B팔(cap10,
    # 파생 저신호 임계 5)은 지연 회복 가설이 기각됐고(콜드 run1 첫delta p50 9.98s vs
    # A팔 14의 9.35s, 최악 19.80s vs 14.67s — 베이스라인 7.85s 복귀 실패) 보강 발동도
    # 양 팔 15회 동일(발동 대부분이 0건 사다리·임계 5 미만 저신호라 임계 7→5 무효과).
    # 반면 인용 풍부도는 cap10에서 중앙값 9→7로 손실(데미안 13→10·채식주의자 13→8·
    # 하루키신작 6→4). W2 전집 SKU 생존·W1 구판 11%·W4 억제 2/2·W5 자카드 1.00·SSE
    # 위반 0은 양 팔 동일 — cap10의 유일한 기대 이득(지연)이 없어 14 유지가 우월.
    # 저신호 임계는 파생(cap//2, overview.py) 유지 — A/B가 임계 독립 효과 없음을 보여
    # 별도 config 분리는 비대화라 하지 않는다.
    overview_max_products: int = 14
    # 오버뷰 **전용 검색 예산**(각도당 파스 상한 / 1페이지 요청 크기). 도구는 이 값을
    # 요청 로컬 state 오버라이드로 읽는다(tools/yes24_search.SEARCH_BUDGET_STATE_KEY) —
    # 챗 세션 state엔 그 키가 없어 챗은 search_result_limit·search_page_size로 돈다.
    # 값을 챗과 나눈 이유는 소비 구조가 다르기 때문이다: 오버뷰는 정렬 2각도 이상을 RRF로
    # 병합해 **상위 overview_max_products행만** 프롬프트에 싣는 선별형이라 후보 풀이 넓을수록
    # 하위 rank의 정답 SKU가 살아나고(구글이 인용한 '해리포터 전집(전10권)' 실SKU가 기본
    # 검색 rank 21 — 2026-08-26 캠페인 W2), 프롬프트 천장은 그 상한이 따로 지킨다. 챗은
    # 파스 행이 그대로 프롬프트로 직송돼 같은 폭이 곧 토큰이다(search_result_limit 주석의
    # A/B 수치). 24 = 사이트 기본 1페이지 전량, 40 = 사이트 UI 페이지 크기 옵션
    # (24/40/80/120) 중 rank 21까지 덮는 최소값(파서가 40행 전부 호환임을 실측).
    overview_search_result_limit: int = 24
    # 0 이하 = size 파라미터 미부착(사이트 기본 24건 페이지 — 롤백 레버).
    overview_search_page_size: int = 40
    # 검색 병합(overview._merge_search_results)의 RRF 상수 k — 점수 = Σ 1/(k+rank).
    # 60은 RRF 원논문(Cormack et al. 2009)이 실측 채택하고 RAG-Fusion 구현들이 표준으로
    # 쓰는 값: 상위 rank 간 점수 격차를 완만하게 눌러, 한 각도의 rank 1 잡음이 여러
    # 각도에 공통 등장한 상품(합산 점수)을 누르지 못하게 하는 평활 상수다.
    search_rrf_k: int = 60
    # 병합 순서에서 **소수 kind 행에 곱하는 RRF 가중치**(1.0 = 무강등 롤백 레버).
    # Yes24 검색은 domain=ALL 키워드 색인이라 질의와 무관한 상품이 제목 토큰 하나로 딸려
    # 온다(2026-08-28 실측: '겨울에 읽을 소설 추천' 병합 53행 중 문구/GIFT 5 — 수면양말·
    # 트리장식·무릎담요·책갈피가 프롬프트 동봉 14행 중 4행을 차지해 본문이 '독서 환경 및
    # 소품' 항목으로 정식 편성). 동봉 행 집합에서 **다수를 이루는 kind**(사이트 라벨 그대로,
    # 코드에 값 목록 없음)가 그 질의가 실제로 매칭한 상품 종류라는 구조 신호이고, 소수 kind는
    # 그만큼 뒤로 밀린다 — 제거가 아니라 강등이라 다수 kind 행이 상한을 못 채우면 소수 행이
    # 그대로 동봉된다(비도서가 정답인 질의는 그 kind가 다수라 자동 보존: '아이패드 케이스'
    # 48행 전부 문구/GIFT → 강등 대상 0).
    # 0.8 근거(저장 행 오프라인 스윕, k=60·cap 14): 강등폭은 RRF 점수 격차에 유계라
    # 가중치가 그대로 순위 이동폭이 된다. 겨울 질의의 문구/GIFT가 동봉에서 완전히 빠지는
    # 구간이 w<=0.870, '나니아 연대기 전집'(kind 9종 혼재)에 **강제 적용**해도 정답 SKU
    # (도서 '나니아 연대기')가 동봉 안에 남는 구간이 w>=0.745 — 두 경계의 기하 중앙이 0.805다.
    overview_minority_kind_weight: float = 0.8
    # 오버뷰 단발 콜의 사고(thinking) 토큰 상한. 오버뷰는 파싱 행 요약의 단발 콜이라 사고가
    # 불필요한데, 상한을 명시하지 않으면 사고 모델이 사고 토큰으로 max_output_tokens를
    # 잠식해 본문이 마커 없이 절단된다(2026-08-26 실측: 3.6-flash가 400 중 384를 사고에
    # 소모, finish=MAX_TOKENS 본문 16자 → 6/6 no_citations 비게시. 비사고 flash-lite만 무사).
    # 기본 1 = 사실상 끔의 최소 유효 상한 — 0(완전 끔)은 사고 모델이 INVALID_ARGUMENT로
    # 거부하고(3.6-flash 실콜 400, 2026-08-26), 1은 양쪽 모델 모두 수용 + 사고 토큰 0으로
    # 동작함을 실콜로 확인했다(모델명 분기 없이 안전한 단일 값).
    overview_thinking_budget: int = 1
    # 무인용 재시도 게이트의 잔여 시간 하한. 데드라인 직전 발화한 재시도는 플래너·재검색·
    # 재생성이 어차피 타임아웃이라 지연(+1~15s — 재검색 HTTP는 deadline 밖이라 http
    # 타임아웃 상한만 받는다)과 예산 2콜만 태운다(속도 감사 D3 시나리오, 2026-08-26).
    # 3.0s는 배터리 v3의 재시도 소요 실측(플래너+재검색+재생성 ≈ 2.5~3.5s) 하단 — 이보다
    # 적게 남았으면 재시도가 완주할 수 없다.
    overview_retry_min_remaining_s: float = 3.0
    # **오버뷰 프롬프트 행당** 태그 상한(W2 다이어트). 태그·features의 접지 가치는 실증됐으므로
    # (배터리 v3) 제거가 아니라 상한이다 — 사이트 노출 순서 상위만 남긴다. fixture 실측
    # (2026-08-26): 제외 필드(author_no·goods_no·image_url)와 합쳐 프롬프트 입력 27.7% 절감.
    #
    # **챗에는 이 상한이 없고, 그게 결함이 아니라 설계다**(2026-08-31 적대 감사 지적 → A/B로
    # 기각). 감사는 "한쪽 소비자만 자르는 이중 기준"이라 봤고 나도 그대로 받아 도구 경계로
    # 승격했다가 되돌렸다. 근거 둘:
    # ① **이 레포는 이미 같은 꼴의 비대칭을 의도해서 갖고 있다** — overview_search_result_limit
    #   =24 대 챗 search_result_limit=10(위 주석: "값을 챗과 나눈 이유는 소비 구조가 다르기
    #   때문"). 오버뷰는 RRF 병합 뒤 상위 N행만 싣는 **선별형**이라 후보 풀은 넓히고 프롬프트는
    #   좁히고, 챗은 파스 행이 **그대로 직송**된다. 태그도 같은 축이다.
    # ② **격리 A/B가 상한을 지지하지 않는다**(n=15/팔, 태그 절단 2줄만 다른 팔): 챗에 상한을
    #   걸면 출처 −12.8%·마커 −4.9%로 **두 축 다 점추정이 음수**다. 사전 등록 기준(−15%)에는
    #   안 닿아 "해롭다"고 말할 수 없지만, **이롭다는 근거는 어디에도 없다** — 관측된 문제
    #   없이 넣은 변경은 되돌린다(삭제 우선).
    # 조합 팔에서는 정반대(출처 +23.2%)로 보였다 — 팔 하나에 네 변경을 묶으면 36pp까지 오도한다.
    overview_max_tags: int = 3
    # 캐시 히트·single-flight 대기자 replay의 총 페이싱 시간 상한(ms). 2026-08-26 사용자 —
    # 캐시 즉시 통짜 방출("즉시 인쇄물")이 생성형 UX 문법을 깬다: 내용은 캐시 그대로 유지,
    # **방출만** 짧은 스트리밍으로 페이싱한다. 조각 수·간격은 이 상한에서 유도하므로
    # (overview._replay_chunks) 본문이 아무리 길어도 replay 총 시간이 이 값을 넘지 않는다.
    # 0 이하 = 종전 전문 1-delta 즉시 방출(롤백 레버). 600ms는 목표 구간 0.5~0.8s의 중앙.
    overview_replay_ms: int = 600
    # 관련성 게이트(F-B, 2026-08-26 난타 배터리 — C7형 "부합 상품 없음" 인용 도배 노출):
    # 생성 스트리밍과 병렬로 도는 마이크로 판정 콜이 "유의미 관련 상품 0"을 확정하면 방류
    # 밸브를 열지 않고 전량 폐기한다(degraded="irrelevant"). False = 무게이트 롤백 레버
    # (판정 콜 자체를 스폰하지 않아 종전 동작과 동일). 판정 실패·타임아웃은 fail-open.
    overview_relevance_gate: bool = True
    # 투기 플래너(G-3, 2026-08-26 구글 정면 비교 후속 — 리콜 업그레이드): 재검색 플래너
    # 콜(원 질의만 입력)을 무인용 실패 후가 아니라 **요청 진입 시 1차 검색과 병렬로** 투기
    # 발사한다. 어느 보강 경로든(0건·저신호·무인용) 이미 도착한 제안을 추가 대기 최소로
    # 소비하고, 해피패스면 미소비 폐기된다(web_prefetch 선례 — 미소비 무해, 첫 delta +0).
    # **비용 순증 정직 명기**: 생성 질의당 초소형 구조화 콜 +1 — 상방은 캐시 single-flight
    # 수렴 + 예산 소진 시 미스폰으로 "예산 잔여 중의 유니크 질의 요청" 수에 유계다.
    # False = 플래너 완전 끔(축약 사다리만 — 종전 사후 직렬 플래너 콜은 중복 구현 금지로
    # 삭제됐으므로 이 레버가 유일한 플래너 스위치다).
    overview_speculative_planner: bool = True
    # 검색 워밍(/overview/warm)의 **동시 워밍 상한**. 0 이하 = 무제한(롤백 레버).
    # 워밍은 인증만 있는 오픈 엔드포인트이고 익명 레이트리밋은 아직 없다(§8.2 B) — 상한이
    # 없으면 유니크 질의 폭주가 요청당 태스크 1개 + Yes24 2콜(size=40 SERP)을 무제한
    # 적립하고, 공유 클라이언트의 rps 리미터(http_rps)가 그것을 직렬화하는 동안 같은 락
    # 뒤에 선 **챗·오버뷰 본 요청의 검색**이 백로그만큼 굶는다. 초과분은 드롭한다(워밍
    # 실패는 무해 — warm_search의 자기 계약). 8 = 워밍 1건이 Yes24 2콜이므로 상한에서도
    # 백로그가 16콜 ≈ http_rps(4.0) 기준 4초 — 타이핑 중 사용자 여러 명을 흡수하면서
    # 본 요청 대기를 왕복 수 초 안에 묶는 값. 같은 질의 연타는 상한과 무관하게 dedup된다.
    overview_warm_max_inflight: int = 8
    # 상세 보강(G-4) 프롬프트에 얹는 상품 소개글 발췌의 행당 문자 상한. fetch_max_chars
    # (도구 응답 예산 6000)와 다른 축이다 — 오버뷰 프롬프트는 행 여러 개 × 발췌라 행당
    # 상한이 총 입력을 지배한다(우연히 비슷해도 서로 따라가면 안 된다). 600자 ≈ 소개글
    # 도입 1~2문단 — 줄거리 서술 근거로 충분하면서 열람 상한(fetch_many_max_items=5)
    # 전부 동봉해도 +3,000자 상방이라 오버뷰 컨텍스트 천장을 지킨다.
    overview_detail_excerpt_chars: int = 600
    # 상세 보강 이어쓰기(H15)의 **벽시계 예산**(초) — 열람(fetch_many) + 2차 생성 전체.
    # overview_timeout_s(첫 유효 마커 방류 전 데드라인) 재사용 금지 근거: 이어쓰기는 1차
    # 본문이 **이미 방류된 뒤**의 구간이라 그 데드라인의 정의역 밖이었고, 그 결과 열람 구간에
    # 벽시계 상한이 아예 없었다 — 2026-08-27 배터리 실측("채식주의자 줄거리" run1): 상세
    # 1건이 Yes24 측 지연으로 16.4s(단독 프로브 재현 10.5s) 걸리자 첫 delta 2s 뒤에도 SSE
    # 스트림이 총 20.23s까지 열린 채 대기했다(fetch_many는 gather라 최장 1건이 배치를
    # 지배하고, 개별 http_timeout_s 15s는 이보다 크다). 6.0s = 정상 소요 합의 2배 여유 —
    # 같은 질의 콜드 실측(2026-08-27 8082 스모크): 열람 5건 1.60s + 2차 생성 1.21s = 2.81s,
    # 총 5.43s(첫 delta 2.66s). 이 값은 개별 HTTP 타임아웃보다 작아야 실효를 갖는다 —
    # 초과 시 열람·2차를 취소하고 1차 본문만으로 정상 마감한다(degraded 아님).
    # 0 이하 = 이어쓰기 사실상 끔(즉시 초과 → 항상 1차 마감, 롤백 레버).
    overview_append_budget_s: float = 6.0
    # 그 예산 중 **열람 몫**(초) — 나머지가 2차 생성 몫이다. 하나의 벽시계를 열람이 끝까지
    # 먹어치우면 2차 콜이 즉시 데드라인이라 이어쓰기가 통째로 사라진다: 2026-08-27 확인
    # 배터리 "채식주의자 줄거리"에서 5건 중 4건이 0.83s에 도착했는데 1건이 14.35s를 끌어
    # 6.0s 전량을 소진했고(도착분 4건도 함께 취소) 줄거리 단락이 0문장이 됐다. 개별 상세
    # 열람 실측 25건은 p50 811ms·p90 2,425ms인데 꼬리만 14.4s·16.4s로 **이봉**이라, 꼬리를
    # 덮는 예산 상향(16s+)은 사용자 대기의 직접 회귀고 건수 축소는 꼬리 확률만 낮춘다 —
    # 몫을 끊고 **도착분을 수확**하는 것이 구조적 답이다(_harvest_details). 3.0s = p90
    # 2.43s 위이면서 꼬리 대역(14s+) 아래의 이봉 골짜기 — 남는 ≥3.0s는 2차 생성 실측
    # (1.0~1.4s)의 2배 여유다. 0 이하 = 열람 즉시 포기(1차 본문만, 롤백 레버).
    overview_detail_fetch_budget_s: float = 3.0
    # 관련성 판정 콜 타임아웃(초). overview_timeout_s(생성 데드라인) 재사용 금지 근거:
    # 게이트는 fail-open이라 이 값의 의미는 "판정 지연이 방류를 최대 얼마나 붙잡을 수
    # 있나"의 상한이다 — 마커가 먼저 온 본문도 판정 도착까지 방류 보류되므로(레이스 보류
    # 원칙) 생성 데드라인 10s를 그대로 쓰면 판정기 행이 최악 첫 delta +10s가 된다. 서로
    # 다른 축이라 따로 둔다. 3.0s는 같은 모델·같은 초소형 구조화 콜인 재검색 플래너의
    # 소요 실측(~1.1s, 배터리 v3) 여유 상한.
    overview_relevance_timeout_s: float = 3.0
    # 관련성 판정 콜의 출력 상한(토큰). 생성용 overview_max_output_tokens(600) 재사용 금지
    # 근거: 판정 출력은 bool 몇 개 + 짧은 재검색어 + reason 한 구절의 구조화 JSON이라 크기
    # 축이 본문 생성과 다르다 — 실콜 계측(2026-08-26, flash-lite 4질의): 66~72토큰이라
    # 128로 잡았다. **2026-08-28 회수 도달 축(A·B)으로 스키마가 커지며 이 상한이 실제로
    # 터졌다**: bool 2개 + queries 배열(최대 3개)이 늘어 무도달 판정 응답이 128을 넘겼고,
    # 절단된 JSON이 파싱에 실패해 게이트가 통째로 fail-open으로 강등됐다(라이브 3/3런
    # JSONDecodeError — 판정 자체는 맞았는데 전달되지 못한 조용한 무력화). 실측 절단 응답이
    # ~128이므로 256 = 그 2배 여유다. 상한은 천장일 뿐이라(생성분만 과금·전송) 여유를 넓게
    # 잡는 비용은 0이고, 빠듯하게 잡는 비용은 기능 전체의 침묵이다 — 위험이 비대칭이다.
    overview_relevance_max_output_tokens: int = 256
    # 오버뷰 3콜(생성·플래너·게이트) 공통 샘플링 seed. None = 미전달(롤백 레버). 기본
    # 고정값(값 자체는 임의 — 24는 무의미 상수)은 캠페인 웨이브2 실측(2026-08-26) 후속:
    # 같은 질의 반복런의 출처 자카드가 0.78→0.30으로 흔들렸고, seed 미전달의 샘플링
    # 변동이 그 한 축이라 기본 고정으로 조성 변동을 **best-effort** 억제한다(W5). 정직한
    # 기대치: seed 고정은 벤더 best-effort라 완전 재현을 보장하지 않고(공식 문서), RECENT
    # 각도의 SERP 시변·프롬프트 행 변화는 어차피 seed 밖이라 자카드가 1.0이 되진 않는다 —
    # 샘플링 축 하나를 줄이는 완화일 뿐이다. 실콜 확인(2026-08-26): flash-lite가 seed
    # 인자를 수용(INVALID_ARGUMENT 없음).
    overview_seed: int | None = 24
    # 접지원 비교 하네스(테스트 전용 — 2026-08-28 사용자 요청 "웹 도구 / yes24 검색 결과 /
    # 둘 다 3개 보이게끔 해서 차이 보게"). 같은 질의를 세 접지원(web·yes24·both)으로 각각
    # 생성해 데모에서 나란히 렌더하고, "웹을 오버뷰에 넣는 게 나은가"를 논쟁이 아니라 화면에서
    # 판단한다. **기본 False**의 근거는 비용·지연이 요청당 3배라는 것이다 — 팔마다 독립
    # 생성 콜이 돌고 web·both 팔은 그라운딩 서브콜까지 더 탄다(캐시 키가 팔별로 갈리므로
    # single-flight 수렴도 팔 안에서만 일어난다). 프로덕션 기본 동작은 불변이다: /overview의
    # sources 미지정 = 현행 yes24 단일 팔이고, 이 스위치가 off면 sources 필드 자체가 400이라
    # 우회 경로가 없다(비교 뷰 노출 게이트인 GET /overview/compare도 함께 미등록).
    # 생성 스택(검색 → 홀드 스트리밍 → 인용 검증 → done)은 팔 무관 **공유**다 — 팔이 바꾸는
    # 것은 프롬프트에 실리는 행의 출처뿐이라 SSE·인용·예산 계약이 팔마다 그대로 성립한다.
    overview_compare_enabled: bool = False

    # 에러 구동 반응형 재시도: pro 경로가 Gemini 과부하/일시장애(429/5xx)로 첫 응답조차 내지
    # 못하면 같은 pro로 딱 1회 조용히 재시도한다. off면 곧장 정직 안내(error+done).
    error_fallback: bool = True
    max_llm_calls: int = 50  # ADK RunConfig 상한

    # 출처·인용
    # 세션 source_id의 시작 번호. 값 자체엔 의미가 없고, **자릿수가 모델 행동을 바꾼다** —
    # 1부터 매기면 `[n]` 마커가 각주 카운터로 재해석돼 언급 순서대로 번호가 붙지만, 3자리는
    # 식별자로 취급된다. 2026-08-13 오프셋 A/B(48런, Luna 다중검색 종합): 순번 인용 7/24 →
    # 0/24(p=0.0094), 인용 소실 0, flash 중립 12/12. 비용·지연 변화 없음. 상세는
    # docs/known-limitations.md 2026-08-13 절.
    # **"인용 소실 0"의 모집단은 다중검색 종합 턴뿐이다** — 짧은 단일 추천 턴에선 각주
    # 습관이 잔존해 무효 마커 제거로 인용이 소실된다(R5 재현 프로브 2/10, 오지정 승격은 0).
    # 오프셋의 비용은 유형 의존이며, 그 결함의 처방은 이 값이 아니라 검증·재작성 층이다.
    # 1이면 종전 동작(1부터 발급) — 이 필드가 롤백 레버다.
    source_id_base: int = 101

    # Yes24 크롤링
    yes24_base_url: str = "https://www.yes24.com"
    # 브라우저형 UA로 바꾸면 302가 난다(실측) — 이 값은 조정 대상이 아니다.
    user_agent: str = "Mozilla/5.0 (compatible; yes24-agent/0.1)"
    http_timeout_s: float = 15.0
    http_connect_timeout_s: float = 5.0
    # 동시 연결 상한. rps와 **함께** 정중함 경계를 잡는다 — rps를 올려도 이 세마포어가 남는다.
    http_concurrency: int = 5
    # 채팅 경로의 Yes24 요청률. **1.5 → 4.0(2026-08-03, cc38952)** — 근거를 여기에 둔다
    # (종전엔 이 블록에 주석이 하나도 없는데 matrix_runner가 "공유 클라이언트 예산"이라며
    # 이 필드를 근거의 집으로 가리키고 있었다 — 2026-08-31 적대 감사 지적).
    # 조건 검증형 질의("300쪽 이하·2만원 이하 소설 2권"처럼 후보마다 상세를 여는 것)는 한 턴에
    # 20요청까지 가므로 0.667초 간격이 그대로 누적됐다 — 냉 캐시 HTTP 구간의 절반이 스로틀
    # 대기였다. 냉 캐시 통제 A/B(yes24_cache_ttl_s=0, 같은 세션 연속 3런): HTTP 구간 중앙
    # 12.0s → 5.7s, 라운드당 2.4s → 1.4s. 정중함은 유지된다 — 같은 egress IP로 매트릭스가 이미
    # matrix_http_rps를 쓰므로 채팅 4.0은 그보다 보수적이고 http_concurrency가 그대로 남는다.
    http_rps: float = 4.0
    # 매트릭스 경로 전용 Yes24 처리량. 매트릭스는 채팅 파이프라인을 16 페르소나로 **동시**
    # 실행하는 개발 확인 화면이라, 전역 rps=1.5의 단일 throttle_lock이 16셀의 Yes24 요청을
    # 0.667초 간격으로 직렬화해 총 벽시계를 단일 채팅의 3배+로 끌어올렸다(2026-07-24 실측:
    # rps만 상향해도 89→60초, concurrency는 throttle 뒤에 가려 단독 효과 0). 매트릭스
    # 셀에서만(contextvar) 이 값으로 클라이언트를 띄워, rps 인공 직렬화 대신 concurrency
    # 세마포어가 정중함 경계가 되게 한다(≤16 동시 연결 = 대형 상용 사이트 허용 dev 버스트).
    # 채팅도 2026-08-03에 1.5 → 4.0으로 올렸다. 2026-07-24의 "채팅은 throttle을 사실상 안
    # 밟는다(27초 불변)"는 **가벼운 질의 기준**이었고, 조건 검증형 질의(후보별 상세 열람)는
    # 한 턴에 20요청까지 가므로 0.667초 간격이 그대로 누적된다. 냉 캐시 통제 A/B(3런씩):
    # HTTP 구간 12.0 → 5.7초, 라운드당 2.4 → 1.4초. 같은 egress IP로 매트릭스가 이미 16을
    # 쓰므로 채팅 4.0은 그보다 훨씬 보수적이고, 동시 연결 상한(http_concurrency=5)은 불변이라
    # 정중함 경계는 세마포어가 계속 잡는다. 하드코딩 금지(원칙 6) 준수 config 필드.
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
    # 메모리 상한 ≈ 페이지 크기 × 상한이다. 라이브 `sys.getsizeof` 실측(2026-08-03): 상품
    # 상세 557K자=1.11MB, 검색 결과 1.16M자=2.32MB로 평균 **1.71MB/엔트리** → 64엔트리
    # 최악 **≈110MB**(검색 페이지로만 차면 148MB). 한글 본문은 CPython이 UCS-2로 담아
    # 문자당 2바이트라 원본 HTML 바이트 크기로 추정하면 절반 이하로 과소평가된다 —
    # 이 주석의 초판이 그 오류였다(fixture 183~598KB 기준 "20~75MB"로 적었다).
    # 같은 박스에 세션 DB가 이미 GB 단위로 있으므로 여유를 크게 잡을 이유가 없다.
    # 64인 근거: 매트릭스 한 실행이 실제로 만지는 고유 URL이 실측 10~12개다(weekend 1런에서
    # 16셀이 인용한 수 — 인용은 fetch의 하한이라 실제는 더 많지만 자릿수는 같다). 64면 실행
    # 1회 + 동시 채팅을 덮고도 남는다. 처음 128로 잡았다가 독립 감사 지적으로 절반으로 줄였다
    # (표적 효과는 그대로이고 최악 메모리만 반감 — 2026-08-03).
    yes24_cache_max_entries: int = 64
    # 검색 1페이지 요청 크기(size GET 파라미터) — **챗 예산**. 0 이하면 파라미터를 붙이지
    # 않아 사이트 기본(24건) 페이지를 받는다. 챗은 파스 상한이 아래 search_result_limit이라
    # 더 넓은 페이지를 받아도 버릴 행만 늘고 다운로드·파싱만 비싸진다(2026-08-27 실측: 같은
    # 질의 495KB → 657KB, +33%). 넓은 풀이 필요한 소비자(오버뷰)는 자기 예산
    # (overview_search_page_size)을 state로 실어 보낸다 — tools/yes24_search.py 예산 주석.
    search_page_size: int = 0
    # 파스 시점 결과 상한(각도당) — **챗 예산**이자 챗 프롬프트 토큰의 주 변수다(파스 행이
    # 그대로 도구 결과 → 프롬프트로 직송되고, 컨텍스트는 라운드마다 재전송되므로 행 순증이
    # 턴당 여러 번 청구된다). 오버뷰 캠페인(2026-08-26 W2)이 24로 올렸던 것을 2026-08-27
    # 페어 A/B로 10에 복귀시켰다 — 같은 질의·2반복 인프로세스 계측(턴 전체 LLM 콜 프롬프트
    # 합)에서 24는 추천 질의 59.6k → 75.5k(+26.7%), 상세 질의 43.8k → 45.3k(+3.5%)를
    # 물렸고 그 대가로 얻은 것이 없었다: 소요는 추천 18.5s → 20.4s로 오히려 늘고 상세는
    # 17.2s → 16.7s로 줄어(계측 노이즈), 인용 출처 수는 6·4 → 5·5로 동률이었다. 도구
    # 결과 블록만 떼어 센 값(같은 검색 HTML, 각도 1개)은 3.1~3.4k → 7.4~8.0k다.
    # 오버뷰는 자기 예산(overview_search_result_limit=24)을 유지한다 — 소비 구조가 달라서다.
    search_result_limit: int = 10
    # 한 번의 yes24_search 호출에서 동시에 던질 검색 각도(쿼리) 수 상한. 탐색 각도 하나당
    # LLM 왕복을 1회씩 소모하던 직렬 구조가 추천 경로 지연의 최대 덩어리였다(2026-07-20 실측:
    # 검색만 5라운드 직렬). web_search_max_queries가 웹 검색에 하는 역할의 Yes24판 —
    # 컨텍스트·지연·Yes24 요청 폭발을 막는 천장이며, 그 대칭으로 같은 기본값을 쓴다.
    # 공유 Yes24Client의 동시성 Semaphore(http_concurrency=5) 안에 들어가는 폭이기도 하다.
    # 초과분은 조용히 버리지 않고 dropped_queries로 명시한다(fail-loud).
    yes24_search_max_queries: int = 4
    # 코너 목록 반환 상한. **초기값·근거 미기록**(e9cf930). 위 search_result_limit는 24→10
    # 되돌림 A/B가 있는데 이쪽은 그 검토를 받은 적이 없다.
    browse_result_limit: int = 10
    # yes24_browse 결과에 싣는 카테고리 내비(이름·번호) 상한. 페이지의 카테고리 트리는
    # 144개+라 전부 실으면 도구 결과가 비대해진다 — 상위·중분류가 앞서 렌더되므로 문서
    # 순서 상위만 담아도 분야 좁히기(소설/경제 등)는 충분하다.
    browse_categories_limit: int = 60
    # yes24_fetch 본문 상한. **초기값 그대로이고 근거가 기록된 적 없다**(e9cf930 스쿼시) —
    # 아래 web_fetch_max_chars가 이 값을 '빌려 쓰지 않으려고' 분리됐을 뿐, 6000 자체의 근거는
    # 미상이다. 조정 대상이 되면 먼저 재검토할 자리다(2026-08-31 적대 감사 표시).
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
    # 요청 하나의 상한이자 **보강 전체의 wall-clock 예산**이다(노브 하나로 표시 메타에 쓸
    # 시간을 정한다). 요청별 상한만으로는 청크가 느리게 계속 흘러오는 서버를 못 막는다.
    web_title_fetch_timeout_s: float = 3.0
    # 제목 보강 스트림 읽기 바이트 상한 — </title>이 안 나와도 이만큼 받으면 끊는다(표시
    # 보강 전용 요청이 본문 전체를 내려받지 않게). 64KiB면 head 블록은 넉넉히 덮는다.
    web_title_fetch_max_bytes: int = 65536
    # 표시용 출처 제목 길이 상한(문자). status_detail_max_chars(진행 라벨)와는 다른 축이다 —
    # 우연히 같은 값이어도 서로를 따라가면 안 된다.
    web_title_max_chars: int = 120
    # 그라운딩 서브콜 시도 횟수 상한(재시도 = attempts-1). 일시 5xx·빈 근거를 흡수하는
    # 값으로, Yes24 재시도(http_max_retries)와 같은 성격의 상한이라 config에 둔다.
    web_grounding_max_attempts: int = 2
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
    # 웹 검색 서브콜 상한. **초기값·근거 미기록**(e9cf930). 그라운딩 전환(2026-07-28) 이후
    # 실제 서브콜 지연과 대조된 적 없다.
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
    # 내장 프론트(UI 페이지·정적 파일·로그인월) 서빙 여부. False면 main.py가 UI 계열 라우트를
    # 등록하지 않아 404가 되고 API(/chat/stream ·/health ·/models ·/toolsets)만 남는다.
    # 백엔드 전용 배포는 env `SERVE_FRONTEND=false`(2026-08-12 사용자 결정 — 프론트는 코드를
    # 두고 라우트만 끈다). 로컬 개발 기본은 True(내장 UI로 도그푸딩).
    serve_frontend: bool = True
    # 공유 패스워드 로그인월. 빈 문자열이면 **비활성**(로컬 개발 기본 — 월 없음), 값이 있으면
    # 활성화돼 미들웨어가 보호 경로(/ ·/matrix ·/chat/*)를 쿠키로 가린다. env `ACCESS_PASSWORD`로
    # 주입한다(하드코딩 대신 env). 진짜 인증이 아니라 데모 접근을 막는 단일 공유 비밀번호 게이트다.
    access_password: str = ""
    # 로그인월의 두 번째 비밀번호(세팅 조정용, env `ADMIN_ACCESS_PASSWORD`). 이 값으로 로그인한
    # 세션만 모델 선택·도구 토글·모델명 노출(/models·/toolsets·done.model)이 허용되고,
    # access_password(데모) 로그인에는 전부 숨긴다. 빈 문자열이면 역할 구분 없음(모든 로그인이
    # 세팅 접근 가능 — 기존 동작). access_password가 켜져 있을 때만 의미가 있으며,
    # admin_password(/admin 운영 데이터 열람)와는 별개다.
    admin_access_password: str = ""
    # 데모 로그인(access_password) 세션에 강제되는 앱 구성(페르소나·도구). 역할 분리가 활성일
    # 때(두 비밀번호 모두 설정) 데모 세션은 서버 기본(agent_persona·enabled_toolsets)·요청
    # 필드와 무관하게 이 구성으로 고정된다 — 브랜딩·프롬프트 정체성·도구가 전부 여기서
    # 파생된다. 세팅 로그인·로그인월 비활성은 기존대로 서버 기본을 따른다.
    demo_persona: str = "yes24"
    demo_enabled_toolsets: list[str] = ["yes24", "web"]
    # 로그인 쿠키 유효기간(초). 데모 접근 게이트라 재로그인 성가심을 줄이되 무한은 아니게 7일.
    access_cookie_max_age_s: int = 7 * 24 * 60 * 60
    # 로그인·admin 쿠키에 `Secure`를 붙일지. **기본 False가 의도**다 — 현재 배포(deploy-mq.sh)는
    # 리버스 프록시·TLS 없이 평문 http로 노출돼, Secure를 켜면 브라우저가 쿠키를 버려 로그인이
    # 통째로 깨진다. TLS 종단(프록시·ALB)이 생기면 env `COOKIE_SECURE=true`로 코드 수정 없이
    # 켠다. httponly·samesite는 평문에서도 안전해 플래그 없이 항상 켜져 있다.
    cookie_secure: bool = False

    # 세션 영속
    # sqlite 기본은 **로컬 개발용**이고, 배포는 env `SESSION_DB_URL`(MySQL)로 덮는다.
    session_db_url: str = "sqlite+aiosqlite:///./data/sessions.db"  # async 드라이버 접미사 필수
    # 세션 서비스 생성 실패 시 InMemory 폴백을 허용할지. 파일 sqlite에선 폴백이 "그래도 기동"
    # 이지만, 네트워크 DB에선 조용한 폴백이 **영속 중이라 믿는 비영속 서비스**를 만든다 —
    # 대화가 재시작마다 증발하고 admin·집계는 위장 정상이 된다. 배포에선 env
    # `SESSION_FALLBACK_ALLOWED=false`로 꺼서 기동 자체를 실패시킨다(fail-fast).
    session_fallback_allowed: bool = True

    # 인증(crema-ai 계약 이식). 프론트가 보내는 헤더 `x-api-key`의 값은 Yes24 service_cookie이며,
    # 그 값으로 Yes24 회원 API를 조회해 userNo(= ADK 세션의 user_id)를 얻는다. 인증 DB(users·
    # rate_limit_log)는 **세션 DB와 같은 계정·database**를 쓰므로 접속 정보를 따로 두지 않고
    # session_db_url을 파싱한다(설정 단일 출처). 그래서 session_db_url이 mysql이 아니면
    # (로컬 sqlite 개발) 인증 스택 전체가 자연히 꺼지고 모든 요청이 익명으로 흐른다.
    yes24_user_info_url: str = "https://api.yes24.com/digital/user/info"
    yes24_user_info_timeout_s: float = 10.0
    # 회원 정보(userNo·userId) 재조회 주기(시간). users.user_cached_at이 이보다 오래되면
    # 다음 인증 때 Yes24를 다시 물어 갱신한다.
    yes24_user_cache_hours: int = 24
    # 개발용 API 키 — **비우면 비활성**(배포 기본). 값이 있으면 그 키 하나만 Yes24 회원 조회를
    # 건너뛰고 `dev_api_user_no`로 식별된 것으로 취급한다. 프론트가 로컬에서 개발하려면
    # x-api-key가 필요한데 진짜 ServiceCookies를 꺼내 오게 하는 건 무리라, 그 통로를 하나 연다.
    # DB에 손으로 행을 심는 대신 설정으로 두는 이유: 환경마다 켜고 끄기가 구조로 되고(빈 값 =
    # 분기 자체가 없음), 값이 레포에 남지 않으며, 폐기가 env 한 줄이다.
    # 나머지 판정(레이트리밋·is_active·세션 소유권)은 일반 키와 **완전히 같은 경로**를 탄다.
    dev_api_key: str = ""
    # 개발 키가 가장할 사용자 번호. 실 회원과 겹치지 않게 9자리 대역을 쓴다 — 대화·피드백이
    # 이 번호 밑에만 쌓여 실사용자 데이터와 섞이지 않는다.
    dev_api_user_no: str = "990000001"
    # api_key → 사용자 in-memory 캐시 TTL(초). 이 창 안의 재요청은 users 조회를 건너뛴다
    # (rate limit 체크는 캐시와 무관하게 매 요청 DB에서 센다).
    auth_cache_ttl_s: float = 300.0
    # 인증 DB 커넥션 풀 상한. 세션 DB 풀(SQLAlchemy)과 별도로 잡히는 aiomysql 풀이라,
    # 요청당 짧은 조회 몇 건이 전부인 용도에 맞춰 작게 둔다.
    auth_pool_max: int = 5
    # aiomysql 풀(인증·사용량 공통) 접속 수립 상한(초). 드라이버 기본은 None = OS TCP
    # 타임아웃(~75s+)이라, RDS가 TCP 블랙홀(SG 오설정 등)이면 인증 503과 종료 배수가
    # 분 단위로 매달린다 — 같은 AZ/VPC의 정상 접속은 밀리초 단위라 초 단위면 넉넉하다.
    mysql_connect_timeout_s: float = 5.0
    # 신규 자동등록 사용자에게 부여하는 기본 rate limit(분당·일당). 기존 사용자는 users
    # 행의 값이 우선한다 — 여기 값은 등록 시점의 초기값이자 행이 비었을 때의 폴백이다.
    rate_limit_rpm: int = 20
    rate_limit_rpd: int = 500

    # 사용자별 대화 부가 데이터(user_data.py → turn_feedback·session_ui) 커넥션 풀 상한.
    # 두 테이블이 한 서비스·한 풀인 이유는 같은 DB·같은 사용자 스코프·같은 실패 정책이기
    # 때문이다(나눠 뒀더니 같은 MySQL에 풀만 두 개가 됐다). 접속 정보는 인증·사용량과 같이
    # session_db_url 파싱(단일 출처)이라 mysql이 아니면 스택이 자연 비활성이다. 쓰기는 사용자
    # 행동당 upsert 1건, 읽기는 목록·복원당 1질의뿐이라 usage와 같은 최소 크기로 둔다.
    # auth·usage와는 풀을 나눈 채로 둔다 — auth는 전 요청의 임계 경로이고 usage는 폭주할 수
    # 있는 백그라운드 기록이라, 한쪽의 고갈이 다른 쪽을 굶기지 않게 격벽을 둔다(의도된 분리).
    user_data_pool_max: int = 2
    # 대화 목록(GET /chat/sessions) 응답 세션 수 상한(최근 갱신순 앞에서 자름). ADK
    # list_sessions는 사용자 전체를 돌려주므로 장수 사용자의 목록 한 장이 무한히 크지 않게
    # 천장을 둔다 — admin_page_size(운영 조회 페이지)와는 다른 축의 값이라 따로 둔다.
    history_sessions_limit: int = 100

    # 토큰 사용량 기록(usage.py → usage_log 테이블) 커넥션 풀 상한. 접속 정보는 인증과
    # 같이 session_db_url을 파싱하므로(단일 출처) mysql이 아니면 스택 전체가 비활성이다 —
    # 별도 on/off 스위치는 두지 않는다(죽은 레버 금지, 구조 분기: mysql=on). 쓰기는
    # fire-and-forget 단건 INSERT뿐이라 auth 풀(5)보다도 작게 둔다.
    usage_pool_max: int = 2
    # 앱 종료 시 usage 기록 배수(drain) 상한(초). DB 장애 중 쌓인 INSERT task를 상한
    # 없이 기다리면 SIGTERM 후 lifespan 종료가 매달려 오케스트레이터 SIGKILL(배포 지연)
    # 로만 끝난다. 상한 초과분은 취소한다 — 부가 채널이라 기록 유실이 종료 지연보다 싸다.
    usage_close_timeout_s: float = 5.0

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
    # 브라우저가 API를 부를 수 있는 오리진 목록. `*`+credentials 조합은 브라우저가 거부하므로
    # 명시 목록만 쓴다. 기본값에 프론트 개발 서버들의 관례 포트를 담아 둔다 — 하나만 두면
    # 프론트가 Vite(5173)나 다른 포트를 쓰는 순간 원인 모를 CORS 차단을 만난다. 배포에서
    # 실제 도메인을 붙일 때는 `CORS_ORIGINS` 환경변수로 통째로 바꾼다(하드코딩 금지).
    cors_origins: list[str] = [
        # 실제 프론트(2026-09-02 chat-test.yes24.com 번들 확인). Next.js 앱이 `ServiceCookies`
        # 쿠키를 document.cookie로 읽어 `x-api-key` 헤더에 실어 보낸다 — 쿠키가 HttpOnly가
        # 아니라 도메인이 달라도 성립하고, baseUrl만 우리 주소로 바꾸면 계약이 그대로 맞는다.
        "https://chat.yes24.com",
        "https://chat-test.yes24.com",
        # crema-test는 처음엔 "crema DEV에 붙는 별도 프론트"로 보고 넣지 않았는데, 실제로는
        # 우리 API로 갈아탄 프론트였다(2026-09-03 프론트 개발자 CORS 에러 보고). 로그에서 그
        # 오리진을 보고도 용도를 넘겨짚어 뺀 것이 원인 — 붙을 수 있는 프론트는 다 넣는다.
        "https://crema-test.griplabs.io",
        # 프론트 개발 서버 포트. 3002·3003·3010은 **관례가 아니라 실측**이다 — crema DEV의
        # Caddy 로그(2026-09-02, 24시간)에서 프론트 개발자들이 실제로 그 포트로 붙고 있었다.
        # 우리 API로 갈아타는 순간 목록에 없는 포트는 브라우저가 막으므로 미리 넣어 둔다.
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3002",
        "http://127.0.0.1:3002",
        "http://localhost:3003",
        "http://127.0.0.1:3003",
        "http://localhost:3010",
        "http://127.0.0.1:3010",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    # 진행 status detail 상한(문자). 사고 요약 라벨·검색 각도는 모델이 쓴 자유 텍스트라
    # 길 수 있는데, 진행 타임라인 한 줄은 짧아야 읽힌다. 문구를 만들지 않고 길이만 자른다.
    status_detail_max_chars: int = 120
    # SSE 연결 상한. **초기값·근거 미기록**(e9cf930). 긴 조사 턴의 실제 소요와 대조된 적 없다.
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
    openai_api_key: str = ""  # LiteLLM 경로(openai/*) 모델용
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


def ensure_openai_api_key_env() -> str:
    """litellm이 기대하는 `OPENAI_API_KEY` 환경변수를 설정하고 사용된 키를 반환한다.

    pydantic-settings는 .env를 os.environ으로 내보내지 않으므로, LiteLLM 경로
    (selectable_models의 "openai/*" 모델) 사용 시 여기서 매핑한다. 키가 없어도
    예외를 던지지 않는다 — Gemini 전용 운용에선 이 키가 필요 없다.
    """
    key = os.environ.get("OPENAI_API_KEY") or get_settings().openai_api_key
    if key:
        os.environ["OPENAI_API_KEY"] = key
    return key


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
