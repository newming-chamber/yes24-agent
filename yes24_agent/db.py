"""aiomysql lazy 커넥션 풀 — 인증(auth)·사용량(usage)·피드백(feedback)의 공유 구현.

지연 생성 + 생성 태스크 공유 + shield 기계가 auth·usage에 두 벌로 복제돼 있었고(주석까지
서로를 참조), 피드백 스택을 얹으며 세 벌이 될 자리라 한 클래스로 모은다(같은 판정 두 곳
금지). **실패 정책은 이 클래스의 관심사가 아니다** — `get()`은 드라이버 예외를 그대로
올리고, 503으로 끊을지(fail-loud: auth·feedback) 삼킬지(부가 채널: usage)는 소비자가 정한다.

잠금 직렬화가 아니라 태스크 공유인 이유: 잠금은 생성 1회 보장은 되지만, DB가 TCP
블랙홀인 채 (재)기동해 첫 풀 생성 전에 호출 k건이 동시 유입되면 대기자들이 잠금 **안에서
각자** connect 실패를 순차 대기해 k번째 실패가 k×connect_timeout 뒤에 난다(실패 지연이
병렬→직렬로 퇴행, 그동안 워커 점유). 한 태스크를 함께 await하면 동시 대기자 전원이 한
시도의 성공·실패를 동반 수신한다. 실패한 태스크는 버려 다음 호출이 새로 시도한다(장애
복구). shield는 대기자 하나의 취소(클라이언트 이탈·태스크 취소)가 공유 태스크를 함께
취소해 무고한 동시 대기자들을 실패시키는 전파를 막는다 — 태스크 자체의 마감은 close()가
직접 한다. check-then-create 사이에 await가 없어 asyncio 단일 스레드에서 원자적이다.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiomysql


class LazyAiomysqlPool:
    """공유 생성 태스크 기반 lazy aiomysql 풀(소비자별 인스턴스, 프로세스 공유 아님).

    pool_factory를 주입할 수 있다 — 테스트는 실 DB 없이 스텁으로 전 경로를 돈다.
    """

    def __init__(self, pool_kwargs: dict[str, Any], pool_factory=None) -> None:
        self._pool_kwargs = pool_kwargs
        self._pool_factory = pool_factory or aiomysql.create_pool
        self._pool: Any = None
        self._task: asyncio.Task | None = None

    async def _create(self) -> Any:
        """생성 본체. 성공 대입을 태스크 **안**에서 한다 — 대기자가 전부 취소돼도
        만들어진 풀이 self._pool에 남아 close()가 닫을 수 있다(고아 풀 방지)."""
        self._pool = await self._pool_factory(**self._pool_kwargs)
        return self._pool

    async def get(self) -> Any:
        """풀을 반환한다(최초 호출 시 생성). 생성 실패는 드라이버 예외 그대로 올라간다."""
        if self._pool is not None:
            return self._pool
        task = self._task
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            task = asyncio.get_running_loop().create_task(self._create())
            self._task = task
        return await asyncio.shield(task)

    async def close(self) -> None:
        """진행 중인 생성 태스크를 취소하고 풀을 닫는다(앱 종료 훅).

        get()의 shield가 대기자 취소 전파를 막으므로 태스크의 마감 책임은 여기에 있다
        (완료된 태스크면 cancel이 no-op). 만들어진 적 없으면 전체가 no-op이다.
        """
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
