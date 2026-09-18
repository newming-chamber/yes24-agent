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
    """한 도구 호출이 내는 진행 줄 묶음 — 인덱스가 그 호출 안의 서브스텝 번호다.

    줄이 하나뿐인 호출은 서브스텝을 내지 않는다(도구 응답 스텝이 그 하나를 이미 말한다).
    그래서 **한 호출의 줄은 전부 한 그룹으로 세야 한다** — 단계마다 그룹을 따로 만들면
    각자 자기 단계만 세어 한쪽이 침묵하고, 다른 쪽 진행 줄이 응답 스텝을 억제해 그 건수가
    통째로 사라진다(2026-09-18 실측: 코너 1개 + 관측). 단계가 다르면 stage만 다르다.
    """

    def __init__(self, tool_context, *, details: list[str]):
        self._call_id = getattr(tool_context, "function_call_id", None)
        self._sink = _sink.get() if len(details) > 1 and self._call_id else None
        # 호출부와 **같은 리스트**다 — 아직 보내지 않은 줄의 문구는 원소 대입으로 나중에
        # 채울 수 있다(코너를 열어야 제목을 아는 관측 줄이 이 계약에 기댄다).
        self._details = details
        # 키가 (인덱스, stage)인 이유: 한 그룹이 여러 단계를 내므로 인덱스만 보면 같은 자리를
        # 다른 단계로 재사용할 때 시작 줄이 통째로 사라지고 완료 줄만 나간다.
        self._started: set[tuple[int, str]] = set()

    async def gather(
        self,
        jobs: list[tuple[int, Awaitable]],
        *,
        stage: str,
        summarize: Callable[[Any], dict],
        complete: Callable[[Any], bool] | None = None,
    ) -> list:
        if self._sink is None or not self._sink.active:
            return await asyncio.gather(*(job for _, job in jobs))

        async def run(index: int, task: asyncio.Future):
            if (index, stage) not in self._started:
                self._started.add((index, stage))
                await self._send(index, stage)
            try:
                result = await task
            except Exception:
                await self._send(index, stage, {"status": "error", "result_count": 0})
                raise
            if complete is None or complete(result):
                await self._send(index, stage, summarize(result))
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

    async def _send(self, index: int, stage: str, payload: dict | None = None) -> None:
        await self._sink.send(ToolProgress(
            call_id=self._call_id,
            index=index,
            stage=stage,
            detail=self._details[index],
            payload=payload,
        ))
