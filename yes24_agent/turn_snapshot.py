"""공개 확정본은 MySQL chat_turn에, 로컬 SQLite에서는 ADK 이벤트에 보존한다."""

import logging
from copy import deepcopy
from datetime import datetime, timezone

from google.adk.events import Event
from google.adk.sessions import BaseSessionService, DatabaseSessionService

from yes24_agent.agent import AGENT_NAME
from yes24_agent.config import get_settings
from yes24_agent.user_data import UserDataService

logger = logging.getLogger(__name__)

TURN_SNAPSHOT_KEY = "turn_snapshot"
_SNAPSHOT_FIELDS = (
    "text",
    "sources",
    "cited_ids",
    "process",
    "meta",
    "status",
    "error",
    "rbti_applied",
)


async def persist_turn_snapshot(
    service: BaseSessionService,
    session_id: str,
    user_id: str,
    turn_id: str | None,
    payload: dict,
    *,
    user_text: str | None = None,
    started_at: float | None = None,
) -> bool:
    """세션 락 안에서 호출한다. 동일한 확정본 재시도는 추가 이벤트를 만들지 않는다."""
    if not turn_id:
        return False
    try:
        snapshot = {field: deepcopy(payload[field]) for field in _SNAPSHOT_FIELDS}
        snapshot.update(session_id=session_id, turn_id=turn_id, history_saved=True)
        data = UserDataService.get_instance()
        table_storage = data.enabled and isinstance(service, DatabaseSessionService)
        if table_storage and user_text is not None and started_at is not None:
            return await data.save_turn(
                app_name=get_settings().app_name,
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                user_text=user_text,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).timestamp(),
                payload=snapshot,
            )
        session = await service.get_session(
            app_name=get_settings().app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            logger.warning(f"턴 스냅샷 저장 생략: 세션 없음(turn_id={turn_id})")
            return False
        if table_storage:
            events = [event for event in session.events if event.invocation_id == turn_id]
            if not events:
                return False
            user_text = "\n".join(
                part.text
                for event in events
                if event.author == "user"
                for part in (event.content.parts if event.content else []) or []
                if part.text and not part.thought
            )
            return await data.save_turn(
                app_name=get_settings().app_name,
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                user_text=user_text,
                started_at=min(event.timestamp for event in events),
                completed_at=datetime.now(timezone.utc).timestamp(),
                payload=snapshot,
            )
        for event in reversed(session.events):
            if event.invocation_id != turn_id:
                continue
            existing = (event.custom_metadata or {}).get(TURN_SNAPSHOT_KEY)
            if existing is not None:
                if existing == snapshot:
                    return True
                logger.warning(f"확정된 턴 스냅샷 변경 거부(turn_id={turn_id})")
                return False
        await service.append_event(
            session,
            Event(
                author=AGENT_NAME,
                invocation_id=turn_id,
                custom_metadata={TURN_SNAPSHOT_KEY: snapshot},
            ),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — 부가 영속 실패가 답변을 막지 않는다
        logger.warning(f"턴 스냅샷 영속 실패(turn_id={turn_id}): {exc}")
        return False
