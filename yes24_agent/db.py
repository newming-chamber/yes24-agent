"""aiomysql lazy 커넥션 풀 + 부가 테이블 서비스 골격 — 인증·사용량·피드백·대화 UI 상태 공용.

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
import logging
from contextlib import asynccontextmanager
from typing import Any

import aiomysql
from fastapi import HTTPException

logger = logging.getLogger(__name__)


# 바깥 취소로 분리된 채 끝까지 도는 DB 연산 태스크. 이벤트 루프는 태스크를 약하게만
# 참조하므로 완료 전 GC되지 않게 여기서 강하게 잡는다(완료 시 자동 제거).
_detached_ops: set[asyncio.Task] = set()


def detached_count() -> int:
    """아직 도는 분리 DB 연산 수 — 정지 진단용(setup_timeout 로그가 싣는다)."""
    return sum(1 for t in _detached_ops if not t.done())


async def run_detached(coro, pending: set[asyncio.Task] | None = None, *, name: str | None = None):
    """DB 연산을 내부 태스크로 돌리고 바깥 취소로부터 차폐한다 — 풀을 쓰는 모든 경로의 공통 관문.

    Starlette 스트림 취소(anyio cancel scope)는 await마다 취소를 다시 넣는다. 그러면 벤더
    `_rollback_on_exception_session`의 rollback과 세션 close가 연달아 취소되고, SQLAlchemy
    `_finalize_fairy`는 체크인 리셋 중 받은 CancelledError를 로그("Exception during reset or
    similar")한 뒤 소켓만 닫고 `checkin()` 전에 재발생시켜 풀 슬롯이 장부에 영구 점유된다
    (2026-10-17 상용 고갈, GC로도 회수 안 됨). aiomysql 풀도 같은 부류다 — 응답 패킷을 읽는
    도중 취소가 떨어지면 프로토콜이 어긋난 접속이 풀로 되돌아가 다음 사용자가 오지 않을
    패킷을 영원히 기다릴 수 있다(2026-10-21 QA 정지의 유력 후보). 내부 태스크는 취소를 받지
    않으므로 "acquire → execute → fetch → release"가 항상 완주해 접속이 정상 상태로 돌아가고,
    호출자는 즉시 CancelledError를 받는다. 바깥이 취소된 뒤 내부 예외는 shield가 retrieved로
    표시해 조용히 사라지므로, 바깥이 취소된 경우에만 직접 로그한다(바깥이 살아 있으면 예외가
    호출자로 전파되니 로그하지 않는다 — 중복 금지). contextvars는 create_task가 생성 시점
    컨텍스트를 복사해 전파된다. pending은 호출자(턴)가 "내가 띄운 연산"을 모아 두는 집합이다.
    """
    name = name or coro.__qualname__
    task = asyncio.create_task(coro)
    _detached_ops.add(task)
    task.add_done_callback(_detached_ops.discard)
    if pending is not None:
        pending.add(task)
        task.add_done_callback(pending.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(lambda done: _log_detached_failure(name, done))
        raise


def _log_detached_failure(name: str, task: asyncio.Task) -> None:
    """호출자가 떠난 뒤 끝난 내부 연산의 실패 — 받을 사람이 없으니 여기서 남긴다."""
    if task.cancelled() or task.exception() is None:
        return
    exc = task.exception()
    logger.warning(f"분리된 DB 연산 실패({name}): {type(exc).__name__}: {exc}")


async def run_within(context, body, pending: set[asyncio.Task] | None = None):
    """`async with context as resource: return await body(resource)`를 분리 태스크로 완주시킨다.

    컨텍스트 매니저(접속 획득·트랜잭션)와 본문이 **한 태스크** 안에 있어야 한다 — 본문만
    호출자 태스크에서 돌면 취소가 문장 중간에 떨어지고, 종료부의 rollback·release가 끊긴다.
    """

    async def within():
        async with context as resource:
            return await body(resource)

    return await run_detached(within(), pending, name=body.__qualname__)


async def rollback_unless_closed(conn: Any) -> None:
    """열린 접속만 되돌린다 — 닫힌 접속의 rollback이 원래 예외를 덮지 않게.

    aiomysql은 응답을 기다리던 중 취소되면 접속을 닫는다(`_close_on_cancel`). 그 접속에
    rollback을 보내면 `InterfaceError("Cancelled during execution")`가 나 **원래 예외를
    대체한다** — 클라이언트 이탈의 취소(CancelledError)가 "DB 트랜잭션 실패 503"으로 둔갑해
    삼켜지고, 커밋 응답 대기 중 취소면 실제로는 커밋된 행을 실패로 보고한다(2026-10-18·19
    상용 2건 실측). 닫힌 접속의 트랜잭션은 서버가 접속 종료로 버리므로 되돌릴 것이 없다.
    """
    if not conn.closed:
        await conn.rollback()


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

    @asynccontextmanager
    async def connection(self):
        """풀 접속 1개(최초 호출 시 풀 생성). `run`이 아닌 직접 사용은 취소 차폐가 없다 —
        본문을 호출자 태스크에서 돌려야 하는 컨텍스트 매니저(admin 트랜잭션)만 쓴다."""
        pool = await self.get()
        async with pool.acquire() as conn:
            yield conn

    async def run(self, body, pending: set[asyncio.Task] | None = None):
        """acquire → body(conn) → release를 분리 태스크로 완주시킨다(run_detached). 풀 질의의
        기본 진입점이다 — 드라이버·HTTP 예외는 그대로 올라가고, 바깥 취소는 호출자에게만 간다."""
        return await run_within(self.connection(), body, pending)

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


class MysqlBackedService:
    """세션 DB(MySQL) 위에 얹히는 부가 테이블 서비스의 공통 골격.

    피드백·대화 UI 상태가 **풀 보유·활성 판정·질의·마감**을 똑같이 반복하던 자리다
    (LazyAiomysqlPool이 생성 기계를 모은 것과 같은 이유 — 같은 판정을 두 벌 두면 한 벌만
    고치는 실수가 난다). 서브클래스가 정하는 것은 **문구뿐**이고, 실패 정책 자체는 공통이다:
    구성 부재도 질의 실패도 503으로 정직하게 끊는다(조용한 no-op 성공으로 위장하지 않는다).

    사용량 계측(usage)은 이 골격을 쓰지 않는다 — 부가 채널이라 실패를 삼키는 **반대 정책**이고,
    그 차이는 통일 대상이 아니라 설계다(계측 실패가 답변을 막으면 안 된다).
    """

    def __init__(
        self,
        pool_kwargs: dict[str, Any] | None,
        pool_factory=None,
        *,
        unavailable_detail: str,
        failure_detail: str,
    ) -> None:
        self._pool_kwargs = pool_kwargs
        self._db = (
            LazyAiomysqlPool(pool_kwargs, pool_factory) if pool_kwargs is not None else None
        )
        self._unavailable_detail = unavailable_detail
        self._failure_detail = failure_detail

    @property
    def enabled(self) -> bool:
        """스택이 성립하는가 — 세션 DB가 mysql일 때만 True(auth.enabled와 같은 판정)."""
        return self._pool_kwargs is not None

    async def close(self) -> None:
        """커넥션 풀을 정리한다(앱 종료 훅) — 마감은 공유 풀 구현이 소유한다."""
        if self._db is not None:
            await self._db.close()

    async def _run_all(self, statements: list[tuple[str, tuple]]) -> None:
        """여러 문장을 **한 트랜잭션**으로 실행한다 — 중간에 실패하면 전부 되돌린다.

        `_run`을 여러 번 부르면 풀이 autocommit이라 각 문장이 독립 커밋된다(session_service.
        mysql_pool_kwargs). 그러면 "절반만 지워진 삭제" 같은 중간 상태가 사용자에게 남는다
        — 실패했다고 알렸는데 일부는 이미 사라진 상태다(2026-09-02 적대 감사 F2 실측).
        """
        async def transaction(conn):
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    for sql, params in statements:
                        await cur.execute(sql, params)
                await conn.commit()
            except Exception:
                await rollback_unless_closed(conn)
                raise

        await self._guarded(transaction)

    async def _run(self, sql: str, params: tuple, *, fetch_all: bool = False):
        """질의 1건 실행. fetch_all이면 전 행, 아니면 `(rowcount, lastrowid)`를 돌려준다.

        쓰기가 rowcount·lastrowid를 돌려주는 이유: 선점형 잠금(`INSERT IGNORE`의 rowcount 0 =
        다른 워커가 먼저 잡았다)과 새 행의 id가 필요한 호출자가 있다. DB 오류는 삼키지 않고
        503으로 올린다.
        """

        async def query(conn):
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchall() if fetch_all else (cur.rowcount, cur.lastrowid)

        return await self._guarded(query)

    async def _guarded(self, body):
        """body(conn)을 풀 접속 위에서 분리 태스크로 완주시킨다(LazyAiomysqlPool.run) — 구성
        부재는 503, 드라이버 실패도 503(fail-loud), HTTPException은 그대로. 서브클래스의 트랜잭션
        진입점도 이 한 판정을 쓴다."""
        if self._db is None:
            raise HTTPException(status_code=503, detail=self._unavailable_detail)
        try:
            return await self._db.run(body)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — 실패를 200으로 숨기지 않는다(fail-loud)
            logger.error(f"{self._failure_detail} ({type(exc).__name__}): {exc}")
            raise HTTPException(status_code=503, detail=self._failure_detail) from exc
