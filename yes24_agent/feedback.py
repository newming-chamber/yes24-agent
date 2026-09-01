"""턴 피드백 저장 — turn_feedback 테이블 (좋아요/싫어요 + 선택 코멘트).

턴의 열쇠는 ADK invocation_id다(`/chat/stream` done의 `turn_id`, events 테이블의 1급
컬럼과 같은 값). 세션 state JSON이 아니라 전용 테이블인 이유는 집계다 — state에 넣으면
"싫어요 상위 턴"이 전 세션 스캔이 된다(사용량 계측이 같은 이유로 events 재사용을 기각하고
usage_log로 간 판단의 반복). DDL은 `scripts/turn_feedback.sql` 수동 적용(usage_log 관례).

접속 정보는 인증·사용량과 같은 단일 출처 — `config.session_db_url`을
session_service.mysql_pool_kwargs로 파싱한다. 세션 DB가 mysql이 아니면(로컬 sqlite 개발)
스택이 자연 비활성이다(구조 분기). 실질적으로는 인증 스택과 활성 조건이 같아, 피드백
라우트(x-api-key 필수)가 통과되는 배포에서는 이 스택도 항상 성립한다.

실패 정책은 usage_log와 **정반대가 의도된 설계**다: 피드백은 사용자 행동에 대한 응답이라
저장 실패를 200으로 숨기면 "남긴 줄 아는 피드백이 없는" 조용한 유실이 된다 — DB 오류는
auth와 같은 503으로 정직하게 끊는다(fail-loud).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from yes24_agent.config import get_settings
from yes24_agent.db import LazyAiomysqlPool
from yes24_agent.session_service import mysql_pool_kwargs

logger = logging.getLogger(__name__)


class FeedbackService:
    """turn_feedback 읽기/쓰기 서비스(프로세스 싱글턴).

    풀 팩토리를 생성자로 주입할 수 있다 — 테스트는 실 DB 없이 스텁으로 전 경로를 돈다
    (AuthService·UsageLogger와 같은 패턴).
    """

    _instance: FeedbackService | None = None

    def __init__(self, pool_factory=None) -> None:
        self._pool_kwargs = mysql_pool_kwargs(
            get_settings().session_db_url, maxsize=get_settings().feedback_pool_max
        )
        # 풀 생성 기계(지연 생성·태스크 공유·shield)는 db.LazyAiomysqlPool 단일 구현이다 —
        # 이 모듈에 남는 것은 실패 정책(503 fail-loud)뿐이다. mysql이 아니면 풀이 없다.
        self._db = (
            LazyAiomysqlPool(self._pool_kwargs, pool_factory)
            if self._pool_kwargs is not None
            else None
        )

    @classmethod
    def get_instance(cls) -> FeedbackService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def enabled(self) -> bool:
        """피드백 스택이 성립하는가 — 세션 DB가 mysql일 때만 True(auth.enabled와 같은 판정)."""
        return self._pool_kwargs is not None

    async def close(self) -> None:
        """커넥션 풀을 정리한다(앱 종료 훅) — 마감은 공유 풀 구현이 소유한다."""
        if self._db is not None:
            await self._db.close()

    async def _run(self, sql: str, params: tuple, *, fetch_all: bool = False):
        """질의 1건 실행(필요하면 전 행 반환). DB 오류는 삼키지 않고 503으로 올린다.

        비활성 구성(sqlite)에서 호출되면 그것도 503이다 — 피드백 라우트는 인증 필수라
        정상 배포에서 이 분기는 죽어 있지만, 조용한 no-op 성공으로 위장하지 않는다.
        """
        if self._db is None:
            raise HTTPException(
                status_code=503, detail="피드백 저장소가 없는 구성입니다(세션 DB가 MySQL이 아님)."
            )
        try:
            pool = await self._db.get()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    return await cur.fetchall() if fetch_all else None
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — 저장 실패를 200으로 숨기지 않는다(fail-loud)
            logger.error(f"피드백 DB 질의 실패: {exc}")
            raise HTTPException(status_code=503, detail="피드백 저장에 실패했습니다.") from exc

    async def upsert(
        self, *, user_id: str, session_id: str, turn_id: str, rating: str, comment: str | None
    ) -> None:
        """턴 피드백을 저장한다 — 같은 (사용자, 세션, 턴)의 재전송은 최신값으로 덮는다(PUT 멱등)."""
        await self._run(
            "INSERT INTO turn_feedback (user_id, session_id, turn_id, rating, comment) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE rating = VALUES(rating), comment = VALUES(comment)",
            (user_id, session_id, turn_id, rating, comment),
        )

    async def withdraw(self, *, user_id: str, session_id: str, turn_id: str) -> None:
        """피드백 철회 — 행을 지운다(집계에 '철회됨' 상태를 남기지 않는다). 없던 행이면 no-op."""
        await self._run(
            "DELETE FROM turn_feedback WHERE user_id = %s AND session_id = %s AND turn_id = %s",
            (user_id, session_id, turn_id),
        )

    async def for_session(self, *, user_id: str, session_id: str) -> dict[str, dict[str, Any]]:
        """세션 복원용 일괄 조회 — {turn_id: {"rating": …, "comment": …}}.

        복원 화면의 썸 상태는 저장의 반대면이라 같은 fail-loud를 쓴다 — 조회 실패를 빈
        dict로 숨기면 "피드백이 없는 것"과 "읽지 못한 것"이 같은 화면이 된다.
        """
        rows = await self._run(
            "SELECT turn_id, rating, comment FROM turn_feedback "
            "WHERE user_id = %s AND session_id = %s",
            (user_id, session_id),
            fetch_all=True,
        )
        return {row[0]: {"rating": row[1], "comment": row[2]} for row in rows or ()}


async def close_feedback_service() -> None:
    """앱 종료 훅 — 서비스가 실제로 만들어졌을 때만 풀을 닫는다(close_auth_service 대칭)."""
    if FeedbackService._instance is not None:
        await FeedbackService._instance.close()
