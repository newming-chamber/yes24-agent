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
import random
from collections import Counter
from datetime import date, timedelta
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
    excluded_condition,
    fetch_analytics,
    fetch_cost,
    fetch_stats,
    jsonable,
    local_day,
    nickname_by_user_pk_sql,
    nickname_sql,
    period_filter,
    refusal_breakdown,
    sql_where,
    with_nickname,
)
from yes24_agent.config import Settings
from yes24_agent.session_service import mysql_pool_kwargs
from yes24_agent.starters import (
    _CHIP_LABEL_MAX_CHARS,
    _POOL_COLUMNS,
    _SLOT_MAX_CHARS,
    TARGET_SEP,
    _squash,
    _today,
    chip_label,
    live_clauses,
    live_predicate,
    pick_starters,
    pool_query,
    slot_labels,
)

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


async def fetch_overview(
    cur: aiomysql.DictCursor, excluded: tuple[str, ...] = ()
) -> dict[str, Any]:
    """개요(누적): 대화한 사용자·세션(대화방)·질의 수.

    세션 = 턴이 1개 이상인 대화방(chat_turn 기준) — 통계 탭 '세션수'와 같은 정의다. sessions 표는
    질문 없이 열리기만 한 빈 세션도 담아 그 행 수를 쓰면 통계와 어긋난다.

    chat_users = 실제로 대화한 사용자(전체 기간) — 통계 탭 '활성사용자수'와 같은 정의다.
    users 행 수는 앱을 연 회원 전부라 운영 지표로 뜻이 없어 싣지 않는다.
    """
    where, params = excluded_condition("user_id", excluded)
    scope = sql_where(where)
    await cur.execute(
        f"SELECT (SELECT {ACTIVE_USERS} FROM chat_turn{scope}) AS chat_users, "
        "(SELECT COUNT(DISTINCT app_name, user_id, session_id) "
        f"FROM chat_turn{scope}) AS sessions, "
        f"(SELECT COUNT(*) FROM chat_turn{scope}) AS turns",
        params * 3,
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
    excluded: tuple[str, ...] = (),
) -> dict[str, Any]:
    """세션 목록(기본 최근 갱신순 페이지네이션 + 검색·기간·피드백 필터, 정렬은 SESSION_SORTS).

    rating('up'|'down')을 주면 그 평가가 1개 이상 달린 세션만 — 같은 문장의 EXISTS다.

    본문 검색은 세션 id 부분 일치·회원번호 일치와 합집합이다 — 운영자가 세션 id·회원번호를
    붙여넣든 대화에 나온 낱말을 치든 같은 입력창에서 찾게 한다(회원 화면의 '대화 보기'가
    회원번호로 건다). 본문은 chat_turn의 질문·답변 TEXT라 한글 원문 LIKE가
    그대로 맞는다(events JSON의 escape 표기 문제가 없다).
    """
    where, params = period_filter(
        "s.update_time", (since, until), settings.admin_utc_offset_hours, excluded, "s.user_id"
    )
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
    source_params: list[Any] | None = None,
    count_source: tuple[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """목록 한 페이지와 전체 건수 — 대화 목록과 같은 응답 모양(total·page·page_size·items).

    source_params = 출처(source) 안의 자리표시자 인자(params는 WHERE 인자). count_source =
    COUNT에만 쓸 가벼운 출처와 그 인자 — 정렬·표시에만 필요한 조인을 건수 세기에서 뺀다.
    """
    where_sql = sql_where(where)
    count_from, count_params = count_source or (source, source_params or [])
    await cur.execute(
        f"SELECT COUNT(*) AS total FROM {count_from}{where_sql}", [*count_params, *params]
    )
    total = (await cur.fetchone())["total"]
    size = settings.admin_page_size
    await cur.execute(
        f"SELECT {columns} FROM {source}{where_sql} ORDER BY {order} LIMIT %s OFFSET %s",
        [*(source_params or []), *params, size, page * size],
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
    # 분야 이름 근사 — 행 라벨, 없으면 슬롯 키의 대상 부분(SQL로 옮길 수 있는 chip_label 단계만).
    # 화면의 분야 이름(_starter_slots — 설정 라벨·슬롯의 최근 라벨 행)과 정렬 위치가 다를 수 있다.
    "label": f"COALESCE(NULLIF(x.label, ''), SUBSTRING_INDEX(x.slot, '{TARGET_SEP}', -1))",
    # 생성일 — 자동은 생성 실행일, 직접 등록은 등록 시각의 어드민 시간대 날짜(화면 '생성·등록일'과
    # 같은 값). 시간대가 설정값이라 식은 fetch_starters가 채운다(여기는 허용 키).
    "run_date": "",
    "pinned": "x.pinned",
    "live": "x.live",
    "uses": "COALESCE(u.uses, 0)",
}
_STARTER_ORDER = "x.live DESC, x.active DESC, x.created_at DESC, x.id DESC"


def _first_turns(days: int, excluded: tuple[str, ...]) -> tuple[str, list[Any]]:
    """새 세션의 첫 턴(최근 days일) — 칩으로 시작한 대화·인기 질문이 같은 정의를 쓴다.

    첫 턴 = 같은 세션에 더 이른 턴이 없는 턴(idx_chat_turn_session_time으로 찾는다). 시각은 UTC
    저장. 내부·테스트 계정(excluded)은 다른 지표 탭과 같은 기준으로 뺀다.
    """
    excluding, params = excluded_condition("t.user_id", excluded)
    return (
        "SELECT t.user_text, t.started_at FROM chat_turn t "
        "WHERE t.started_at >= UTC_TIMESTAMP() - INTERVAL %s DAY"
        + "".join(f" AND {condition}" for condition in excluding)
        + " AND NOT EXISTS (SELECT 1 FROM chat_turn p WHERE p.app_name = t.app_name "
        "AND p.user_id = t.user_id AND p.session_id = t.session_id "
        "AND (p.started_at < t.started_at OR (p.started_at = t.started_at AND p.id < t.id)))",
        [days, *params],
    )


# 칩 문장 비교는 이진 — 두 표의 콜레이션이 다르고(chat_turn utf8mb4_unicode_ci, starters 서버 기본)
# 칩은 누르면 그대로 전송되는 문장이라 대소문자·전각을 접으면 다른 문장까지 칩 시작으로 센다.
_BIN = "COLLATE utf8mb4_bin"
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
    excluded: tuple[str, ...] = (),
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
    where, excluded_params = excluded_condition("COALESCE(u.user_no, t.user_id)", excluded)
    params.extend(excluded_params)
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


async def _starter_slots(cur: aiomysql.DictCursor, settings: Settings) -> list[dict[str, str]]:
    """분야 선택지·분야 이름의 단일 출처 — 노출이 켜진 행이 있는 슬롯만, 이름은 서빙과 같은 규칙
    (`starters.slot_labels` — 라벨이 실린 가장 최근 행 → `chip_label`, 설정 라벨이 먼저). 라벨 없는
    직접 등록 행이 그 슬롯의 최신 행이어도 실제 칩과 같은 이름이다. 꺼진 행만 남은 슬롯(교체가 끝난
    옛 슬롯·시험 입력)은 선택지에 없다. 목록·날짜 패널·필터·폼이 모두 이 이름을 쓴다. 이름순.
    """
    await cur.execute("SELECT id, slot, label FROM starters WHERE active = 1")
    rows = list(await cur.fetchall())
    names = slot_labels(rows)
    slots = [{"slot": slot, "label": chip_label({"slot": slot, "label": names.get(slot)}, settings)}
             for slot in {row["slot"] for row in rows}]
    return sorted(slots, key=lambda slot: (slot["label"], slot["slot"]))


async def fetch_starters(
    cur: aiomysql.DictCursor,
    settings: Settings,
    *,
    page: int,
    sort: str = "",
    direction: str = "desc",
    query: str = "",
    status: str = "",
    source: str = "",
    slot: str = "",
    term: str = "",
    item_id: int | None = None,
    excluded: tuple[str, ...] = (),
) -> dict[str, Any]:
    """초기 질문 풀 — 목록·편집 패널 열 + 지금 노출(live)·사용 수(uses).

    live는 서빙 풀과 같은 식(`starters.live_predicate`)이다. uses는 최근 starter_uses_days일 새
    세션 중 첫 질문이 이 문장과 같은 세션 수 — 문장 단위라 같은 문장 행이 여럿이면 수를 나눠
    갖지 않고 같은 값을 보인다(uses_shared). 쓰기는 starters.py 라우트다.
    """
    today = _today()
    live_sql, live_params = live_predicate(today, settings)
    # 미노출 사유 = 판정 조건 중 처음 실패한 것의 키(순서가 우선순위). 키는 코드 상수라 인라인.
    clauses = live_clauses(today, settings)
    reason_sql = (
        "CASE " + " ".join(f"WHEN NOT ({sql}) THEN '{key}'" for key, sql, _ in clauses) + " END"
    )
    reason_params = [param for _, _, params in clauses for param in params]
    slots = await _starter_slots(cur, settings)
    first_sql, first_params = _first_turns(settings.starter_uses_days, excluded)
    where, params = [], []
    if query:
        # 분야 이름은 행 라벨이 비어 있는 경우가 많다(직접 등록·옛 자동 행) — 선택지와 같은 출처
        # (_starter_slots)의 이름으로 맞는 분야 키를 골라 함께 찾는다.
        matched = [slot["slot"] for slot in slots if query.casefold() in slot["label"].casefold()]
        in_slots = f" OR x.slot IN ({', '.join(['%s'] * len(matched))})" if matched else ""
        where.append(f"(x.text LIKE %s OR x.label LIKE %s{in_slots})")
        params.extend([f"%{query}%", f"%{query}%", *matched])
    if status in ("live", "stopped"):  # all(또는 빈 값) = 거르지 않음
        where.append("x.live = %s")
        params.append(status == "live")
    if source:
        where.append("x.source = %s")
        params.append(source)
    if slot:
        where.append("x.slot = %s")
        params.append(slot)
    if item_id is not None:  # 한 행(감사 기록의 '이 질문 보기')
        where.append("x.id = %s")
        params.append(item_id)
    if term:  # 노출 기간: none = 시작·끝 모두 없음(매일), set = 하나라도 있음
        where.append(f"{'NOT ' if term == 'set' else ''}"
                     "(x.valid_from IS NULL AND x.valid_until IS NULL)")
    # 판정 열(live·사유)을 파생 표에서 한 번 계산해 WHERE·ORDER BY가 이름으로 쓴다. 건수는 이
    # 파생 표만으로 센다 — 사용 수·같은 문장 수 조인은 표시·정렬용이라 페이지 문장에만 붙인다.
    judged = (
        f"(SELECT s.*, ({live_sql}) AS live, {reason_sql} AS not_live_reason FROM starters s) x"
    )
    page_data = await _fetch_page(
        cur,
        settings,
        columns="x.id, x.slot, x.label, x.text, x.source, x.goods_no, x.source_url, x.run_date, "
        "x.pinned, x.active, x.valid_from, x.valid_until, x.created_at, x.live, x.not_live_reason, "
        "d.copies > 1 AS uses_shared, COALESCE(u.uses, 0) AS uses",
        source=f"{judged} "
        # 같은 문장 행 수 — 행마다 상관 COUNT를 돌리지 않고 문장별로 한 번 묶어 붙인다.
        f"LEFT JOIN (SELECT text {_BIN} AS dup_text, COUNT(*) AS copies FROM starters "
        f"GROUP BY dup_text) d ON d.dup_text = x.text {_BIN} "
        f"LEFT JOIN (SELECT user_text {_BIN} AS chip_text, COUNT(*) AS uses "
        f"FROM ({first_sql}) f GROUP BY chip_text) u ON u.chip_text = x.text {_BIN}",
        source_params=[*live_params, *reason_params, *first_params],
        count_source=(judged, [*live_params, *reason_params]),
        where=where,
        params=params,
        # 기본은 지금 노출 중인 풀이 먼저, 생성 최근순.
        order=_order(
            {**STARTER_SORTS, "run_date": "COALESCE(x.run_date, "
             f"{local_day('x.created_at', settings.admin_utc_offset_hours)})"},
            sort, direction, "x.id", _STARTER_ORDER,
        ),
        page=page,
    )
    # 분야 이름은 선택지와 같은 출처(_starter_slots) — 꺼진 행만 남은 슬롯은 칩 라벨 규칙 그대로.
    names = {slot["slot"]: slot["label"] for slot in slots}
    for item in page_data["items"]:
        item["label"] = names.get(item["slot"]) or chip_label(item, settings)
        item["live"], item["uses_shared"] = bool(item["live"]), bool(item["uses_shared"])
    await _split_inactive(cur, page_data["items"])
    return {**page_data, "slots": slots, "summary": {"pool_days": settings.starter_pool_days}}


async def _split_inactive(cur: aiomysql.DictCursor, items: list[dict[str, Any]]) -> None:
    """'사용 중지'(inactive)를 누가 내렸는지로 나눈다 — 이 페이지 행만.

    생성은 새 세트를 저장하며 이전 자동 행을 active=0으로 내린다(감사는 슬롯 단위 generate).
    운영자는 DELETE(deactivate) 또는 PATCH(after.active=false)로 내리고 행 단위 감사가 남는다.
    그래서 자동 행에 행 단위 중지 감사가 없으면 '새 생성분으로 교체됨'(replaced), 있거나 수동
    행이면 '운영자가 중지'(stopped). 감사는 판정에만 쓰고 누가 했는지는 싣지 않는다.
    """
    ids = [str(item["id"]) for item in items if item["not_live_reason"] == "inactive"]
    stopped: set[str] = set()
    if ids:
        marks = ", ".join(["%s"] * len(ids))
        await cur.execute(
            "SELECT target_id, action, `after` FROM admin_audit WHERE target_type = 'starter' "
            f"AND action IN ('deactivate', 'update') AND target_id IN ({marks})",
            ids,
        )
        for row in await cur.fetchall():
            after = jsonable(row, ("after",))["after"] or {}
            if row["action"] == "deactivate" or after.get("active") is False:
                stopped.add(str(row["target_id"]))
    for item in items:
        if item["not_live_reason"] == "inactive":
            operator = item["source"] != "auto" or str(item["id"]) in stopped
            item["not_live_reason"] = "stopped" if operator else "replaced"


async def fetch_starter_preview(
    cur: aiomysql.DictCursor,
    settings: Settings,
    *,
    day: date,
    n: int,
    seed: int,
    add_slot: str = "",
    add_pinned: bool = False,
) -> dict[str, Any]:
    """그날 첫 화면 미리보기 — 서빙과 같은 풀 판정(`live_predicate`)과 선택(`pick_starters`).

    serve()·생성 트리거는 부르지 않는다(서빙 로그·생성 선점이 미리보기로 오염되지 않게).
    mode: 오늘 = exact(그날 풀 그대로), 미래 = estimated(수동은 그날 판정, 자동은 그날 생성분을
    알 수 없어 **오늘 노출 중인 자동 행**으로 가정), 과거 = record(그날 생성된 자동 행 기록만 —
    그때의 노출은 재현할 수 없어 확률·샘플이 없다).
    행별 확률 = 날짜 시드의 선택을 starter_preview_draws번 반복해 뽑힌 비율(같은 날 같은 값),
    sample = 요청 시드로 뽑은 예시 한 벌.
    add_slot이 있으면 그 슬롯에 가상 행 하나를 넣어 등록 전 예상 확률(expected_probability)을 센다.
    """
    today = _today()
    mode = "record" if day < today else "exact" if day == today else "estimated"
    columns = ", ".join(_POOL_COLUMNS)
    if mode == "record":
        await cur.execute(
            f"SELECT {columns} FROM starters WHERE source = 'auto' AND run_date = %s "
            "ORDER BY slot, id",
            [day],
        )
    elif mode == "exact":
        await cur.execute(*pool_query(day, settings))
    else:
        manual_sql, manual_params = live_predicate(day, settings)
        auto_sql, auto_params = live_predicate(today, settings)
        await cur.execute(
            f"SELECT {columns} FROM starters WHERE (source = 'manual' AND {manual_sql}) "
            f"OR (source = 'auto' AND {auto_sql})",
            [*manual_params, *auto_params],
        )
    pool = list(await cur.fetchall())
    counts, skipped, sample, expected = Counter(), Counter(), [], None
    draws = settings.starter_preview_draws
    if mode != "record":
        virtual = {"id": -1, "slot": add_slot, "text": "", "pinned": add_pinned,
                   "goods_no": None, "source_url": None}
        candidates = [*pool, virtual] if add_slot else pool
        # 예시 한 벌만 요청 시드('다른 예시 보기'), 확률은 날짜에서 정한 시드 — 같은 날 같은 풀이면
        # 패널을 다시 열어도, 목록·행 패널에서도 같은 값이다(시드마다 표본 오차로 흔들리지 않게).
        sample = pick_starters(pool, n, random.Random(seed))
        rng = random.Random(day.toordinal())
        for _ in range(draws):
            picked = pick_starters(candidates, n, rng, skipped)
            counts.update(row["id"] for row in picked)
        expected = counts[virtual["id"]] / draws if add_slot else None
    pinned_slots = {row["slot"] for row in pool if row["pinned"]}
    rows = [
        {"id": row["id"], "slot": row["slot"], "label": chip_label(row, settings),
         "text": row["text"], "source": row["source"], "pinned": bool(row["pinned"]),
         "probability": None if mode == "record" else counts[row["id"]] / draws,
         "zero_reason": None if mode == "record" or counts[row["id"]]
         else zero_reason(bool(row["pinned"]), row["slot"] in pinned_slots, skipped[row["id"]])}
        for row in pool
    ]
    # 분야별 확률은 싣지 않는다 — 분야는 균등하게 섞여 n / 분야 수로 같고(추첨 표본의 흔들림만
    # 남는다), 화면은 그 값 하나를 머리말·고정 예상에 쓴다. 분야 이름은 목록·필터와 같은 출처
    # (_starter_slots), 그 밖(꺼진 슬롯의 기록)은 행의 칩 라벨.
    names = {slot["slot"]: slot["label"] for slot in await _starter_slots(cur, settings)}
    slots = [
        {"slot": slot, "label": names.get(slot)
         or chip_label(next(r for r in pool if r["slot"] == slot), settings),
         "rows": sum(1 for r in pool if r["slot"] == slot), "pinned": slot in pinned_slots}
        for slot in sorted({row["slot"] for row in pool})
    ]
    rows.sort(key=lambda row: (-(row["probability"] or 0), row["slot"], row["id"]))
    return {
        "date": day.isoformat(), "n": n, "mode": mode, "draws": draws, "pool_size": len(pool),
        # 직접 등록 폼의 새 분야 이름 상한 = 칩 라벨 상한(서버 검증과 같다).
        "slot_name_max": _CHIP_LABEL_MAX_CHARS,
        "slot_count": len({row["slot"] for row in pool}),
        "sample": [
            {"id": row["id"], "label": chip_label(row, settings), "text": row["text"]}
            for row in sample
        ],
        "rows": rows,
        "slots": slots,
        "expected_probability": expected,
    }


def zero_reason(pinned: bool, slot_has_pinned: bool, skipped: int) -> str:
    """후보인데 추첨에서 한 번도 뽑히지 않은 질문의 이유 — 문구를 보지 않고 선택의 사실로 가른다.

    분야에 고정 질문이 있으면 그 분야는 고정 질문 중에서만 고른다(pinned_sibling). 같은 상품·출처·
    문구를 가리키는 질문이 이미 뽑혀 실제로 건너뛴 적이 있으면 duplicate(pick_starters가 센 수).
    둘 다 아니면 표본에서 안 나왔을 뿐 아주 드물게 나온다(rare — 1/draws 미만).
    """
    if slot_has_pinned and not pinned:
        return "pinned_sibling"
    return "duplicate" if skipped else "rare"


async def fetch_starter_calendar(
    cur: aiomysql.DictCursor, settings: Settings, *, month: date
) -> dict[str, Any]:
    """월 달력 — 날짜 칸 수치 + 기간 막대(수동 기간 질문) + 상시 고정 띠 + 상단 한 줄.

    칸 수치(모두 서버 today 기준): 오늘 = 지금 노출 풀(live_predicate의 수·슬롯 수, 목록 summary와
    같은 식) + 오늘 생성된 자동 행 수, 과거 = 그날 생성된 자동 행 수(생성 기록 — 그날의 실제 노출
    재현이 아니다). 미래 칸은 기간 막대만 그린다. 생성 수는 달력에 보이는 앞뒤 달 칸(그 주의
    월~일)까지 센다(outside). ongoing = 기간 없이 오늘 노출 중인 수동 행의 수와
    분야 라벨(상단 한 줄 — 기간 없는 행은 막대가 없어 달력에 안 보인다).
    """
    today = _today()
    first = month.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    # 화면 격자 = 첫 주 월요일 ~ 마지막 주 일요일(앞뒤 달 칸 포함) — 생성 수는 오늘까지 한 번에.
    grid_start = first - timedelta(days=first.weekday())
    grid_end = last + timedelta(days=6 - last.weekday())
    await cur.execute(
        "SELECT run_date, COUNT(*) AS `generated` FROM starters WHERE source = 'auto' "
        "AND run_date BETWEEN %s AND %s GROUP BY run_date",
        [grid_start, min(grid_end, today)],
    )
    generated = {row["run_date"]: row["generated"] for row in await cur.fetchall()}
    outside = [
        {"date": day.isoformat(), "generated": generated.get(day, 0)}
        for day in (grid_start + timedelta(days=i) for i in range((grid_end - grid_start).days + 1))
        if not first <= day <= last and day <= today
    ]
    live_sql, live_params = live_predicate(today, settings)
    counted = None
    if first <= today <= last:
        await cur.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT slot) AS slots FROM starters "
            f"WHERE {live_sql}",
            live_params,
        )
        counted = await cur.fetchone()
    cells = []
    for day in days:
        cell: dict[str, Any] = {"date": day.isoformat(), "live": None, "slots": None,
                                "generated": None}
        if day < today:
            cell.update(kind="past", generated=generated.get(day, 0))
        elif day > today:
            cell.update(kind="future")
        else:
            cell.update(kind="today", live=counted["n"], slots=counted["slots"],
                        generated=generated.get(day, 0))
        cells.append(cell)
    columns = "id, slot, label, text, pinned, active, valid_from, valid_until"
    await cur.execute(
        f"SELECT {columns} FROM starters WHERE active = 1 AND source = 'manual' "
        "AND (valid_from IS NOT NULL OR valid_until IS NOT NULL) "
        "AND (valid_from IS NULL OR valid_from <= %s) "
        "AND (valid_until IS NULL OR valid_until >= %s) ORDER BY COALESCE(valid_from, %s), id",
        [last, first, first],
    )
    schedules = [jsonable(row) for row in await cur.fetchall()]
    await cur.execute(
        f"SELECT {columns} FROM starters WHERE {live_sql} AND pinned = 1 "
        "AND valid_from IS NULL AND valid_until IS NULL ORDER BY slot, id",
        live_params,
    )
    always = [jsonable(row) for row in await cur.fetchall()]
    await cur.execute(
        f"SELECT slot, MAX(label) AS label, COUNT(*) AS n FROM starters WHERE {live_sql} "
        "AND source = 'manual' AND valid_from IS NULL AND valid_until IS NULL "
        "GROUP BY slot ORDER BY n DESC, slot",
        live_params,
    )
    ongoing = await cur.fetchall()
    slots = await _starter_slots(cur, settings)
    names = {slot["slot"]: slot["label"] for slot in slots}
    for row in (*schedules, *always, *ongoing):
        row["label"] = names.get(row["slot"]) or chip_label(row, settings)
    for row in (*schedules, *always):
        row["pinned"], row["active"] = bool(row["pinned"]), bool(row["active"])
    return {
        "today": today.isoformat(),
        "month": first.strftime("%Y-%m"), "days": cells,
        "outside": outside, "pinned_always": always, "schedules": schedules,
        "ongoing": {"count": sum(row["n"] for row in ongoing),
                    "slots": [{"label": row["label"], "n": row["n"]} for row in ongoing]},
        # 기간 선택 → 수동 추가 폼의 슬롯 선택지(목록을 거치지 않고 캘린더만 열어도).
        "slots": slots,
    }


async def fetch_starter_popular(
    cur: aiomysql.DictCursor, settings: Settings, *, days: int, excluded: tuple[str, ...] = ()
) -> dict[str, Any]:
    """인기 질문 — 최근 days일 새 세션의 첫 질문 중 어떤 칩 문장과도 같지 않은 것을 묶어 센다.

    SQL은 앞뒤 공백만 벗겨 묶고, 서빙과 같은 정규화(`starters._squash` — 공백 종류·제로폭)로
    파이썬에서 한 번 더 합친 뒤 starter_popular_min_sessions번 이상만 남긴다(합치기 전에
    자르면 표기만 다른 1+1이 빠진다). fits = 칩 문장 길이 상한 안인지(수동 추가 가능 여부).
    """
    first_sql, first_params = _first_turns(days, excluded)
    # 머리말 — 같은 기간 새 대화 중 첫 화면 질문(칩 문장 그대로)으로 시작한 비율.
    await cur.execute(
        "SELECT COUNT(*) AS sessions, COALESCE(SUM(EXISTS(SELECT 1 FROM starters c WHERE "
        f"c.text {_BIN} = f.user_text {_BIN})), 0) AS chip_sessions FROM ({first_sql}) f",
        first_params,
    )
    summary = jsonable(await cur.fetchone())
    await cur.execute(
        "SELECT TRIM(f.user_text) AS text, COUNT(*) AS sessions, MAX(f.started_at) AS last_at "
        f"FROM ({first_sql}) f WHERE NOT EXISTS (SELECT 1 FROM starters c "
        f"WHERE c.text {_BIN} = f.user_text {_BIN}) GROUP BY TRIM(f.user_text)",
        first_params,
    )
    merged: dict[str, dict[str, Any]] = {}
    for row in await cur.fetchall():
        row = jsonable(row)
        key = _squash(row["text"])
        if not key:
            continue
        item = merged.setdefault(key, {"text": key, "sessions": 0, "last_at": row["last_at"]})
        item["sessions"] += row["sessions"]
        item["last_at"] = max(item["last_at"], row["last_at"])
    items = sorted(
        (item for item in merged.values()
         if item["sessions"] >= settings.starter_popular_min_sessions),
        key=lambda item: (-item["sessions"], -item["last_at"]),
    )[: settings.starter_popular_limit]
    for item in items:
        item["fits"] = len(item["text"]) <= settings.starter_max_chars
    # '칩으로 추가'가 여는 수동 추가 폼의 슬롯 선택지(풀 목록을 거치지 않고 이 화면만 열어도).
    return {
        "days": days, "max_chars": settings.starter_max_chars, "items": items, "summary": summary,
        "min_chars": settings.starter_popular_min_chars,
        "slots": await _starter_slots(cur, settings),
    }


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
    "LEFT JOIN users u ON x.target_type = 'user' AND u.id = x.target_id "
    "LEFT JOIN starters s ON x.target_type = 'starter' AND s.id = x.target_id"
)
# 서버 관리 도구(CLI) 기록 — actor 없이 after.by="cli"로 남는다(scripts/admin_accounts.py _CLI).
# 계정명 형식([a-z0-9._-])에 없는 글자로 시작하는 필터 값이라 실제 계정과 겹치지 않는다.
AUDIT_CLI_ACTOR = "@cli"
_AUDIT_CLI = "x.actor_name IS NULL AND JSON_UNQUOTE(JSON_EXTRACT(x.`after`, '$.by')) = 'cli'"


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
    # 기본(빈 값)은 '변경 작업' — 로그인·로그아웃 기록이 목록을 덮지 않게 뺀다.
    # all이면 전부, 그 밖은 그 종류만.
    if not target:
        where.append("x.target_type <> 'login'")
    elif target != "all":
        where.append("x.target_type = %s")
        params.append(target)
    if actor == AUDIT_CLI_ACTOR:
        where.append(_AUDIT_CLI)
    elif actor:
        where.append("x.actor_name = %s")
        params.append(actor)
    page_data = await _fetch_page(
        cur,
        settings,
        columns="x.id, x.created_at, x.actor_name, x.target_type, "
        "CASE x.target_type WHEN 'admin' THEN a.username WHEN 'user' THEN u.user_no "
        "WHEN 'login' THEN la.username ELSE x.target_id END AS target_name, "
        # target_text = 초기 질문 대상의 문장 앞부분(대상 칸·상세의 '이 질문 보기')
        "x.action, x.`before`, x.`after`, x.ip, "
        f"LEFT(s.text, {int(settings.admin_preview_max_chars)}) AS target_text",
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
    # CLI 기록이 있으면 계정 선택지에 '서버 관리 도구(CLI)'(값 AUDIT_CLI_ACTOR)를 덧붙인다.
    await cur.execute(f"SELECT 1 FROM admin_audit x WHERE {_AUDIT_CLI} LIMIT 1")
    page_data["cli_actor"] = AUDIT_CLI_ACTOR if await cur.fetchone() else None
    # 변경 내용·대상의 분야(슬롯 키)를 화면이 칩 라벨로 보이게 — 목록·캘린더와 같은 출처.
    page_data["slots"] = await _starter_slots(cur, settings)
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

    def excluded_scope(internal: bool = False) -> tuple[str, ...]:
        """집계·목록에서 뺄 계정 — 기본은 설정의 내부·테스트 계정, internal=1이면 빼지 않는다."""
        return () if internal else tuple(settings.admin_excluded_user_ids)

    scope = Depends(excluded_scope)

    @router.get("/api/overview", dependencies=[Depends(require_admin)])
    async def admin_overview(excluded: tuple[str, ...] = scope) -> Any:
        return await _query(fetch_overview, excluded)

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
        excluded: tuple[str, ...] = scope,
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
                excluded=excluded,
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
        excluded: tuple[str, ...] = scope,
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
                excluded=excluded,
            )
        )

    @router.get("/api/starters", dependencies=[Depends(require_admin)])
    async def admin_starters(
        page: int = page_query,
        sort: Literal[("", *STARTER_SORTS)] = "",  # type: ignore[valid-type]
        dir: Literal[SORT_DIRECTIONS] = "desc",  # type: ignore[valid-type]
        q: str = "",
        status: Literal["", "live", "stopped", "all"] = "",
        source: Literal["", "auto", "manual"] = "",
        slot: str = Query(default="", max_length=_SLOT_MAX_CHARS),
        term: Literal["", "none", "set"] = "",
        id: int | None = Query(default=None, ge=1),
        excluded: tuple[str, ...] = scope,
    ) -> Any:
        return await _query(
            lambda cur: fetch_starters(
                cur, settings, page=page, sort=sort, direction=dir, query=q.strip(), status=status,
                source=source, slot=slot, term=term, item_id=id, excluded=excluded,
            )
        )

    @router.get("/api/starters/preview", dependencies=[Depends(require_admin)])
    async def admin_starter_preview(
        day: date | None = Query(default=None, alias="date"),
        seed: int | None = Query(default=None, ge=0),
        add_slot: str = Query(default="", max_length=_SLOT_MAX_CHARS),
        add_pinned: bool = False,
    ) -> Any:
        """그날 첫 화면 미리보기(서빙과 같은 풀·선택·개수, 서빙 로그·생성 트리거 없음).

        과거는 생성 기록(mode=record), 미래는 max_date까지만(그 뒤는 추정 근거가 없다).
        add_slot은 등록 전 예상 확률용 가상 행(DB 쓰기 없음).
        """
        today = _today()
        day = day or today
        max_date = today + timedelta(days=settings.starter_preview_max_days)
        if day > max_date:
            raise HTTPException(
                status_code=422, detail=f"미리보기 날짜는 {max_date}까지입니다."
            )
        seed = random.randrange(2**31) if seed is None else seed
        return await _query(
            lambda cur: fetch_starter_preview(
                cur, settings, day=day, n=settings.starter_count, seed=seed,
                add_slot=add_slot.strip(), add_pinned=add_pinned,
            )
        )

    @router.get("/api/starters/calendar", dependencies=[Depends(require_admin)])
    async def admin_starter_calendar(
        month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    ) -> Any:
        """초기 질문 월 달력(기본 이번 달 — 서버 today 기준)."""
        try:  # 형식은 맞아도 날짜로 셀 수 없는 달(0000-01·9999-12의 다음 달)은 422
            first = date.fromisoformat(f"{month}-01") if month else _today().replace(day=1)
            first + timedelta(days=32)
        except (ValueError, OverflowError):
            raise HTTPException(status_code=422, detail="볼 수 없는 달입니다.") from None
        return await _query(lambda cur: fetch_starter_calendar(cur, settings, month=first))

    @router.get("/api/starters/popular", dependencies=[Depends(require_admin)])
    async def admin_starter_popular(
        days: int = Query(
            default=settings.starter_uses_days, ge=1, le=settings.admin_stats_max_days
        ),
        excluded: tuple[str, ...] = scope,
    ) -> Any:
        """인기 질문 — 칩이 아닌 첫 질문 중 자주 나온 것(칩 후보). 기간 선택지는 설정값."""
        result = await _query(
            lambda cur: fetch_starter_popular(cur, settings, days=days, excluded=excluded)
        )
        return {**result, "day_options": list(settings.starter_popular_day_options)}

    @router.get("/api/analytics", dependencies=[Depends(require_admin)])
    async def admin_analytics(
        period: tuple[date | None, date | None] = Depends(date_range),
        excluded: tuple[str, ...] = scope,
    ) -> Any:
        return await _query(fetch_analytics, settings, period, excluded)

    @router.get("/api/audit", dependencies=[Depends(require_owner)])
    async def admin_audit(
        page: int = page_query,
        target: Literal[("", "all", *AUDIT_TARGETS)] = "",  # type: ignore[valid-type]
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
    async def admin_cost(
        period: tuple[date | None, date | None] = Depends(date_range),
        excluded: tuple[str, ...] = scope,
    ) -> Any:
        """비용 패널(owner 전용). 대시보드 응답에는 비용 필드가 없다."""
        return await _query(fetch_cost, settings, period, excluded)

    @router.get("/api/stats", dependencies=[Depends(require_admin)])
    async def admin_stats(since: date, until: date, excluded: tuple[str, ...] = scope) -> Any:
        """통계 탭(KST 일별 보고). 빈 날도 채우므로 기간이 필수이고 길이에 상한이 있다."""
        date_range(since, until)
        if (until - since).days >= settings.admin_stats_max_days:
            raise HTTPException(
                status_code=422,
                detail=f"통계 기간은 최대 {settings.admin_stats_max_days}일입니다.",
            )
        return await _query(fetch_stats, settings, (since, until), excluded)

    app.include_router(router)
