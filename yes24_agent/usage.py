"""LLM 토큰 사용량 기록 — usage_log 테이블 (비용 측정·모델별 히스토리 재료).

모든 LLM 호출의 토큰 사용량을 MySQL `usage_log`에 남긴다: 메인 에이전트 턴
(runner.py, component="main" 1행/턴)과 경량 서브콜들(enrichment·thought_translation·
web_grounding·web_prefetch_hint, 콜당 1행). component는 호출부가 넘기는 파라미터일
뿐이고 이 모듈엔 어떤 사례 분기도 없다.

접속 정보는 인증(auth.py)과 같은 단일 출처 — `config.session_db_url`을
session_service.mysql_pool_kwargs로 파싱한다. 세션 DB가 mysql이 아니면(로컬 sqlite
개발) 스택 전체가 자연 비활성(no-op)이다 — 스위치 필드 없는 구조 분기. 테이블은 배포
환경에 사전 생성돼 있다고 전제하며 코드에 DDL이 없다(auth 관례, `scripts/usage_log.sql`).

계약(부가 채널 — auth의 fail-loud 503과 **정반대가 의도된 설계**다):
- 어떤 실패도 예외를 밖으로 던지지 않는다. 인증은 답변의 전제 조건이라 실패가 요청을
  끊어야 정직하지만, 사용량 기록은 관측/과금 재료라 실패가 턴을 막으면 안 된다 —
  warning 로그 후 무시한다(풀 생성 실패 포함).
- INSERT는 fire-and-forget task로 띄워 턴·서브콜 지연에 얹히지 않는다. created_at은
  DB DEFAULT에 위임한다(naive timestamp는 DB 시계끼리만 비교 — auth 관례).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiomysql

from yes24_agent.config import get_settings
from yes24_agent.session_service import mysql_pool_kwargs

logger = logging.getLogger(__name__)


class UsageLogger:
    """usage_log 쓰기 전용 서비스(프로세스 싱글턴).

    풀 팩토리를 생성자로 주입할 수 있다 — 테스트는 실 DB 없이 스텁으로 전 경로를 돈다
    (AuthService와 같은 패턴).
    """

    _instance: UsageLogger | None = None

    def __init__(self, pool_factory=aiomysql.create_pool) -> None:
        self._pool_kwargs = mysql_pool_kwargs(
            get_settings().session_db_url, maxsize=get_settings().usage_pool_max
        )
        self._pool_factory = pool_factory
        self._pool: Any = None
        # 풀 생성 태스크(공유) — fire-and-forget이라 첫 INSERT들이 버스트로 겹치면
        # check-then-act만으로는 task마다 풀을 만들고, close()는 마지막 대입 하나만 닫아
        # 고아 풀의 연결이 GC까지 잔류한다. 잠금이 아니라 태스크 공유인 이유는
        # _get_pool 주석 참조(auth.AuthService와 같은 구조).
        self._pool_task: asyncio.Task | None = None
        # 진행 중인 INSERT task 참조 — GC 취소를 막고, close가 유실 없이 배수(drain)한다.
        self._tasks: set[asyncio.Task] = set()

    @classmethod
    def get_instance(cls) -> UsageLogger:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def enabled(self) -> bool:
        """기록 스택이 성립하는가 — 세션 DB가 mysql일 때만 True(auth.enabled와 같은 판정)."""
        return self._pool_kwargs is not None

    def record(
        self,
        component: str,
        usage: Any,
        *,
        model: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        endpoint: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """사용량 1행 기록을 예약한다(fire-and-forget) — 어떤 실패도 밖으로 던지지 않는다.

        usage는 google-genai `GenerateContentResponseUsageMetadata`(또는 같은 필드명을
        가진 객체)다 — ADK가 Gemini·LiteLLM 양 경로 모두 이 타입으로 정규화하므로
        호출부는 필드 매핑 없이 응답의 usage_metadata를 그대로 넘긴다. usage가 None이면
        기록할 재료가 없어 조용히 지나간다(모델이 usage를 안 실어준 응답 — 실패 아님).
        """
        try:
            if usage is None or not self.enabled:
                return
            row = (
                session_id,
                user_id,
                endpoint,
                component,
                model,
                getattr(usage, "prompt_token_count", None),
                getattr(usage, "candidates_token_count", None),
                getattr(usage, "total_token_count", None),
                latency_ms,
            )
            # 본류(스트리밍·서브콜)와 분리된 task로 쓴다 — DB 왕복이 턴 지연에 얹히지 않는다.
            task = asyncio.get_running_loop().create_task(self._insert(row))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as exc:  # noqa: BLE001 — 부가 채널: 예약 실패(루프 없음 등)도 삼킨다
            logger.warning(f"usage_log 기록 예약 실패(무시): {exc}")

    async def _create_pool(self) -> Any:
        """생성 본체. 성공 대입을 태스크 **안**에서 한다 — 대기자가 전부 취소돼도
        만들어진 풀이 self._pool에 남아 close()가 닫을 수 있다(고아 풀 방지)."""
        self._pool = await self._pool_factory(**self._pool_kwargs)
        return self._pool

    async def _get_pool(self):
        """풀 지연 생성 — 생성 시도를 태스크 하나로 공유한다(성공 뒤 빠른 경로는 무대기).

        잠금 직렬화가 아니라 태스크 공유인 이유: 잠금은 생성 1회 보장은 되지만, DB가
        TCP 블랙홀이면 대기자들이 잠금 **안에서 각자** connect 실패를 순차 대기해
        k번째 실패가 k×connect_timeout 뒤에 난다(실패 지연이 병렬→직렬로 퇴행).
        한 태스크를 함께 await하면 동시 대기자 전원이 한 시도의 성공·실패를 동반
        수신한다. 실패한 태스크는 버려 다음 호출이 새로 시도한다(장애 복구). shield는
        대기자(INSERT task) 하나의 취소가 공유 태스크를 함께 취소해 나머지 대기자를
        실패시키는 전파를 막는다 — 태스크 자체의 마감은 close()가 직접 한다.
        check-then-create 사이에 await가 없어 asyncio 단일 스레드에서 원자적이다.
        """
        if self._pool is not None:
            return self._pool
        task = self._pool_task
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            task = asyncio.get_running_loop().create_task(self._create_pool())
            self._pool_task = task
        return await asyncio.shield(task)

    async def _insert(self, row: tuple) -> None:
        """usage_log INSERT 1건. 풀 생성 실패·테이블 부재·연결 끊김 전부 warning 후 무시."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO usage_log (session_id, user_id, endpoint, component, "
                        "model, prompt_tokens, response_tokens, total_tokens, latency_ms) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        row,
                    )
        except Exception as exc:  # noqa: BLE001 — 부가 채널: 기록 실패가 턴을 막으면 안 된다
            logger.warning(f"usage_log INSERT 실패(무시): {exc}")

    async def close(self) -> None:
        """진행 중인 기록을 상한 안에서 배수하고 풀을 닫는다(앱 종료 훅).

        배수에 상한(usage_close_timeout_s)을 두는 이유: DB 장애 중에는 쌓인 INSERT
        task들이 풀 생성 실패 대기 중이라, 상한 없는 gather는 SIGTERM 후 lifespan
        종료를 통째로 매달아 오케스트레이터 SIGKILL로만 끝난다(배포 지연). 초과분은
        취소한다 — 부가 채널이라 기록 유실이 종료 지연보다 싸다. 진행 중인 풀 생성
        태스크도 여기서 직접 취소한다(_get_pool의 shield가 대기자 취소 전파를 막으므로).
        """
        if self._tasks:
            _, pending = await asyncio.wait(
                list(self._tasks), timeout=get_settings().usage_close_timeout_s
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self._pool_task is not None:
            self._pool_task.cancel()  # 완료된 태스크면 no-op
            await asyncio.gather(self._pool_task, return_exceptions=True)
            self._pool_task = None
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None


def record_usage(
    component: str,
    usage: Any,
    *,
    model: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    endpoint: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """모듈 진입점 — 호출부(runner·서브콜들)는 이 함수 하나만 안다(최소 결합).

    서브콜에는 세션 문맥이 없을 수 있어 session_id·user_id·endpoint·latency_ms 전부
    선택이다(테이블도 NULL 허용).
    """
    UsageLogger.get_instance().record(
        component,
        usage,
        model=model,
        session_id=session_id,
        user_id=user_id,
        endpoint=endpoint,
        latency_ms=latency_ms,
    )


async def close_usage_logger() -> None:
    """앱 종료 훅 — 로거가 실제로 만들어졌을 때만 배수·정리한다(close_auth_service 대칭)."""
    if UsageLogger._instance is not None:
        await UsageLogger._instance.close()
