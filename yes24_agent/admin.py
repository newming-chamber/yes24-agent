"""운영자용 데이터 조회 페이지 — 세션 DB(MySQL)를 **읽기 전용**으로 들여다본다.

대화의 정본은 턴 테이블 `chat_turn`(2026-09-09 정규화, docs/chat-turn-schema-2026-09-09.md)이고,
운영 뷰 `chat_turn_activity`(scripts/operational_views.sql)가 거기에 턴별 토큰·피드백·클릭을
결합해 준다. 이 모듈은 그 투영을 조회만 하는 얇은 라우터로, 삭제·수정·실행 엔드포인트를 두지
않는다(운영 사고 방지). ADK `events` JSON은 읽지 않는다 — 조사 과정(도구 호출·결과)까지
`chat_turn.process.steps`에 백필 행 포함 전부 들어 있어(라이브 실측) 파싱 계층이 필요 없다.

접속은 세션 DB와 **같은 URL**(session_service.mysql_pool_kwargs — 접속 정보의 단일 출처)에
`SET SESSION TRANSACTION READ ONLY`를 init_command로 얹어 연다. 쓰기 문장은 서버가 1792로
거부하므로(라이브 실측) 읽기 전용이 코드 규율이 아니라 접속의 속성이다. 요청마다 접속을
열고 닫는다 — 운영자 클릭 몇 번이 전부라 풀·종료 훅이 필요 없다.

접근은 `admin_password`로 가린다. 값이 비어 있거나 세션 DB가 MySQL이 아니면 `register_admin`이
라우트를 아예 등록하지 않아 404가 된다(matrix_enabled와 같은 패턴) — 설정하지 않은 환경에
admin이 존재조차 하지 않게 하는 편이, 등록해 두고 인증으로 막는 것보다 노출 표면이 작다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from secrets import compare_digest
from typing import Any

import aiomysql
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from yes24_agent.auth import signed_access_token, token_matches
from yes24_agent.config import Settings, get_settings
from yes24_agent.session_service import mysql_pool_kwargs

logger = logging.getLogger(__name__)

_ADMIN_HTML = Path(__file__).parent / "static" / "admin.html"

# admin 게이트 쿠키. 채팅 로그인월(yes24_access)과 별도 이름·별도 비밀번호라, 데모 접근 권한이
# 곧 운영 데이터 열람 권한이 되지 않는다.
ADMIN_COOKIE = "yes24_admin"
# 토큰 HMAC 메시지(비밀번호가 키). 값 자체는 비밀이 아니며 용도·버전만 구분한다.
_TOKEN_MESSAGE = b"yes24-agent-admin-v1"

# 접속 수준 읽기 전용. 세션 단위라 이 접속으로 실행되는 모든 문장에 걸린다.
_READ_ONLY_COMMAND = "SET SESSION TRANSACTION READ ONLY"
# 뷰가 JSON 텍스트로 돌려주는 열 — 응답에는 구조로 싣는다(DDL scripts/chat_turn.sql의 JSON 열).
_JSON_COLUMNS = ("sources", "process", "meta", "error")


def _expected_token(password: str) -> str:
    """admin 토큰(auth.signed_access_token의 admin message 바인딩)."""
    return signed_access_token(password, _TOKEN_MESSAGE)


def client_ip(request: Request) -> str:
    """요청 출발지 IP — 프록시 뒤에서는 X-Forwarded-For 첫 항목을 쓴다.

    로그인월(main.py)과 admin 로그인이 같이 쓴다. 위조 가능한 헤더라 차단 근거가 아니라
    실패 시도를 사후에 알아볼 **관측 신호**로만 쓴다(차단은 프록시 계층 몫).
    """
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _authorized(request: Request, password: str) -> bool:
    """요청 쿠키가 현재 admin 비밀번호에서 파생된 토큰인지 상수시간 비교로 판정한다."""
    cookie = request.cookies.get(ADMIN_COOKIE)
    return token_matches(cookie, password, _TOKEN_MESSAGE)


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
    return kwargs


# ── 행 정형 ────────────────────────────────────────────────────────────────


def _epoch(value: datetime | None) -> float | None:
    """DB의 naive UTC DATETIME → epoch 초(히스토리 API와 같은 축, 화면이 로컬 시각으로 그린다)."""
    return None if value is None else value.replace(tzinfo=timezone.utc).timestamp()


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    """뷰 행을 응답 dict로 — 시각은 epoch, JSON 열은 구조, 집계(Decimal)는 정수."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            value = _epoch(value)
        elif isinstance(value, Decimal):
            value = int(value)
        elif key in _JSON_COLUMNS and isinstance(value, (str, bytes)):
            value = json.loads(value)
        out[key] = value
    return out


# ── 조회 ───────────────────────────────────────────────────────────────────

_SESSION_TURNS = (
    "t.app_name = s.app_name AND t.user_id = s.user_id AND t.session_id = s.id"
)


async def fetch_overview(cur: aiomysql.DictCursor) -> dict[str, Any]:
    """개요: 세션·턴 수, DB 크기, 최근 활동, 앱별 분포."""
    await cur.execute(
        "SELECT (SELECT COUNT(*) FROM sessions) AS sessions, "
        "(SELECT COUNT(*) FROM chat_turn) AS turns, "
        "(SELECT MAX(update_time) FROM sessions) AS last_activity, "
        "(SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables "
        "WHERE table_schema = DATABASE()) AS db_bytes"
    )
    overview = _jsonable(await cur.fetchone())
    await cur.execute(
        "SELECT app_name, COUNT(*) AS count FROM sessions GROUP BY app_name ORDER BY count DESC"
    )
    overview["apps"] = list(await cur.fetchall())
    return overview


async def fetch_sessions(
    cur: aiomysql.DictCursor, settings: Settings, *, query: str, since: str, until: str, page: int
) -> dict[str, Any]:
    """세션 목록(최근 갱신순 페이지네이션 + 검색·기간 필터).

    본문 검색은 세션 id 매칭과 합집합이다 — 운영자가 세션 id를 붙여넣든 대화에 나온 낱말을
    치든 같은 입력창에서 찾게 한다. 본문은 chat_turn의 질문·답변 TEXT라 한글 원문 LIKE가
    그대로 맞는다(events JSON의 escape 표기 문제가 없다).
    """
    where: list[str] = []
    params: list[Any] = []

    if query:
        where.append(
            "(s.id LIKE %s OR EXISTS (SELECT 1 FROM chat_turn t WHERE "
            f"{_SESSION_TURNS} AND (t.user_text LIKE %s OR t.text LIKE %s)))"
        )
        params.extend([f"%{query}%"] * 3)
    if since:
        where.append("s.update_time >= %s")
        params.append(since)
    if until:
        # until은 날짜(YYYY-MM-DD)라 그날 하루를 통째로 포함해야 한다 — 다음 날 00:00 미만.
        where.append("s.update_time < %s + INTERVAL 1 DAY")
        params.append(until)

    sql_where = f"WHERE {' AND '.join(where)}" if where else ""
    await cur.execute(f"SELECT COUNT(*) AS total FROM sessions s {sql_where}", params)
    total = (await cur.fetchone())["total"]

    size = settings.admin_page_size
    await cur.execute(
        "SELECT s.app_name, s.user_id, s.id, s.create_time, s.update_time, "
        f"(SELECT COUNT(*) FROM chat_turn t WHERE {_SESSION_TURNS}) AS turn_count, "
        # 미리보기 = 첫 턴의 질문 앞부분. 자르기를 SQL에 맡겨 본문 전체를 끌어오지 않는다.
        f"(SELECT LEFT(t.user_text, %s) FROM chat_turn t WHERE {_SESSION_TURNS} "
        "ORDER BY t.started_at, t.id LIMIT 1) AS preview "
        f"FROM sessions s {sql_where} ORDER BY s.update_time DESC LIMIT %s OFFSET %s",
        [settings.admin_preview_max_chars, *params, size, max(page, 0) * size],
    )
    items = [_jsonable(row) for row in await cur.fetchall()]
    return {"total": total, "page": page, "page_size": size, "items": items}


async def fetch_session_detail(cur: aiomysql.DictCursor, session_id: str) -> dict[str, Any] | None:
    """세션 상세: 턴 타임라인(질문·답변·상태·과정·출처·토큰·피드백·클릭) + 간단 지표."""
    await cur.execute(
        "SELECT app_name, user_id, id, create_time, update_time FROM sessions WHERE id = %s",
        (session_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    session = _jsonable(row)

    await cur.execute(
        "SELECT turn_id, asked_at, completed_at, user_message, assistant_message, status, "
        "error, sources, process, meta, rbti_applied, history_saved, elapsed_ms, "
        "attributable_total_tokens, likes, dislikes, clicks FROM chat_turn_activity "
        "WHERE app_name = %s AND user_id = %s AND session_id = %s ORDER BY asked_at, turn_id",
        (session["app_name"], session["user_id"], session_id),
    )
    turns = [_jsonable(turn) for turn in await cur.fetchall()]
    for turn in turns:
        turn["history_saved"] = bool(turn["history_saved"])

    elapsed = [turn["elapsed_ms"] for turn in turns if turn["elapsed_ms"] is not None]
    return {
        "session": session,
        "turns": turns,
        "metrics": {
            "turns": len(turns),
            "avg_turn_seconds": round(sum(elapsed) / len(elapsed) / 1000, 2) if elapsed else None,
        },
    }


def require_admin(request: Request) -> None:
    """운영자 자격 판정자 — admin 쿠키(/admin 로그인) 또는 헤더 `x-admin-key`.

    헤더를 함께 받는 이유: 쿠키 없이 붙는 외부 운영 도구(스크립트)의 자리다. 로그인월
    (main.access_gate)은 이 함수를 **의존성으로 가진 라우트만** x-admin-key로 열어 준다
    (판정 위임, get_authenticated_user와 같은 규칙).
    """
    password = get_settings().admin_password
    header = request.headers.get("x-admin-key", "")
    if password and (
        _authorized(request, password)
        or compare_digest(header.encode("utf-8"), password.encode("utf-8"))
    ):
        return
    raise HTTPException(status_code=401, detail="인증이 필요합니다.")


# ── 라우터 ─────────────────────────────────────────────────────────────────


def register_admin(app: FastAPI, settings: Settings, connect=aiomysql.connect) -> None:
    """admin_password가 설정되고 세션 DB가 MySQL일 때만 admin 라우트를 등록한다(아니면 404).

    connect는 테스트 주입점이다(실 DB 없이 전 경로를 돈다 — AuthService의 pool_factory 패턴).
    """
    connect_kwargs = readonly_connect_kwargs(settings.session_db_url)
    if not settings.admin_password or connect_kwargs is None:
        return

    router = APIRouter(prefix="/admin")

    def _guard(request: Request) -> JSONResponse | None:
        if _authorized(request, settings.admin_password):
            return None
        return JSONResponse({"detail": "인증이 필요합니다."}, status_code=401)

    async def _query(fetch, *args):
        """읽기 전용 접속을 열어 조회 하나를 실행하고 닫는다."""
        conn = await connect(**connect_kwargs)
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                return await fetch(cur, *args)
        finally:
            conn.close()

    @router.get("")
    async def admin_page() -> FileResponse:
        """admin UI 셸(데이터 없음 — 조회는 아래 API가 쿠키를 요구한다)."""
        return FileResponse(_ADMIN_HTML, media_type="text/html")

    @router.post("/api/login")
    async def admin_login(request: Request) -> JSONResponse:
        """admin 비밀번호를 검증해 성공 시 게이트 쿠키를 발급한다."""
        body = await request.json()
        candidate = str(body.get("password", ""))
        if not compare_digest(
            candidate.encode("utf-8"), settings.admin_password.encode("utf-8")
        ):
            logger.warning(f"admin 로그인 실패: ip={client_ip(request)}")
            return JSONResponse({"detail": "비밀번호가 올바르지 않습니다."}, status_code=401)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            ADMIN_COOKIE,
            _expected_token(settings.admin_password),
            max_age=settings.access_cookie_max_age_s,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
            # admin 쿠키를 읽는 곳은 전부 /admin 하위(admin.html의 fetch 3종 + 이 로그인)라
            # path를 좁힌다. 기본 "/"면 채팅·SSE·정적 요청마다 운영자 토큰이 함께 실려 나간다.
            path=router.prefix,
        )
        return resp

    @router.get("/api/overview")
    async def admin_overview(request: Request) -> Any:
        if denied := _guard(request):
            return denied
        return await _query(fetch_overview)

    @router.get("/api/sessions")
    async def admin_sessions(
        request: Request, q: str = "", since: str = "", until: str = "", page: int = 0
    ) -> Any:
        if denied := _guard(request):
            return denied
        return await _query(
            lambda cur, s: fetch_sessions(
                cur, s, query=q.strip(), since=since, until=until, page=page
            ),
            settings,
        )

    @router.get("/api/sessions/{session_id}")
    async def admin_session_detail(request: Request, session_id: str) -> Any:
        if denied := _guard(request):
            return denied
        detail = await _query(fetch_session_detail, session_id)
        if detail is None:
            return JSONResponse({"detail": "세션을 찾을 수 없습니다."}, status_code=404)
        return detail

    app.include_router(router)
