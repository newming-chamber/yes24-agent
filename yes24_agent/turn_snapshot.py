"""완료 당시 공개 턴 데이터를 ADK 이벤트에 보존한다."""

import logging
from copy import deepcopy

from google.adk.events import Event
from google.adk.sessions import BaseSessionService

from yes24_agent.agent import AGENT_NAME
from yes24_agent.config import get_settings

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
) -> bool:
    """세션 락 안에서 호출한다. 동일한 확정본 재시도는 추가 이벤트를 만들지 않는다."""
    if not turn_id:
        return False
    try:
        snapshot = {field: deepcopy(payload[field]) for field in _SNAPSHOT_FIELDS}
        snapshot.update(session_id=session_id, turn_id=turn_id, history_saved=True)
        session = await service.get_session(
            app_name=get_settings().app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            logger.warning("턴 스냅샷 저장 생략: 세션 없음(turn_id=%s)", turn_id)
            return False
        for event in reversed(session.events):
            if event.invocation_id != turn_id:
                continue
            existing = (event.custom_metadata or {}).get(TURN_SNAPSHOT_KEY)
            if existing is not None:
                if existing == snapshot:
                    return True
                logger.warning("확정된 턴 스냅샷 변경 거부(turn_id=%s)", turn_id)
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
        logger.warning("턴 스냅샷 영속 실패(turn_id=%s): %s", turn_id, exc)
        return False
