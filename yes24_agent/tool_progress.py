"""병렬 조회의 실제 시작·완료를 현재 ADK 스트림으로 전달한다."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolProgress:
    call_id: str
    index: int
    stage: str
    detail: str
    payload: dict | None = None


@dataclass
class _ProgressSink:
    queue: asyncio.Queue
    active: bool = True

    async def send(self, event: ToolProgress) -> None:
        if self.active:
            await self.queue.put((event, None))


_sink: ContextVar[_ProgressSink | None] = ContextVar("tool_progress", default=None)


@contextmanager
def bind_tool_progress(queue: asyncio.Queue) -> Iterator[None]:
    sink = _ProgressSink(queue)
    token = _sink.set(sink)
    try:
        yield
    finally:
        sink.active = False
        _sink.reset(token)


def parsed_progress(outcome: dict) -> dict:
    sources = outcome.get("parsed", [])
    return {
        "status": outcome["status"],
        "error_type": outcome.get("error_type"),
        "result_count": len(sources),
        "sources": sources,
    }


class ToolProgressGroup:
    def __init__(self, tool_context, *, stage: str, details: list[str]):
        self._call_id = getattr(tool_context, "function_call_id", None)
        self._sink = _sink.get() if len(details) > 1 and self._call_id else None
        self._stage = stage
        self._details = details
        self._started: set[int] = set()

    async def gather(
        self,
        jobs: list[tuple[int, Awaitable]],
        *,
        summarize: Callable[[Any], dict],
        complete: Callable[[Any], bool] | None = None,
    ) -> list:
        if self._sink is None or not self._sink.active:
            return await asyncio.gather(*(job for _, job in jobs))

        async def run(index: int, task: asyncio.Future):
            if index not in self._started:
                self._started.add(index)
                await self._send(index)
            try:
                result = await task
            except Exception:
                await self._send(index, {"status": "error", "result_count": 0})
                raise
            if complete is None or complete(result):
                await self._send(index, summarize(result))
            return result

        queries = [(index, asyncio.ensure_future(job)) for index, job in jobs]
        tasks = [asyncio.create_task(run(index, task)) for index, task in queries]
        try:
            return await asyncio.gather(*tasks)
        finally:
            pending = [*tasks, *(task for _, task in queries)]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _send(self, index: int, payload: dict | None = None) -> None:
        await self._sink.send(ToolProgress(
            call_id=self._call_id,
            index=index,
            stage=self._stage,
            detail=self._details[index],
            payload=payload,
        ))
