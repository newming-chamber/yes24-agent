"""운영자용 데이터 라우트 — 세션 DB(MySQL)를 조회하고, 회원·키 상태만 바꾼다.

모듈 경계: 누가(계정·세션·역할·감사 트랜잭션)는 `admin_auth`가, 무엇을 본다/바꾼다는 이
모듈이 소유한다. 역방향 import는 없다. 권한은 라우트마다 최소 역할 하나다 — 조회는
`require_admin`(viewer), 회원·키 변경은 `require_editor`
(docs/admin-management-design-20260914.md §4).

조회: 대화의 정본은 턴 테이블 `chat_turn`(2026-09-09 정규화)이고, 운영 뷰 `chat_turn_activity`
(scripts/operational_views.sql)가 거기에 턴별 토큰·피드백·클릭을 결합해 준다. ADK `events`
JSON은 읽지 않는다 — 조사 과정까지 `chat_turn.process.steps`에 들어 있어 파싱 계층이 필요 없다.
조회 접속은 세션 DB와 **같은 URL**(session_service.mysql_pool_kwargs — 접속 정보의 단일 출처)에
`SET SESSION TRANSACTION READ ONLY`를 init_command로 얹어 요청마다 연다. 쓰기 문장은 서버가
1792로 거부하므로 읽기 전용이 코드 규율이 아니라 접속의 속성이다.

변경: 회원 PATCH·키 PATCH는 읽기 전용 접속을 쓰지 않고 `AdminService.transaction`의 쓰기
커넥션에서 편집 프로토콜(`AdminTx.edit_row` — FOR UPDATE·expected 대조)과 감사를 같은
트랜잭션으로 커밋한다. 감사 기록 조회는 전용 API 없이 데이터셋 `audit`이 서빙한다.

세션 DB가 MySQL이 아니면 `register_admin`이 라우트를 아예 등록하지 않아 404가 된다 — 설정하지
않은 환경에 admin이 존재조차 하지 않게 하는 편이 노출 표면이 작다.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

import aiomysql
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import Field, StrictBool, StrictInt

from yes24_agent.admin_auth import (
    AdminActor,
    AdminService,
    EditBody,
    EditChanges,
    require_admin,
    require_editor,
)
from yes24_agent.admin_data import (
    BOOL_DECODERS,
    DATASETS,
    EXACT_FILTER_KEYS,
    dataset_for,
    dataset_query,
    date_range,
    fetch_analytics,
    fetch_dataset,
    jsonable,
    period_filter,
    sql_where,
    stream_csv,
)
from yes24_agent.config import Settings
from yes24_agent.session_service import mysql_pool_kwargs

logger = logging.getLogger(__name__)

_ADMIN_HTML = Path(__file__).parent / "static" / "admin.html"

# 접속 수준 읽기 전용. 세션 단위라 이 접속으로 실행되는 모든 문장에 걸린다.
_READ_ONLY_COMMAND = "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"

# /admin* 응답 헤더. admin.html은 인라인 스크립트·스타일이 없어 'self'만으로 돈다.
_ADMIN_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}

_DB_ERRORS = (aiomysql.Error, OSError, TimeoutError)

# 편집 가능 컬럼(편집 프로토콜의 SELECT … FOR UPDATE 대상이자 감사 before/after의 범위).
USER_EDITABLE = ("is_active", "rate_limit_rpm", "rate_limit_rpd")
KEY_EDITABLE = ("is_active",)


def readonly_connect_kwargs(session_db_url: str) -> dict[str, Any] | None:
    """읽기 전용 접속 kwargs — 세션 DB가 MySQL이 아니면 None(admin 비활성).

    접속 정보 파싱은 mysql_pool_kwargs 한 곳이 소유한다(설정 단일 출처). 풀 전용 인자만
    떼고 UTC init_command 뒤에 읽기 전용 문장을 잇는다(다중 문장 init_command는 라이브 실측).
    """
    kwargs = mysql_pool_kwargs(session_db_url, maxsize=1)
    if kwargs is None:
        return None
    for pool_only in ("minsize", "maxsize"):
        kwargs.pop(pool_only)
    kwargs["init_command"] = f"{kwargs['init_command']}; {_READ_ONLY_COMMAND}"
    kwargs["conv"] = BOOL_DECODERS
    return kwargs


# ── 조회 ───────────────────────────────────────────────────────────────────

_SESSION_TURNS = "t.app_name = s.app_name AND t.user_id = s.user_id AND t.session_id = s.id"
# chat_turn_activity의 JSON 열(DDL scripts/chat_turn.sql) — 세션 상세가 구조로 싣는다.
_TURN_JSON_COLUMNS = ("error", "sources", "process", "meta")


async def fetch_overview(cur: aiomysql.DictCursor) -> dict[str, Any]:
    """개요: 세션·턴·회원 수, DB 크기, 최근 활동, 앱·턴 상태별 분포."""
    await cur.execute(
        "SELECT (SELECT COUNT(*) FROM sessions) AS sessions, "
        "(SELECT COUNT(*) FROM chat_turn) AS turns, "
        "(SELECT COUNT(*) FROM users) AS users, "
        "(SELECT COUNT(*) FROM users WHERE is_active = 1) AS active_users, "
        "(SELECT MAX(update_time) FROM sessions) AS last_activity, "
        "(SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables "
        "WHERE table_schema = DATABASE()) AS db_bytes"
    )
    overview = jsonable(await cur.fetchone())
    await cur.execute(
        "SELECT app_name, COUNT(*) AS count FROM sessions GROUP BY app_name ORDER BY count DESC"
    )
    overview["apps"] = list(await cur.fetchall())
    await cur.execute("SELECT status, COUNT(*) AS count FROM chat_turn GROUP BY status")
    overview["turn_statuses"] = list(await cur.fetchall())
    return overview


async def fetch_sessions(
    cur: aiomysql.DictCursor,
    settings: Settings,
    *,
    query: str,
    since: date | None,
    until: date | None,
    page: int,
) -> dict[str, Any]:
    """세션 목록(최근 갱신순 페이지네이션 + 검색·기간 필터).

    본문 검색은 세션 id 매칭과 합집합이다 — 운영자가 세션 id를 붙여넣든 대화에 나온 낱말을
    치든 같은 입력창에서 찾게 한다. 본문은 chat_turn의 질문·답변 TEXT라 한글 원문 LIKE가
    그대로 맞는다(events JSON의 escape 표기 문제가 없다).
    """
    where, params = period_filter("s.update_time", (since, until))

    if query:
        where.append(
            "(s.id LIKE %s OR EXISTS (SELECT 1 FROM chat_turn t WHERE "
            f"{_SESSION_TURNS} AND (t.user_text LIKE %s OR t.text LIKE %s)))"
        )
        params.extend([f"%{query}%"] * 3)
    where_sql = sql_where(where)
    await cur.execute(f"SELECT COUNT(*) AS total FROM sessions s {where_sql}", params)
    total = (await cur.fetchone())["total"]

    size = settings.admin_page_size
    await cur.execute(
        "SELECT s.app_name, s.user_id, s.id, s.create_time, s.update_time, "
        f"(SELECT COUNT(*) FROM chat_turn t WHERE {_SESSION_TURNS}) AS turn_count, "
        # 미리보기 = 첫 턴의 질문 앞부분. 자르기를 SQL에 맡겨 본문 전체를 끌어오지 않는다.
        f"(SELECT LEFT(t.user_text, %s) FROM chat_turn t WHERE {_SESSION_TURNS} "
        "ORDER BY t.started_at, t.id LIMIT 1) AS preview "
        f"FROM sessions s {where_sql} "
        "ORDER BY s.update_time DESC, s.app_name, s.user_id, s.id LIMIT %s OFFSET %s",
        [settings.admin_preview_max_chars, *params, size, page * size],
    )
    items = [jsonable(row) for row in await cur.fetchall()]
    return {"total": total, "page": page, "page_size": size, "items": items}


async def fetch_session_detail(
    cur: aiomysql.DictCursor,
    session_id: str,
    app_name: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """세션 상세: 턴 타임라인(질문·답변·상태·과정·출처·토큰·피드백·클릭) + 간단 지표."""
    where = ["id = %s"]
    params = [session_id]
    for column, value in (("app_name", app_name), ("user_id", user_id)):
        if value is not None:
            where.append(f"{column} = %s")
            params.append(value)
    await cur.execute(
        "SELECT app_name, user_id, id, create_time, update_time FROM sessions WHERE "
        + " AND ".join(where)
        + " LIMIT 2",
        params,
    )
    rows = await cur.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail="앱과 사용자 식별자를 함께 지정해 주세요.")
    session = jsonable(rows[0])

    await cur.execute(
        "SELECT turn_id, asked_at, completed_at, user_message, assistant_message, status, "
        "error, sources, process, meta, rbti_applied, history_saved, elapsed_ms, "
        "attributable_total_tokens, likes, dislikes, clicks FROM chat_turn_activity "
        "WHERE app_name = %s AND user_id = %s AND session_id = %s ORDER BY asked_at, turn_id",
        (session["app_name"], session["user_id"], session_id),
    )
    turns = [jsonable(turn, _TURN_JSON_COLUMNS) for turn in await cur.fetchall()]

    elapsed = [turn["elapsed_ms"] for turn in turns if turn["elapsed_ms"] is not None]
    return {
        "session": session,
        "turns": turns,
        "metrics": {
            "turns": len(turns),
            "avg_turn_seconds": round(sum(elapsed) / len(elapsed) / 1000, 2) if elapsed else None,
        },
    }


async def fetch_user(cur: aiomysql.DictCursor, user_id: int) -> dict[str, Any] | None:
    """회원 1명 — 행 패널이 편집 대상 필드(expected의 출처)를 읽는다."""
    await cur.execute(
        "SELECT id, user_no, user_login_id, is_active, rate_limit_rpm, rate_limit_rpd, "
        "created_at, updated_at FROM users WHERE id = %s",
        (user_id,),
    )
    row = await cur.fetchone()
    return None if row is None else jsonable(row)


async def fetch_user_detail(cur: aiomysql.DictCursor, user_id: int) -> dict[str, Any] | None:
    """회원 + 키 목록. 키 해시·raw_user_info는 싣지 않는다."""
    user = await fetch_user(cur, user_id)
    if user is None:
        return None
    await cur.execute(
        "SELECT id, kind, is_active, created_at, user_cached_at FROM auth_keys "
        "WHERE user_id = %s ORDER BY id",
        (user_id,),
    )
    return {"user": user, "keys": [jsonable(row) for row in await cur.fetchall()]}


# ── 변경 본문 ──────────────────────────────────────────────────────────────

# users.rate_limit_rpm·rpd는 `INT`(scripts/auth_schema.sql) — 부호 있는 32비트 한계. 범위 밖 값이
# DB 오류(503)가 되기 전에 DB CHECK(ck_users_limits, >= 0)와 함께 422로 먼저 건다.
MYSQL_INT_MAX = 2**31 - 1
_NonNegative = Annotated[StrictInt, Field(ge=0, le=MYSQL_INT_MAX)]


class UserChanges(EditChanges):
    # 타입에 None이 없다 — 생략은 기본값(미검증)이라 통과하고, 명시한 null은 422다(NOT NULL 열).
    is_active: StrictBool = Field(default=None)
    rate_limit_rpm: _NonNegative = Field(default=None)
    rate_limit_rpd: _NonNegative = Field(default=None)


class KeyChanges(EditChanges):
    is_active: StrictBool


class UserEdit(EditBody):
    changes: UserChanges


class KeyEdit(EditBody):
    changes: KeyChanges


# ── 라우터 ─────────────────────────────────────────────────────────────────


class _ExportResponse(StreamingResponse):
    """스트림이 끝나든 시작 전에 끊기든 접속을 닫는다(접속 수명의 단일 소유자)."""

    def __init__(self, content, connection, **kwargs):
        super().__init__(content, **kwargs)
        self.connection = connection

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.connection.close()


def register_admin(app: FastAPI, settings: Settings, connect=aiomysql.connect) -> None:
    """세션 DB가 MySQL일 때만 admin 데이터 라우트를 등록한다(아니면 404).

    등록 조건은 `admin_auth.admin_enabled`와 같은 판정(mysql_pool_kwargs가 None이 아님)이며,
    읽기 전용 접속 kwargs를 만드는 김에 그 결과로 판정한다.
    connect는 테스트 주입점이다(실 DB 없이 전 경로를 돈다 — AuthService의 pool_factory 패턴).
    """
    connect_kwargs = readonly_connect_kwargs(settings.session_db_url)
    if connect_kwargs is None:
        return
    # CSV 내보내기는 조건 전체를 긁을 수 있어 질의 시간 상한을 접속에 건다(SELECT에만 적용).
    export_kwargs = {
        **connect_kwargs,
        "init_command": f"{connect_kwargs['init_command']}; "
        f"SET SESSION MAX_EXECUTION_TIME = {settings.admin_export_max_execution_ms}",
    }

    router = APIRouter(prefix="/admin", include_in_schema=False)

    @app.middleware("http")
    async def admin_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == router.prefix or request.url.path.startswith(router.prefix + "/"):
            response.headers.update(_ADMIN_HEADERS)
        return response

    async def _connect_snapshot(kwargs: dict[str, Any]):
        conn = await connect(**kwargs)
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            return conn
        except BaseException:
            conn.close()
            raise

    async def _query(fetch, *args):
        """한 요청의 모든 SELECT를 동일한 읽기 전용 스냅샷에서 실행한다."""
        try:
            conn = await _connect_snapshot(connect_kwargs)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    return await fetch(cur, *args)
            finally:
                try:
                    await conn.rollback()
                finally:
                    conn.close()
        except _DB_ERRORS as exc:
            logger.warning(f"admin DB 조회 실패: {type(exc).__name__}")
            raise HTTPException(status_code=503, detail="데이터를 불러올 수 없습니다.") from None

    @router.get("")
    async def admin_page() -> FileResponse:
        """admin UI 셸(데이터 없음 — 조회는 아래 API가 세션을 요구한다)."""
        return FileResponse(_ADMIN_HTML, media_type="text/html")

    @router.get("/api/overview", dependencies=[Depends(require_admin)])
    async def admin_overview() -> Any:
        return await _query(fetch_overview)

    @router.get("/api/sessions", dependencies=[Depends(require_admin)])
    async def admin_sessions(
        q: str = "",
        period: tuple[date | None, date | None] = Depends(date_range),
        page: int = Query(default=0, ge=0, le=settings.admin_max_page),
    ) -> Any:
        return await _query(
            lambda cur: fetch_sessions(
                cur, settings, query=q.strip(), since=period[0], until=period[1], page=page
            )
        )

    @router.get("/api/sessions/{session_id}", dependencies=[Depends(require_admin)])
    async def admin_session_detail(
        session_id: str,
        app_name: str | None = None,
        user_id: str | None = None,
    ) -> Any:
        detail = await _query(fetch_session_detail, session_id, app_name, user_id)
        if detail is None:
            return JSONResponse({"detail": "세션을 찾을 수 없습니다."}, status_code=404)
        return detail

    @router.get("/api/users/{user_id}", dependencies=[Depends(require_admin)])
    async def admin_user_detail(user_id: int) -> Any:
        detail = await _query(fetch_user_detail, user_id)
        if detail is None:
            return JSONResponse({"detail": "회원을 찾을 수 없습니다."}, status_code=404)
        return detail

    @router.patch("/api/users/{user_id}")
    async def admin_patch_user(
        user_id: int,
        body: UserEdit,
        request: Request,
        actor: AdminActor = Depends(require_editor),
    ) -> Any:
        """회원 활성·한도 변경. 다음 요청부터 두 서버 모두 반영된다(auth가 요청마다 DB를 읽는다)."""
        async with AdminService.get_instance().transaction(actor, request) as tx:
            before, after = await tx.edit_row(
                "users",
                user_id,
                USER_EDITABLE,
                body.changes.model_dump(exclude_unset=True),
                body.expected,
            )
            if after:
                await tx.audit("user", user_id, "update", before, after)
            user = await fetch_user(tx.cur, user_id)
        return {"id": user_id, "updated": sorted(after), "user": user}

    @router.patch("/api/users/{user_id}/keys/{key_id}")
    async def admin_patch_key(
        user_id: int,
        key_id: int,
        body: KeyEdit,
        request: Request,
        actor: AdminActor = Depends(require_editor),
    ) -> Any:
        """키 활성 변경. 키가 그 회원 것이 아니면 행이 없는 것과 같이 404다."""
        async with AdminService.get_instance().transaction(actor, request) as tx:
            before, after = await tx.edit_row(
                "auth_keys",
                key_id,
                KEY_EDITABLE,
                body.changes.model_dump(exclude_unset=True),
                body.expected,
                scope={"user_id": user_id},
            )
            if after:
                await tx.audit("auth_key", key_id, "update", before, after)
        return {"id": key_id, "updated": sorted(after)}

    @router.get("/api/datasets", dependencies=[Depends(require_admin)])
    async def admin_datasets() -> Any:
        return {"items": [dataset.metadata() for dataset in DATASETS.values()]}

    def _data_selection(
        dataset_id: str,
        request: Request,
        q: str = "",
        sort: str = "",
        direction: Literal["asc", "desc"] = "desc",
        status: str = "",
        status_null: bool = False,
        app_name: str = "",
        period: tuple[date | None, date | None] = Depends(date_range),
    ):
        dataset = dataset_for(dataset_id, sort, status, app_name)
        selection = dataset_query(
            dataset,
            query=q.strip(),
            period=period,
            app_name=app_name,
            status=status,
            status_null=status_null,
            sort=sort,
            direction=direction,
            # 정확 일치 필터 이름은 데이터셋 선언이 정본이다(여기서 다시 열거하지 않는다).
            exact={key: request.query_params.get(key, "") for key in EXACT_FILTER_KEYS},
        )
        return dataset, selection

    @router.get("/api/data/{dataset_id}", dependencies=[Depends(require_admin)])
    async def admin_dataset(
        selected=Depends(_data_selection),
        page: int = Query(default=0, ge=0, le=settings.admin_max_page),
    ) -> Any:
        dataset, selection = selected
        return await _query(
            lambda cur: fetch_dataset(
                cur, dataset, selection, page=page, size=settings.admin_page_size
            )
        )

    @router.get("/api/data/{dataset_id}/export", dependencies=[Depends(require_admin)])
    async def admin_export(
        selected=Depends(_data_selection),
        page: int | None = Query(default=None, ge=0, le=settings.admin_max_page),
    ) -> StreamingResponse:
        """조건 전체(page 없음) 또는 그 페이지만 CSV로 — 조회와 같은 스냅샷 규칙이다."""
        dataset, (_, rows_sql, params) = selected
        size = settings.admin_page_size
        if page is not None:
            rows_sql, params = rows_sql + " LIMIT %s OFFSET %s", [*params, size, page * size]
        connection = None
        try:
            connection = await _connect_snapshot(export_kwargs)
            cur = await connection.cursor(aiomysql.SSDictCursor)
            await cur.execute(rows_sql, params)
            return _ExportResponse(
                stream_csv(cur, dataset, size),
                connection,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{dataset.id}.csv"'},
            )
        except BaseException as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, _DB_ERRORS):
                logger.warning(f"admin CSV 조회 실패: {type(exc).__name__}")
                raise HTTPException(
                    status_code=503, detail="데이터를 불러올 수 없습니다."
                ) from None
            raise

    @router.get("/api/analytics", dependencies=[Depends(require_admin)])
    async def admin_analytics(
        period: tuple[date | None, date | None] = Depends(date_range),
        app_name: str = "",
    ) -> Any:
        return await _query(fetch_analytics, period, app_name)

    app.include_router(router)
