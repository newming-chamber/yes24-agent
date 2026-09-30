"""운영 데이터 조회와 집계. 공개할 테이블·열을 명세하고 값은 전부 바인딩한다."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from pymysql.constants import FIELD_TYPE
from pymysql.converters import decoders

from yes24_agent.admin_cost import (
    attach_costs,
    cost_notes,
    per_turn,
    total,
)
from yes24_agent.config import Settings

# admin 접속(읽기 전용 조회·AdminService 풀) 공통 디코더 — BOOLEAN 열을 bool로 싣는다.
# 열 이름이 아니라 결과 열 타입(TINY)으로 판정하므로 뷰·조인을 거쳐도 같다. 파생값(SUM(a=1)
# DECIMAL, (1=1)·a+0 LONGLONG)은 TINY가 아니라 int로 남는다(격리 MySQL 실측). 스키마의
# TINYINT 열은 전부 BOOLEAN(TINYINT(1))이다 — 수치 TINYINT 열이 생기면 이 판정을 다시 볼 것.
BOOL_DECODERS = {**decoders, FIELD_TYPE.TINY: lambda value: value != "0"}


def _nickname(keys: str, match: str) -> str:
    """닉네임 스칼라 서브쿼리 — keys(auth_keys 별칭 nick_k를 포함한 FROM 절) 중 match에 맞는 키.

    닉네임은 그 회원의 auth_keys 중 가장 최근에 관측한(user_cached_at, 없으면 created_at)
    raw_user_info의 nickNm 문자열이다(Yes24 회원 조회 응답 원문). nickNm이 로그인 아이디(userId)와
    같으면 닉네임으로 인정하지 않는다(dev 실측 24% — 로그인 아이디 비노출 원칙). userId는 이
    비교에만 쓰고 결과로는 싣지 않는다. 목록 한 문장 안의 상관 서브쿼리라 왕복은 늘지 않는다.
    """
    nick = "JSON_UNQUOTE(JSON_EXTRACT(nick_k.raw_user_info, '$.nickNm'))"
    login = "JSON_UNQUOTE(JSON_EXTRACT(nick_k.raw_user_info, '$.userId'))"
    return (
        f"(SELECT {nick} FROM {keys} WHERE {match} "
        "AND JSON_TYPE(JSON_EXTRACT(nick_k.raw_user_info, '$.nickNm')) = 'STRING' "
        f"AND {nick} <> '' AND NOT {nick} <=> {login} "
        "ORDER BY COALESCE(nick_k.user_cached_at, nick_k.created_at) DESC, nick_k.id DESC LIMIT 1)"
    )


def nickname_sql(user_no: str) -> str:
    """회원번호(user_no) 식 → 닉네임(없으면 NULL). 회원번호만 아는 곳(대화·비용)에서 쓴다.

    안쪽 별칭은 바깥 질의와 겹치지 않게 고유하게 둔다 — 바깥이 users u면 u.user_no = u.user_no가
    안쪽으로 가려져 항상 참이 된다(전 회원 중 최신 닉네임이 붙던 실측 결함).
    """
    return _nickname(
        "users nick_u JOIN auth_keys nick_k ON nick_k.user_id = nick_u.id",
        f"nick_u.user_no = {user_no}",
    )


def nickname_by_user_pk_sql(user_pk: str) -> str:
    """회원 행 id(users.id) 식 → 닉네임. 회원 행을 이미 읽는 곳은 users를 다시 찾지 않는다."""
    return _nickname("auth_keys nick_k", f"nick_k.user_id = {user_pk}")


def with_nickname(row: dict[str, Any]) -> dict[str, Any]:
    """표시용 닉네임 — 모르면 회원번호 그대로(빈칸·추측 금지)."""
    return {**row, "nickname": row.get("nickname") or row["user_id"]}


# 활성 사용자 = 실제로 대화한 고유 사용자. overview(누적)·대시보드(기간)·통계가 같은 정의다.
ACTIVE_USERS = "COUNT(DISTINCT app_name, user_id)"


def jsonable(row: dict[str, Any], json_columns: tuple[str, ...] = ()) -> dict[str, Any]:
    """행을 응답 dict로 — 시각은 epoch, 날짜는 ISO, 집계(Decimal)는 수, json_columns는 구조."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            value = value.replace(tzinfo=timezone.utc).timestamp()
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = int(value) if value == value.to_integral_value() else float(value)
        elif key in json_columns and isinstance(value, (str, bytes)):
            value = json.loads(value)
        out[key] = value
    return out


def date_range(
    since: date | None = None, until: date | None = None
) -> tuple[date | None, date | None]:
    if since and until and since > until:
        raise HTTPException(status_code=422, detail="시작일은 종료일보다 늦을 수 없습니다.")
    return since, until


# ── 어드민 표시 시간대 ─────────────────────────────────────────────────────
# 저장 시각은 전부 UTC다(chat_turn·usage_log DATETIME, turn_click·turn_feedback TIMESTAMP는 접속
# time_zone '+00:00'이라 UTC로 읽힌다). 사람이 보는 날짜 경계·일별 묶음은 어드민 시간대(설정
# admin_utc_offset_hours, 기본 KST)다. 경계는 열이 아니라 인자 쪽을 옮긴다(열을 함수로 감싸지 않음).
# 오프셋은 호출부가 받은 settings에서 넘긴다(여기서 전역 설정을 다시 읽지 않는다 — 출처 하나).


def local_time(column: str, offset_hours: int) -> str:
    """열의 UTC 시각 → 어드민 시간대 시각(SQL 식)."""
    return f"({column} + INTERVAL {int(offset_hours)} HOUR)"


def local_day(column: str, offset_hours: int) -> str:
    """열의 어드민 시간대 날짜(일별 묶음 식)."""
    return f"DATE{local_time(column, offset_hours)}"


def period_filter(
    column: str,
    period: tuple[date | None, date | None],
    offset_hours: int,
) -> tuple[list[str], list[Any]]:
    """어드민 시간대 달력일 [since, until] 조건. until은 그날 하루를 통째로 포함한다."""
    since, until = period
    shift = f" - INTERVAL {int(offset_hours)} HOUR"
    where, params = [], []
    if since:
        where.append(f"{column} >= %s{shift}")
        params.append(since)
    if until and until < date.max:
        where.append(f"{column} < %s + INTERVAL 1 DAY{shift}")
        params.append(until)
    return where, params


def sql_where(conditions: list[str]) -> str:
    return " WHERE " + " AND ".join(conditions) if conditions else ""


# usage.py 계약: 턴당 1행(그 턴의 LLM 콜 합산)인 행의 component. 나머지는 콜당 1행인 서브콜이다.
MAIN_COMPONENT = "main"
# 과금 토큰 세 갈래의 합(admin_cost 모듈 docstring). 측정 불성립 행은 합에서 빼고 행 수로만 남긴다.
_MEASURED = "prompt_tokens IS NOT NULL AND total_tokens >= prompt_tokens"
_USAGE_SUMS = (
    f"COUNT(*) AS `rows`, COUNT(CASE WHEN {_MEASURED} THEN 1 END) AS measured_rows, "
    f"SUM(CASE WHEN {_MEASURED} THEN prompt_tokens - COALESCE(cached_tokens, 0) END) "
    "AS input_uncached, "
    f"SUM(CASE WHEN {_MEASURED} THEN COALESCE(cached_tokens, 0) END) AS input_cached, "
    f"SUM(CASE WHEN {_MEASURED} THEN total_tokens - prompt_tokens END) AS output_billed, "
    f"COUNT(CASE WHEN {_MEASURED} AND cached_tokens IS NULL THEN 1 END) AS cache_unknown_rows"
)
# 답변거절 = 답을 내지 못하고 끝난 턴(실패·중단). unknown은 종료 기록 누락이라 세지 않는다.
# 대시보드 '답변거절률'·통계 '답변거절수'·대화 목록 배지가 같은 정의를 쓴다. SQL은 상태별 수를
# 상태 이름 열로 세고, 합계와 세부는 refusal_breakdown이 만든다(화면이 실패·중단을 나눠 보인다).
REFUSED = ("failed", "interrupted")
# 피드백 평가별 수 — 대시보드(기간 합계)와 통계 '피드백'(일별)이 같은 식을 쓴다.
# 시각 기준은 updated_at(최신 평가).
FEEDBACK_COUNTS = (
    "COUNT(CASE WHEN rating = 'up' THEN 1 END) AS likes, "
    "COUNT(CASE WHEN rating = 'down' THEN 1 END) AS dislikes"
)
_REFUSALS = ", ".join(
    f"COUNT(CASE WHEN status = '{status}' THEN 1 END) AS {status}" for status in REFUSED
)


def refusal_breakdown(rows) -> dict[str, Any]:
    """상태별 거절 수 열(REFUSED) → 합계 refusals와 세부 refused({상태: 수})."""
    refused = {status: sum(int(row[status]) for row in rows) for status in REFUSED}
    return {"refusals": sum(refused.values()), "refused": refused}


_LATENCY_QUANTILES = ", ".join(
    f"MAX(CASE WHEN rank_no = CEIL(samples * {share}) THEN elapsed_ms END) / 1000 AS {alias}"
    for share, alias in ((0.5, "p50_seconds"), (0.95, "p95_seconds"))
)


def _ranked_turns(columns: str, where: str, partition: str = "") -> str:
    """응답시간 측정 행끼리 순위를 매긴 chat_turns(`rank_no`·`samples`) — 분위수 서브쿼리.

    NULL 행은 `elapsed_ms IS NULL` 파티션으로 따로 묶여 순위를 나눠 갖지 않는다.
    """
    lead = f"{partition}, " if partition else ""
    samples = f"PARTITION BY {partition}" if partition else ""
    return (
        f"SELECT {columns}, elapsed_ms, ROW_NUMBER() OVER "
        f"(PARTITION BY {lead}elapsed_ms IS NULL ORDER BY elapsed_ms) AS rank_no, "
        f"COUNT(elapsed_ms) OVER ({samples}) AS samples FROM chat_turns{where}"
    )


def _grouped(rows, *keys: str) -> dict[tuple, list]:
    groups: dict[tuple, list] = {}
    for row in rows:
        groups.setdefault(tuple(row[key] for key in keys), []).append(row)
    return groups


def _sums(rows, keys: tuple[str, ...]) -> dict[str, int]:
    return {key: sum(int(row[key]) for row in rows) for key in keys}


def _periods(settings: Settings, period: tuple[date | None, date | None]):
    """(이번 기간 표기, 비교 기간|None, 기간 표기 함수, 두 기간을 잇는 조회 범위).

    비교 기간 = 바로 앞의 같은 길이 기간.
    """
    since, until = period
    previous = None
    if since and until:
        length = (until - since).days + 1
        if (since - date.min).days >= length:
            previous = (since - timedelta(days=length), since - timedelta(days=1))

    def stamp(start, end) -> dict[str, Any]:
        return {"since": start, "until": end, "timezone": settings.admin_timezone_label}

    return stamp(since, until), previous, stamp, ((previous[0], until) if previous else period)


def _split(rows, since, previous, key: str = "day") -> tuple[list, list]:
    """(이번 기간, 비교 기간) — 비교 기간이 없으면 전부 이번 기간이다. key = 표시 날짜 열."""
    if previous is None:
        return list(rows), []
    return [r for r in rows if r[key] >= since], [r for r in rows if r[key] < since]


async def _fetch(cur, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    await cur.execute(sql, params)
    return list(await cur.fetchall())


async def fetch_analytics(
    cur, settings: Settings, period: tuple[date | None, date | None]
) -> dict[str, Any]:
    """대시보드(모든 역할) — 사용량·품질·시간대. 비용·토큰·내부 과정은 싣지 않는다(비용은 owner 전용
    fetch_cost). 모든 문장이 호출부(_query)의 같은 읽기 전용 스냅샷에서 돈다.

    비교 기간이 있으면 날짜로 가를 수 있는 집계(피드백·클릭)는 두 기간을 한 문장으로 읽어 날짜로
    나눈다. 날짜 경계·일별 묶음은 어드민 시간대다.
    """
    since, _ = period
    hours = settings.admin_utc_offset_hours
    current, previous, stamp, spanned = _periods(settings, period)

    async def turn_summary(selected) -> dict[str, Any]:
        where, params = period_filter("asked_at", selected, hours)
        [row] = await _fetch(
            cur,
            f"SELECT COUNT(*) AS turns, {ACTIVE_USERS} AS users, {_REFUSALS}, "
            f"{_LATENCY_QUANTILES} FROM ("
            + _ranked_turns("app_name, user_id, status", sql_where(where))
            + ") ranked",
            params,
        )
        return {**jsonable(row), **refusal_breakdown([row])}

    turn_where, turn_params = period_filter("asked_at", period, hours)
    daily = await _fetch(
        cur,
        f"SELECT day, {_LATENCY_QUANTILES} FROM ("
        + _ranked_turns(
            f"{local_day('asked_at', hours)} AS day",
            sql_where(turn_where),
            local_day("asked_at", hours),
        )
        + ") ranked GROUP BY day ORDER BY day",
        turn_params,
    )
    asked = local_time("asked_at", hours)
    hourly = await _fetch(
        cur,
        f"SELECT HOUR{asked} AS hour, COUNT(*) AS turns "
        f"FROM chat_turns{sql_where(turn_where)} GROUP BY hour ORDER BY hour",
        turn_params,
    )
    feedback_where, feedback_params = period_filter("updated_at", spanned, hours)
    click_where, click_params = period_filter("created_at", spanned, hours)
    engagement = await _fetch(
        cur,
        f"SELECT {local_day('updated_at', hours)} AS day, {FEEDBACK_COUNTS}, 0 AS clicks "
        f"FROM turn_feedback{sql_where(feedback_where)} GROUP BY day "
        f"UNION ALL SELECT {local_day('created_at', hours)} AS day, 0, 0, COUNT(*) "
        f"FROM turn_click{sql_where(click_where)} GROUP BY day",
        [*feedback_params, *click_params],
    )
    now, before = _split(engagement, since, previous)
    engaged = ("likes", "dislikes", "clicks")
    comparison = None
    if previous:
        comparison = {
            "period": stamp(*previous),
            "summary": {**await turn_summary(previous), **_sums(before, engaged)},
        }
    return {
        "period": current,
        "summary": {**await turn_summary(period), **_sums(now, engaged)},
        "comparison": comparison,
        "daily": [jsonable(row) for row in daily],
        "hourly": [jsonable(row) for row in hourly],
    }


async def fetch_cost(
    cur, settings: Settings, period: tuple[date | None, date | None]
) -> dict[str, Any]:
    """비용 패널(owner 전용 라우트) — 기간·비교 기간 비용, 일별 비용, 사용자별 비용 상위, 각주.

    금액은 SQL이 아니라 admin_cost가 행의 **UTC 날짜**(`day`)에 유효한 단가로 붙인다 — 단가
    유효일은 과금 사실의 기준일이라 표시 시간대를 바꿔도 금액이 흔들리지 않게 UTC로 둔다. 표시
    날짜는 `local_day`다. 모델명은 싣지 않는다(모델별 단가는 행에 붙여 합산만 한다).
    """
    since, _ = period
    hours = settings.admin_utc_offset_hours
    current, previous, stamp, spanned = _periods(settings, period)
    usage_where, usage_params = period_filter("created_at", spanned, hours)
    usage = attach_costs(
        await _fetch(
            cur,
            f"SELECT DATE(created_at) AS day, {local_day('created_at', hours)} AS local_day, "
            f"model, component, {_USAGE_SUMS} FROM usage_log{sql_where(usage_where)} "
            "GROUP BY day, local_day, model, component",
            usage_params,
        ),
        settings.llm_prices,
    )
    # 사용자 귀속은 main 행뿐이다(서브콜은 턴 문맥이 없을 수 있다).
    user_where, user_params = period_filter("created_at", period, hours)
    user_usage = attach_costs(
        await _fetch(
            cur,
            f"SELECT user_id, DATE(created_at) AS day, model, {_USAGE_SUMS} "
            f"FROM usage_log{sql_where([*user_where, 'component = %s', 'user_id IS NOT NULL'])} "
            "GROUP BY user_id, day, model",
            [*user_params, MAIN_COMPONENT],
        ),
        settings.llm_prices,
    )

    def summary_of(rows) -> dict[str, Any]:
        main = total(r for r in rows if r["component"] == MAIN_COMPONENT)
        return {
            # 과금 턴 — 단가가 적용되고 측정된 main 행. 비용의 분모는 이것뿐이다(분자와 같은 모집단:
            # 대화 삭제는 chat_turn만 지우고, 미등록·미측정 행은 금액에 없으니 분모에도 없다).
            "priced_rows": main["priced_rows"],
            "cost_usd": total(rows)["cost_usd"],
            "cost_per_turn_usd": per_turn(main["cost_usd"], main["priced_rows"]),
        }

    now, before = _split(usage, since, previous, "local_day")
    daily = [
        # 단가 미등록 모델만 있는 날은 None(금액을 모름) — 0으로 바꾸지 않는다.
        {"day": day, "cost_usd": total(rows)["cost_usd"]}
        for (day,), rows in sorted(_grouped(now, "local_day").items())
    ]
    user_totals = {key: total(rows) for key, rows in _grouped(user_usage, "user_id").items()}
    users = [
        {
            "user_id": user_id,
            "priced_rows": group["priced_rows"],
            "cost_usd": group["cost_usd"],
            "cost_per_turn_usd": per_turn(group["cost_usd"], group["priced_rows"]),
        }
        for (user_id,), group in user_totals.items()
    ]
    # 비용 내림차순, 단가 미등록(None)은 뒤로, 동률은 사용자 id순.
    users.sort(key=lambda u: (u["cost_usd"] is None, -(u["cost_usd"] or 0), u["user_id"]))
    users = users[: settings.admin_top_users]
    if users:
        # 표에 실을 사용자들의 닉네임을 한 문장으로(사용자마다 따로 묻지 않는다).
        ids = [u["user_id"] for u in users]
        marks = ", ".join(["%s"] * len(ids))
        nicknames = {
            row["user_no"]: row["nickname"]
            for row in await _fetch(
                cur,
                f"SELECT u.user_no, {nickname_sql('u.user_no')} AS nickname FROM users u "
                f"WHERE u.user_no IN ({marks})",
                ids,
            )
        }
        users = [with_nickname({**u, "nickname": nicknames.get(u["user_id"])}) for u in users]
    turn_where, turn_params = period_filter("asked_at", period, hours)
    [turns] = await _fetch(
        cur, f"SELECT COUNT(*) AS turns FROM chat_turns{sql_where(turn_where)}", turn_params
    )
    coverage = total(now)
    main_rows = total(r for r in now if r["component"] == MAIN_COMPONENT)["rows"]
    return {
        "period": current,
        "currency": {
            "ledger": "USD",
            "krw_per_usd": settings.krw_per_usd,
            "krw_as_of": settings.krw_per_usd_as_of,
        },
        "summary": summary_of(now),
        "comparison": {"period": stamp(*previous), "summary": summary_of(before)}
        if previous
        else None,
        "daily": daily,
        "users": users,
        "notes": cost_notes(coverage, int(turns["turns"]), main_rows),
    }


_WEEKDAYS = "월화수목금토일"  # date.weekday() 순서


def _stats(session_days, clicks: int) -> dict[str, Any]:
    """(날짜, 세션) 행 묶음 → 보고 지표. 비율은 합계끼리 나눈다(일별 비율의 평균이 아니다)."""
    sessions = {(r["app_name"], r["user_id"], r["session_id"]) for r in session_days}
    queries = sum(int(r["queries"]) for r in session_days)
    links = sum(int(r["links"] or 0) for r in session_days)
    seconds = [float(r["seconds"]) for r in session_days]
    return {
        "sessions": len(sessions),
        "users": len({(r["app_name"], r["user_id"]) for r in session_days}),
        "queries": queries,
        "queries_per_session": queries / len(sessions) if sessions else None,
        # 세션-일 단위 평균(턴 가중 아님). 한 턴 세션은 그 답변 시간이다.
        "avg_session_seconds": sum(seconds) / len(seconds) if seconds else None,
        **refusal_breakdown(session_days),
        "links": links,
        "clicks": clicks,
        "click_rate": clicks / links * 100 if links else None,
    }


async def fetch_stats(cur, settings: Settings, period: tuple[date, date]) -> dict[str, Any]:
    """통계 탭 — 어드민 시간대 일별 세션·질의·링크·클릭(고객사 엑셀 11열) + 기간 요약 + 요일 평균
    + RBTI 유형별 질의 수(적용률은 화면이 요약 질의 수로 나눈다 — 일별 표·엑셀 양식에는 넣지
    않는다).

    SQL은 (날짜, 세션)까지만 묶고 나머지는 여기서 센다. 기간 요약은 일별 값을 더하거나
    평균하지 않고 같은 행에서 다시 센다 — 이틀에 걸친 세션·여러 날 온 사용자는 기간에 한 번이다.
    기록 없는 날도 0행으로 채운다. 일별 표는 최신 날짜가 먼저다.
    """
    hours = settings.admin_utc_offset_hours
    where, params = period_filter("started_at", period, hours)
    await cur.execute(
        f"SELECT {local_day('started_at', hours)} AS day, app_name, user_id, session_id, "
        "COUNT(*) AS queries, "
        f"{_REFUSALS}, "
        # sources는 그 턴에 공개(인용)된 출처 배열이다(chat_turn.sql ck_chat_turn_sources).
        "SUM(JSON_LENGTH(sources)) AS links, "
        "TIMESTAMPDIFF(MICROSECOND, MIN(started_at), MAX(completed_at)) / 1000000 AS seconds "
        f"FROM chat_turn{sql_where(where)} GROUP BY day, app_name, user_id, session_id",
        params,
    )
    session_days = await cur.fetchall()
    by_day = {key: rows for (key,), rows in _grouped(session_days, "day").items()}
    where, params = period_filter("created_at", period, hours)
    await cur.execute(
        f"SELECT {local_day('created_at', hours)} AS day, COUNT(*) AS clicks "
        f"FROM turn_click{sql_where(where)} GROUP BY day",
        params,
    )
    clicks = {row["day"]: int(row["clicks"]) for row in await cur.fetchall()}
    # RBTI 유형별 질의 수(RBTI가 적용된 턴만, 많은 순) — 유형 목록은 데이터에 나온 코드뿐이다.
    where, params = period_filter("started_at", period, hours)
    await cur.execute(
        "SELECT rbti_applied AS rbti, COUNT(*) AS turns "
        f"FROM chat_turn{sql_where([*where, 'rbti_applied IS NOT NULL'])} "
        "GROUP BY rbti_applied ORDER BY turns DESC, rbti",
        params,
    )
    rbti = [jsonable(row) for row in await cur.fetchall()]
    # 피드백 — 일별 좋아요·싫어요(최신 평가 시각 기준)와 의견이 달린 최근 피드백(질문 앞부분과
    # 함께, 한 화면 분량 = admin_page_size).
    where, params = period_filter("updated_at", period, hours)
    await cur.execute(
        f"SELECT {local_day('updated_at', hours)} AS day, {FEEDBACK_COUNTS} "
        f"FROM turn_feedback{sql_where(where)} GROUP BY day",
        params,
    )
    feedback_daily = [jsonable(row) for row in await cur.fetchall()]
    where, params = period_filter("f.updated_at", period, hours)
    where.append("TRIM(f.comment) <> ''")  # NULL도 여기서 빠진다
    await cur.execute(
        "SELECT f.updated_at, f.rating, f.comment, f.user_id, f.session_id, "
        "LEFT(t.user_text, %s) AS question FROM turn_feedback f "
        "LEFT JOIN chat_turn t ON t.app_name = f.app_name AND t.user_id = f.user_id "
        "AND t.session_id = f.session_id AND t.turn_id = f.turn_id"
        f"{sql_where(where)} "
        "ORDER BY f.updated_at DESC, f.id DESC LIMIT %s",
        [settings.admin_preview_max_chars, *params, settings.admin_page_size],
    )
    comments = [jsonable(row) for row in await cur.fetchall()]

    since, until = period
    days = [until - timedelta(days=i) for i in range((until - since).days + 1)]
    daily = [
        {"day": d, "weekday": _WEEKDAYS[d.weekday()], **_stats(by_day.get(d, []), clicks.get(d, 0))}
        for d in days
    ]
    weekday = []
    for label in "일월화수목금토":  # 보고 양식 순서
        rows = [row for row in daily if row["weekday"] == label]
        weekday.append(
            {
                "weekday": label,
                "days": len(rows),
                **{
                    key: sum(row[key] for row in rows) / len(rows) if rows else None
                    for key in ("sessions", "queries", "clicks")
                },
            }
        )
    return {
        "period": {"since": since, "until": until, "timezone": settings.admin_timezone_label},
        "summary": _stats(session_days, sum(clicks.values())),
        "daily": daily,
        "weekday": weekday,
        "rbti": rbti,
        "feedback": {"daily": feedback_daily, "comments": comments},
    }
