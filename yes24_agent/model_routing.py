"""답변 모델 선택과 폴백 — 런타임 모델 설정, 메인 모델 쿨다운, 벤더 일시 오류 판정.

2026-10-17 Gemini 월 지출 한도 429로 3시간 동안 모든 턴이 실패했고, 운영자가 모델을 바꿀 길은
재배포뿐이었다. 그래서 두 가지를 둔다.

- **런타임 설정**(`model_settings` 행 1개, owner 어드민이 쓴다): 기본 모델·폴백 모델을 재시작 없이
  바꾼다. 서버들은 짧은 TTL(config model_settings_ttl_s) 캐시로 읽어 수렴하고, 읽기가 실패하면
  config 값으로 돈다(요청을 실패시키지 않는다 — WARNING만).
- **폴백**: 메인 모델이 첫 진행·본문 프레임 전에 벤더 일시 오류로 실패하면 같은 턴을 폴백 모델로
  한 번 다시 돈다. 429면 쿨다운 동안 메인을 건너뛴다(웹검색 그라운딩 쿨다운과 같은 모양 —
  web_search._mark_grounding_quota_exhausted).
- **호출 재시도**(CallRetry): 그보다 먼저, 실패한 LLM 요청 한 번만 같은 모델로 짧게 다시 부른다.
  턴 중간(도구 라운드 뒤) 503도 살린다 — 턴 재실행이 아니라 SSE 프레임에 닿지 않는다.

우선순위: 요청의 명시 선택(화이트리스트 통과값) > 런타임 설정 > config(model_name·fallback_model).
명시 선택한 턴은 폴백하지 않는다 — 그 선택은 모델 비교·QA의 레버라, 조용히 다른 모델로 답하면
비교가 오염된다(실패는 error로 정직하게 드러난다).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import sys
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx
from google.genai.errors import APIError

from yes24_agent.config import Settings, get_settings
from yes24_agent.db import LazyAiomysqlPool
from yes24_agent.session_service import mysql_pool_kwargs

logger = logging.getLogger(__name__)

# 벤더 일시 오류로 보는 상태코드. 408=타임아웃, 429=레이트·쿼터·지출 한도, 5xx=일시 서버 오류,
# 529=Overloaded. 400·401·403·404 같은 영구 오류는 다시 해도 같아서 제외한다.
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
_QUOTA_STATUS = 429

# 감사 기록(admin_audit.target_type) — 모델 설정 변경.
AUDIT_TARGET = "model"
# model_settings는 행이 하나다(DDL scripts/model_routing.sql이 id=1을 심는다).
SETTINGS_ROW_ID = 1
SETTINGS_COLUMNS = (
    "default_model", "fallback_model", "service_tiers", "reasoning_efforts", "prompt_versions"
)
# 모델별 맵 열({모델: 값}, 정본 직렬화 JSON 문자열) → (config 기본 맵 필드, 허용 값 필드).
# 처리 등급·사고 강도·프롬프트 버전 바인딩이 같은 기계(저장·단일 판정·병합·감사·TTL 캐시)를 쓴다.
# 프롬프트 버전은 config 기본도 허용 값 목록도 없다("" — 바인딩은 런타임 전용, 값 = 버전 id).
PER_MODEL_COLUMNS = {
    "service_tiers": ("litellm_service_tiers", "litellm_service_tier_options"),
    "reasoning_efforts": ("litellm_reasoning_efforts", "litellm_reasoning_effort_options"),
    "prompt_versions": ("", ""),
}
# 프롬프트 버전 바인딩의 "모든 모델" 키 — 모델별 키가 없을 때 쓰인다(prompt_version.model NULL만).
ANY_MODEL = "*"
_SELECT_SETTINGS = (
    f"SELECT {', '.join(SETTINGS_COLUMNS)} FROM model_settings WHERE id = {SETTINGS_ROW_ID}"
)


def vendor_error_status(exc: BaseException) -> int | None:
    """벤더 일시 오류면 그 상태코드(타임아웃은 408), 아니면 None — 폴백·재시도 판정의 단일 지점.

    문구가 아니라 예외 계보와 코드로 판정한다. Gemini(google-genai)는 APIError.code, LiteLLM 경로는
    litellm 예외가 openai 예외를 상속하고 status_code를 싣는다(RateLimitError 429,
    ServiceUnavailableError 503, Timeout 408, APIConnectionError 500 …). openai는 LiteLLM 경로를
    탄 프로세스에서만 로드된다 — 미로드면 openai 예외일 수 없으니 import하지 않고 None이다(Gemini
    경로의 임의 예외마다 첫 import ~150ms가 이벤트 루프를 막지 않게).
    """
    if isinstance(exc, APIError):
        status = exc.code
    elif isinstance(exc, httpx.TimeoutException):
        status = 408
    else:
        if "openai" not in sys.modules:
            return None
        import openai

        if not isinstance(exc, openai.OpenAIError):
            return None
        timeout = isinstance(exc, openai.APITimeoutError)
        status = 408 if timeout else getattr(exc, "status_code", None)
    return status if status in _TRANSIENT_STATUS else None


# ── 모델 호출 예외 회수 ────────────────────────────────────────────────────
# ADK 2.3 워크플로는 모델 호출 예외를 스트림으로 올리지 않고
# `Event(error_code=type(e).__name__, error_message=str(e))`로 바꿔 흘린다
# (workflow/_node_runner.py).
# 런너가 받는 것은 클래스 이름 문자열뿐이라 그것으로 429를 가리면 문자열 매칭이 된다 — 2026-10-19
# 로컬 429 재현에서 확인했고, 종전 과부하 재시도의 `except APIError` 분기도 이 경로에선 닿지 않았다
# (노드 러너가 모든 예외를 이벤트로 바꾼다). 그래서 원본 예외를 ADK 모델 오류 콜백
# (on_model_error_callback — 변환 전에 원본을 받는다)에서 턴별 목록에 담고, 런너가 오류 이벤트를
# 받으면 그 원본을 다시 올린다. 목록은 이벤트 스트림을 소비하는 pump 태스크 안에서 묶으므로
# (adk_stream._pump) 동시 턴끼리 섞이지 않는다.
_model_errors: ContextVar[list[BaseException] | None] = ContextVar("model_errors", default=None)


@contextmanager
def bind_model_errors(sink: list[BaseException]) -> Iterator[None]:
    token = _model_errors.set(sink)
    try:
        yield
    finally:
        _model_errors.reset(token)


def record_model_error(callback_context, llm_request, error: Exception) -> None:
    """ADK on_model_error_callback — 원본 예외를 이 턴의 목록에 담기만 한다.

    None을 돌려 ADK는 종전대로 오류를 낸다(대체 응답을 만들지 않는다)."""
    sink = _model_errors.get()
    if sink is not None:
        sink.append(error)
    return None


def raise_recorded_model_error(event, sink: list[BaseException]) -> None:
    """ADK가 모델 호출 예외를 오류 이벤트로 바꿔 흘렸으면 그 원본 예외를 다시 올린다."""
    if sink and getattr(event, "error_code", None) and not event.partial:
        raise sink.pop()


# ── 모델 호출 재시도 ──────────────────────────────────────────────────────
# 2026-10-19 운영: 도구 2라운드 뒤 메인 호출이 503 UNAVAILABLE로 실패해 턴 전체가 실패했다(3시간
# ~670턴 중 2~3턴). 턴 폴백(retry_model)은 첫 출력 전만 돌 수 있어(SSE 계약) 이 경우를 못 살린다.
# 그래서 ADK 모델 객체(BaseLlm.generate_content_async)를 감싸 **그 요청만** 다시 부른다.
#
# 벤더 SDK 재시도(google-genai HttpRetryOptions·litellm num_retries)를 쓰지 않는 이유: 판정이
# 벤더마다 두 벌이 되고(genai는 상태코드 목록, litellm은 자체 정책), genai는 재시도 로그를 INFO·
# 모델명 없이 남겨 운영에서 보이지 않는다. 여기선 판정(vendor_error_status)·config·WARNING 한 줄이
# 두 경로에 하나다. 응답이 한 조각이라도 나간 호출은 다시 부르지 않는다 — 이미 흐른 delta와
# 겹친다(genai 재시도도 응답 시작 전 실패만 다시 부른다 — 스트림 중간 끊김은 같은 한계).
# 적용 범위는 답변 에이전트 모델뿐이다(agent.create_agent). 그라운딩·오버뷰·이력 압축 등 서브
# 호출은 각자 시간 예산이 있어 손대지 않는다.

# 테스트가 백오프를 재지 않고 값만 기록하도록 갈아끼우는 지점.
_sleep = asyncio.sleep


def call_retryable(exc: BaseException) -> bool:
    """호출 재시도 대상인가 — 일시 오류(5xx·타임아웃) 중 429를 뺀 것.

    429(RESOURCE_EXHAUSTED)는 분당 한도·일 한도·지출 한도를 본문으로 구분할 수 없고, 2026-10-17·
    10-19 장애는 일·월 한도라 초 단위 재시도로 풀리지 않았다. 폴백이 있는 턴은 곧장 쿨다운·폴백으로
    가야 한다(retry_model) — 재시도는 그 전환만 늦춘다."""
    status = vendor_error_status(exc)
    return status is not None and status != _QUOTA_STATUS


def call_retry_delay(retry: int, settings: Settings) -> float:
    """n번째 재시도 전 대기(초) — 지수 증가, model_call_retry_max_s 상한."""
    delay = settings.model_call_retry_initial_s * 2 ** (retry - 1)
    return min(settings.model_call_retry_max_s, delay)


class CallRetry:
    """ADK 모델 클래스(Gemini·LiteLlm) 앞에 섞는 믹스인 — retrying()으로 조립한다."""

    async def generate_content_async(self, llm_request, stream: bool = False) -> AsyncGenerator:
        settings = get_settings()
        retry = 0
        while True:
            started = False
            try:
                async for response in super().generate_content_async(llm_request, stream):
                    started = True
                    yield response
                return
            except Exception as exc:
                retry += 1
                if started or retry > settings.model_call_retries or not call_retryable(exc):
                    raise
                delay = call_retry_delay(retry, settings)
                logger.warning(
                    f"모델 호출 {self.model} 일시 오류({vendor_error_status(exc)}) → 같은 요청 "
                    f"재시도 {retry}/{settings.model_call_retries} ({delay:.1f}초 뒤)"
                )
                await _sleep(delay)


@functools.cache
def retrying(model_cls: type) -> type:
    """ADK 모델 클래스의 호출 재시도판(클래스당 하나)."""
    return type(model_cls.__name__, (CallRetry, model_cls), {})


# ── 런타임 설정 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelChoice:
    """지금 유효한 기본·폴백 모델과 모델별 처리 등급, 각각의 출처("config" | "runtime").

    폴백 ""는 끔. service_tiers는 LiteLLM 모델 → 등급(config 위에 런타임을 모델 단위로 얹은 값)이고,
    없는 모델은 표준이다. reasoning_efforts는 같은 방식의 사고 강도이고, 없는 모델은 전역
    litellm_reasoning_effort다. runtime_*는 그중 런타임 설정이 정한 몫(어드민 패널의 출처 표시).
    """

    default: str
    fallback: str
    default_source: str = "config"
    fallback_source: str = "config"
    service_tiers: dict[str, str] = field(default_factory=dict)
    runtime_tiers: dict[str, str] = field(default_factory=dict)
    reasoning_efforts: dict[str, str] = field(default_factory=dict)
    runtime_efforts: dict[str, str] = field(default_factory=dict)
    # {모델 | ANY_MODEL: 프롬프트 버전 id} — 런타임 전용(config 몫 없음), 없는 모델은 코드 프롬프트.
    prompt_versions: dict[str, int] = field(default_factory=dict)


def prompt_version_for(bindings: dict[str, int | None], model: str) -> int | None:
    """모델의 프롬프트 버전 — 모델 키 → ANY_MODEL → None(코드 프롬프트). 값 None은 "그 모델은
    오버레이 없음"(적재 실패 포함)이라 ANY_MODEL로 내려가지 않는다."""
    return bindings[model] if model in bindings else bindings.get(ANY_MODEL)


def uses_litellm(model_id: str) -> bool:
    """LiteLLM(OpenAI) 경로 모델인가 — 벤더 목록이 아니라 ID 형태("provider/model")가 신호다
    (config.selectable_models 주석). 에이전트 조립(agent.create_agent)과 등급 판정이 같이 쓴다."""
    return "/" in model_id


def tiered_models(settings: Settings) -> list[str]:
    """처리 등급을 고를 수 있는 모델 — 선택 가능 모델 중 LiteLLM 경로(선언 순서)."""
    return [model for model in settings.selectable_models.values() if uses_litellm(model)]


def encode_model_map(values: dict[str, str]) -> str | None:
    """모델별 맵 열(PER_MODEL_COLUMNS) 정본 직렬화(키 정렬·공백 없는 JSON). 빈 맵은 None(config).

    DB의 JSON 형식이 아니라 문자열로 두는 이유: 편집 프로토콜(edit_row)의 같은 값 판정·expected
    대조가 문자열 비교라, 직렬화를 앱 한 곳이 정해야 같은 맵이 같은 값이 된다.
    """
    if not values:
        return None
    return json.dumps(dict(sorted(values.items())), ensure_ascii=False, separators=(",", ":"))


def storable_setting(settings: Settings, column: str, value: Any) -> bool:
    """model_settings 열에 둘 수 있는 값인가 — 저장(어드민 검증)·서빙(effective_choice) 단일 판정.

    - default_model·fallback_model: 선택 가능 모델(폴백만 ""=끔 허용).
    - service_tiers·reasoning_efforts: {LiteLLM 선택 가능 모델: 그 열의 허용 값} 맵.
    - prompt_versions: {선택 가능 모델 | ANY_MODEL: 양의 정수 버전 id} 맵. 버전의 존재·폐기·모델
      일치·승격 게이트는 DB 판정이라 어드민 라우트가 같은 트랜잭션에서 더한다(admin_prompts).
    NULL(config 따름)은 호출부가 먼저 거른다.
    """
    if column == "prompt_versions":
        models = (ANY_MODEL, *settings.selectable_models.values())
        return isinstance(value, dict) and all(
            model in models and type(item) is int and item > 0 for model, item in value.items()
        )
    if column in PER_MODEL_COLUMNS:
        options = getattr(settings, PER_MODEL_COLUMNS[column][1])
        return isinstance(value, dict) and all(
            model in tiered_models(settings) and item in options for model, item in value.items()
        )
    if column == "fallback_model" and value == "":
        return True
    return value in settings.selectable_models.values()


def effective_choice(settings: Settings, row: dict[str, Any] | None) -> ModelChoice:
    """config 위에 런타임 행을 얹는다 — NULL은 "config 따름", 화이트리스트 밖 값은 무시한다.

    저장 시점에 검증해도 배포가 화이트리스트에서 모델을 빼면 행 값이 낡는다. 그 값으로 에이전트를
    만들면 매 턴 실패하므로 읽는 쪽에서 한 번 더 거른다(경고만 — 운영자가 패널에서 고친다).
    등급은 모델 단위로 거른다 — 한 모델이 목록에서 빠져도 다른 모델의 등급은 산다.
    """
    row = row or {}

    def pick(column: str, configured: str) -> tuple[str, str]:
        value = row.get(column)
        if value is None:
            return configured, "config"
        if storable_setting(settings, column, value):
            return value, "runtime"
        logger.warning(f"런타임 모델 설정 {column}={value!r}는 선택 가능 목록 밖이라 무시합니다.")
        return configured, "config"

    default, default_source = pick("default_model", settings.model_name)
    fallback, fallback_source = pick("fallback_model", settings.fallback_model)
    tiers, runtime_tiers = _per_model(settings, row, "service_tiers")
    efforts, runtime_efforts = _per_model(settings, row, "reasoning_efforts")
    versions, _runtime_versions = _per_model(settings, row, "prompt_versions")
    return ModelChoice(
        default,
        fallback,
        default_source,
        fallback_source,
        service_tiers=tiers,
        runtime_tiers=runtime_tiers,
        reasoning_efforts=efforts,
        runtime_efforts=runtime_efforts,
        prompt_versions=versions,
    )


def _per_model(
    settings: Settings, row: dict[str, Any], column: str
) -> tuple[dict[str, str], dict[str, str]]:
    """모델별 맵 열 하나 → (유효 맵, 그중 런타임 몫). config·런타임을 합친 뒤 한 판정으로 거른다 —
    config 값도 허용 목록을 우회하지 못하고, 버린 항목은 WARNING이다(깨진 JSON은 런타임 없음)."""
    stored = None
    if row.get(column) is not None:
        try:
            stored = json.loads(row[column])
        except ValueError:
            pass
    runtime = stored if isinstance(stored, dict) else {}
    configured_field = PER_MODEL_COLUMNS[column][0]
    configured = getattr(settings, configured_field) if configured_field else {}
    merged: dict[str, Any] = {}
    for model, value in {**configured, **runtime}.items():
        if storable_setting(settings, column, {model: value}):
            merged[model] = value
        else:
            source = "런타임" if model in runtime else "config"
            logger.warning(f"{source} {column} {model}={value!r}는 허용 목록 밖이라 무시합니다.")
    return merged, {model: value for model, value in merged.items() if model in runtime}


class ModelSettingsStore:
    """model_settings 읽기 전용 풀(프로세스 싱글턴). 쓰기는 어드민 트랜잭션(AdminService)이 한다.

    MysqlBackedService를 쓰지 않는 이유: 그 골격은 실패를 ERROR 로그 + 503으로 올린다(fail-loud).
    이 읽기는 실패해도 config로 도는 부가 경로라 반대 정책이다(usage와 같은 이유 — db.py 주석).
    """

    _instance: ModelSettingsStore | None = None

    def __init__(self, pool_factory=None) -> None:
        kwargs = mysql_pool_kwargs(get_settings().session_db_url, maxsize=1)
        self._db = LazyAiomysqlPool(kwargs, pool_factory) if kwargs is not None else None

    @classmethod
    def get_instance(cls) -> ModelSettingsStore:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def enabled(self) -> bool:
        return self._db is not None

    async def read(self) -> dict[str, Any] | None:
        """설정 행(없으면 None). 드라이버 예외는 그대로 올린다(실패 정책은 current_models)."""

        async def select(conn):
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_SETTINGS)
                return await cur.fetchone()

        row = await self._db.run(select)
        return dict(zip(SETTINGS_COLUMNS, row)) if row else None

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()


async def close_model_settings_store() -> None:
    """앱 종료 훅 — 만들어진 적 없으면 no-op."""
    if ModelSettingsStore._instance is not None:
        await ModelSettingsStore._instance.close()


# (만료 monotonic 시각, 값). 실패도 config 값으로 TTL 동안 캐시한다 — DB 장애 중 매 턴이 접속
# 타임아웃을 다시 기다리지 않게.
_cache: tuple[float, ModelChoice] | None = None


async def current_models() -> ModelChoice:
    """지금 유효한 기본·폴백 모델. 어떤 실패도 던지지 않는다(config로 돈다)."""
    global _cache
    if _cache is not None and time.monotonic() < _cache[0]:
        return _cache[1]
    settings = get_settings()
    row = None
    store = ModelSettingsStore.get_instance()
    if store.enabled:
        try:
            row = await store.read()
        except Exception as exc:  # noqa: BLE001 — 설정 읽기 실패가 답변을 막지 않는다
            logger.warning(f"런타임 모델 설정 읽기 실패 → config 값으로 진행: {type(exc).__name__}")
    choice = effective_choice(settings, row)
    _cache = (time.monotonic() + settings.model_settings_ttl_s, choice)
    return choice


def invalidate_cache() -> None:
    """이 인스턴스의 캐시를 버린다(저장 직후 — 다른 인스턴스는 TTL 안에 따라온다)."""
    global _cache
    _cache = None


# ── 폴백·쿨다운 ────────────────────────────────────────────────────────────

# 모델 → 쿨다운 만료(monotonic). 메인·폴백 모두 429면 여기 들어간다. 키가 있는 동안이 "전환 중"이다
# — 만료 뒤에도 그 모델이 턴을 마칠 때까지 남겨, 재프로브의 재429(WARNING)를 새 전환(ERROR = 운영
# Slack 알림)과 구분한다.
_cooldown_until: dict[str, float] = {}


def cooling_down(model: str) -> bool:
    until = _cooldown_until.get(model)
    return until is not None and time.monotonic() < until


def cooldowns() -> list[dict[str, Any]]:
    """이 프로세스의 쿨다운 상태(어드민 패널용) — 남은 초가 0이면 다음 턴이 재프로브한다."""
    now = time.monotonic()
    return [
        {"model": model, "remaining_s": max(0, round(until - now))}
        for model, until in _cooldown_until.items()
    ]


@dataclass
class TurnModels:
    """한 턴의 모델 계획. active는 지금 도는 모델(폴백 전환 시 바뀐다), fallback ""는 폴백 없음.

    probe는 "전환 중인 모델을 이 턴이 시험한다"(active가 쿨다운 키를 가진 채 출발)일 때 출발 시점의
    그 키 만료값이다(None = 시험 아님). 성공했을 때 쿨다운을 푸는 것은 probe 턴뿐이고, 그 키가 출발
    때와 같을 때만이다. 쿨다운이 걸리기 전에 출발한 턴, 또는 같은 창의 다른 프로브가 429로 재무장한
    뒤 성공한 턴이 풀면, 막 건 쿨다운이 풀려 다음 턴이 다시 429를 맞고 전환 알림이 재발한다(플랩).
    """

    primary: str
    fallback: str
    active: str
    probe: float | None = None
    # LiteLLM 모델 → 처리 등급·사고 강도(ModelChoice). 지금 모델의 값은 아래 두 속성.
    service_tiers: dict[str, str] = field(default_factory=dict)
    reasoning_efforts: dict[str, str] = field(default_factory=dict)
    # 프롬프트 버전 축. None = 코드 프롬프트. plan_turn_models가 prompt_versions(적재된 바인딩)를
    # 채워 switch가 지금 모델의 버전을 고른다.
    prompt_version: int | None = None
    prompt_versions: dict[str, int | None] = field(default_factory=dict)

    @property
    def service_tier(self) -> str:
        """지금 도는 모델의 처리 등급("" = 표준·미전달 — Gemini는 맵에 없어 항상 "")."""
        return self.service_tiers.get(self.active, "")

    @property
    def reasoning_effort(self) -> str:
        """지금 도는 모델에 실을 사고 강도 — 모델별 값, 없으면 전역 litellm_reasoning_effort.

        ""는 미전달(벤더 기본). Gemini는 LiteLLM 경로가 아니라 ""다(thinking_budget이 따로 있다).
        """
        if not uses_litellm(self.active):
            return ""
        return self.reasoning_efforts.get(self.active, get_settings().litellm_reasoning_effort)

    @property
    def fallback_from(self) -> str | None:
        """폴백으로 답했으면 원래 메인 모델(chat_turn.fallback_from), 아니면 None."""
        return self.primary if self.active != self.primary else None

    def switch(self, model: str) -> None:
        """지금 모델을 바꾼다. 바인딩이 있으면 프롬프트 버전도 그 모델 것으로 — 모델 전용
        프로필이 폴백 모델로 따라가지 않는다. 없으면 prompt_version을 유지한다."""
        self.active, self.probe = model, _cooldown_until.get(model)
        if self.prompt_versions:
            self.prompt_version = prompt_version_for(self.prompt_versions, model)


async def plan_turn_models(requested: str | None) -> TurnModels:
    """요청의 명시 선택(검증된 값 또는 None)으로 이 턴의 모델 계획을 세운다.

    메인이 쿨다운 중이면 폴백으로 출발한다. 폴백도 쿨다운 중이면 메인을 시험한다 — 둘 다 막고
    즉시 실패시키면 한도가 풀려도 쿨다운이 끝날 때까지 복구를 못 본다(무한 차단 금지). 이때 턴당
    벤더 호출은 메인 1회다(쿨다운 중인 폴백으로는 넘어가지 않는다 — retry_model).
    """
    choice = await current_models()
    primary = requested or choice.default
    # 명시 선택은 폴백하지 않는다(모듈 docstring). 폴백이 메인과 같으면 갈 곳이 없다.
    fallback = "" if requested or choice.fallback == primary else choice.fallback
    skip_primary = fallback and cooling_down(primary) and not cooling_down(fallback)
    models = TurnModels(
        primary,
        fallback,
        primary,
        service_tiers=choice.service_tiers,
        reasoning_efforts=choice.reasoning_efforts,
        prompt_versions=await _loaded_bindings(choice),
    )
    models.switch(fallback if skip_primary else primary)
    return models


# (해석한 ModelChoice, 그 바인딩의 적재 결과). current_models가 TTL 동안 같은 객체를 돌려주므로
# 해석은 설정 캐시 주기당 한 번이다 — 적재 실패(행 없음·선언 밖 슬롯·읽기 실패)도 그 주기 동안
# None으로 남아, 낡은 바인딩이 매 턴 prompt_version SELECT(maxsize=1 풀)·WARNING을 반복하지 않는다.
_bindings: tuple[ModelChoice, dict[str, int | None]] | None = None


async def _loaded_bindings(choice: ModelChoice) -> dict[str, int | None]:
    """바인딩된 버전을 프로세스 캐시에 올린다 — 실패한 키는 None(그 모델은 코드 프롬프트, WARNING은
    ensure_loaded). 버전이 불변이라 성공한 적재는 id당 한 번이고, 실패는 설정 TTL 뒤 다시 본다."""
    global _bindings
    if not choice.prompt_versions:
        return {}
    if _bindings is not None and _bindings[0] is choice:
        return _bindings[1]
    # 지연 import: prompt_versions → agent → model_routing 순환(agent가 이 모듈을 import한다).
    from yes24_agent.prompt_versions import ensure_loaded

    loaded = {
        model: version if await ensure_loaded(version) else None
        for model, version in choice.prompt_versions.items()
    }
    _bindings = (choice, loaded)
    return loaded


def _arm_cooldown(model: str, models: TurnModels, cooldown_s: float) -> None:
    """정상→전환은 ERROR(운영 Slack), 이 턴이 그 모델을 재프로브한 것이면(probe) 재429는 WARNING.

    판정을 "만료 키가 남아 있나"가 아니라 "이 턴이 시험했나"로 둔다 — 메인이 복귀하면 폴백은 다시
    시험되지 않아 만료 키가 남고, 그 키로 판정하면 다음 장애의 폴백 전환이 WARNING으로 묻힌다.
    쿨다운이 살아 있으면 무동작이다(동시 턴들의 429가 알림을 겹쳐 내지 않는다). 알림·쿨다운은
    프로세스(컨테이너)마다 따로다."""
    if cooling_down(model):
        return
    log = logger.warning if models.probe is not None and model == models.active else logger.error
    _cooldown_until[model] = time.monotonic() + cooldown_s
    role = "메인" if model == models.primary else "폴백"
    log(
        f"답변 {role} 모델 {model} 429(쿼터·지출 한도) → {cooldown_s:.0f}초 동안 건너뜀 "
        f"(메인 {models.primary} · 폴백 {models.fallback})"
    )


def mark_turn_ok(models: TurnModels) -> None:
    """턴을 마쳤다 — 이 턴이 전환 중인 모델을 시험한 것이면(probe) 쿨다운을 풀고 INFO."""
    if models.probe is not None and _cooldown_until.get(models.active) == models.probe:
        del _cooldown_until[models.active]
        logger.info(f"답변 모델 {models.active} 복귀(쿨다운 해제)")


def retry_model(models: TurnModels, exc: BaseException, *, retryable: bool) -> str | None:
    """스트림 예외 뒤 다시 돌 모델(없으면 None = 실패 마감) — 폴백·재시도 판정의 단일 지점.

    retryable은 "화면에 진행·본문 프레임이 아직 안 나갔고 이번 턴에 재시도를 쓰지 않았다"이다.
    429는 폴백이 구성된 턴이면 지금 모델(메인이든 폴백이든)에 쿨다운을 건다 — 재시도할 수 없어도
    건다(다음 턴부터 건너뛴다). 폴백으로는 쿨다운 중이 아닐 때만 넘어간다. 같은 모델로 턴을 다시
    돌지는 않는다 — 5xx·타임아웃은 이미 호출 단위로 재시도한 뒤라(CallRetry) 같은 모델 턴 재실행은
    같은 일을 한 번 더 하는 셈이다(2026-10-19 error_fallback 삭제).
    """
    status = vendor_error_status(exc)
    if status is None:
        return None
    if status == _QUOTA_STATUS and models.fallback:
        _arm_cooldown(models.active, models, get_settings().model_fallback_cooldown_s)
    if not (retryable and models.fallback) or models.active == models.fallback:
        return None
    if cooling_down(models.fallback):
        return None
    if status != _QUOTA_STATUS:
        logger.warning(
            f"답변 모델 {models.active} 일시 오류({status}) → 이번 턴은 폴백 {models.fallback}"
        )
    return models.fallback
