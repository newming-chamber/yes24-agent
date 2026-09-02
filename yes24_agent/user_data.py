"""우리 DB가 소유하는 **사용자별 대화 부가 데이터** — `turn_feedback`·`session_ui` 두 테이블.

경계가 이 모듈의 존재 이유다: **대화 내용은 ADK가, 사용자가 대화에 남긴 것은 우리가** 소유한다.
- `turn_feedback` — 턴 좋아요/싫어요 + 코멘트. 턴의 열쇠는 ADK invocation_id(`done.turn_id`).
  세션 state JSON이 아니라 전용 테이블인 이유는 집계다 — state에 넣으면 "싫어요 상위 턴"이
  전 세션 스캔이 된다(usage 계측이 events 재사용을 기각하고 usage_log로 간 판단의 반복).
- `session_ui` — 읽음 시각·사용자가 지은 제목. ADK 세션 state에 두면 안 되는 이유는 그 테이블의
  `update_time`에 `onupdate=func.now()`가 걸려 있어서다: state를 쓰는 순간 활동 시각이 현재로
  밀려 "읽으면 다시 안 읽음"이 되고 목록 순서가 튄다. `event.timestamp`에 현재 값을 그대로
  실어 고정하려던 초안은 **값이 같아 SQLAlchemy가 컬럼을 dirty로 보지 않아 UPDATE에서 빠지고
  그 자리를 onupdate가 채워** 오히려 실패했다(2026-09-01 라이브 실측).

**두 테이블이 한 서비스인 이유**: 같은 DB·같은 사용자 스코프·같은 실패 정책이라 풀·활성 판정·
마감을 두 벌 둘 근거가 없다(처음엔 나눠 뒀다가 커넥션 풀만 두 개가 됐다). 대화 삭제가 두
테이블을 함께 비우는 것도 여기서 한 메서드로 성립한다.

접속 정보는 인증·사용량과 같은 단일 출처 — `config.session_db_url`을 파싱한다. 세션 DB가
mysql이 아니면(로컬 sqlite) 스택이 자연 비활성이다(구조 분기).

실패 정책은 usage_log와 **정반대가 의도된 설계**다: 사용자 행동에 대한 응답이라 저장 실패를
200으로 숨기면 "남긴 줄 아는 피드백이 없는" 조용한 유실이 된다 — auth와 같은 503으로 끊는다.
단 **읽기는 비활성 구성에서 빈 값으로 내려간다**: 저장소가 없으면 상태도 없는 것이 사실이라
(전부 unread·자동 제목) 목록·복원이 그대로 동작한다.

DDL은 `scripts/turn_feedback.sql`·`scripts/session_ui.sql` 수동 적용(usage_log 관례).
"""

from __future__ import annotations

from typing import Any

from yes24_agent.config import get_settings
from yes24_agent.db import MysqlBackedService
from yes24_agent.session_service import mysql_pool_kwargs


class UserDataService(MysqlBackedService):
    """turn_feedback·session_ui 읽기/쓰기 서비스(프로세스 싱글턴, 풀 1개).

    풀 보유·활성 판정·질의·마감은 db.MysqlBackedService가 소유한다 — 여기 남는 것은 SQL뿐이다.
    풀 팩토리를 주입할 수 있어 테스트는 실 DB 없이 전 경로를 돈다(AuthService·UsageLogger 패턴).
    """

    _instance: UserDataService | None = None

    def __init__(self, pool_factory=None) -> None:
        super().__init__(
            mysql_pool_kwargs(
                get_settings().session_db_url, maxsize=get_settings().user_data_pool_max
            ),
            pool_factory,
            unavailable_detail="대화 데이터 저장소가 없는 구성입니다(세션 DB가 MySQL이 아님).",
            failure_detail="대화 데이터 저장에 실패했습니다.",
        )

    @classmethod
    def get_instance(cls) -> UserDataService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── turn_feedback ──────────────────────────────────────────────────────

    async def upsert_feedback(
        self, *, user_id: str, session_id: str, turn_id: str, rating: str, comment: str | None
    ) -> None:
        """턴 피드백 저장 — 같은 (사용자, 세션, 턴)의 재전송은 최신값으로 덮는다(PUT 멱등)."""
        await self._run(
            "INSERT INTO turn_feedback (user_id, session_id, turn_id, rating, comment) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE rating = VALUES(rating), comment = VALUES(comment)",
            (user_id, session_id, turn_id, rating, comment),
        )

    async def withdraw_feedback(self, *, user_id: str, session_id: str, turn_id: str) -> None:
        """피드백 철회 — 행을 지운다(집계에 '철회됨' 상태를 남기지 않는다). 없던 행이면 no-op."""
        await self._run(
            "DELETE FROM turn_feedback WHERE user_id = %s AND session_id = %s AND turn_id = %s",
            (user_id, session_id, turn_id),
        )

    async def feedback_for_session(
        self, *, user_id: str, session_id: str
    ) -> dict[str, dict[str, Any]]:
        """세션 복원용 일괄 조회 — {turn_id: {"rating": …, "comment": …}}. 비활성이면 빈 dict.

        **저장소가 있는데 못 읽은 것**은 fail-loud다(503) — 조회 실패를 빈 dict로 숨기면
        "피드백이 없는 것"과 "읽지 못한 것"이 같은 화면이 된다. 반면 **저장소가 아예 없는
        구성**(sqlite 로컬)은 "남긴 피드백이 없다"가 사실이므로 빈 값이 맞다. 이 가드가
        빠져 있어 sqlite에서 대화 복원 전체가 503이었다(2026-09-02 적대 감사 F1) — 목록은
        뜨는데 아무거나 클릭하면 죽어서 원인을 찾기 어려웠다. 읽기 셋(ui_for_user·ui_get·
        이 함수)이 같은 규칙을 따라야 그 비대칭이 다시 생기지 않는다.
        """
        if not self.enabled:
            return {}
        rows = await self._run(
            "SELECT turn_id, rating, comment FROM turn_feedback "
            "WHERE user_id = %s AND session_id = %s",
            (user_id, session_id),
            fetch_all=True,
        )
        return {row[0]: {"rating": row[1], "comment": row[2]} for row in rows or ()}

    # ── session_ui ─────────────────────────────────────────────────────────

    async def ui_for_user(self, *, user_id: str) -> dict[str, tuple[str | None, float | None]]:
        """목록용 일괄 조회 — {session_id: (title, last_read_at)}.

        세션마다 질의하지 않는다(목록 1장에 N+1 질의가 되는 자리다).
        """
        if not self.enabled:
            return {}
        rows = await self._run(
            "SELECT session_id, title, last_read_at FROM session_ui WHERE user_id = %s",
            (user_id,),
            fetch_all=True,
        )
        return {row[0]: (row[1], row[2]) for row in rows or ()}

    async def ui_get(self, *, user_id: str, session_id: str) -> tuple[str | None, float | None]:
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

    # ── 대화 삭제 ──────────────────────────────────────────────────────────

    async def purge_session(self, *, user_id: str, session_id: str) -> None:
        """세션이 남긴 **우리 쪽 데이터 전부**를 지운다 — 대화 삭제와 함께 불린다.

        세션만 지우고 이 행들을 남기면 사용자가 쓴 코멘트·제목이 session_id·user_id와 함께
        남는다. 삭제 API의 존재 이유가 프라이버시인데 그게 남으면 삭제가 아니다(2026-09-01
        라이브 검증에서 고아 행으로 관측 — 결정론 테스트는 DB가 없어 못 잡았다).
        두 테이블이 한 서비스라 **빠뜨릴 수 있는 자리가 없다**(테이블이 늘면 여기만 는다).

        **한 트랜잭션**으로 묶는다 — 문장을 따로 보내면 풀이 autocommit이라 앞 테이블만 지워진
        채 실패할 수 있고, 그러면 "삭제 실패"라고 알린 뒤에 코멘트만 사라진 상태가 남는다.
        """
        await self._run_all(
            [
                (
                    "DELETE FROM turn_feedback WHERE user_id = %s AND session_id = %s",
                    (user_id, session_id),
                ),
                (
                    "DELETE FROM session_ui WHERE user_id = %s AND session_id = %s",
                    (user_id, session_id),
                ),
            ]
        )


async def close_user_data_service() -> None:
    """앱 종료 훅 — 서비스가 실제로 만들어졌을 때만 풀을 닫는다(close_auth_service 대칭)."""
    if UserDataService._instance is not None:
        await UserDataService._instance.close()
