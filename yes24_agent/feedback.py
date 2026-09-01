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

from typing import Any

from yes24_agent.config import get_settings
from yes24_agent.db import MysqlBackedService
from yes24_agent.session_service import mysql_pool_kwargs


class FeedbackService(MysqlBackedService):
    """turn_feedback 읽기/쓰기 서비스(프로세스 싱글턴).

    풀 보유·활성 판정·질의·마감은 db.MysqlBackedService가 소유한다 — 이 클래스에 남는 것은
    테이블과 SQL뿐이다. 풀 팩토리를 생성자로 주입할 수 있어 테스트는 실 DB 없이 전 경로를
    돈다(AuthService·UsageLogger와 같은 패턴).
    """

    _instance: FeedbackService | None = None

    def __init__(self, pool_factory=None) -> None:
        super().__init__(
            mysql_pool_kwargs(
                get_settings().session_db_url, maxsize=get_settings().feedback_pool_max
            ),
            pool_factory,
            unavailable_detail="피드백 저장소가 없는 구성입니다(세션 DB가 MySQL이 아님).",
            failure_detail="피드백 저장에 실패했습니다.",
        )

    @classmethod
    def get_instance(cls) -> FeedbackService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

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

    async def purge_session(self, *, user_id: str, session_id: str) -> None:
        """세션의 피드백을 전부 지운다 — **대화 삭제와 함께 불린다**.

        세션만 지우고 이 행을 남기면 사용자가 쓴 코멘트가 session_id·user_id와 함께 남는다.
        삭제 API의 존재 이유가 프라이버시인데 그게 남으면 삭제가 아니다(2026-09-01 라이브
        검증에서 고아 행으로 관측 — 결정론 테스트는 DB가 없어 못 잡았다).
        집계 신호를 잃는 대가는 치른다 — 사용자가 지우겠다고 한 것이 우선이다.
        """
        await self._run(
            "DELETE FROM turn_feedback WHERE user_id=%s AND session_id=%s",
            (user_id, session_id),
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
