"""ADK DatabaseSessionService 프로젝트 소유 어댑터 — DATETIME UTC 경계 · 취소 내성."""

import asyncio
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

from google.adk.events import Event
from google.adk.sessions import DatabaseSessionService
from google.adk.sessions.base_session_service import (
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.schemas.v1 import StorageEvent, StorageSession
from google.adk.sessions.session import Session
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.orm.attributes import set_committed_value

from yes24_agent.db import run_detached

_append_epoch: ContextVar[float | None] = ContextVar("adk_append_epoch", default=None)


def _utc_assignment(target, value, oldvalue, initiator):
    epoch = _append_epoch.get()
    if epoch is not None:
        return datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _load_event_timestamp(target, context):
    epoch = (target.event_data or {}).get("timestamp")
    timestamp = (
        datetime.fromtimestamp(epoch, timezone.utc)
        if epoch is not None
        else target.timestamp.replace(tzinfo=timezone.utc)
    )
    set_committed_value(target, "timestamp", timestamp)


if not globals().get("_hooks_installed", False):
    sqlalchemy_event.listen(StorageSession.update_time, "set", _utc_assignment, retval=True)
    sqlalchemy_event.listen(StorageEvent.timestamp, "set", _utc_assignment, retval=True)
    sqlalchemy_event.listen(StorageEvent, "load", _load_event_timestamp)
    _hooks_installed = True


# 요청(턴) 단위로 진행 중인 세션 append 태스크. db._detached_ops 전역을 기다리면 다른 요청의
# DB 연산까지 기다려 세션 락 보유가 늘어나므로, 턴이 연 범위 안의 append만 따로 모은다.
_pending_appends: ContextVar[set[asyncio.Task] | None] = ContextVar(
    "pending_session_appends", default=None
)


def begin_append_scope() -> tuple[set[asyncio.Task], Token]:
    """이 컨텍스트(턴)에서 시작되는 append_event 태스크를 모을 집합을 연다."""
    pending: set[asyncio.Task] = set()
    return pending, _pending_appends.set(pending)


def finish_append_scope(pending: set[asyncio.Task], token: Token) -> None:
    """범위를 닫는다 — 다른 Context에서 닫히면 reset이 거부되므로 같은 집합일 때만 비운다."""
    try:
        _pending_appends.reset(token)
    except ValueError:
        if _pending_appends.get() is pending:
            _pending_appends.set(None)


async def settle_pending_appends(pending: set[asyncio.Task]) -> None:
    """범위 안에서 아직 도는 append가 끝날 때까지 기다린다. 결과·예외는 보지 않되 gather가
    예외를 retrieved로 표시한다 — 취소 없이 범위만 남은 append가 실패하면 wait만으로는 GC 때
    "never retrieved" 경고가 난다. shield는 호출자 취소가 이 대기를 끊지 않게 한다."""
    if pending:
        await asyncio.shield(asyncio.gather(*tuple(pending), return_exceptions=True))


class UtcDatabaseSessionService(DatabaseSessionService):
    """DB를 만지는 공개 메서드 전부를 `db.run_detached`로 감싼다(취소 내성 — 근거는 그쪽 docstring).
    """

    async def append_event(self, session: Session, event: Event) -> Event:
        return await run_detached(
            self._append_event_utc(session, event), _pending_appends.get()
        )

    async def _append_event_utc(self, session: Session, event: Event) -> Event:
        token = _append_epoch.set(event.timestamp)
        try:
            return await super().append_event(session, event)
        finally:
            _append_epoch.reset(token)

    async def get_session(
        self, *, app_name: str, user_id: str, session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        if config and config.after_timestamp is not None:
            # ADK는 이 값을 timezone 없는 fromtimestamp로 SQL 경계에 넣는다.
            wall_time = datetime.fromtimestamp(config.after_timestamp, timezone.utc)
            config = config.model_copy(update={
                "after_timestamp": wall_time.replace(tzinfo=None).timestamp(),
            })
        return await run_detached(super().get_session(
            app_name=app_name, user_id=user_id, session_id=session_id, config=config,
        ))

    async def create_session(self, **kwargs: Any) -> Session:
        return await run_detached(super().create_session(**kwargs))

    async def list_sessions(self, **kwargs: Any) -> ListSessionsResponse:
        return await run_detached(super().list_sessions(**kwargs))

    async def delete_session(self, *args: Any, **kwargs: Any) -> None:
        return await run_detached(super().delete_session(*args, **kwargs))

    async def get_user_state(self, **kwargs: Any) -> dict[str, Any]:
        return await run_detached(super().get_user_state(**kwargs))
