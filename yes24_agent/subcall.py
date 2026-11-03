"""모델 조립과 경량 서브콜의 단일 벤더 분기.

모델 ID 형태("provider/model" = LiteLLM 경로, 그 외 = Gemini — model_routing.uses_litellm)로
벤더를 가르는 자리는 **여기 한 곳**이다. 루트 에이전트(agent.create_agent)는 build_llm으로
모델 인스턴스를 받고, 경량 유틸(enrichment·thought_translation·starters·web_search prefetch
힌트)은 generate 한 함수로 단발 호출한다 — 유틸마다 google.genai 클라이언트를 직접 잡던
구조에서는 `*_model` 설정에 LiteLLM 경로 값을 넣어도 동작하지 않았다(2026-11-03, Gemini
결제 차단 중 Luna 전환 불가 실측).

LiteLLM 경로의 서브콜 계약(ADK LiteLlm 2.3.0 · Luna 실호출로 확정):
- 구조화 출력은 Gemini `types.Schema`를 표준 JSON Schema로 옮겨 litellm `json_schema`
  response_format 딕셔너리로 싣는다 — ADK는 `type`이 json_schema인 딕셔너리를 그대로 통과시키고
  (lite_llm._to_litellm_response_format), Gemini 대문자 타입(`OBJECT`)은 OpenAI가 거부한다.
- reasoning 모델은 `temperature`를 거부한다(400) — 이 경로에선 싣지 않는다.
- 사고 강도는 Responses 요청의 reasoning 필드(extra_body)로 싣는다(agent.create_agent와 같은 이유).
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

from google import genai
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from yes24_agent.config import (
    ensure_google_api_key_env,
    ensure_openai_api_key_env,
    get_genai_client,
    get_settings,
)
from yes24_agent.litellm_reasoning import ReasoningCarryClient
from yes24_agent.model_routing import retrying, uses_litellm


def build_llm(model_id: str, *, reasoning_effort: str = "", service_tier: str = ""):
    """루트 에이전트용 모델 인스턴스 — 호출 재시도(model_routing.CallRetry)를 섞어 조립한다.

    service_tier는 OpenAI 경로에만, reasoning_effort는 LiteLLM 경로(OpenAI·anthropic)에 실린다
    (Gemini 무시). 등급 ""는 표준·미전달, 강도 ""는 미전달(벤더 기본).
    """
    if not uses_litellm(model_id):
        # 문자열이 아니라 인스턴스로 싣는다 — 호출 재시도(model_routing.CallRetry)를 섞기 위해서다.
        return retrying(Gemini)(model=model_id)
    # "provider/model"은 LiteLLM 경로(config.selectable_models 주석 참조).
    # 지연 import — litellm 로드 비용(수 초)은 실제 사용 시에만 지불한다.
    from google.adk.models.lite_llm import LiteLlm

    ensure_openai_api_key_env()
    # 강도는 reasoning_effort 파라미터가 아니라 Responses 요청의 reasoning 필드로 직접 싣는다
    # (extra_body — 브리지가 그대로 요청에 옮긴다). reasoning_effort 경로는 litellm이 모델명으로
    # 두 번 거른다: 모르는 신모델은 거부하고(allowed_openai_params 우회가 필요했다), GPT-5 계열은
    # 문자열로 평탄화한 뒤 자기 표(none~xhigh)에 없는 값(max)을 조용히 버린다(2026-10-19 실측:
    # 5.6 Luna의 max → 요청 본문 reasoning=null). 모든 모델×옵션의 실제 본문은 테스트가 계약이다.
    if model_id.startswith("openai/"):
        extra: dict = {}
        if reasoning_effort:
            extra["extra_body"] = {"reasoning": {"effort": reasoning_effort}}
        if service_tier:
            extra["service_tier"] = service_tier
        # reasoning 아이템을 같은 턴의 다음 도구 라운드로 되돌려 싣는 클라이언트(litellm_reasoning).
        return retrying(LiteLlm)(model=model_id, llm_client=ReasoningCarryClient(), **extra)
    # OpenAI Responses 전용 장치(reasoning 필드·등급·reasoning carry)는 다른 벤더에 싣지
    # 않는다 — anthropic/*는 LiteLLM 기본 Messages 경로 그대로(사고는 벤더 기본 adaptive).
    # Anthropic은 캐시가 명시 표시제다(Gemini·OpenAI는 자동). 시스템 표시가 도구 정의까지
    # 덮고, 마지막 메시지 표시가 직전 라운드까지를 다음 라운드의 캐시 읽기로 만든다
    # (2026-10-22 단독 실측: 2번째 호출부터 앞부분 전량 읽기, 새로 붙은 부분만 쓰기).
    extra = {}
    if model_id.startswith("anthropic/"):
        extra["cache_control_injection_points"] = [
            {"location": "message", "role": "system"},
            {"location": "message", "index": -1},
        ]
        # Haiku 5.5는 thinking을 생략하면 adaptive로 사고한다(effort 기본 medium). 강도는 litellm
        # reasoning_effort가 아니라 output_config.effort로 싣는다 — reasoning_effort는
        # thinking.type.enabled로 바뀌어 400(2026-10-22 직접 호출 실측). 강도는 어드민 모델별
        # 값으로만 켠다(TurnModels.reasoning_effort가 anthropic은 전역 기본으로 떨어지지 않는다).
        if reasoning_effort:
            extra["output_config"] = {"effort": reasoning_effort}
    return retrying(LiteLlm)(model=model_id, **extra)


# ── 구조화 출력 스키마: Gemini 표기 → 표준 JSON Schema ─────────────────────────────────
# 호출부는 Gemini `types.Schema`로 스키마를 쓴다(property_ordering·min_items 등 Gemini 계약).
# LiteLLM 경로엔 같은 스키마를 표준 표기로 옮겨 싣는다 — 두 벌의 스키마를 두지 않기 위해서다.
_KEY_RENAME = {
    "min_items": "minItems", "max_items": "maxItems",
    "min_length": "minLength", "max_length": "maxLength",
    "min_properties": "minProperties", "max_properties": "maxProperties",
    "any_of": "anyOf",
}
# Gemini 전용 키 — property_ordering은 JSON Schema에 없고(properties 삽입 순서가 그 역할을
# 대신한다), title·format은 OpenAI 구조화 출력이 거부하거나 무시한다. nullable은 타입 합집합으로.
_GEMINI_ONLY_KEYS = frozenset({"property_ordering", "title", "format", "nullable"})


def to_json_schema(schema: types.Schema | dict) -> dict:
    """Gemini `types.Schema`(또는 대문자 타입 딕셔너리)를 표준 JSON Schema 딕셔너리로 옮긴다."""
    if isinstance(schema, types.Schema):
        schema = schema.model_dump(exclude_none=True, mode="json")
    return _convert(schema)


def _convert(node: Any) -> Any:
    if isinstance(node, list):
        return [_convert(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict = {}
    for key, value in node.items():
        if key in _GEMINI_ONLY_KEYS:
            continue
        key = _KEY_RENAME.get(key, key)
        if key == "type" and isinstance(value, str):
            value = value.lower()
        elif key == "properties" and isinstance(value, dict):
            value = {name: _convert(sub) for name, sub in value.items()}
            out[key] = value
            continue
        out[key] = _convert(value)
    if node.get("nullable") is True and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
        if isinstance(out.get("enum"), list) and None not in out["enum"]:
            out["enum"] = [*out["enum"], None]
    if out.get("type") == "object" and "properties" in out:
        out.setdefault("additionalProperties", False)
    return out


def _litellm_response_format(schema: types.Schema | dict) -> dict:
    """ADK LiteLlm이 그대로 통과시키는 litellm response_format(strict=False — minItems·maxLength
    등 strict 모드가 거부하는 제약을 Gemini 스키마가 쓴다)."""
    return {
        "type": "json_schema",
        "json_schema": {"name": "response", "strict": False, "schema": to_json_schema(schema)},
    }


# ── 단발 서브콜 ────────────────────────────────────────────────────────────────────────
class SubcallResponse:
    """LiteLLM 경로 응답을 genai 응답 모양(.text·.usage_metadata)으로 맞춘 것 — 호출부의
    `record_usage(component, response.usage_metadata, ...)`가 벤더를 모른 채 그대로 쓴다."""

    __slots__ = ("text", "usage_metadata")

    def __init__(self, text: str, usage_metadata: Any) -> None:
        self.text = text
        self.usage_metadata = usage_metadata


@lru_cache(maxsize=4)
def _gemini_client(http_timeout_ms: int) -> genai.Client:
    """HTTP 타임아웃을 지정한 genai 클라이언트(타임아웃 값당 1개)."""
    ensure_google_api_key_env()
    return genai.Client(http_options=types.HttpOptions(timeout=http_timeout_ms))


async def generate(
    model_id: str,
    contents: str,
    *,
    system_instruction: str,
    timeout_s: float,
    response_schema: types.Schema | dict | None = None,
    max_output_tokens: int | None = None,
    temperature: float | None = None,
    http_timeout_ms: int | None = None,
) -> Any:
    """단발 생성 호출(구조화 출력 선택). 반환값은 `.text`·`.usage_metadata`를 가진다.

    타임아웃은 여기서 한 번만 건다(호출부가 wait_for를 겹치지 않는다). 재시도는 섞지 않는다 —
    서브콜은 best-effort 채널이라 실패를 호출부가 폴백으로 흡수한다.
    """
    if uses_litellm(model_id):
        return await asyncio.wait_for(
            _generate_litellm(
                model_id, contents, system_instruction, response_schema, max_output_tokens
            ),
            timeout=timeout_s,
        )
    client = _gemini_client(http_timeout_ms) if http_timeout_ms else get_genai_client()
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    if response_schema is not None:
        config.response_mime_type = "application/json"
        config.response_schema = response_schema
    return await asyncio.wait_for(
        client.aio.models.generate_content(model=model_id, contents=contents, config=config),
        timeout=timeout_s,
    )


async def _generate_litellm(
    model_id: str,
    contents: str,
    system_instruction: str,
    response_schema: types.Schema | dict | None,
    max_output_tokens: int | None,
) -> SubcallResponse:
    from google.adk.models.lite_llm import LiteLlm

    settings = get_settings()
    ensure_openai_api_key_env()
    extra: dict = {}
    if model_id.startswith("openai/") and settings.subcall_reasoning_effort:
        extra["extra_body"] = {"reasoning": {"effort": settings.subcall_reasoning_effort}}
    llm = LiteLlm(model=model_id, **extra)
    # temperature는 싣지 않는다(reasoning 모델 400). openai/*는 max_output_tokens도 싣지 않는다 —
    # Responses는 그 예산에 reasoning 토큰을 포함해 번역 80 예산을 사고가 먹고 본문이 절단된다
    # (2026-11-03 실측: low에서도 사고 21~176 토큰, 라벨 3건 중 2건 절단). 출력 길이는 스키마·
    # 지시문이 묶는다. response_schema는 생성자 인자가 아니라 대입으로 넣는다 — genai가 생성 시
    # 딕셔너리를 Schema로 검증하며 "json_schema is not a valid Type" 경고를 내기 때문(값은
    # 딕셔너리로 남는다).
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=None if model_id.startswith("openai/") else max_output_tokens,
    )
    if response_schema is not None:
        config.response_mime_type = "application/json"
        config.response_schema = _litellm_response_format(response_schema)
    request = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=contents)])], config=config
    )
    last: LlmResponse | None = None
    async for response in llm.generate_content_async(request, stream=False):
        last = response
    parts = (last.content.parts if last and last.content else None) or []
    text = "".join(part.text or "" for part in parts if not part.thought)
    return SubcallResponse(text, last.usage_metadata if last else None)
