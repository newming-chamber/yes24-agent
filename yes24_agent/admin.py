"""운영자용 데이터 라우트 — 세션 DB(MySQL)를 조회하고, 회원 이용 가능 여부만 바꾼다.

모듈 경계: 누가(계정·세션·역할·감사 트랜잭션)는 `admin_auth`가, 무엇을 본다/바꾼다는 이
모듈이 소유한다. 역방향 import는 없다. 권한은 라우트마다 최소 역할 하나다 — 조회는
`require_admin`(viewer), 회원 차단 토글은 `require_editor`, 비용은 `require_owner`
(docs/admin-management-design-20260914.md §4).

내부 정보(조사 과정·오류 내부·토큰·비용)는 화면이 아니라 **응답에서** 뺀다 — 개발자 도구로
보이면 화면에서 숨긴 의미가 없다. 비용만 owner 전용 라우트 하나로 나간다.

조회: 대화의 정본은 턴 테이블 `chat_turn`(2026-09-09 정규화)이고 뷰 `chat_turns`로 읽는다.
세션 상세의 턴별 피드백·클릭은 그 세션 턴에 한정한 서브쿼리로 붙인다 — 토큰까지 결합한 운영 뷰
`chat_turn_activity`는 usage_log 파생 테이블을 매번 전체 스캔해 쓰지 않는다. ADK `events` JSON은
읽지 않는다.
조회 접속은 세션 DB와 **같은 URL**(session_service.mysql_pool_kwargs — 접속 정보의 단일 출처)에
`SET SESSION TRANSACTION READ ONLY`를 init_command로 얹어 요청마다 연다. 쓰기 문장은 서버가
1792로 거부하므로 읽기 전용이 코드 규율이 아니라 접속의 속성이다.

변경: 회원 PATCH는 읽기 전용 접속을 쓰지 않고 `AdminService.transaction`의 쓰기
커넥션에서 편집 프로토콜(`AdminTx.edit_row` — FOR UPDATE·expected 대조)과 감사를 같은
트랜잭션으로 커밋한다.

세션 DB가 MySQL이 아니면 `register_admin`이 라우트를 아예 등록하지 않아 404가 된다 — 설정하지
않은 환경에 admin이 존재조차 하지 않게 하는 편이 노출 표면이 작다.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import aiomysql
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, StrictBool

from yes24_agent.admin_auth import (
    AdminActor,
    AdminService,
    EditBody,
    EditChanges,
    require_admin,
    require_editor,
    require_owner,
)
from yes24_agent.admin_data import (
    ACTIVE_USERS,
    BOOL_DECODERS,
    REFUSED,
    date_range,
    fetch_analytics,
    fetch_cost,
    fetch_stats,
    jsonable,
    nickname_by_user_pk_sql,
    nickname_sql,
    period_filter,
    refusal_breakdown,
    sql_where,
    with_nickname,
)
from yes24_agent.config import Settings
from yes24_agent.session_service import mysql_pool_kwargs
from yes24_agent.starters import chip_label

logger = logging.getLogger(__name__)

_ADMIN_HTML = Path(__file__).parent / "static" / "admin.html"

# 접속 수준 읽기 전용. 세션 단위라 이 접속으로 실행되는 모든 문장에 걸린다.
_READ_ONLY_COMMAND = "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"


def admin_headers(settings: Settings) -> dict[str, str]:
    """/admin* 응답 헤더. admin.html은 인라인 스크립트·스타일이 없어 'self'만으로 돈다.

    이미지만 설정한 출처(admin_image_origins — 대화 상세의 도서 표지)를 더 연다.
    """
    img_src = " ".join(("'self'", *settings.admin_image_origins))
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": f"default-src 'self'; img-src {img_src}; frame-ancestors 'none'",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
    }


_DB_ERRORS = (aiomysql.Error, OSError, TimeoutError)

# 편집 가능 컬럼(편집 프로토콜의 SELECT … FOR UPDATE 대상이자 감사 before/after의 범위).
USER_EDITABLE = ("is_active",)


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

# 세션 상세 턴 JSON 열(DDL scripts/chat_turn.sql) — 구조로 싣는다.
_TURN_JSON_COLUMNS = ("sources",)
# 턴 하나에 붙는 피드백·클릭 수 — 그 턴의 소유자 스코프 키(앱·사용자·세션·턴)로만 센다.
_TURN_SCOPE = "x.app_name = t.app_name AND x.user_id = t.user_id AND x.session_id = t.session_id "
_TURN_SCOPE += "AND x.turn_id = t.turn_id"
_DETAIL_TURNS_SQL = (
    "SELECT t.turn_id, t.asked_at, t.completed_at, t.user_message, t.assistant_message, "
    "t.status, t.sources, t.rbti_applied, t.elapsed_ms, "
    f"(SELECT COUNT(*) FROM turn_feedback x WHERE {_TURN_SCOPE} AND x.rating = 'up') AS likes, "
    f"(SELECT COUNT(*) FROM turn_feedback x WHERE {_TURN_SCOPE} AND x.rating = 'down') "
    "AS dislikes, "
    f"(SELECT COUNT(*) FROM turn_click x WHERE {_TURN_SCOPE}) AS clicks "
    "FROM chat_turns t WHERE t.app_name = %s AND t.user_id = %s AND t.session_id = %s "
    "ORDER BY t.asked_at, t.turn_id"
)


async def fetch_overview(cur: aiomysql.DictCursor) -> dict[str, Any]:
    """개요(누적): 대화한 사용자·세션(대화방)·질의 수.

    세션 = 턴이 1개 이상인 대화방(chat_turn 기준) — 통계 탭 '세션수'와 같은 정의다. sessions 표는
    질문 없이 열리기만 한 빈 세션도 담아 그 행 수를 쓰면 통계와 어긋난다.

    chat_users = 실제로 대화한 사용자(전체 기간) — 통계 탭 '활성사용자수'와 같은 정의다.
    users 행 수는 앱을 연 회원 전부라 운영 지표로 뜻이 없어 싣지 않는다.
    """
    await cur.execute(
        f"SELECT (SELECT {ACTIVE_USERS} FROM chat_turn) AS chat_users, "
        "(SELECT COUNT(DISTINCT app_name, user_id, session_id) FROM chat_turn) AS sessions, "
        "(SELECT COUNT(*) FROM chat_turn) AS turns"
    )
    return jsonable(await cur.fetchone())


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

    본문 검색은 세션 id 부분 일치·회원번호 일치와 합집합이다 — 운영자가 세션 id·회원번호를
    붙여넣든 대화에 나온 낱말을 치든 같은 입력창에서 찾게 한다(회원 화면의 '대화 보기'가
    회원번호로 건다). 본문은 chat_turn의 질문·답변 TEXT라 한글 원문 LIKE가
    그대로 맞는다(events JSON의 escape 표기 문제가 없다).
    """
    where, params = period_filter("s.update_time", (since, until), settings.admin_utc_offset_hours)

    if query:
        where.append(
            "(s.id LIKE %s OR s.user_id = %s OR EXISTS (SELECT 1 FROM chat_turn t WHERE "
            f"{_SESSION_TURNS} AND (t.user_text LIKE %s OR t.text LIKE %s)))"
        )
        params.extend([f"%{query}%", query, f"%{query}%", f"%{query}%"])
    where_sql = sql_where(where)
    await cur.execute(f"SELECT COUNT(*) AS total FROM sessions s {where_sql}", params)
    total = (await cur.fetchone())["total"]

    size = settings.admin_page_size
    # 목록 배지(실패·중단 n) — 상태별 수. 같은 문장의 상관 서브쿼리라 행마다 왕복하지 않는다.
    refused = "".join(
        f"(SELECT COUNT(*) FROM chat_turn t WHERE {_SESSION_TURNS} "
        f"AND t.status = '{status}') AS {status}, "
        for status in REFUSED
    )
    await cur.execute(
        f"SELECT s.user_id, {nickname_sql('s.user_id')} AS nickname, "
        "s.id, s.create_time, s.update_time, "
        f"(SELECT COUNT(*) FROM chat_turn t WHERE {_SESSION_TURNS}) AS turn_count, {refused}"
        # 미리보기 = 첫 턴의 질문 앞부분. 자르기를 SQL에 맡겨 본문 전체를 끌어오지 않는다.
        f"(SELECT LEFT(t.user_text, %s) FROM chat_turn t WHERE {_SESSION_TURNS} "
        "ORDER BY t.started_at, t.id LIMIT 1) AS preview "
        f"FROM sessions s {where_sql} "
        "ORDER BY s.update_time DESC, s.app_name, s.user_id, s.id LIMIT %s OFFSET %s",
        [settings.admin_preview_max_chars, *params, size, page * size],
    )
    items = [
        {**with_nickname(jsonable(row)), **refusal_breakdown([row])} for row in await cur.fetchall()
    ]
    return {"total": total, "page": page, "page_size": size, "items": items}


async def fetch_session_detail(
    cur: aiomysql.DictCursor,
    session_id: str,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """세션 상세: 턴 타임라인(질문·답변·상태·출처·피드백·클릭) + 간단 지표.

    앱은 값이 하나라 조회·응답에서 뺐다. 세션 식별 키(app_name, user_id, id)는 조인 계약이라 SQL이
    내부에서 그대로 쓴다.
    """
    where = ["s.id = %s"]
    params = [session_id]
    if user_id is not None:
        where.append("s.user_id = %s")
        params.append(user_id)
    await cur.execute(
        # 서브쿼리 안의 auth_keys에도 user_id 열이 있어 세션 열은 별칭으로 못 박는다.
        f"SELECT s.app_name, s.user_id, {nickname_sql('s.user_id')} AS nickname, s.id, "
        "s.create_time, s.update_time FROM sessions s WHERE " + " AND ".join(where) + " LIMIT 2",
        params,
    )
    rows = await cur.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail="사용자 식별자를 함께 지정해 주세요.")
    session = with_nickname(jsonable(rows[0]))
    app_name = session.pop("app_name")

    await cur.execute(_DETAIL_TURNS_SQL, (app_name, session["user_id"], session_id))
    turns = [jsonable(turn, _TURN_JSON_COLUMNS) for turn in await cur.fetchall()]

    elapsed = [turn["elapsed_ms"] for turn in turns if turn["elapsed_ms"] is not None]
    return {
        "session": session,
        "turns": turns,
        "metrics": {
            "turns": len(turns),
            "avg_turn_seconds": round(sum(elapsed) / len(elapsed) / 1000, 2) if elapsed else None,
            # 대화 지속시간 = 첫 질문부터 마지막 답변 완료까지(턴 시각은 epoch 초).
            "duration_seconds": round(
                max(turn["completed_at"] for turn in turns)
                - min(turn["asked_at"] for turn in turns),
                2,
            )
            if turns
            else None,
        },
    }


async def _fetch_page(
    cur: aiomysql.DictCursor,
    settings: Settings,
    *,
    columns: str,
    source: str,
    where: list[str],
    params: list[Any],
    order: str,
    page: int,
) -> dict[str, Any]:
    """목록 한 페이지와 전체 건수 — 대화 목록과 같은 응답 모양(total·page·page_size·items)."""
    where_sql = sql_where(where)
    await cur.execute(f"SELECT COUNT(*) AS total FROM {source}{where_sql}", params)
    total = (await cur.fetchone())["total"]
    size = settings.admin_page_size
    await cur.execute(
        f"SELECT {columns} FROM {source}{where_sql} ORDER BY {order} LIMIT %s OFFSET %s",
        [*params, size, page * size],
    )
    items = [jsonable(row) for row in await cur.fetchall()]
    return {"total": total, "page": page, "page_size": size, "items": items}


# 대화한 회원 = chat_turn에 턴이 있는 회원 — 대시보드·통계 '활성 사용자'(admin_data.ACTIVE_USERS)와
# 같은 소스다. idx_chat_turn_session_time 선두 (app_name, user_id)로 인덱스만 훑어 묶고(앱은 하나라
# 회원당 한 행), users(uq_users_user_no)에 붙인다. 전 회원 뷰(user_activity)는 쓰지 않는다(운영 28k
# 회원에서 2.5~4.7s).
_CHATTED = (
    "(SELECT user_id, COUNT(*) AS turns, MAX(started_at) AS last_chat_at "
    "FROM chat_turn GROUP BY app_name, user_id) t"
)


def _user_nickname(alias: str) -> str:
    """회원 닉네임 — 회원 행 id로 auth_keys에 바로 붙는다(users 재조회 없음). 모르면 회원번호."""
    return f"COALESCE({nickname_by_user_pk_sql(f'{alias}.id')}, {alias}.user_no)"


async def fetch_users(
    cur: aiomysql.DictCursor, settings: Settings, *, query: str, page: int
) -> dict[str, Any]:
    """한 번이라도 대화한 회원(마지막 질의 최근순).

    검색: 회원번호 정확 일치는 전체 회원에서 찾는다(대화 전인 회원도 차단할 수 있게, 유일 인덱스).
    닉네임 부분 일치는 대화한 회원 안에서만 — 전 회원 닉네임 LIKE는 JSON 서브쿼리가 행마다 돈다.
    로그인 아이디는 싣지도 찾지도 않는다.
    """
    if query:
        # 두 갈래를 UNION으로 따로 찾는다 — 한 WHERE의 OR로 묶으면 전 회원에 닉네임 식이
        # 돈다(운영 2.8s → 0.03s).
        hits = (
            "(SELECT id FROM users WHERE user_no = %s UNION "
            f"SELECT m.id FROM {_CHATTED} JOIN users m ON m.user_no = t.user_id "
            f"WHERE {_user_nickname('m')} LIKE %s) hit"
        )
        source = (
            f"{hits} JOIN users u ON u.id = hit.id LEFT JOIN {_CHATTED} ON t.user_id = u.user_no"
        )
        params: list[Any] = [query, f"%{query}%"]
    else:
        source, params = f"{_CHATTED} JOIN users u ON u.user_no = t.user_id", []
    return await _fetch_page(
        cur,
        settings,
        # 표에는 닉네임 원값(없으면 null — 회원번호 칸이 따로 있다). 검색만 회원번호 폴백을 쓴다.
        columns=f"u.id, u.user_no, {nickname_by_user_pk_sql('u.id')} AS nickname, u.is_active, "
        "t.turns, t.last_chat_at, u.created_at",
        source=source,
        where=[],
        params=params,
        order="t.last_chat_at DESC, u.id DESC",
        page=page,
    )


async def fetch_starters(
    cur: aiomysql.DictCursor, settings: Settings, *, page: int
) -> dict[str, Any]:
    """초기 질문 풀(생성 최근순) — 목록과 편집 패널이 쓰는 열만. 쓰기는 starters.py 라우트다."""
    page_data = await _fetch_page(
        cur,
        settings,
        columns="id, slot, label, text, source, goods_no, source_url, run_date, pinned, active, "
        "valid_from, valid_until, created_at",
        source="starters",
        where=[],
        params=[],
        order="active DESC, created_at DESC, id DESC",  # 지금 노출 중인 풀이 먼저
        page=page,
    )
    # 슬롯 키 대신 칩에 보이는 라벨 — 서빙과 같은 규칙(starters.chip_label)을 서버가 채운다.
    for item in page_data["items"]:
        item["label"] = chip_label(item, settings)
    return page_data


async def fetch_user(cur: aiomysql.DictCursor, user_id: int) -> dict[str, Any] | None:
    """회원 1명 — 편집 뒤 응답에 싣는다(편집 대상 필드와 식별자만)."""
    await cur.execute(
        "SELECT id, user_no, is_active FROM users WHERE id = %s",
        (user_id,),
    )
    row = await cur.fetchone()
    return None if row is None else jsonable(row)


# ── 변경 본문 ──────────────────────────────────────────────────────────────


class UserChanges(EditChanges):
    # 타입에 None이 없다 — 생략은 기본값(미검증)이라 통과하고, 명시한 null은 422다(NOT NULL 열).
    is_active: StrictBool = Field(default=None)


class UserEdit(EditBody):
    changes: UserChanges


# ── 라우터 ─────────────────────────────────────────────────────────────────


def register_admin(app: FastAPI, settings: Settings, connect=aiomysql.connect) -> None:
    """세션 DB가 MySQL일 때만 admin 데이터 라우트를 등록한다(아니면 404).

    등록 조건은 `admin_auth.admin_enabled`와 같은 판정(mysql_pool_kwargs가 None이 아님)이며,
    읽기 전용 접속 kwargs를 만드는 김에 그 결과로 판정한다.
    connect는 테스트 주입점이다(실 DB 없이 전 경로를 돈다 — AuthService의 pool_factory 패턴).
    """
    connect_kwargs = readonly_connect_kwargs(settings.session_db_url)
    if connect_kwargs is None:
        return
    router = APIRouter(prefix="/admin", include_in_schema=False)
    headers = admin_headers(settings)

    @app.middleware("http")
    async def add_admin_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == router.prefix or request.url.path.startswith(router.prefix + "/"):
            response.headers.update(headers)
        return response

    async def _query(fetch, *args):
        """한 요청의 모든 SELECT를 동일한 읽기 전용 스냅샷에서 실행한다."""
        try:
            conn = await connect(**connect_kwargs)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                    return await fetch(cur, *args)
            finally:
                try:
                    await conn.rollback()
                finally:
                    conn.close()
        except _DB_ERRORS as exc:
            logger.warning(f"admin DB 조회 실패: {type(exc).__name__}")
            raise HTTPException(status_code=503, detail="데이터를 불러올 수 없습니다.") from None

    # page 상한 — 거대값이 OFFSET 전체 스캔(503·DB 장애 로그)이 아니라 422로 떨어진다.
    page_query = Query(default=0, ge=0, le=settings.admin_max_page)

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
        page: int = page_query,
    ) -> Any:
        return await _query(
            lambda cur: fetch_sessions(
                cur, settings, query=q.strip(), since=period[0], until=period[1], page=page
            )
        )

    @router.get("/api/sessions/{session_id}", dependencies=[Depends(require_admin)])
    async def admin_session_detail(
        session_id: str,
        user_id: str | None = None,
    ) -> Any:
        detail = await _query(fetch_session_detail, session_id, user_id)
        if detail is None:
            return JSONResponse({"detail": "세션을 찾을 수 없습니다."}, status_code=404)
        return detail

    @router.get("/api/users", dependencies=[Depends(require_admin)])
    async def admin_users(q: str = "", page: int = page_query) -> Any:
        return await _query(lambda cur: fetch_users(cur, settings, query=q.strip(), page=page))

    @router.get("/api/starters", dependencies=[Depends(require_admin)])
    async def admin_starters(page: int = page_query) -> Any:
        return await _query(lambda cur: fetch_starters(cur, settings, page=page))

    @router.patch("/api/users/{user_id}")
    async def admin_patch_user(
        user_id: int,
        body: UserEdit,
        request: Request,
        actor: AdminActor = Depends(require_editor),
    ) -> Any:
        """회원 이용 가능(차단) 토글 — 다음 요청부터 반영된다(auth가 요청마다 DB를 읽는다)."""
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

    @router.get("/api/analytics", dependencies=[Depends(require_admin)])
    async def admin_analytics(
        period: tuple[date | None, date | None] = Depends(date_range),
    ) -> Any:
        return await _query(fetch_analytics, settings, period)

    @router.get("/api/analytics/cost", dependencies=[Depends(require_owner)])
    async def admin_cost(period: tuple[date | None, date | None] = Depends(date_range)) -> Any:
        """비용 패널(owner 전용). 대시보드 응답에는 비용 필드가 없다."""
        return await _query(fetch_cost, settings, period)

    @router.get("/api/stats", dependencies=[Depends(require_admin)])
    async def admin_stats(since: date, until: date) -> Any:
        """통계 탭(KST 일별 보고). 빈 날도 채우므로 기간이 필수이고 길이에 상한이 있다."""
        date_range(since, until)
        if (until - since).days >= settings.admin_stats_max_days:
            raise HTTPException(
                status_code=422,
                detail=f"통계 기간은 최대 {settings.admin_stats_max_days}일입니다.",
            )
        return await _query(fetch_stats, settings, (since, until))

    app.include_router(router)
