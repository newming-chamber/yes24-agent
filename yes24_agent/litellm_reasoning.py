"""OpenAI reasoning 아이템을 같은 턴의 다음 도구 라운드로 되돌려 싣는 LiteLLM 클라이언트.

2026-10-19 프로브: Luna(LiteLLM responses 브리지) 한 턴 3라운드에서 응답은 매 라운드 reasoning
아이템(encrypted_content 3.5~7.7K자)을 주는데 다음 요청 input엔 0개였다. 브리지는 양쪽을
이미 지원한다 — 스트림 마지막 청크의 delta.reasoning_items로 내주고, assistant 메시지의
msg["reasoning_items"]를 input reasoning 아이템으로 되돌린다. 끊긴 곳은 ADK LiteLlm(2.3.0)이다:
청크→Part 변환이 reasoning_items를 모르고 버려, 다음 라운드 메시지에 실을 재료가 없다.

그래서 ADK가 공개 주입점으로 둔 llm_client(LiteLLMClient — "for better testability")를 갈아끼운다.
ADK 내부 함수는 건드리지 않는다:
- 응답 스트림을 그대로 흘리면서 reasoning_items와 그 응답의 도구 호출 id를 턴 저장소에 담는다.
- 다음 요청의 assistant 메시지 중 도구 호출 id가 저장소에 있는 것에 reasoning_items를 붙인다.

범위는 **같은 턴의 도구 라운드 사이만**이다(OpenAI 권장: 도구 라운드 사이 필수, 턴 사이 선택).
저장소는 이벤트 스트림을 소비하는 pump 태스크 안에서 턴마다 새로 묶이고(adk_stream._pump — 모델 오류
회수와 같은 자리) 턴이 끝나면 사라진다. 세션에 영속하지 않으므로 세션 크기·히스토리 복원·사고
타임라인·인용 검증에 닿는 경로가 없다. 턴 사이에 싣지 않는 이유: 같은 컨테이너가 다음 턴을 받을 때만
실리는 비결정 동작이 되고(서버 2대×컨테이너 2), 그걸 피하려면 암호문 수 KB를 매 라운드 세션에
써야 한다.

판정은 데이터 자체다 — reasoning_items를 돌려주는 응답(responses 브리지)이 아니면 저장소가 비어
아무것도 붙지 않는다. 벤더·모델 목록이 없다.

같은 자리에서 하나 더: 텍스트와 도구 호출을 함께 가진 assistant 메시지를 [텍스트, 도구 호출] 두
메시지로 나눈다. ADK는 경과 서술 텍스트와 function_call을 한 메시지(content + tool_calls)로 싣는데,
responses 브리지는 tool_calls가 있는 assistant 메시지에서 reasoning·function_call만 input으로 옮기고
content를 버린다(litellm transformation.py `elif role == "assistant" and tool_calls` 분기). 그러면
모델이 자기가 이미 쓴 경과 서술을 다음 라운드에 못 보고, 요청 확인 문장을 라운드마다 새로 쓴다
(2026-10-20 QA: 멀티라운드 47턴 중 10턴). 나눈 순서(reasoning → message → function_call)는 모델이
실제로 출력한 아이템 순서와 같다. chat completions 경로에서도 연속 assistant 메시지는 유효하다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from google.adk.models.lite_llm import LiteLLMClient

# 도구 호출 id → 그 호출을 낸 응답의 reasoning 아이템(턴 단위, pump 태스크 문맥).
_turn_items: ContextVar[dict[str, list[dict]] | None] = ContextVar(
    "turn_reasoning_items", default=None
)


@contextmanager
def bind_turn_reasoning() -> Iterator[None]:
    """한 턴(한 시도)의 reasoning 저장소를 연다(묶이지 않은 문맥에서 클라이언트는 그냥 통과한다)."""
    token = _turn_items.set({})
    try:
        yield
    finally:
        _turn_items.reset(token)


def _call_ids(tool_calls: Any) -> list[str]:
    ids = []
    for call in tool_calls or ():
        call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
        if call_id:
            ids.append(call_id)
    return ids


def _split_text_from_tool_calls(messages: list[dict]) -> list[dict]:
    """content와 tool_calls를 함께 가진 assistant 메시지를 [content만, tool_calls만]으로 나눈다.

    reasoning_items는 아래 acompletion이 tool_calls 쪽 메시지에서 찾아 붙이므로, 붙인 뒤 텍스트
    쪽으로 옮겨 브리지가 reasoning → message → function_call 순서로 싣게 한다
    (_move_reasoning_to_text).
    """
    out: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls") and message.get("content"):
            text = {key: value for key, value in message.items() if key != "tool_calls"}
            calls = {key: value for key, value in message.items() if key != "content"}
            calls["content"] = None
            out.extend((text, calls))
        else:
            out.append(message)
    return out


def _move_reasoning_to_text(messages: list[dict]) -> None:
    """나뉜 쌍(텍스트 → 도구 호출)에서 도구 호출 쪽 reasoning_items를 텍스트 쪽으로 옮긴다."""
    for text, calls in zip(messages, messages[1:]):
        if (
            text.get("role") == calls.get("role") == "assistant"
            and text.get("content") and not text.get("tool_calls")
            and calls.get("tool_calls") and calls.get("reasoning_items")
        ):
            text["reasoning_items"] = calls.pop("reasoning_items")


class ReasoningCarryClient(LiteLLMClient):
    """LiteLLMClient 대역 — 위 모듈 docstring의 두 동작만 더한다(나머지는 부모 그대로)."""

    async def acompletion(self, model, messages, tools, **kwargs):
        messages = _split_text_from_tool_calls(messages)
        store = _turn_items.get()
        if store:
            for message in messages:
                if message.get("role") != "assistant" or message.get("reasoning_items"):
                    continue
                items: list[dict] = []
                for call_id in _call_ids(message.get("tool_calls")):
                    items.extend(item for item in store.get(call_id, ()) if item not in items)
                if items:
                    message["reasoning_items"] = items
        _move_reasoning_to_text(messages)
        response = await super().acompletion(model, messages, tools, **kwargs)
        if store is None or not kwargs.get("stream"):
            return response  # 비스트림 경로는 쓰지 않는다(런너는 항상 스트림) — 그대로 통과
        return self._remember(response, store)

    @staticmethod
    async def _remember(stream, store: dict[str, list[dict]]) -> AsyncIterator[Any]:
        """청크를 손대지 않고 흘리면서, 이 응답의 도구 호출 id에 reasoning 아이템을 묶어 둔다."""
        call_ids: list[str] = []
        items: list[dict] = []
        async for chunk in stream:
            delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
            if delta is not None:
                call_ids.extend(_call_ids(getattr(delta, "tool_calls", None)))
                items.extend(getattr(delta, "reasoning_items", None) or ())
            yield chunk
        if items:
            for call_id in call_ids:
                store[call_id] = items
