"""사용자별 대화 UI 상태 — session_ui 테이블(읽음 시각·사용자가 지은 제목).

**왜 ADK 세션 state가 아니라 우리 테이블인가**(2026-09-01 라이브 실측):
ADK 세션 테이블의 `update_time`에는 `onupdate=func.now()`가 걸려 있어, state를 쓰는 어떤
이벤트든 세션의 활동 시각을 현재로 민다. 읽음 표시를 state에 쓰면 읽는 순간 다시 안 읽음이
되고, 이름 변경이 목록의 '최근 활동순'을 밀어 올린다. `event.timestamp`에 현재
`last_update_time`을 그대로 실어 값을 고정하려던 초안은 **같은 값이라 SQLAlchemy가 컬럼을
dirty로 보지 않아 UPDATE의 SET 절에서 빠졌고, 그 자리를 onupdate가 채워** 오히려 실패했다
(UTC에서는 now로, KST에서는 +9시간으로 — 이유가 정반대일 뿐 양쪽 다 깨진다).

그래서 경계를 나눈다: **대화 내용은 ADK가, 사용자 UI 상태는 우리 DB가 소유한다.**
turn_feedback과 같은 원칙이고, 부수 효과로 시간대·ORM 내부 동작에 대한 전제가 전부 사라진다
(초안이 달고 다니던 "프로세스가 UTC여야 성립" 경고도 함께 삭제).

DDL은 `scripts/session_ui.sql` 수동 적용(turn_feedback·usage_log 관례).
"""

from __future__ import annotations

from yes24_agent.config import get_settings
from yes24_agent.db import MysqlBackedService
from yes24_agent.session_service import mysql_pool_kwargs


class SessionUiService(MysqlBackedService):
    """session_ui 읽기/쓰기 서비스(프로세스 싱글턴).

    풀·활성 판정·질의·마감은 db.MysqlBackedService가 소유한다 — 여기 남는 것은 SQL뿐이다.
    실패 정책은 피드백과 같은 fail-loud다: 읽음 상태를 조용히 잃으면 "안 읽음 배지가 안
    꺼지는" 현상만 남고 원인을 볼 수 없다.
    """

    _instance: SessionUiService | None = None

    def __init__(self, pool_factory=None) -> None:
        super().__init__(
            mysql_pool_kwargs(
                get_settings().session_db_url, maxsize=get_settings().session_ui_pool_max
            ),
            pool_factory,
            unavailable_detail="대화 상태 저장소가 없는 구성입니다(세션 DB가 MySQL이 아님).",
            failure_detail="대화 상태 저장에 실패했습니다.",
        )

    @classmethod
    def get_instance(cls) -> SessionUiService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def for_user(self, *, user_id: str) -> dict[str, tuple[str | None, float | None]]:
        """목록용 일괄 조회 — {session_id: (title, last_read_at)}.

        세션마다 질의하지 않는다(목록 1장에 N+1 질의가 되는 자리다). 사용자당 행 수는
        대화 수와 같은 규모라 한 번에 읽어 메모리에서 합친다.

        **읽기는 비활성 구성(sqlite 로컬)에서 빈 값으로 내려간다** — 저장소가 없으면 상태도
        없는 것이 사실이라(전부 unread·자동 제목) 목록·복원이 그대로 동작한다. 쓰기는 반대로
        503이다: 사용자가 누른 이름 변경이 조용히 사라지면 안 된다(fail-loud).
        """
        if not self.enabled:
            return {}
        rows = await self._run(
            "SELECT session_id, title, last_read_at FROM session_ui WHERE user_id = %s",
            (user_id,),
            fetch_all=True,
        )
        return {row[0]: (row[1], row[2]) for row in rows or ()}

    async def get(self, *, user_id: str, session_id: str) -> tuple[str | None, float | None]:
        """단건 조회 — (title, last_read_at). 행이 없거나 비활성 구성이면 (None, None)."""
        if not self.enabled:
            return (None, None)
        rows = await self._run(
            "SELECT title, last_read_at FROM session_ui WHERE user_id = %s AND session_id = %s",
            (user_id, session_id),
            fetch_all=True,
        )
        return (rows[0][0], rows[0][1]) if rows else (None, None)

    async def mark_read(self, *, user_id: str, session_id: str, read_at: float) -> None:
        """읽음 기록. `read_at`은 세션의 `last_update_time`을 그대로 싣는다 — 벽시계가 아니라
        **비교 대상과 같은 값**을 저장해야 부동소수·시계 오차로 unread가 되살아나지 않는다."""
        await self._run(
            "INSERT INTO session_ui (user_id, session_id, last_read_at) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE last_read_at = VALUES(last_read_at)",
            (user_id, session_id, read_at),
        )

    async def set_title(self, *, user_id: str, session_id: str, title: str) -> None:
        """사용자 제목 저장(이름 변경). ADK 세션에는 쓰지 않으므로 목록 순서가 밀리지 않는다."""
        await self._run(
            "INSERT INTO session_ui (user_id, session_id, title) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE title = VALUES(title)",
            (user_id, session_id, title),
        )

    async def purge_session(self, *, user_id: str, session_id: str) -> None:
        """대화 삭제와 함께 불린다 — 사용자가 지은 제목도 사용자 데이터다(고아 행 방지)."""
        await self._run(
            "DELETE FROM session_ui WHERE user_id = %s AND session_id = %s",
            (user_id, session_id),
        )


async def close_session_ui_service() -> None:
    """앱 종료 훅 — 서비스가 실제로 만들어졌을 때만 풀을 닫는다(close_feedback_service 대칭)."""
    if SessionUiService._instance is not None:
        await SessionUiService._instance.close()
