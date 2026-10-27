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
    price_for,
    total,
)
from yes24_agent.config import Settings
from yes24_agent.model_routing import (
    AUDIT_TARGET as MODEL_AUDIT_TARGET,
)
from yes24_agent.model_routing import (
    SETTINGS_COLUMNS,
    SETTINGS_ROW_ID,
    cooldowns,
    default_effort,
    effective_choice,
    per_model_targets,
)

# admin 접속(읽기 전용 조회·AdminService 풀) 공통 디코더 — BOOLEAN 열을 bool로 싣는다.
# 열 이름이 아니라 결과 열 타입(TINY)으로 판정하므로 뷰·조인을 거쳐도 같다. 파생값(SUM(a=1)
# DECIMAL, (1=1)·a+0 LONGLONG)은 TINY가 아니라 int로 남는다(격리 MySQL 실측). TINYINT 열은
# 0/1이면 bool, 그 밖의 값은 수로 남긴다 — starters.pinned는 0/1/2(없음·분야 고정·첫 화면 고정,
# starters.PIN_*)라 2를 True로 접으면 첫 화면 고정이 분야 고정으로 읽힌다(True == 1 == PIN_SLOT).
BOOL_DECODERS = {
    **decoders, FIELD_TYPE.TINY: lambda value: value == "1" if value in ("0", "1") else int(value)
}


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


def excluded_condition(column: str, excluded: tuple[str, ...]) -> tuple[list[str], list[Any]]:
    """내부·테스트 계정 제외(설정 admin_excluded_user_ids) — 조건과 인자. 비면 조건 없음.

    NULL은 남긴다: usage_log의 서브콜 행은 user_id가 없고, NOT IN만 쓰면 NULL 행이 통째로 빠져
    비용 합계가 줄어든다.
    """
    if not excluded:
        return [], []
    marks = ", ".join(["%s"] * len(excluded))
    return [f"({column} IS NULL OR {column} NOT IN ({marks}))"], list(excluded)


def period_filter(
    column: str,
    period: tuple[date | None, date | None],
    offset_hours: int,
    excluded: tuple[str, ...] = (),
    user_column: str = "user_id",
) -> tuple[list[str], list[Any]]:
    """어드민 집계 범위 — 어드민 시간대 달력일 [since, until](until은 그날 하루를 통째로 포함)
    + 제외 계정(excluded가 있으면 user_column 기준, excluded_condition)."""
    since, until = period
    shift = f" - INTERVAL {int(offset_hours)} HOUR"
    where, params = [], []
    if since:
        where.append(f"{column} >= %s{shift}")
        params.append(since)
    if until and until < date.max:
        where.append(f"{column} < %s + INTERVAL 1 DAY{shift}")
        params.append(until)
    excluding, excluded_params = excluded_condition(user_column, excluded)
    return [*where, *excluding], [*params, *excluded_params]


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
    f"COUNT(CASE WHEN {_MEASURED} AND cached_tokens IS NULL THEN 1 END) AS cache_unknown_rows, "
    # 검색 쿼리는 토큰 측정 여부와 독립인 과금 단위라 측정 조건 밖에서 합한다(NULL = 0).
    "SUM(search_queries) AS search_queries"
)
# 질의 수 = 측정된 main 행(묶음의 main_rows) 중 단가가 붙은 것. 단가는 component와 무관해 비용
# 집계는 component로 나누지 않는다 — 나누면 운영 30일 사용자별에 22만 묶음이 넘어와 8초가
# 걸렸다(10-26).
_MAIN_ROWS = (
    f"COUNT(CASE WHEN component = '{MAIN_COMPONENT}' AND {_MEASURED} THEN 1 END) AS main_rows"
)


def _priced_turns(rows) -> int:
    return sum(int(row["main_rows"]) for row in rows if row["cost_usd"] is not None)


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

    NULL 행은 `elapsed_ms IS NULL` 파티션으로 따로 묶여 순위를 나눠 갖지 않는다. columns는 별칭
    있는 식이면 그 별칭으로 partition을 준다.

    쓸 열만 먼저 물질화한다(NO_MERGE) — 뷰가 병합되면 윈도 정렬이 process JSON이 든 원래 행을
    끌고 다녀 운영 10만 턴에 5~6초가 걸렸다(물질화 후 0.4초, 결과 동일).
    """
    lead = f"{partition}, " if partition else ""
    samples = f"PARTITION BY {partition}" if partition else ""
    return (
        f"SELECT /*+ NO_MERGE(measured) */ *, ROW_NUMBER() OVER "
        f"(PARTITION BY {lead}elapsed_ms IS NULL ORDER BY elapsed_ms) AS rank_no, "
        f"COUNT(elapsed_ms) OVER ({samples}) AS samples "
        f"FROM (SELECT {columns}, elapsed_ms FROM chat_turns{where}) measured"
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
    cur,
    settings: Settings,
    period: tuple[date | None, date | None],
    excluded: tuple[str, ...] = (),
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
        where, params = period_filter("asked_at", selected, hours, excluded)
        [row] = await _fetch(
            cur,
            f"SELECT COUNT(*) AS turns, {ACTIVE_USERS} AS users, {_REFUSALS}, "
            f"{_LATENCY_QUANTILES} FROM ("
            + _ranked_turns("app_name, user_id, status", sql_where(where))
            + ") ranked",
            params,
        )
        return {**jsonable(row), **refusal_breakdown([row])}

    turn_where, turn_params = period_filter("asked_at", period, hours, excluded)
    daily = await _fetch(
        cur,
        f"SELECT day, {_LATENCY_QUANTILES} FROM ("
        + _ranked_turns(f"{local_day('asked_at', hours)} AS day", sql_where(turn_where), "day")
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
    feedback_where, feedback_params = period_filter("updated_at", spanned, hours, excluded)
    click_where, click_params = period_filter("created_at", spanned, hours, excluded)
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
    cur,
    settings: Settings,
    period: tuple[date | None, date | None],
    excluded: tuple[str, ...] = (),
) -> dict[str, Any]:
    """비용 패널(owner 전용 라우트) — 기간·비교 기간 비용, 일별 비용, 사용자별 비용 상위, 각주.

    금액은 SQL이 아니라 admin_cost가 행의 **UTC 날짜**(`day`)에 유효한 단가로 붙인다 — 단가
    유효일은 과금 사실의 기준일이라 표시 시간대를 바꿔도 금액이 흔들리지 않게 UTC로 둔다. 표시
    날짜는 `local_day`다. 모델명은 싣지 않는다(모델별 단가는 행에 붙여 합산만 한다).
    """
    since, _ = period
    hours = settings.admin_utc_offset_hours
    current, previous, stamp, spanned = _periods(settings, period)
    usage_where, usage_params = period_filter("created_at", spanned, hours, excluded)
    usage = attach_costs(
        await _fetch(
            cur,
            # background = 사용자에 귀속되지 않은 행(초기 질문 생성 등 대화 밖 작업) — 열 값으로.
            f"SELECT DATE(created_at) AS day, {local_day('created_at', hours)} AS local_day, "
            f"model, service_tier, user_id IS NULL AS background, {_USAGE_SUMS}, {_MAIN_ROWS} "
            f"FROM usage_log{sql_where(usage_where)} "
            "GROUP BY day, local_day, model, service_tier, background",
            usage_params,
        ),
        settings.llm_prices,
    )
    # 사용자별 = 그 사용자에 귀속된 행 전부(대화 비용 — 요약 카드와 같은 정의), 질의는 main 행.
    user_where, user_params = period_filter("created_at", period, hours, excluded)
    user_usage = attach_costs(
        await _fetch(
            cur,
            f"SELECT user_id, DATE(created_at) AS day, model, service_tier, {_USAGE_SUMS}, "
            f"{_MAIN_ROWS} "
            f"FROM usage_log{sql_where([*user_where, 'user_id IS NOT NULL'])} "
            "GROUP BY user_id, day, model, service_tier",
            user_params,
        ),
        settings.llm_prices,
    )

    def summary_of(rows) -> dict[str, Any]:
        turns = _priced_turns(rows)
        chat = total(r for r in rows if not r["background"])
        return {
            # 질의(분모) — 단가가 적용되고 측정된 main 행(대화 삭제는 chat_turn만 지우고,
            # 미등록·미측정 행은 금액에 없으니 분모에도 없다). 분자는 대화 비용(사용자 귀속 행 전부
            # — main과 그 턴의 서브콜), 대화 외 작업은 빼서 화면 정의 "대화 비용 ÷ 질의 수"와 같다.
            "priced_rows": turns,
            "cost_usd": total(rows)["cost_usd"],
            "cost_per_turn_usd": per_turn(chat["cost_usd"], turns),
        }

    now, before = _split(usage, since, previous, "local_day")
    daily = [
        # 단가 미등록 모델만 있는 날은 None(금액을 모름) — 0으로 바꾸지 않는다. background_usd =
        # 그중 대화 밖 작업(사용자 귀속 없음) 몫 — 질의 없는 날에도 비용이 있는 이유를 나눠 보인다.
        {"day": day, "cost_usd": total(rows)["cost_usd"],
         "background_usd": total(r for r in rows if r["background"])["cost_usd"]}
        for (day,), rows in sorted(_grouped(now, "local_day").items())
    ]
    users = []
    for (user_id,), rows in _grouped(user_usage, "user_id").items():
        cost = total(rows)["cost_usd"]
        turns = _priced_turns(rows)
        users.append({"user_id": user_id, "priced_rows": turns, "cost_usd": cost,
                      "cost_per_turn_usd": per_turn(cost, turns)})
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
        "notes": cost_notes(total(now)),
    }


async def fetch_model_panel(cur, settings: Settings) -> dict[str, Any]:
    """모델 설정 패널(owner 전용 라우트) — 유효 모델·출처, 모델별 턴·폴백·비용, 최근 변경 이력.

    유효 모델은 서빙과 같은 판정(model_routing.effective_choice)을 DB 원본 행에 다시 적용한 값이다
    — 다른 인스턴스의 캐시는 model_settings_ttl_s 안에 이 값으로 수렴한다. 쿨다운은 이 응답을 만든
    프로세스의 상태뿐이다(서버·컨테이너마다 따로 있다). 창(admin_model_window_hours)은 지금부터
    거꾸로 센 시간이라 달력일 집계(통계 탭)와 다르다. 턴 수·폴백 수는 chat_turn.model 기준이고
    열 도입 전 턴은 model이 NULL인 한 행으로 모인다. 비용은 그 모델이 답한 main 행(usage_log)만이다.
    """
    windows = list(settings.admin_model_window_hours)
    spans = range(len(windows))
    rows = await _fetch(
        cur,
        f"SELECT {', '.join(SETTINGS_COLUMNS)}, updated_at FROM model_settings WHERE id = %s",
        [SETTINGS_ROW_ID],
    )
    stored = rows[0] if rows else None
    choice = effective_choice(
        settings, {key: stored[key] for key in SETTINGS_COLUMNS} if stored else None
    )

    in_window = "started_at >= NOW(6) - INTERVAL %s HOUR"
    turns = {
        row["model"]: row
        for row in await _fetch(
            cur,
            "SELECT model, "
            + ", ".join(
                f"SUM({in_window}) AS turns_{i}, "
                f"SUM(fallback_from IS NOT NULL AND {in_window}) AS fallbacks_{i}"
                for i in spans
            )
            + f" FROM chat_turn WHERE {in_window} GROUP BY model",
            [*(hours for hours in windows for _ in range(2)), max(windows)],
        )
    }
    flags = ", ".join(f"created_at >= NOW(3) - INTERVAL %s HOUR AS w{i}" for i in spans)
    usage = attach_costs(
        await _fetch(
            cur,
            f"SELECT DATE(created_at) AS day, model, service_tier, {flags}, {_USAGE_SUMS} "
            "FROM usage_log WHERE component = %s AND created_at >= NOW(3) - INTERVAL %s HOUR "
            f"GROUP BY day, model, service_tier, {', '.join(f'w{i}' for i in spans)}",
            [*windows, MAIN_COMPONENT, max(windows)],
        ),
        settings.llm_prices,
    )
    models = [
        {
            "model": model,
            "turns": [int((turns.get(model) or {}).get(f"turns_{i}") or 0) for i in spans],
            "fallbacks": [int((turns.get(model) or {}).get(f"fallbacks_{i}") or 0) for i in spans],
            "cost_usd": [
                total(r for r in usage if r["model"] == model and r[f"w{i}"])["cost_usd"]
                for i in spans
            ],
            # 처리 등급별 분해(""= 표준) — 등급이 섞인 모델(fast 전환 전후)의 비용 근거.
            "cost_by_tier": [
                {
                    "service_tier": tier,
                    "cost_usd": [
                        total(
                            r for r in usage
                            if r["model"] == model and (r["service_tier"] or "") == tier
                            and r[f"w{i}"]
                        )["cost_usd"]
                        for i in spans
                    ],
                }
                for tier in sorted(
                    {r["service_tier"] or "" for r in usage if r["model"] == model}
                )
            ],
        }
        for model in {*turns, *(row["model"] for row in usage)}
    ]
    today = datetime.now(timezone.utc).date()
    labels = {model: label for label, model in settings.selectable_models.items()}
    in_use = {choice.default, choice.fallback} - {""}
    prices = []
    for price in settings.llm_prices:
        in_effect = price_for(
            settings.llm_prices, price.model, today, price.service_tier
        ) is price
        prices.append({
            **jsonable(price.model_dump()),
            "label": labels.get(price.model),
            "in_effect": in_effect,
            # 지금 기본·폴백으로 쓰는 모델의 그 등급 행 중 오늘 유효한 것(화면 강조).
            "current": in_effect and price.model in in_use
            and price.service_tier == choice.service_tiers.get(price.model, ""),
        })
    coverage = total(usage)  # 비용 각주 재료
    history = await _fetch(
        cur,
        "SELECT created_at, actor_name, action, `before`, `after` FROM admin_audit "
        "WHERE target_type = %s ORDER BY created_at DESC, id DESC LIMIT %s",
        [MODEL_AUDIT_TARGET, settings.admin_model_history_rows],
    )
    return {
        "default": {"model": choice.default, "source": choice.default_source},
        "fallback": {"model": choice.fallback, "source": choice.fallback_source},
        "config": {"default_model": settings.model_name, "fallback_model": settings.fallback_model},
        "stored": jsonable(stored) if stored else None,
        "selectable": settings.selectable_models,
        # 처리 등급(OpenAI 모델만 — model_routing.per_model_targets).
        "service_tiers": [
            {
                "model": model,
                "label": labels.get(model),
                "tier": choice.service_tiers.get(model, ""),
                "config_tier": settings.litellm_service_tiers.get(model, ""),
                "source": "runtime" if model in choice.runtime_tiers else "config",
            }
            for model in per_model_targets(settings, "service_tiers")
        ],
        "tier_options": list(settings.litellm_service_tier_options),
        "tier_notes": settings.litellm_service_tier_notes,
        # 사고 강도(같은 기계·같은 모델 목록). 맵에 없는 모델은 전역 litellm_reasoning_effort.
        "reasoning_efforts": [
            {
                "model": model,
                "label": labels.get(model),
                "effort": choice.reasoning_efforts.get(model, default_effort(settings, model)),
                "config_effort": settings.litellm_reasoning_efforts.get(
                    model, default_effort(settings, model)
                ),
                "source": "runtime" if model in choice.runtime_efforts else "config",
            }
            for model in per_model_targets(settings, "reasoning_efforts")
        ],
        "effort_options": list(settings.litellm_reasoning_effort_options),
        # 비용 계산의 근거 단가(정본 config llm_prices — 화면은 숫자를 들지 않는다).
        "prices": prices,
        "cooldown_s": settings.model_fallback_cooldown_s,
        "settings_ttl_s": settings.model_settings_ttl_s,
        "cooldowns": cooldowns(),
        "window_hours": windows,
        # 턴 수(7일) 많은 순, 기록 없는 모델(NULL)은 뒤로.
        "models": sorted(
            models, key=lambda m: (m["model"] is None, -m["turns"][-1], m["model"] or "")
        ),
        "history": [jsonable(row, ("before", "after")) for row in history],
        # 비용 각주(예상치·빠진 기록·등급 추정) — 판정·문장은 admin_cost.cost_notes 한 곳이다.
        "cost_notes": cost_notes(coverage, background=False),
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


async def fetch_stats(
    cur, settings: Settings, period: tuple[date, date], excluded: tuple[str, ...] = ()
) -> dict[str, Any]:
    """통계 탭 — 어드민 시간대 일별 세션·질의·링크·클릭(고객사 엑셀 11열) + 기간 요약 + 요일 평균
    + RBTI 유형별 질의 수(적용률은 화면이 요약 질의 수로 나눈다 — 일별 표·엑셀 양식에는 넣지
    않는다).

    SQL은 (날짜, 세션)까지만 묶고 나머지는 여기서 센다. 기간 요약은 일별 값을 더하거나
    평균하지 않고 같은 행에서 다시 센다 — 이틀에 걸친 세션·여러 날 온 사용자는 기간에 한 번이다.
    기록 없는 날도 0행으로 채운다. 일별 표는 최신 날짜가 먼저다.
    """
    hours = settings.admin_utc_offset_hours
    where, params = period_filter("started_at", period, hours, excluded)
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
    where, params = period_filter("created_at", period, hours, excluded)
    await cur.execute(
        f"SELECT {local_day('created_at', hours)} AS day, COUNT(*) AS clicks "
        f"FROM turn_click{sql_where(where)} GROUP BY day",
        params,
    )
    clicks = {row["day"]: int(row["clicks"]) for row in await cur.fetchall()}
    # RBTI 유형별 질의 수·회원 수(그 유형으로 대화한 고유 사용자 — 기간 중 유형이 바뀐 회원은 두
    # 유형에 모두 센다), RBTI가 적용된 턴만, 많은 순 — 유형 목록은 데이터에 나온 코드뿐이다.
    where, params = period_filter("started_at", period, hours, excluded)
    await cur.execute(
        "SELECT rbti_applied AS rbti, COUNT(*) AS turns, COUNT(DISTINCT user_id) AS users "
        f"FROM chat_turn{sql_where([*where, 'rbti_applied IS NOT NULL'])} "
        "GROUP BY rbti_applied ORDER BY turns DESC, rbti",
        params,
    )
    rbti = [jsonable(row) for row in await cur.fetchall()]
    # 피드백 — 일별 좋아요·싫어요(최신 평가 시각 기준)와 의견이 달린 최근 피드백(질문 앞부분과
    # 함께, 한 화면 분량 = admin_page_size).
    where, params = period_filter("updated_at", period, hours, excluded)
    await cur.execute(
        f"SELECT {local_day('updated_at', hours)} AS day, {FEEDBACK_COUNTS} "
        f"FROM turn_feedback{sql_where(where)} GROUP BY day",
        params,
    )
    feedback_daily = [jsonable(row) for row in await cur.fetchall()]
    where, params = period_filter("f.updated_at", period, hours, excluded, "f.user_id")
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
    giftcard = await _giftcard_stats(cur, settings, period, excluded)

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
        "giftcard": giftcard,
    }


# ── 상품권 이벤트 기록(giftcard_issue) ──────────────────────────────────────
# 클라이언트가 전달한 발급 결과를 모은 운영 참고 이력이다(giftcard.py) — Yes24 실제 발급 수와 다를
# 수 있고 잔여 수량의 근거가 아니다. 조회는 설정의 현재 이벤트 키 하나로 한정하고, 키가 비면(이벤트
# 종료 스위치) 조회하지 않는다.

# "받은 회원" 판정 식 — Yes24가 "이 회원은 받았다"고 답한 코드(giftcard.py 독스트링). 대상자 확인
# (user_data.giftcard_issued)과 어드민 집계가 이 식 하나를 쓴다. 이 모듈에 두는 이유는 import 방향 —
# user_data가 admin 계층(admin_auth → admin_data)을 이미 읽는다.
GIFTCARD_RECEIVED = "result_code IN ('SUCCESS', 'ALREADY_APPLIED')"


def _giftcard_event(settings: Settings) -> dict[str, Any] | None:
    key = settings.giftcard_event_key
    if not key:
        return None
    return {"key": key, "start": settings.giftcard_event_start, "end": settings.giftcard_event_end}


async def _giftcard_stats(
    cur, settings: Settings, period: tuple[date, date], excluded: tuple[str, ...]
) -> dict[str, Any] | None:
    """통계 '상품권' 묶음 — 기간(어드민 시간대) 안의 시도를 결과 코드별·일별·시간대별(기간 합계)로,
    회원은 고유 수로. 세 묶음은 (날짜, 시, 코드) 한 문장에서 나눠 센다.

    received = 그 코드가 "받은" 판정(GIFTCARD_RECEIVED)인가 — 묶음 열(result_code)의 식이라 행마다
    같다. 받은 회원은 일별 값을 더하지 않고 기간 전체에서 다시 센다(여러 번 시도한 회원은 한 번).
    """
    event = _giftcard_event(settings)
    if event is None:
        return None
    hours = settings.admin_utc_offset_hours
    where, params = period_filter("created_at", period, hours, excluded)
    scope, params = sql_where(["event_key = %s", *where]), [event["key"], *params]
    await cur.execute(
        f"SELECT {local_day('created_at', hours)} AS day, "
        f"HOUR{local_time('created_at', hours)} AS hour, result_code, "
        f"{GIFTCARD_RECEIVED} AS received, COUNT(*) AS attempts "
        f"FROM giftcard_issue{scope} GROUP BY day, hour, result_code",
        params,
    )
    rows = await cur.fetchall()
    await cur.execute(
        "SELECT COUNT(*) AS attempts, COUNT(DISTINCT user_id) AS users, "
        f"COUNT(DISTINCT CASE WHEN {GIFTCARD_RECEIVED} THEN user_id END) AS received_users "
        f"FROM giftcard_issue{scope}",
        params,
    )
    summary = jsonable(await cur.fetchone())

    def attempts(group, received: bool = False) -> int:
        return sum(int(row["attempts"]) for row in group if row["received"] or not received)

    codes = [
        {"result_code": code, "attempts": attempts(group), "received": bool(group[0]["received"])}
        for (code,), group in _grouped(rows, "result_code").items()
    ]
    return {
        "event": event,
        "summary": summary,
        "codes": sorted(codes, key=lambda row: (-row["attempts"], row["result_code"])),
        "daily": [
            {"day": day, "attempts": attempts(group), "received": attempts(group, received=True)}
            for (day,), group in sorted(_grouped(rows, "day").items())
        ],
        "hourly": [
            {"hour": hour, "attempts": attempts(group), "received": attempts(group, received=True)}
            for (hour,), group in sorted(_grouped(rows, "hour").items())
        ],
    }


async def fetch_user_giftcard(cur, settings: Settings, user_no: str) -> dict[str, Any]:
    """회원 옆 패널 — 그 회원의 현재 이벤트 시도 기록(최신순, 한 화면 분량). 한 행 더 읽어
    truncated(더 있음)를 알린다. idx_giftcard_issue_owner (user_id, event_key) 등치로 찾는다."""
    event = _giftcard_event(settings)
    if event is None:
        return {"event": None, "items": [], "truncated": False}
    size = settings.admin_page_size
    await cur.execute(
        "SELECT created_at, result_code FROM giftcard_issue WHERE user_id = %s AND event_key = %s "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        [user_no, event["key"], size + 1],
    )
    rows = await cur.fetchall()
    return {"event": event, "items": [jsonable(row) for row in rows[:size]],
            "truncated": len(rows) > size}


async def with_user_giftcard(cur, settings: Settings, page: dict[str, Any]) -> dict[str, Any]:
    """회원 목록 한 페이지에 상품권 상태(giftcard)를 붙인다 — 목록 쿼리는 그대로 두고 그 페이지
    회원번호만 한 번 더 조회한다(페이지당 1문장, 인덱스 (user_id, event_key)).

    giftcard = {received: 받은 판정 기록이 있는가, last_result: 시각순 마지막 결과 코드}, 기록이
    없으면 None. 이벤트가 꺼져 있으면 조회도 필드도 없다(giftcard_event None)."""
    event = _giftcard_event(settings)
    if event is None or not page["items"]:
        return {**page, "giftcard_event": event}
    ids = sorted({str(row["user_no"]) for row in page["items"]})
    await cur.execute(
        f"SELECT user_id, result_code, {GIFTCARD_RECEIVED} AS received FROM giftcard_issue "
        f"WHERE event_key = %s AND user_id IN ({', '.join(['%s'] * len(ids))}) "
        "ORDER BY created_at, id",
        [event["key"], *ids],
    )
    status: dict[str, dict[str, Any]] = {}
    for row in await cur.fetchall():
        before = status.get(row["user_id"], {"received": False})
        status[row["user_id"]] = {
            "received": before["received"] or bool(row["received"]),
            "last_result": row["result_code"],
        }
    items = [{**row, "giftcard": status.get(str(row["user_no"]))} for row in page["items"]]
    return {**page, "items": items, "giftcard_event": event}


# 추적 화면의 도구 스텝 칩 — 공개 process.steps에서 칩에 필요한 키만(출처 목록은 세션 상세 몫).
_TRACE_STEP_KEYS = ("stage", "detail", "state", "result_count")


async def fetch_turn_trace(
    cur, settings: Settings, session_id: str, turn_id: str, user_id: str | None = None
) -> dict[str, Any] | None:
    """턴 추적(owner 전용 라우트) — 콜별 라운드(turn_round)·시도 전환·usage_log 행과 비용.

    라운드 행은 TurnProcess.rounds 그대로다(시도별·콜별, 같은 라운드에 콜이 여럿일 수 있다).
    도구 스텝(chat_turn.process.steps)은 **마지막 시도**의 것뿐이라(재시도는 누적기를 새로 연다)
    마지막 시도의 각 라운드 마지막 콜에 붙인다. turn_round 도입 전 턴은 rounds가 빈 목록이다.
    비용은 그 턴의 usage_log 행(main·서브콜)에 admin_cost 단가를 붙인 합이다.
    """
    where, params = ["session_id = %s", "turn_id = %s"], [session_id, turn_id]
    if user_id is not None:
        where.append("user_id = %s")
        params.append(user_id)
    turns = await _fetch(
        cur,
        "SELECT app_name, user_id, process, model, fallback_from, prompt_version_id "
        f"FROM chat_turn{sql_where(where)} LIMIT 2",
        params,
    )
    if not turns:
        return None
    if len(turns) > 1:
        raise HTTPException(status_code=409, detail="사용자 식별자를 함께 지정해 주세요.")
    turn = jsonable(turns[0], ("process",))
    scope = [turn["app_name"], turn["user_id"], session_id, turn_id]
    scoped = "WHERE app_name = %s AND user_id = %s AND session_id = %s AND turn_id = %s"
    rounds = [
        jsonable(row, ("tool_calls",))
        for row in await _fetch(
            cur,
            "SELECT attempt, round_index AS round, model, started_ms, ended_ms, prompt_tokens, "
            "response_tokens, thinking_tokens, cached_tokens, tool_calls, error_status "
            f"FROM turn_round {scoped} ORDER BY seq",
            scope,
        )
    ]
    last_call: dict[int, dict[str, Any]] = {}
    final_attempt = max((row["attempt"] for row in rounds), default=0)
    for row in rounds:
        row["steps"] = []
        if row["attempt"] == final_attempt:
            last_call[row["round"]] = row
    for step in (turn["process"] or {}).get("steps", []):
        if step.get("round") in last_call:
            last_call[step["round"]]["steps"].append(
                {key: step[key] for key in _TRACE_STEP_KEYS if key in step}
            )
    attempts: dict[int, dict[str, Any]] = {}
    for row in rounds:  # 시도의 모델·상태코드 = 그 시도 마지막 행의 값
        attempts[row["attempt"]] = {
            "attempt": row["attempt"], "model": row["model"], "error_status": row["error_status"],
        }
    # 행마다 단가(UTC 날짜 `day`는 date 그대로 — 응답 정형은 단가를 붙인 뒤).
    usage = [
        jsonable(row)
        for row in attach_costs(
            await _fetch(
                cur,
                "SELECT DATE(created_at) AS day, component, model, service_tier, "
                "reasoning_effort, prompt_tokens, response_tokens, thinking_tokens, cached_tokens, "
                f"latency_ms, llm_calls, {_USAGE_SUMS} FROM usage_log {scoped} "
                "GROUP BY id ORDER BY id",
                scope,
            ),
            settings.llm_prices,
        )
    ]
    main = next((row for row in usage if row["component"] == MAIN_COMPONENT), {})
    return {
        "turn_id": turn_id,
        "model": turn["model"],
        "fallback_from": turn["fallback_from"],
        "prompt_version": turn["prompt_version_id"],
        "service_tier": main.get("service_tier"),
        "reasoning_effort": main.get("reasoning_effort"),
        "elapsed_ms": (turn["process"] or {}).get("elapsed_ms"),
        "rounds": rounds,
        "attempts": list(attempts.values()),
        "usage": usage,
        "cost_usd": total(usage)["cost_usd"],
    }
