"""ADK 이벤트 스트림을 하나의 task에서 소비하며 이벤트 간 타임아웃을 적용한다."""

import asyncio
import logging
from collections.abc import AsyncIterator

import anyio

from yes24_agent.config import get_settings
from yes24_agent.litellm_reasoning import bind_turn_reasoning
from yes24_agent.model_routing import bind_model_errors
from yes24_agent.tool_progress import bind_tool_progress

_END = object()

logger = logging.getLogger(__name__)


async def _pump(stream, queue: asyncio.Queue, model_errors: list) -> None:
    try:
        # 턴(시도)별 문맥: 도구 진행 큐 · 모델 오류 원본 · OpenAI reasoning 아이템 저장소.
        with bind_tool_progress(queue), bind_model_errors(model_errors), bind_turn_reasoning():
            async for event in stream:
                await queue.put((event, None))
    except Exception as exc:  # noqa: BLE001 — 소비 task로 원래 예외를 전달한다
        await queue.put((_END, exc))
    else:
        await queue.put((_END, None))


async def iter_adk_events(
    stream, *, timeout_s: float, model_errors: list | None = None
) -> AsyncIterator:
    """ADK async generator의 context를 고정한 채 이벤트 간 타임아웃을 건다.

    ``wait_for(stream.__anext__())``는 호출마다 새 task를 만들어, 여러 yield에 걸쳐 유지되는
    ADK/OpenTelemetry context token을 다른 Context에서 detach하게 한다. 전용 pump task 하나가
    스트림을 끝까지 소유하고, 호출자는 타임아웃이 적용된 bounded queue만 기다린다.

    model_errors는 이 스트림의 모델 호출 원본 예외가 담길 목록이다
    (model_routing.record_model_error).
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    sink = model_errors if model_errors is not None else []
    pump = asyncio.create_task(_pump(stream, queue, sink), name="adk-event-stream")
    try:
        while True:
            event, error = await asyncio.wait_for(queue.get(), timeout=timeout_s)
            if event is _END:
                if error is not None:
                    raise error
                return
            yield event
    finally:
        # pump(→ 도구 태스크)는 **딱 한 번만** 취소한다. 정리 중 재취소된 httpx 요청은
        # httpcore 1.0.9 버그로 연결을 공유 풀에 영구히 남긴다(SSE 끊김 1회당 진행 중 요청 전부
        # — 2026-11-01 상용 풀 막힘의 후보 경로, 실측 60→0). 두 장치가 나눠 맡는다:
        # - `asyncio.wait`: `await pump`와 달리 기다리는 쪽이 (네이티브 task.cancel()로) 다시
        #   취소돼도 pump로 번지지 않고, 상한이 지나면 그냥 돌아온다.
        # - shield: anyio 취소(Starlette의 SSE 끊김 task group)는 scope를 나갈 때까지 체크포인트마다
        #   재전달되는데, 그걸 가려 정리가 끝까지 돈다. 네이티브 task.cancel()은 못 가린다(실측).
        limit = get_settings().sse_pump_cleanup_timeout_s
        with anyio.CancelScope(shield=True):
            if not pump.done():
                pump.cancel()
            await asyncio.wait({pump}, timeout=limit)
        if not pump.done():  # 상한 초과 — 정리를 포기하고 진행한다(pump는 취소 요청된 채 남음)
            logger.warning(f"adk event pump did not stop within {limit}s")
        elif not pump.cancelled():
            pump.result()  # 기존 `await pump`처럼 pump의 예외(Exception 밖의 것)를 올린다
