"""공개 대화 확정본과 사용자별 피드백·클릭·UI 상태를 같은 DB 풀로 관리한다.

ADK는 LLM 원시 이벤트를, chat_turn은 사용자에게 공개한 확정본을 소유한다.
- `turn_feedback` — 턴 좋아요/싫어요 + 코멘트. 턴의 열쇠는 ADK invocation_id(`done.turn_id`).
  세션 state JSON이 아니라 전용 테이블인 이유는 집계다 — state에 넣으면 "싫어요 상위 턴"이
  전 세션 스캔이 된다(usage 계측이 events 재사용을 기각하고 usage_log로 간 판단의 반복).
- `turn_click` — 답변 안의 링크(상품·공지·웹·판형 무엇이든) 클릭. 열쇠는 **URL**이다 — crema
  시절의 goods_no·판형 열거형 스키마를 복제하지 않는다(2026-09-08): 대상이 늘어도 URL은 항상
  있으므로 스키마가 안 바뀐다. 피드백과 달리 **append-only**다(같은 URL 재클릭 = 행 추가).
  **purge 대상이 아니다** — 사용자 상태(피드백·제목)가 아니라 usage_log 같은 제품 분석 이벤트
  로그라, 대화 삭제와 함께 지우면 집계에 구멍이 난다(2026-09-08 사용자 결정).
- `session_ui` — 읽음 시각·사용자가 지은 제목. ADK 세션 state에 두면 안 되는 이유는 그 테이블의
  `update_time`에 `onupdate=func.now()`가 걸려 있어서다: state를 쓰는 순간 활동 시각이 현재로
  밀려 "읽으면 다시 안 읽음"이 되고 목록 순서가 튄다. `event.timestamp`에 현재 값을 그대로
  실어 고정하려던 초안은 **값이 같아 SQLAlchemy가 컬럼을 dirty로 보지 않아 UPDATE에서 빠지고
  그 자리를 onupdate가 채워** 오히려 실패했다(2026-09-01 라이브 실측).

동일한 DB·앱·사용자·세션 스코프를 공유하므로 풀과 삭제 트랜잭션을 한 서비스에 둔다.

접속 정보는 인증·사용량과 같은 단일 출처 — `config.session_db_url`을 파싱한다. 세션 DB가
mysql이 아니면(로컬 sqlite) 스택이 자연 비활성이다(구조 분기).

실패 정책은 usage_log와 **정반대가 의도된 설계**다: 사용자 행동에 대한 응답이라 저장 실패를
200으로 숨기면 "남긴 줄 아는 피드백이 없는" 조용한 유실이 된다 — auth와 같은 503으로 끊는다.
단 **읽기는 비활성 구성에서 빈 값으로 내려간다**: 저장소가 없으면 상태도 없는 것이 사실이라
(전부 unread·자동 제목) 목록·복원이 그대로 동작한다.

DDL은 `scripts/chat_turn.sql`·`scripts/turn_feedback.sql`·`scripts/turn_click.sql`·
`scripts/session_ui.sql`
(usage_log 관례).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from yes24_agent.config import get_settings
from yes24_agent.db import MysqlBackedService
from yes24_agent.session_service import mysql_pool_kwargs

logger = logging.getLogger(__name__)


class UserDataService(MysqlBackedService):
    """chat_turn·turn_feedback·turn_click·session_ui 서비스(프로세스 싱글턴, 풀 1개).

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

    async def verify_schema(self) -> None:
        """필수 저장 계약이 준비되지 않은 MySQL 인스턴스는 요청 수신 전에 실패한다."""
        if not self.enabled:
            return
        await self._run(
            "SELECT u.user_no, a.key_hash, t.id, t.app_name, t.user_id, t.session_id, "
            "t.turn_id, t.started_at, t.completed_at, t.user_text, t.text, t.status, "
            "t.sources, t.process, t.meta, t.error, t.rbti_applied, t.history_saved, "
            "f.app_name, c.app_name, s.app_name, l.app_name "
            "FROM users u, auth_keys a, chat_turn t, turn_feedback f, turn_click c, "
            "session_ui s, usage_log l WHERE 1 = 0",
            (),
            fetch_all=True,
        )

    async def _run_owned(
        self,
        statements: list[tuple[str, tuple]],
        *,
        owner: tuple[str, str, str],
        missing_ok: bool = False,
    ):
        """부모 세션 잠금 안에서 쓰고 삭제한다. 삭제 뒤 UI·확정본이 다시 생기지 않는다."""
        if self._db is None:
            raise HTTPException(status_code=503, detail=self._unavailable_detail)
        try:
            pool = await self._db.get()
            async with pool.acquire() as conn:
                await conn.begin()
                try:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT id FROM sessions WHERE app_name = %s "
                            "AND user_id = %s AND id = %s FOR UPDATE",
                            owner,
                        )
                        if not await cur.fetchone():
                            if missing_ok:
                                await conn.rollback()
                                return None
                            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
                        result = None
                        for sql, params in statements:
                            await cur.execute(sql, params)
                            result = await cur.fetchall() if cur.description else cur.rowcount
                    await conn.commit()
                    return result
                except BaseException:
                    await conn.rollback()
                    raise
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"대화 데이터 트랜잭션 실패: {type(exc).__name__}")
            raise HTTPException(status_code=503, detail=self._failure_detail) from exc

    async def _write_owned(self, sql: str, params: tuple) -> None:
        """소유권 열(app_name,user_id,session_id)을 먼저 받는 단일 쓰기 문장."""
        app_name, user_id, session_id, *_ = params
        await self._run_owned([(sql, params)], owner=(app_name, user_id, session_id))

    async def turns_for_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        rows = await self._run(
            "SELECT turn_id, user_text, started_at, completed_at, status, text, "
            "sources, process, meta, error, rbti_applied, history_saved FROM chat_turn "
            "WHERE app_name = %s AND user_id = %s AND session_id = %s "
            "ORDER BY started_at, completed_at, id",
            (app_name, user_id, session_id),
            fetch_all=True,
        )
        return [self._turn_record(row, session_id) for row in rows or ()]

    async def has_turn(self, *, app_name: str, user_id: str, session_id: str, turn_id: str) -> bool:
        if not self.enabled:
            return False
        return bool(
            await self._run(
                "SELECT 1 FROM chat_turn WHERE app_name = %s AND user_id = %s "
                "AND session_id = %s AND turn_id = %s",
                (app_name, user_id, session_id, turn_id),
                fetch_all=True,
            )
        )

    @staticmethod
    def _turn_record(row: tuple, session_id: str) -> dict[str, Any]:
        def decoded(value):
            return json.loads(value) if isinstance(value, (str, bytes)) else value

        sources, process, meta, error = map(decoded, row[6:10])
        return {
            "turn_id": row[0],
            "user_text": row[1],
            "started_at": row[2].replace(tzinfo=timezone.utc).timestamp(),
            "completed_at": row[3].replace(tzinfo=timezone.utc).timestamp(),
            "snapshot": {
                "session_id": session_id,
                "turn_id": row[0],
                "status": row[4],
                "text": row[5],
                "sources": sources,
                "cited_ids": [source["id"] for source in sources],
                "process": process,
                "meta": meta,
                "error": error,
                "rbti_applied": row[10],
                "history_saved": bool(row[11]),
            },
        }

    async def save_turn(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        turn_id: str,
        user_text: str,
        started_at: float,
        completed_at: float,
        payload: dict,
        verify_completed_at: bool = False,
    ) -> bool:
        """최초 확정본만 저장한다. 재전송은 값이 같을 때만 성공으로 인정한다."""
        if type(payload["history_saved"]) is not bool:
            raise ValueError("history_saved는 boolean이어야 합니다.")
        if payload["cited_ids"] != [source["id"] for source in payload["sources"]]:
            raise ValueError("인용 번호와 출처 순서가 일치하지 않습니다.")
        start = datetime.fromtimestamp(started_at, timezone.utc).replace(tzinfo=None)
        end = datetime.fromtimestamp(completed_at, timezone.utc).replace(tzinfo=None)
        encoded = tuple(
            json.dumps(payload[key], ensure_ascii=False) if payload[key] is not None else None
            for key in ("sources", "process", "meta", "error")
        )
        rows = await self._run_owned(
            [
                (
                    "INSERT INTO chat_turn (app_name, user_id, session_id, turn_id, "
                    "user_text, started_at, completed_at, status, text, "
                    "sources, process, meta, error, "
                    "rbti_applied, history_saved) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE id = id",
                    (
                        app_name,
                        user_id,
                        session_id,
                        turn_id,
                        user_text,
                        start,
                        end,
                        payload["status"],
                        payload["text"],
                        *encoded,
                        payload["rbti_applied"],
                        payload["history_saved"],
                    ),
                ),
                (
                    "SELECT turn_id, user_text, started_at, completed_at, status, text, "
                    "sources, process, meta, error, rbti_applied, history_saved FROM chat_turn "
                    "WHERE app_name = %s AND user_id = %s AND session_id = %s AND turn_id = %s",
                    (app_name, user_id, session_id, turn_id),
                ),
            ],
            owner=(app_name, user_id, session_id),
            missing_ok=True,
        )
        if not rows:
            return False
        existing = self._turn_record(rows[0], session_id)
        return (
            existing["user_text"] == user_text
            and rows[0][2] == start
            and (not verify_completed_at or rows[0][3] == end)
            and all(
                existing["snapshot"][key] == payload[key]
                for key in (
                    "status",
                    "text",
                    "sources",
                    "cited_ids",
                    "process",
                    "meta",
                    "error",
                    "rbti_applied",
                    "history_saved",
                )
            )
        )

    # ── turn_feedback ──────────────────────────────────────────────────────

    async def upsert_feedback(
        self, *, user_id: str, session_id: str, turn_id: str, rating: str, comment: str | None
    ) -> None:
        """턴 피드백 저장 — 같은 (사용자, 세션, 턴)의 재전송은 최신값으로 덮는다(PUT 멱등)."""
        await self._write_owned(
            "INSERT INTO turn_feedback (app_name, user_id, session_id, turn_id, rating, comment) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE rating = VALUES(rating), comment = VALUES(comment)",
            (get_settings().app_name, user_id, session_id, turn_id, rating, comment),
        )

    async def withdraw_feedback(self, *, user_id: str, session_id: str, turn_id: str) -> None:
        """피드백 철회 — 행을 지운다(집계에 '철회됨' 상태를 남기지 않는다). 없던 행이면 no-op."""
        await self._write_owned(
            "DELETE FROM turn_feedback "
            "WHERE app_name = %s AND user_id = %s AND session_id = %s AND turn_id = %s",
            (get_settings().app_name, user_id, session_id, turn_id),
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
            "WHERE app_name = %s AND user_id = %s AND session_id = %s",
            (get_settings().app_name, user_id, session_id),
            fetch_all=True,
        )
        return {row[0]: {"rating": row[1], "comment": row[2]} for row in rows or ()}

    # ── turn_click ─────────────────────────────────────────────────────────

    async def record_click(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        url: str,
        source_id: int | None,
        source_type: str | None,
        label: str | None,
    ) -> None:
        """링크 클릭 1건 기록 — 행 추가만 한다(같은 URL 재클릭은 행이 늘어난다, upsert 아님)."""
        await self._run(
            "INSERT INTO turn_click "
            "(app_name, user_id, session_id, turn_id, url, source_id, source_type, label) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                get_settings().app_name,
                user_id,
                session_id,
                turn_id,
                url,
                source_id,
                source_type,
                label,
            ),
        )

    # ── session_ui ─────────────────────────────────────────────────────────

    async def ui_for_user(self, *, user_id: str) -> dict[str, tuple[str | None, float | None]]:
        """목록용 일괄 조회 — {session_id: (title, last_read_at)}.

        세션마다 질의하지 않는다(목록 1장에 N+1 질의가 되는 자리다).
        """
        if not self.enabled:
            return {}
        rows = await self._run(
            "SELECT session_id, title, last_read_at FROM session_ui "
            "WHERE app_name = %s AND user_id = %s",
            (get_settings().app_name, user_id),
            fetch_all=True,
        )
        return {row[0]: (row[1], row[2]) for row in rows or ()}

    async def ui_get(self, *, user_id: str, session_id: str) -> tuple[str | None, float | None]:
        """단건 조회 — (title, last_read_at). 행이 없거나 비활성 구성이면 (None, None)."""
        if not self.enabled:
            return (None, None)
        rows = await self._run(
            "SELECT title, last_read_at FROM session_ui "
            "WHERE app_name = %s AND user_id = %s AND session_id = %s",
            (get_settings().app_name, user_id, session_id),
            fetch_all=True,
        )
        return (rows[0][0], rows[0][1]) if rows else (None, None)

    async def mark_read(self, *, user_id: str, session_id: str, read_at: float) -> None:
        """읽음 기록. `read_at`은 세션의 `last_update_time`을 그대로 싣는다 — 벽시계가 아니라
        **비교 대상과 같은 값**을 저장해야 부동소수·시계 오차로 unread가 되살아나지 않는다."""
        await self._write_owned(
            "INSERT INTO session_ui (app_name, user_id, session_id, last_read_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE last_read_at = "
            "GREATEST(COALESCE(last_read_at, VALUES(last_read_at)), VALUES(last_read_at))",
            (get_settings().app_name, user_id, session_id, read_at),
        )

    async def set_title(self, *, user_id: str, session_id: str, title: str) -> None:
        """사용자 제목 저장(이름 변경). ADK 세션에는 쓰지 않으므로 목록 순서가 밀리지 않는다."""
        await self._write_owned(
            "INSERT INTO session_ui (app_name, user_id, session_id, title) VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE title = VALUES(title)",
            (get_settings().app_name, user_id, session_id, title),
        )

    # ── 대화 삭제 ──────────────────────────────────────────────────────────

    async def purge_session(self, *, user_id: str, session_id: str) -> None:
        """부모 행락 안에서 공개 턴·사용자 상태·ADK 세션을 함께 삭제한다.

        events는 sessions FK의 CASCADE로 지워진다. 같은 부모 잠금을 사용하는 저장은
        삭제 후 존재 검사를 통과하지 못한다. 분석 로그(turn_click·usage_log)는 보존한다.
        """
        owner = (get_settings().app_name, user_id, session_id)
        await self._run_owned(
            [
                (
                    "DELETE FROM chat_turn WHERE app_name = %s "
                    "AND user_id = %s AND session_id = %s",
                    owner,
                ),
                (
                    "DELETE FROM turn_feedback WHERE app_name = %s "
                    "AND user_id = %s AND session_id = %s",
                    owner,
                ),
                (
                    "DELETE FROM session_ui WHERE app_name = %s "
                    "AND user_id = %s AND session_id = %s",
                    owner,
                ),
                ("DELETE FROM sessions WHERE app_name = %s AND user_id = %s AND id = %s", owner),
            ],
            owner=owner,
        )


async def close_user_data_service() -> None:
    """앱 종료 훅 — 서비스가 실제로 만들어졌을 때만 풀을 닫는다(close_auth_service 대칭)."""
    if UserDataService._instance is not None:
        await UserDataService._instance.close()
