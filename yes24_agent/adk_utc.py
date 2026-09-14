"""ADK DATETIME 경계에서 호스트 시간대를 제거하는 프로젝트 소유 어댑터."""

from contextvars import ContextVar
from datetime import datetime, timezone

from google.adk.events import Event
from google.adk.sessions import DatabaseSessionService
from google.adk.sessions.base_session_service import GetSessionConfig
from google.adk.sessions.schemas.v1 import StorageEvent, StorageSession
from google.adk.sessions.session import Session
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.orm.attributes import set_committed_value

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


class UtcDatabaseSessionService(DatabaseSessionService):
    async def append_event(self, session: Session, event: Event) -> Event:
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
        return await super().get_session(
            app_name=app_name, user_id=user_id, session_id=session_id, config=config,
        )
