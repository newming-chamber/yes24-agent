"""ADK 이벤트 스트림을 하나의 task에서 소비하며 이벤트 간 타임아웃을 적용한다."""

import asyncio
from collections.abc import AsyncIterator

SET_MODEL_RESPONSE_TOOL_NAME = "set_model_response"

_END = object()


async def _pump(stream, queue: asyncio.Queue) -> None:
    try:
        async for event in stream:
            await queue.put((event, None))
    except Exception as exc:  # noqa: BLE001 — 소비 task로 원래 예외를 전달한다
        await queue.put((_END, exc))
    else:
        await queue.put((_END, None))


async def iter_adk_events(stream, *, timeout_s: float) -> AsyncIterator:
    """ADK async generator의 context를 고정한 채 이벤트 간 타임아웃을 건다.

    ``wait_for(stream.__anext__())``는 호출마다 새 task를 만들어, 여러 yield에 걸쳐 유지되는
    ADK/OpenTelemetry context token을 다른 Context에서 detach하게 한다. 전용 pump task 하나가
    스트림을 끝까지 소유하고, 호출자는 타임아웃이 적용된 bounded queue만 기다린다.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    pump = asyncio.create_task(_pump(stream, queue), name="adk-event-stream")
    try:
        while True:
            event, error = await asyncio.wait_for(queue.get(), timeout=timeout_s)
            if event is _END:
                if error is not None:
                    raise error
                return
            yield event
    finally:
        if not pump.done():
            pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass
