"""LLM 토큰 사용량(usage_log)·웹 검색 원문(web_search_log) 기록 — 요청 스코프를 공유하는 부가 채널.

모든 LLM 호출의 토큰 사용량을 MySQL `usage_log`에 남긴다: 메인 에이전트 턴
(runner.py, component="main" 1행/턴)과 경량 서브콜들(enrichment·thought_translation·
web_grounding·web_prefetch_hint·web_perplexity, 콜당 1행). component는 호출부가 넘기는 파라미터일
뿐이고 이 모듈엔 어떤 사례 분기도 없다.

접속 정보는 인증(auth.py)과 같은 단일 출처 — `config.session_db_url`을
session_service.mysql_pool_kwargs로 파싱한다. 세션 DB가 mysql이 아니면(로컬 sqlite
개발) 스택 전체가 자연 비활성(no-op)이다 — 스위치 필드 없는 구조 분기. 테이블은 배포
환경에 사전 생성돼 있다고 전제하며 코드에 DDL이 없다(auth 관례, `scripts/usage_log.sql`).

계약(부가 채널 — auth의 fail-loud 503과 **정반대가 의도된 설계**다):
- 어떤 실패도 예외를 밖으로 던지지 않는다. 인증은 답변의 전제 조건이라 실패가 요청을
  끊어야 정직하지만, 사용량 기록은 관측/과금 재료라 실패가 턴을 막으면 안 된다 —
  warning 로그 후 무시한다(풀 생성 실패 포함).
- 부가 호출은 요청의 최종 turn_id가 확정될 때 모아 fire-and-forget으로 쓴다.
  created_at은 호출 당시 UTC를 보존한다. 늦게 끝나는 공유 prefetch도 최초 요청에 귀속한다.

web_search_log(scripts/web_search_log.sql)는 웹 검색 백엔드 요청 1건마다 질의·결과 전체를 남긴다
(수집·재사용 재료). usage_log와 같은 풀·스코프·실패 정책을 타되 테이블을 나눈다 — 어드민 비용·
통계가 매번 훑는 usage_log에 수십 KB JSON을 섞지 않기 위해서다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Any

from yes24_agent.config import get_settings
from yes24_agent.db import LazyAiomysqlPool
from yes24_agent.session_service import mysql_pool_kwargs

logger = logging.getLogger(__name__)


@dataclass
class UsageScope:
    """요청 1건의 귀속 문맥 — turn_id·session_id가 채워진 뒤(finish 이후) 행마다 기록한다.

    pending은 (쓰기 함수, 필드)다 — usage_log·web_search_log가 같은 보류→마감 경로를 탄다.
    """

    user_id: str
    endpoint: str
    session_id: str | None = None
    turn_id: str | None = None
    closed: bool = False
    pending: list[tuple[Callable[..., None], dict]] = field(default_factory=list)

    def record(self, write: Callable[..., None], fields: dict) -> None:
        if not self.closed:
            self.pending.append((write, fields))
            return
        for name in ("session_id", "user_id", "endpoint", "turn_id"):
            if fields.get(name) is None:
                fields[name] = getattr(self, name)
        write(**fields)

    def finish(self, turn_id: str | None) -> None:
        self.turn_id = turn_id
        self.closed = True
        pending, self.pending = self.pending, []
        for write, fields in pending:
            self.record(write, fields)


_usage_scope: ContextVar[UsageScope | None] = ContextVar("usage_scope", default=None)


def _route(write: Callable[..., None], fields: dict) -> None:
    """요청 스코프가 있으면 그 스코프로(턴 id 확정까지 보류), 없으면 바로 쓴다."""
    scope = _usage_scope.get()
    if scope is not None:
        scope.record(write, fields)
    else:
        write(**fields)


def begin_usage_scope(user_id: str, endpoint: str) -> tuple[UsageScope, Token]:
    scope = UsageScope(user_id=user_id, endpoint=endpoint)
    return scope, _usage_scope.set(scope)


def finish_usage_scope(scope: UsageScope, token: Token, turn_id: str | None) -> None:
    scope.finish(turn_id)
    try:
        _usage_scope.reset(token)
    except ValueError:
        # aclose가 다른 task에서 실행되는 경우에도 원래 요청의 문맥을 재사용하지 않는다.
        if _usage_scope.get() is scope:
            _usage_scope.set(None)


# INSERT 열 순서 — record()의 row 튜플·record_web_search의 필드 이름과 1:1이다.
_USAGE_COLUMNS = tuple(
    "app_name session_id user_id endpoint component model prompt_tokens response_tokens "
    "total_tokens thinking_tokens cached_tokens latency_ms turn_id outcome llm_calls tool_calls "
    "cited_sources service_tier reasoning_effort search_queries created_at".split()
)
_WEB_SEARCH_COLUMNS = tuple(
    "app_name created_at session_id user_id endpoint turn_id backend search_type query "
    "domain_filters outcome error latency_ms result_count results response".split()
)


class UsageLogger:
    """usage_log·web_search_log 쓰기 전용 서비스(프로세스 싱글턴, 풀 1개).

    풀 팩토리를 생성자로 주입할 수 있다 — 테스트는 실 DB 없이 스텁으로 전 경로를 돈다
    (AuthService와 같은 패턴).
    """

    _instance: UsageLogger | None = None

    def __init__(self, pool_factory=None) -> None:
        self._pool_kwargs = mysql_pool_kwargs(
            get_settings().session_db_url, maxsize=get_settings().usage_pool_max
        )
        # 풀 생성 기계(지연 생성·태스크 공유·shield)는 db.LazyAiomysqlPool 단일 구현이다 —
        # 이 모듈에 남는 것은 부가 채널 실패 정책(전부 삼킴)뿐이다. mysql이 아니면 풀이 없다.
        self._db = (
            LazyAiomysqlPool(self._pool_kwargs, pool_factory)
            if self._pool_kwargs is not None
            else None
        )
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
        turn_id: str | None = None,
        outcome: str | None = None,
        llm_calls: int | None = None,
        tool_calls: int | None = None,
        cited_sources: int | None = None,
        service_tier: str | None = None,
        reasoning_effort: str | None = None,
        search_queries: int | None = None,
        created_at: datetime | None = None,
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
                get_settings().app_name,
                session_id,
                user_id,
                endpoint,
                component,
                model,
                getattr(usage, "prompt_token_count", None),
                getattr(usage, "candidates_token_count", None),
                getattr(usage, "total_token_count", None),
                # 사고·캐시 토큰은 usage 객체에서 그대로 뽑는다 — 서브콜은 원본
                # usage_metadata를 넘기므로 호출부 수정 없이 자동으로 채워진다. 이 둘이
                # 없으면 금액이 안 나온다: 우리 246행 실측에서 total-prompt-response가
                # 턴당 964토큰이었고(기록된 출력 661보다 크다), 그게 과금되는 사고 토큰이다.
                getattr(usage, "thoughts_token_count", None),
                getattr(usage, "cached_content_token_count", None),
                latency_ms,
                turn_id,
                outcome,
                llm_calls,
                tool_calls,
                cited_sources,
                # LiteLLM 처리 등급 — 비용 단가 조회의 축(admin_cost.price_for). NULL = 표준.
                service_tier,
                # 실제로 실은 사고 강도(LiteLLM만) — 사고 토큰·지연을 강도별로 가르는 축.
                reasoning_effort,
                # 검색 과금 단위 수 — 그라운딩 쿼리(Gemini 3)·퍼플렉시티 요청. usage 객체 밖이라
                # 호출부가 넘긴다. NULL = 검색 과금이 없는 행·열 추가 전 기록.
                search_queries,
                created_at or datetime.now(timezone.utc).replace(tzinfo=None),
            )
            self._schedule("usage_log", _USAGE_COLUMNS, row)
        except Exception as exc:  # noqa: BLE001 — 부가 채널: 예약 실패(루프 없음 등)도 삼킨다
            logger.warning(f"usage_log 기록 예약 실패(무시): {exc}")

    def record_web_search(self, **fields: Any) -> None:
        """web_search_log 1행 예약(fire-and-forget) — JSON 열은 호출부가 이미 직렬화해 넘긴다."""
        try:
            if not self.enabled:
                return
            fields["app_name"] = get_settings().app_name
            self._schedule(
                "web_search_log", _WEB_SEARCH_COLUMNS,
                tuple(fields.get(column) for column in _WEB_SEARCH_COLUMNS),
            )
        except Exception as exc:  # noqa: BLE001 — 부가 채널: 예약 실패(루프 없음 등)도 삼킨다
            logger.warning(f"web_search_log 기록 예약 실패(무시): {exc}")

    def _schedule(self, table: str, columns: tuple[str, ...], row: tuple) -> None:
        """본류(스트리밍·서브콜)와 분리된 task로 쓴다 — DB 왕복이 턴 지연에 얹히지 않는다."""
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join(['%s'] * len(columns))})"
        )
        task = asyncio.get_running_loop().create_task(self._insert(table, sql, row))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _insert(self, table: str, sql: str, row: tuple) -> None:
        """INSERT 1건. 풀 생성 실패·테이블 부재·연결 끊김 전부 warning 후 무시."""

        async def insert(conn):
            async with conn.cursor() as cur:
                await cur.execute(sql, row)

        try:
            await self._db.run(insert)
        except Exception as exc:  # noqa: BLE001 — 부가 채널: 기록 실패가 턴을 막으면 안 된다
            logger.warning(f"{table} INSERT 실패(무시): {exc}")

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
        if self._db is not None:
            await self._db.close()


def record_usage(
    component: str,
    usage: Any,
    *,
    model: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    endpoint: str | None = None,
    latency_ms: int | None = None,
    turn_id: str | None = None,
    outcome: str | None = None,
    llm_calls: int | None = None,
    tool_calls: int | None = None,
    cited_sources: int | None = None,
    service_tier: str | None = None,
    reasoning_effort: str | None = None,
    search_queries: int | None = None,
) -> None:
    """모듈 진입점 — 호출부(runner·서브콜들)는 이 함수 하나만 안다(최소 결합).

    서브콜에는 세션 문맥이 없을 수 있어 session_id·user_id·endpoint·latency_ms 전부
    선택이다(테이블도 NULL 허용).
    """
    if usage is None:
        return
    if not UsageLogger.get_instance().enabled:
        return
    fields = dict(
        model=model,
        session_id=session_id,
        user_id=user_id,
        endpoint=endpoint,
        latency_ms=latency_ms,
        turn_id=turn_id,
        outcome=outcome,
        llm_calls=llm_calls,
        tool_calls=tool_calls,
        cited_sources=cited_sources,
        service_tier=service_tier,
        reasoning_effort=reasoning_effort,
        search_queries=search_queries,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    _route(partial(UsageLogger.get_instance().record, component, usage), fields)


def record_web_search(
    backend: str,
    query: str,
    *,
    outcome: str,
    latency_ms: int,
    results: Any = None,
    response: Any = None,
    error: str | None = None,
    search_type: str | None = None,
    domain_filters: list[str] | None = None,
) -> None:
    """웹 검색 백엔드 요청 1건 → web_search_log 1행(질의·결과 전체, 잘라내지 않는다).

    results·response는 **여기서 즉시** 직렬화한다 — 스코프가 턴 id 확정까지 행을 보류하는 동안
    호출부가 같은 dict를 고쳐도(제목 보강 등) 기록은 호출 시점 값이다. 어떤 실패도 삼킨다.
    """
    try:
        if not get_settings().web_search_log_enabled:
            return
        if not UsageLogger.get_instance().enabled:
            return

        def dump(value: Any) -> str | None:
            return None if value is None else json.dumps(value, ensure_ascii=False, default=str)

        _route(UsageLogger.get_instance().record_web_search, dict(
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            session_id=None, user_id=None, endpoint=None, turn_id=None,
            backend=backend, search_type=search_type, query=query,
            domain_filters=dump(domain_filters or None), outcome=outcome, error=error,
            latency_ms=latency_ms,
            result_count=len(results) if isinstance(results, list) else None,
            results=dump(results), response=dump(response),
        ))
    except Exception as exc:  # noqa: BLE001 — 부가 채널: 직렬화 실패도 검색을 막지 않는다
        logger.warning(f"web_search_log 기록 실패(무시): {exc}")


async def close_usage_logger() -> None:
    """앱 종료 훅 — 로거가 실제로 만들어졌을 때만 배수·정리한다(close_auth_service 대칭)."""
    if UsageLogger._instance is not None:
        await UsageLogger._instance.close()
