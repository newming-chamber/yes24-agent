"""에이전트 공통 런타임 — 도구 registry, ADK factory, function-response 헬퍼."""

from google.adk.agents import LlmAgent
from google.genai import types

from yes24_agent.rbti.persona import axis_label
from yes24_agent.tools.fetch_many import fetch_many
from yes24_agent.tools.reply_directly import reply_directly
from yes24_agent.tools.web_fetch import web_fetch
from yes24_agent.tools.web_search import web_search
from yes24_agent.tools.yes24_browse import yes24_browse
from yes24_agent.tools.yes24_fetch import yes24_fetch
from yes24_agent.tools.yes24_search import yes24_search

AGENT_TOOLS = (
    yes24_search,
    yes24_fetch,
    fetch_many,
    yes24_browse,
    web_search,
    web_fetch,
    reply_directly,
)
YES24_SEARCH_ONLY = ["yes24_search"]
WEB_SEARCH_ONLY = ["web_search"]


_PERSONA_TOOL_DIRECTIVE = """
## 독자 맞춤 반영 (RBTI {code} · {label})
독서 성향을 추천 후보의 검색과 선택에 반영하되, 사용자 조건과 증거 계약보다 우선하지 마세요.
구체적인 성향 정의와 후보 판단 관점은 뒤의 독자 페르소나를 따릅니다.
"""


def persona_tool_directive(code: str) -> str:
    label = axis_label(code)
    if not label:  # 무효 코드
        return ""
    return _PERSONA_TOOL_DIRECTIVE.format(code=code, label=label)


def build_llm_agent(
    *,
    model: str,
    thinking_budget: int,
    name: str,
    description: str,
    instruction,
    tools: tuple = AGENT_TOOLS,
    before_model_callback=None,
    output_schema=None,
) -> LlmAgent:
    generate_content_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget)
    )
    return LlmAgent(
        model=model,
        name=name,
        description=description,
        instruction=instruction,
        tools=list(tools),
        generate_content_config=generate_content_config,
        before_model_callback=before_model_callback,
        output_schema=output_schema,
    )


def has_function_response(content) -> bool:
    parts = getattr(content, "parts", None) or []
    return any(getattr(p, "function_response", None) is not None for p in parts)


def latest_function_response(llm_request) -> tuple[str, dict] | None:
    for content in reversed(getattr(llm_request, "contents", None) or []):
        for part in reversed(getattr(content, "parts", None) or []):
            response = getattr(part, "function_response", None)
            if response is None:
                continue
            payload = response.response if isinstance(response.response, dict) else {}
            return response.name or "", payload
    return None


def current_turn_has_function_response(llm_request) -> bool:
    contents = getattr(llm_request, "contents", None) or []
    return bool(contents and has_function_response(contents[-1]))
