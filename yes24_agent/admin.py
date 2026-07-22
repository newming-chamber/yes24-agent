"""운영자용 데이터 조회 페이지 — ADK 세션 DB를 **읽기 전용**으로 들여다본다.

세션은 ADK `DatabaseSessionService`가 sqlite에 영속한다(sessions·events). 이 모듈은 그
테이블을 조회만 하는 얇은 라우터로, 삭제·수정·실행 엔드포인트를 두지 않는다(운영 사고 방지).
연결도 sqlite URI `mode=ro`로 열어 쓰기가 구조적으로 불가능하게 만든다 — 실행 중인 서버가
같은 파일을 쓰고 있으므로 읽기 전용은 안전장치이자 잠금 회피다.

접근은 `admin_password`로 가린다. 값이 비어 있으면 `register_admin`이 라우트를 아예 등록하지
않아 404가 된다(matrix_enabled와 같은 패턴) — 설정하지 않은 환경에 admin이 존재조차 하지
않게 하는 편이, 등록해 두고 인증으로 막는 것보다 노출 표면이 작다.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from secrets import compare_digest
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from yes24_agent.config import Settings

_ADMIN_HTML = Path(__file__).parent / "static" / "admin.html"

# admin 게이트 쿠키. 채팅 로그인월(yes24_access)과 별도 이름·별도 비밀번호라, 데모 접근 권한이
# 곧 운영 데이터 열람 권한이 되지 않는다.
ADMIN_COOKIE = "yes24_admin"
# 토큰 HMAC 메시지(비밀번호가 키). 값 자체는 비밀이 아니며 용도·버전만 구분한다.
_TOKEN_MESSAGE = b"yes24-agent-admin-v1"


def _expected_token(password: str) -> str:
    """비밀번호에서 결정론적 admin 토큰(HMAC-SHA256 hex)을 만든다(세션 저장소 불필요)."""
    return hmac.new(password.encode("utf-8"), _TOKEN_MESSAGE, sha256).hexdigest()


def _authorized(request: Request, password: str) -> bool:
    """요청 쿠키가 현재 admin 비밀번호에서 파생된 토큰인지 상수시간 비교로 판정한다."""
    cookie = request.cookies.get(ADMIN_COOKIE)
    return bool(cookie) and compare_digest(cookie, _expected_token(password))


def db_path_from_url(session_db_url: str) -> Path:
    """SQLAlchemy sqlite URL에서 파일 경로를 뽑는다.

    `sqlite+aiosqlite:///./data/sessions.db` 형태의 URL이 경로의 단일 소스다 — admin이
    별도 경로 설정을 갖게 하면 서버와 다른 파일을 보는 사고가 가능해진다.
    """
    _, _, tail = session_db_url.partition(":///")
    return Path(tail or session_db_url)


def _connect(path: Path) -> sqlite3.Connection:
    """세션 DB를 읽기 전용으로 연다(쓰기 불가 · 실행 중 서버와 잠금 충돌 없음)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def json_escaped(term: str) -> str:
    """검색어를 events.event_data에 저장된 표기로 바꾼다.

    ADK는 event_data를 `ensure_ascii=True` JSON으로 저장해 한글이 `\\uXXXX` 이스케이프로
    들어간다(실측: 원문 '채식주의자' LIKE는 0건, 이스케이프형은 386건). 원문 그대로 LIKE를
    걸면 한글 본문 검색이 **조용히 0건**을 반환하므로, 저장 표기로 변환해 질의한다.
    """
    return json.dumps(term, ensure_ascii=True)[1:-1]


# ── event_data 렌더 ────────────────────────────────────────────────────────


def _summarize_part(part: dict[str, Any], max_chars: int) -> dict[str, Any] | None:
    """ADK content part 하나를 사람이 읽을 형태로 요약한다(텍스트·도구호출·도구결과)."""
    if (text := part.get("text")) is not None:
        return {"kind": "text", **_clip(str(text), max_chars)}
    if call := part.get("function_call"):
        return {
            "kind": "call",
            "name": call.get("name", ""),
            **_clip(json.dumps(call.get("args", {}), ensure_ascii=False), max_chars),
        }
    if resp := part.get("function_response"):
        return {
            "kind": "result",
            "name": resp.get("name", ""),
            **_clip(json.dumps(resp.get("response", {}), ensure_ascii=False), max_chars),
        }
    return None


def _clip(text: str, max_chars: int) -> dict[str, Any]:
    """긴 본문을 상한까지 자르고 절단 사실을 함께 싣는다(조용한 잘림 방지)."""
    if len(text) <= max_chars:
        return {"text": text, "truncated": False, "total_chars": len(text)}
    return {"text": text[:max_chars], "truncated": True, "total_chars": len(text)}


def render_event(raw: str, max_chars: int) -> dict[str, Any]:
    """저장된 event_data JSON을 타임라인 항목으로 변환한다.

    깨진 JSON은 조용히 건너뛰지 않고 parse_error 항목으로 남긴다 — 운영 조회 화면에서
    "이벤트가 없는 것"과 "읽을 수 없는 것"은 다른 사실이다.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"author": "?", "parse_error": True, "parts": []}

    content = data.get("content") or {}
    parts = [
        summarized
        for part in content.get("parts") or []
        if (summarized := _summarize_part(part, max_chars)) is not None
    ]
    usage = data.get("usage_metadata") or {}
    rendered = {
        "author": data.get("author", "?"),
        "role": content.get("role", ""),
        "timestamp": data.get("timestamp"),
        "invocation_id": data.get("invocation_id", ""),
        "model": data.get("model_version", ""),
        "total_tokens": usage.get("total_token_count"),
        "partial": bool(data.get("partial")),
        "parts": parts,
    }
    return rendered


def _first_user_text(rows: list[sqlite3.Row], max_chars: int) -> str:
    """세션 앞부분 이벤트에서 첫 사용자 발화를 뽑아 목록 미리보기로 쓴다."""
    for row in rows:
        event = render_event(row["event_data"], max_chars)
        if event["author"] == "user":
            for part in event["parts"]:
                if part["kind"] == "text" and part["text"].strip():
                    return part["text"].strip()[:max_chars]
    return ""


def _session_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """타임라인에서 바로 나오는 지표만 계산한다: 턴 지연·도구 사용 분포.

    턴 경계는 invocation_id다(한 사용자 발화가 낳은 이벤트 묶음). 지연은 그 묶음의
    첫↔마지막 이벤트 타임스탬프 차이이며, 이벤트가 하나뿐인 턴은 지연이 0이라 제외한다.
    """
    spans: dict[str, list[float]] = {}
    tools: dict[str, int] = {}
    for event in events:
        if (ts := event.get("timestamp")) is not None and (inv := event["invocation_id"]):
            spans.setdefault(inv, []).append(float(ts))
        for part in event["parts"]:
            if part["kind"] == "call" and part["name"]:
                tools[part["name"]] = tools.get(part["name"], 0) + 1

    durations = [max(ts) - min(ts) for ts in spans.values() if len(ts) > 1]
    return {
        "turns": len(spans),
        "avg_turn_seconds": round(sum(durations) / len(durations), 2) if durations else None,
        "tool_counts": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
    }


# ── 조회 ───────────────────────────────────────────────────────────────────


def fetch_overview(conn: sqlite3.Connection, db_file: Path) -> dict[str, Any]:
    """개요: 세션·이벤트 수, DB 파일 크기, 최근 활동, 앱별 분포."""
    return {
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "db_bytes": db_file.stat().st_size if db_file.exists() else 0,
        "last_activity": conn.execute("SELECT MAX(update_time) FROM sessions").fetchone()[0],
        "apps": [
            dict(row)
            for row in conn.execute(
                "SELECT app_name, COUNT(*) AS count FROM sessions "
                "GROUP BY app_name ORDER BY count DESC"
            )
        ],
    }


def _matching_session_ids(conn: sqlite3.Connection, query: str, limit: int) -> list[str]:
    """본문(event_data)에 검색어가 든 세션 id를 찾는다(저장 표기로 변환해 LIKE)."""
    rows = conn.execute(
        "SELECT DISTINCT session_id FROM events WHERE event_data LIKE ? LIMIT ?",
        (f"%{json_escaped(query)}%", limit),
    )
    return [row["session_id"] for row in rows]


def fetch_sessions(
    conn: sqlite3.Connection, settings: Settings, *, query: str, since: str, until: str, page: int
) -> dict[str, Any]:
    """세션 목록(최근 갱신순 페이지네이션 + 검색·기간 필터).

    본문 검색은 세션 id 매칭과 합집합이다 — 운영자가 세션 id를 붙여넣든 대화에 나온 낱말을
    치든 같은 입력창에서 찾게 한다.
    """
    where: list[str] = []
    params: list[Any] = []

    if query:
        ids = _matching_session_ids(conn, query, settings.admin_search_max_sessions)
        placeholders = ",".join("?" * len(ids))
        # id 부분일치는 항상 함께 본다(본문 히트가 0이어도 id로는 찾을 수 있어야 한다).
        clause = "id LIKE ?"
        params.append(f"%{query}%")
        if ids:
            clause = f"({clause} OR id IN ({placeholders}))"
            params.extend(ids)
        where.append(clause)
    if since:
        where.append("update_time >= ?")
        params.append(since)
    if until:
        # until은 날짜(YYYY-MM-DD)라 그날 하루를 통째로 포함해야 한다 — 다음 날 00:00 미만으로
        # 잡는다(update_time은 'YYYY-MM-DD HH:MM:SS.ffffff' 문자열이라 사전순 비교가 곧 시간순).
        where.append("update_time < datetime(?, '+1 day')")
        params.append(until)

    sql_where = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM sessions {sql_where}", params).fetchone()[0]

    size = settings.admin_page_size
    rows = conn.execute(
        f"SELECT app_name, user_id, id, create_time, update_time FROM sessions {sql_where} "
        "ORDER BY update_time DESC LIMIT ? OFFSET ?",
        [*params, size, max(page, 0) * size],
    ).fetchall()

    items = []
    for row in rows:
        key = (row["app_name"], row["user_id"], row["id"])
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE app_name=? AND user_id=? AND session_id=?", key
        ).fetchone()[0]
        # 미리보기는 앞쪽 몇 건만 읽는다 — 세션 전체 이벤트(건당 평균 13KB)를 목록마다
        # 끌어오면 페이지 한 장에 수십 MB가 된다.
        head = conn.execute(
            "SELECT event_data FROM events WHERE app_name=? AND user_id=? AND session_id=? "
            "ORDER BY timestamp LIMIT ?",
            (*key, settings.admin_preview_scan_events),
        ).fetchall()
        items.append(
            {
                **dict(row),
                "event_count": count,
                "preview": _first_user_text(head, settings.admin_preview_max_chars),
            }
        )

    return {"total": total, "page": page, "page_size": size, "items": items}


def fetch_session_detail(
    conn: sqlite3.Connection, settings: Settings, session_id: str
) -> dict[str, Any] | None:
    """세션 상세: 대화 타임라인 + 누적 인용 출처 + 간단 지표."""
    row = conn.execute(
        "SELECT app_name, user_id, id, state, create_time, update_time FROM sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None

    raw_events = conn.execute(
        "SELECT event_data FROM events WHERE app_name=? AND user_id=? AND session_id=? "
        "ORDER BY timestamp LIMIT ?",
        (row["app_name"], row["user_id"], session_id, settings.admin_session_max_events),
    ).fetchall()
    events = [render_event(r["event_data"], settings.admin_part_max_chars) for r in raw_events]

    try:
        state = json.loads(row["state"] or "{}")
    except json.JSONDecodeError:
        state = {}

    return {
        "session": {k: row[k] for k in ("app_name", "user_id", "id", "create_time", "update_time")},
        "events": events,
        "truncated": len(events) >= settings.admin_session_max_events,
        "sources": state.get("sources", []),
        "metrics": _session_metrics(events),
    }


# ── 라우터 ─────────────────────────────────────────────────────────────────


def register_admin(app: FastAPI, settings: Settings) -> None:
    """admin_password가 설정된 경우에만 admin 라우트를 등록한다(미설정이면 404)."""
    if not settings.admin_password:
        return

    router = APIRouter(prefix="/admin")
    db_file = db_path_from_url(settings.session_db_url)

    def _guard(request: Request) -> JSONResponse | None:
        if _authorized(request, settings.admin_password):
            return None
        return JSONResponse({"detail": "인증이 필요합니다."}, status_code=401)

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
            return JSONResponse({"detail": "비밀번호가 올바르지 않습니다."}, status_code=401)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            ADMIN_COOKIE,
            _expected_token(settings.admin_password),
            max_age=settings.access_cookie_max_age_s,
            httponly=True,
            samesite="lax",
        )
        return resp

    # 아래 조회 3종은 동기 sqlite를 쓴다. `async def`로 두면 LIKE 풀스캔·세션별 추가 쿼리가
    # 이벤트 루프를 잡아 admin 조회 중 모든 채팅 SSE가 정지한다(2026-07-21 감사). 동기 `def`로
    # 선언해 Starlette 스레드풀에서 돌린다.
    @router.get("/api/overview")
    def admin_overview(request: Request) -> Any:
        if denied := _guard(request):
            return denied
        with closing(_connect(db_file)) as conn:
            return fetch_overview(conn, db_file)

    @router.get("/api/sessions")
    def admin_sessions(
        request: Request, q: str = "", since: str = "", until: str = "", page: int = 0
    ) -> Any:
        if denied := _guard(request):
            return denied
        with closing(_connect(db_file)) as conn:
            return fetch_sessions(
                conn, settings, query=q.strip(), since=since, until=until, page=page
            )

    @router.get("/api/sessions/{session_id}")
    def admin_session_detail(request: Request, session_id: str) -> Any:
        if denied := _guard(request):
            return denied
        with closing(_connect(db_file)) as conn:
            detail = fetch_session_detail(conn, settings, session_id)
        if detail is None:
            return JSONResponse({"detail": "세션을 찾을 수 없습니다."}, status_code=404)
        return detail

    app.include_router(router)
