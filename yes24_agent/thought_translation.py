"""사고 요약 라벨의 표시용 한국어 번역 (best-effort · 비파괴).

Gemini 사고 요약은 벤더측 요약기가 생성해 **영어로 고정**돼 있다 — 프롬프트(중간·말미)·
PlanReActPlanner·최신 3세대 모델(3.1-pro/3.6-flash)·"Think in Korean" 시스템 지시 전부
무효과로 실측 확정(2026-07-23, known-limitations.md). 언어 파라미터도 없다. 그래서 표시
직전에 경량 모델로 번역하는 것이 한국어 타임라인의 유일한 경로다.

계약:
- **비파괴**: 번역 실패·타임아웃이면 원문(영어) 라벨을 그대로 쓴다. 진행 표시는 best-effort
  부가 채널이므로 번역이 본류(도구·본문)를 지연시키거나 라벨을 잃게 해서는 안 된다.
- **본류 무차단**: 호출자는 이 코루틴을 asyncio task로 띄워 병행시킨다(runner의 병합 대기).
"""

import asyncio
import logging
from functools import lru_cache

from google import genai
from google.genai import types

from yes24_agent.config import get_settings

logger = logging.getLogger(__name__)

# 번역기는 문구를 만들지 않는다 — 입력(진행 단계 제목)을 옮기기만 한다(하드코딩 라벨 금지
# 원칙과 양립). 퍼플렉시티·Claude Code식 짧은 단계 표기에 맞춰 간결한 진행형으로 옮긴다.
_TRANSLATE_INSTRUCTION = (
    "AI의 진행 단계 제목을 자연스럽고 짧은 한국어 진행형 문구 한 줄로 옮긴다. 문구만 출력한다."
)


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    """번역 전용 genai 클라이언트(프로세스당 1개). GOOGLE_API_KEY는 앱 기동 시 매핑됨."""
    return genai.Client()


async def translate_thought_label(label: str) -> str:
    """사고 요약 라벨을 한국어로 번역한다. 실패하면 원문을 그대로 돌려준다(비파괴)."""
    settings = get_settings()
    if not settings.thought_translation_model:
        return label
    try:
        response = await asyncio.wait_for(
            _client().aio.models.generate_content(
                model=settings.thought_translation_model,
                contents=label,
                config=types.GenerateContentConfig(
                    system_instruction=_TRANSLATE_INSTRUCTION,
                    temperature=0,
                    max_output_tokens=settings.thought_translation_max_tokens,
                ),
            ),
            timeout=settings.thought_translation_timeout_s,
        )
        translated = " ".join((response.text or "").split())
        return translated or label
    except Exception as exc:  # noqa: BLE001 — 표시용 부가 경로: 어떤 실패도 원문 폴백
        logger.warning("사고 라벨 번역 실패(원문 폴백): %s", exc)
        return label
