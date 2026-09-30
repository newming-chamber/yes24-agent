"""운영자용 데이터 라우트 — 세션 DB(MySQL)를 읽기 전용으로 조회한다.

모듈 경계: 누가(계정·세션·역할·감사 트랜잭션)는 `admin_auth`가, 무엇을 본다/바꾼다는 이
모듈이 소유한다. 역방향 import는 없다. 권한은 라우트마다 최소 역할 하나다 — 조회는
`require_admin`(viewer), 비용·감사 기록은 `require_owner`(docs/admin-management-design-20260914.md
§4). 쓰기 라우트(계정·초기 질문)는 `admin_auth`·`starters`가 소유한다.

내부 정보(조사 과정·오류 내부·토큰·비용)는 화면이 아니라 **응답에서** 뺀다 — 개발자 도구로
보이면 화면에서 숨긴 의미가 없다. 비용만 owner 전용 라우트 하나로 나간다.

조회: 대화의 정본은 턴 테이블 `chat_turn`(2026-09-09 정규화)이고 뷰 `chat_turns`로 읽는다.
세션 상세의 턴별 피드백·클릭은 그 세션 턴에 한정한 서브쿼리로 붙인다 — 토큰까지 결합한 운영 뷰
`chat_turn_activity`는 usage_log 파생 테이블을 매번 전체 스캔해 쓰지 않는다. ADK `events` JSON은
읽지 않는다.
조회 접속은 세션 DB와 **같은 URL**(session_service.mysql_pool_kwargs — 접속 정보의 단일 출처)에
`SET SESSION TRANSACTION READ ONLY`를 init_command로 얹어 요청마다 연다. 쓰기 문장은 서버가
1792로 거부하므로 읽기 전용이 코드 규율이 아니라 접속의 속성이다.

세션 DB가 MySQL이 아니면 `register_admin`이 라우트를 아예 등록하지 않아 404가 된다 — 설정하지
않은 환경에 admin이 존재조차 하지 않게 하는 편이 노출 표면이 작다.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Literal

import aiomysql
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from yes24_agent.admin_auth import (
    require_admin,
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
from yes24_agent.starters import TARGET_SEP, chip_label

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
# 세션의 피드백 — uq_turn_feedback_user_turn 앞부분(앱·사용자·세션)으로 찾는다.
_SESSION_FEEDBACK = "f.app_name = s.app_name AND f.user_id = s.user_id AND f.session_id = s.id"
# 세션 RBTI = 그 세션에서 가장 최근에 적용된 유형(적용 안 된 턴은 건너뛴다) — 목록 배지·필터가
# 같은 식.
_SESSION_RBTI = (
    f"(SELECT t.rbti_applied FROM chat_turn t WHERE {_SESSION_TURNS} "
    "AND t.rbti_applied IS NOT NULL ORDER BY t.started_at DESC, t.id DESC LIMIT 1)"
)
# RBTI 필터 값 — ""(전체)·any(있음)·none(없음)·유형 코드(4글자 대문자, 데이터에 나온 코드를 화면이
# 목록으로 받는다). 라우트가 이 패턴으로 검증하고 코드는 바인딩한다.
RBTI_FILTER = r"^(|any|none|[A-Z]{4})$"


def _rbti_condition(expr: str, rbti: str) -> tuple[str, list[Any]] | None:
    """RBTI 필터 → (조건, 파라미터). expr는 대상의 RBTI 식(회원 t.rbti·세션 _SESSION_RBTI)."""
    if not rbti:
        return None
    if rbti == "any":
        return f"{expr} IS NOT NULL", []
    if rbti == "none":
        return f"{expr} IS NULL", []
    return f"{expr} = %s", [rbti]


async def _rbti_types(cur: aiomysql.DictCursor) -> list[str]:
    """필터 선택지 — 데이터에 나온 RBTI 코드만(코드 목록을 코드에 적지 않는다)."""
    await cur.execute(
        "SELECT DISTINCT rbti_applied AS rbti FROM chat_turn "
        "WHERE rbti_applied IS NOT NULL ORDER BY rbti_applied"
    )
    return [row["rbti"] for row in await cur.fetchall()]


# 피드백 평가 값(DDL ck_turn_feedback_rating) → 목록 열 이름.
FEEDBACK_RATINGS = {"up": "likes", "down": "dislikes"}

# 세션 상세 턴 JSON 열(DDL scripts/chat_turn.sql) — 구조로 싣는다.
_TURN_JSON_COLUMNS = ("sources",)
# 턴 하나에 붙는 클릭 수 — 그 턴의 소유자 스코프 키(앱·사용자·세션·턴)로만 센다.
_TURN_SCOPE = "x.app_name = t.app_name AND x.user_id = t.user_id AND x.session_id = t.session_id "
_TURN_SCOPE += "AND x.turn_id = t.turn_id"
_DETAIL_TURNS_SQL = (
    "SELECT t.turn_id, t.asked_at, t.completed_at, t.user_message, t.assistant_message, "
    "t.status, t.sources, t.rbti_applied, t.elapsed_ms, "
    # 피드백은 턴당 최신 1행(uq_turn_feedback_user_turn)이라 JOIN해도 턴이 늘지 않는다.
    "f.rating AS rating, f.comment AS feedback_comment, "
    f"(SELECT COUNT(*) FROM turn_click x WHERE {_TURN_SCOPE}) AS clicks "
    "FROM chat_turns t LEFT JOIN turn_feedback f ON f.app_name = t.app_name "
    "AND f.user_id = t.user_id AND f.session_id = t.session_id AND f.turn_id = t.turn_id "
    "WHERE t.app_name = %s AND t.user_id = %s AND t.session_id = %s "
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
    rating: str = "",
    rbti: str = "",
    status: str = "",
    sort: str = "",
    direction: str = "desc",
) -> dict[str, Any]:
    """세션 목록(기본 최근 갱신순 페이지네이션 + 검색·기간·피드백 필터, 정렬은 SESSION_SORTS).

    rating('up'|'down')을 주면 그 평가가 1개 이상 달린 세션만 — 같은 문장의 EXISTS다.

    본문 검색은 세션 id 부분 일치·회원번호 일치와 합집합이다 — 운영자가 세션 id·회원번호를
    붙여넣든 대화에 나온 낱말을 치든 같은 입력창에서 찾게 한다(회원 화면의 '대화 보기'가
    회원번호로 건다). 본문은 chat_turn의 질문·답변 TEXT라 한글 원문 LIKE가
    그대로 맞는다(events JSON의 escape 표기 문제가 없다).
    """
    where, params = period_filter("s.update_time", (since, until), settings.admin_utc_offset_hours)
    # 질문 없이 열리기만 한 빈 세션은 싣지 않는다 — 대시보드·통계 '세션'(턴 있는 세션)과 같은 정의.
    where.append(f"EXISTS (SELECT 1 FROM chat_turn t WHERE {_SESSION_TURNS})")

    if query:
        # 닉네임은 세션 행마다 닉네임 식을 돌리지 않고, 닉네임이 맞는 회원번호 집합으로 건다.
        where.append(
            "(s.id LIKE %s OR s.user_id = %s OR s.user_id IN ("
            "SELECT nick_u.user_no FROM users nick_u "
            "JOIN auth_keys nick_k ON nick_k.user_id = nick_u.id "
            "WHERE JSON_UNQUOTE(JSON_EXTRACT(nick_k.raw_user_info, '$.nickNm')) LIKE %s) "
            "OR EXISTS (SELECT 1 FROM chat_turn t WHERE "
            f"{_SESSION_TURNS} AND (t.user_text LIKE %s OR t.text LIKE %s)))"
        )
        params.extend([f"%{query}%", query, f"%{query}%", f"%{query}%", f"%{query}%"])
    if rating:
        where.append(
            f"EXISTS (SELECT 1 FROM turn_feedback f WHERE {_SESSION_FEEDBACK} AND f.rating = %s)"
        )
        params.append(rating)
    if condition := _rbti_condition(_SESSION_RBTI, rbti):
        where.append(condition[0])
        params.extend(condition[1])
    if status == "refused":  # 답변거절(실패·중단) 턴이 있는 세션 — 대시보드 답변거절률과 같은 정의
        marks = ", ".join(["%s"] * len(REFUSED))
        where.append(
            f"EXISTS (SELECT 1 FROM chat_turn t WHERE {_SESSION_TURNS} AND t.status IN ({marks}))"
        )
        params.extend(REFUSED)
    where_sql = sql_where(where)
    rbti_types = await _rbti_types(cur)  # 필터 선택지(목록 문장보다 먼저 — 목록이 마지막 두 문장)
    await cur.execute(f"SELECT COUNT(*) AS total FROM sessions s {where_sql}", params)
    total = (await cur.fetchone())["total"]

    size = settings.admin_page_size
    # 목록 배지(실패·중단 n) — 상태별 수. 같은 문장의 상관 서브쿼리라 행마다 왕복하지 않는다.
    refused = "".join(
        f"(SELECT COUNT(*) FROM chat_turn t WHERE {_SESSION_TURNS} "
        f"AND t.status = '{status}') AS {status}, "
        for status in REFUSED
    )
    # 목록 배지(좋아요·싫어요 n) — 같은 방식의 상관 서브쿼리.
    feedback = "".join(
        f"(SELECT COUNT(*) FROM turn_feedback f WHERE {_SESSION_FEEDBACK} "
        f"AND f.rating = '{value}') AS {column}, "
        for value, column in FEEDBACK_RATINGS.items()
    )
    await cur.execute(
        f"SELECT s.user_id, {nickname_sql('s.user_id')} AS nickname, "
        "s.id, s.create_time, s.update_time, "
        f"(SELECT COUNT(*) FROM chat_turn t WHERE {_SESSION_TURNS}) AS turn_count, "
        f"{refused}{feedback}"
        f"{_SESSION_RBTI} AS rbti, "
        # 미리보기 = 첫 턴의 질문 앞부분. 자르기를 SQL에 맡겨 본문 전체를 끌어오지 않는다.
        f"(SELECT LEFT(t.user_text, %s) FROM chat_turn t WHERE {_SESSION_TURNS} "
        "ORDER BY t.started_at, t.id LIMIT 1) AS preview "
        f"FROM sessions s {where_sql} "
        f"ORDER BY {_order(SESSION_SORTS, sort, direction, 's.id', _SESSION_ORDER)} "
        "LIMIT %s OFFSET %s",
        [settings.admin_preview_max_chars, *params, size, page * size],
    )
    items = [
        {**with_nickname(jsonable(row)), **refusal_breakdown([row])} for row in await cur.fetchall()
    ]
    return {
        "total": total,
        "page": page,
        "page_size": size,
        "items": items,
        "rbti_types": rbti_types,
    }


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
    json_columns: tuple[str, ...] = (),
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
    items = [jsonable(row, json_columns) for row in await cur.fetchall()]
    return {"total": total, "page": page, "page_size": size, "items": items}


# 대화한 회원 = chat_turn에 턴이 있는 회원 — 대시보드·통계 '활성 사용자'(admin_data.ACTIVE_USERS)와
# 같은 소스다. idx_chat_turn_session_time 선두 (app_name, user_id)로 인덱스만 훑어 묶고(앱은 하나라
# 회원당 한 행), users(uq_users_user_no)에 붙인다. 전 회원 뷰(user_activity)는 쓰지 않는다(운영 28k
# 회원에서 2.5~4.7s).
# rbti = 그 회원의 가장 최근 적용 RBTI(대화 목록 배지와 같은 정의). users.rbti는 운영에서 비어
# 있어 쓰지 않는다. GROUP_CONCAT은 NULL을 건너뛰고 최신순이라 첫 항목이 가장 최근 적용값이다
# (group_concat_max_len으로 뒤가 잘려도 첫 항목은 남는다).
_CHATTED = (
    "(SELECT user_id, COUNT(*) AS turns, MAX(started_at) AS last_chat_at, "
    "SUBSTRING_INDEX(GROUP_CONCAT(rbti_applied ORDER BY started_at DESC, id DESC), ',', 1) "
    "AS rbti FROM chat_turn GROUP BY app_name, user_id) t"
)


def _user_nickname(alias: str) -> str:
    """회원 닉네임 — 회원 행 id로 auth_keys에 바로 붙는다(users 재조회 없음). 모르면 회원번호."""
    return f"COALESCE({nickname_by_user_pk_sql(f'{alias}.id')}, {alias}.user_no)"


# 목록 정렬 — 화면 열 키 → SQL 식. 라우트가 이 키만 받고(Literal) 값은 식으로만 바꾼다
# (입력 보간 없음).
# 같은 값끼리는 id로 순서를 고정한다. 기본(sort 없음)은 각 목록의 원래 순서다.
USER_SORTS = {
    "nickname": "nickname",  # SELECT 별칭 — 대화한 회원 집합(수백 행)에만 계산된다
    # 회원번호는 숫자 순 — 숫자가 아닌 값은 NULL로 두어 뒤로 보낸다(_order의 IS NULL).
    "user_no": "IF(COALESCE(u.user_no, t.user_id) REGEXP '^[0-9]+$', "
    "CAST(COALESCE(u.user_no, t.user_id) AS UNSIGNED), NULL)",
    "rbti": "t.rbti",
    "turns": "t.turns",
    "last_chat_at": "t.last_chat_at",
    "created_at": "u.created_at",
}
# 대화 목록 — 최근/오래된(갱신 시각)·질의 수(SELECT 별칭, 걸러진 세션에만 계산된다).
SESSION_SORTS = {"update_time": "s.update_time", "turn_count": "turn_count"}
_SESSION_ORDER = "s.update_time DESC, s.app_name, s.user_id, s.id"
STARTER_SORTS = {
    # 칩 라벨 근사 — 행 라벨, 없으면 슬롯 키의 대상 부분(chip_label의 앞 두 단계). 설정·시드 라벨은
    # 표시에만 쓰여 정렬 위치가 조금 다를 수 있다.
    "label": f"COALESCE(NULLIF(label, ''), SUBSTRING_INDEX(slot, '{TARGET_SEP}', -1))",
    "run_date": "run_date",
    "active": "active",
    "pinned": "pinned",
}
SORT_DIRECTIONS = ("asc", "desc")


def _order(sorts: dict[str, str], sort: str, direction: str, tie: str, default: str) -> str:
    """정렬 키·방향 → ORDER BY 식(허용 목록 밖은 라우트가 이미 422로 막는다). 빈 값(NULL)은
    방향과 무관하게 뒤로 — 닉네임·RBTI처럼 비어 있는 행이 많은 열에서 앞 페이지를 채우지 않게."""
    if not sort:
        return default
    expr, way = sorts[sort], direction.upper()
    return f"{expr} IS NULL, {expr} {way}, {tie} {way}"


async def fetch_users(
    cur: aiomysql.DictCursor,
    settings: Settings,
    *,
    query: str,
    page: int,
    sort: str = "",
    direction: str = "desc",
    rbti: str = "",
    nickname: str = "",
) -> dict[str, Any]:
    """한 번이라도 대화한 회원(기본은 마지막 질의 최근순, sort로 열 정렬 — 검색과 함께 동작).

    필터: rbti(전체·있음·없음·유형 — 가장 최근 적용값 t.rbti 기준), nickname=any(닉네임 있는
    회원만).

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
        # users 행이 없는 대화 사용자(개발 키·시험 계정)도 활성 사용자에 들어가므로 LEFT JOIN으로
        # 싣는다 — 회원 수가 대시보드 '활성 사용자'와 같다(회원번호 칸은 대화의 user_id).
        source, params = f"{_CHATTED} LEFT JOIN users u ON u.user_no = t.user_id", []
    where: list[str] = []
    if condition := _rbti_condition("t.rbti", rbti):
        where.append(condition[0])
        params.extend(condition[1])
    if nickname == "any":
        where.append(f"{nickname_by_user_pk_sql('u.id')} IS NOT NULL")
    rbti_types = await _rbti_types(cur)
    page_data = await _fetch_page(
        cur,
        settings,
        # 표에는 닉네임 원값(없으면 null — 회원번호 칸이 따로 있다). 검색만 회원번호 폴백을 쓴다.
        columns=f"u.id, COALESCE(u.user_no, t.user_id) AS user_no, "
        f"{nickname_by_user_pk_sql('u.id')} AS nickname, t.rbti, "
        "t.turns, t.last_chat_at, u.created_at",
        source=source,
        where=where,
        params=params,
        order=_order(USER_SORTS, sort, direction, "u.id", "t.last_chat_at DESC, u.id DESC"),
        page=page,
    )
    return {**page_data, "rbti_types": rbti_types}


async def fetch_starters(
    cur: aiomysql.DictCursor,
    settings: Settings,
    *,
    page: int,
    sort: str = "",
    direction: str = "desc",
) -> dict[str, Any]:
    """초기 질문 풀(생성 최근순) — 목록과 편집 패널이 쓰는 열만. 쓰기는 starters.py 라우트다."""
    # 수동 추가·지금 생성의 슬롯 선택지 — 풀에 있는 슬롯 키와 그 칩 라벨(행 라벨이 있으면 최신 것).
    await cur.execute("SELECT slot, MAX(label) AS label FROM starters GROUP BY slot ORDER BY slot")
    slots = [
        {"slot": row["slot"], "label": chip_label(row, settings)} for row in await cur.fetchall()
    ]
    page_data = await _fetch_page(
        cur,
        settings,
        columns="id, slot, label, text, source, goods_no, source_url, run_date, pinned, active, "
        "valid_from, valid_until, created_at",
        source="starters",
        where=[],
        params=[],
        # 기본은 지금 노출 중인 풀이 먼저, 생성 최근순.
        order=_order(STARTER_SORTS, sort, direction, "id", "active DESC, created_at DESC, id DESC"),
        page=page,
    )
    # 슬롯 키 대신 칩에 보이는 라벨 — 서빙과 같은 규칙(starters.chip_label)을 서버가 채운다.
    for item in page_data["items"]:
        item["label"] = chip_label(item, settings)
    return {**page_data, "slots": slots}


# 감사 기록 대상 종류(admin_management.sql target_type) — 화면 필터 값이 곧 이 값이다.
AUDIT_TARGETS = ("login", "admin", "user", "starter", "session")
# 대상 이름 — 계정은 계정명, 회원은 회원번호로 바꿔 싣고(행 id는 사람이 못 읽는다) 나머지는
# target_id 그대로.
# 로그인 기록의 target_id는 사용자가 입력한 계정명 원문이다 — 실제 계정일 때만 이름을 싣고(없으면
# NULL → 화면 "알 수 없는 계정"), 원문(target_id)은 응답에 싣지 않는다. 아이디 칸에 비밀번호를
# 잘못 친 실패 기록이 화면에 비밀번호를 드러내지 않게.
_AUDIT_SOURCE = (
    "admin_audit x "
    "LEFT JOIN admin_users a ON x.target_type = 'admin' AND a.id = x.target_id "
    "LEFT JOIN admin_users la ON x.target_type = 'login' AND la.username = x.target_id "
    "LEFT JOIN users u ON x.target_type = 'user' AND u.id = x.target_id"
)


async def fetch_audit(
    cur: aiomysql.DictCursor,
    settings: Settings,
    *,
    page: int,
    target: str,
    actor: str,
    period: tuple[date | None, date | None],
) -> dict[str, Any]:
    """관리 감사 기록(owner 전용 라우트) — 최신순, 대상 종류·계정·기간(어드민 시간대 달력일) 필터.

    before/after는 바뀐 열만 담고 해시·키·원문 회원정보는 애초에 기록하지 않는다(쓰는 쪽 계약).
    필터 값은 전부 바인딩한다. 기간은 idx_admin_audit_time, 계정은 스냅샷 이름(actor_name)이다.
    """
    where, params = period_filter("x.created_at", period, settings.admin_utc_offset_hours)
    if target:
        where.append("x.target_type = %s")
        params.append(target)
    if actor:
        where.append("x.actor_name = %s")
        params.append(actor)
    page_data = await _fetch_page(
        cur,
        settings,
        columns="x.id, x.created_at, x.actor_name, x.target_type, "
        "CASE x.target_type WHEN 'admin' THEN a.username WHEN 'user' THEN u.user_no "
        "WHEN 'login' THEN la.username ELSE x.target_id END AS target_name, "
        "x.action, x.`before`, x.`after`, x.ip",
        source=_AUDIT_SOURCE,
        where=where,
        params=params,
        order="x.created_at DESC, x.id DESC",
        page=page,
        json_columns=("before", "after"),
    )
    # 계정 필터 선택지 — 기록에 남은 계정 이름(스냅샷). 계정 관리 API가 없어 여기서 준다.
    await cur.execute(
        "SELECT DISTINCT actor_name FROM admin_audit "
        "WHERE actor_name IS NOT NULL ORDER BY actor_name"
    )
    page_data["actors"] = [row["actor_name"] for row in await cur.fetchall()]
    return page_data


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
        rating: Literal["", "up", "down"] = "",
        rbti: str = Query(default="", pattern=RBTI_FILTER),
        status: Literal["", "refused"] = "",
        sort: Literal[("", *SESSION_SORTS)] = "",  # type: ignore[valid-type]
        dir: Literal[SORT_DIRECTIONS] = "desc",  # type: ignore[valid-type]
    ) -> Any:
        return await _query(
            lambda cur: fetch_sessions(
                cur,
                settings,
                query=q.strip(),
                since=period[0],
                until=period[1],
                page=page,
                rating=rating,
                rbti=rbti,
                status=status,
                sort=sort,
                direction=dir,
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
    async def admin_users(
        q: str = "",
        page: int = page_query,
        sort: Literal[("", *USER_SORTS)] = "",  # type: ignore[valid-type]
        dir: Literal[SORT_DIRECTIONS] = "desc",  # type: ignore[valid-type]
        rbti: str = Query(default="", pattern=RBTI_FILTER),
        nickname: Literal["", "any"] = "",
    ) -> Any:
        return await _query(
            lambda cur: fetch_users(
                cur,
                settings,
                query=q.strip(),
                page=page,
                sort=sort,
                direction=dir,
                rbti=rbti,
                nickname=nickname,
            )
        )

    @router.get("/api/starters", dependencies=[Depends(require_admin)])
    async def admin_starters(
        page: int = page_query,
        sort: Literal[("", *STARTER_SORTS)] = "",  # type: ignore[valid-type]
        dir: Literal[SORT_DIRECTIONS] = "desc",  # type: ignore[valid-type]
    ) -> Any:
        return await _query(
            lambda cur: fetch_starters(cur, settings, page=page, sort=sort, direction=dir)
        )

    @router.get("/api/analytics", dependencies=[Depends(require_admin)])
    async def admin_analytics(
        period: tuple[date | None, date | None] = Depends(date_range),
    ) -> Any:
        return await _query(fetch_analytics, settings, period)

    @router.get("/api/audit", dependencies=[Depends(require_owner)])
    async def admin_audit(
        page: int = page_query,
        target: Literal[("", *AUDIT_TARGETS)] = "",  # type: ignore[valid-type]
        actor: str = "",
        period: tuple[date | None, date | None] = Depends(date_range),
    ) -> Any:
        """관리 감사 기록(owner 전용) — 누가 언제 무엇을 바꿨는지."""
        return await _query(
            lambda cur: fetch_audit(
                cur, settings, page=page, target=target, actor=actor.strip(), period=period
            )
        )

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
