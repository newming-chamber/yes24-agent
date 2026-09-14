"""운영 데이터 조회와 집계. 공개할 테이블·열을 명세하고 값은 전부 바인딩한다."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from pymysql.constants import FIELD_TYPE
from pymysql.converters import decoders

from yes24_agent.admin_cost import (
    TOKEN_KINDS,
    attach_costs,
    cost_notes,
    model_order,
    per_turn,
    total,
)
from yes24_agent.config import Settings

# admin 접속(읽기 전용 조회·AdminService 풀) 공통 디코더 — BOOLEAN 열을 bool로 싣는다.
# 열 이름이 아니라 결과 열 타입(TINY)으로 판정하므로 뷰·조인을 거쳐도 같다. 파생값(SUM(a=1)
# DECIMAL, (1=1)·a+0 LONGLONG)은 TINY가 아니라 int로 남는다(격리 MySQL 실측). 스키마의
# TINYINT 열은 전부 BOOLEAN(TINYINT(1))이다 — 수치 TINYINT 열이 생기면 이 판정을 다시 볼 것.
BOOL_DECODERS = {**decoders, FIELD_TYPE.TINY: lambda value: value != "0"}

_DATE_TYPES = {
    "created_at": "datetime",
    "updated_at": "datetime",
    "started_at": "datetime",
    "completed_at": "datetime",
    "last_request_at": "datetime",
    "run_date": "date",
    "valid_from": "date",
    "valid_until": "date",
}


@dataclass(frozen=True)
class Dataset:
    id: str
    label: str
    table: str
    columns: tuple[str, ...]
    search_columns: tuple[str, ...]
    date_column: str
    order: tuple[str, ...]
    status_column: str | None = None
    status_values: tuple[str, ...] = ()
    # 상태 열이 NULL(미측정)일 수 있어 "미측정" 필터를 여는가.
    nullable_status: bool = False
    # 정확 일치 필터로 여는 열 — 라우트의 쿼리 파라미터 목록도 여기서 파생한다(단일 출처).
    exact_filters: tuple[str, ...] = ()
    # 정렬 대상에서 빼는 열(본문·URL·JSON처럼 정렬 의미가 없는 값).
    unsortable: tuple[str, ...] = ()
    # 드라이버가 텍스트로 돌려주는 JSON 열 — 응답에는 구조로 싣는다(jsonable).
    json_columns: tuple[str, ...] = ()

    @property
    def sort_columns(self) -> tuple[str, ...]:
        return tuple(column for column in self.columns if column not in self.unsortable)

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "columns": [
                {"key": column, "label": column, "type": _DATE_TYPES.get(column)}
                for column in self.columns
            ],
            "search_columns": self.search_columns,
            "sort_columns": self.sort_columns,
            "date_column": self.date_column,
            "default_sort": self.order[0],
            "status_column": self.status_column,
            "status_values": self.status_values,
            "app_filter": "app_name" in self.columns,
            "exact_filters": self.exact_filters,
            "nullable_status": self.nullable_status,
        }


DATASETS = {
    dataset.id: dataset
    for dataset in (
        Dataset(
            "turns",
            "대화 턴",
            "chat_turn",
            tuple(
                "id app_name user_id session_id turn_id started_at completed_at "
                "status history_saved "
                "user_text text rbti_applied".split()
            ),
            ("user_text", "text", "session_id", "user_id", "turn_id"),
            "started_at",
            ("started_at", "id"),
            "status",
            ("completed", "failed", "interrupted", "unknown"),
            exact_filters=("user_id", "session_id"),
            unsortable=("user_text", "text"),
        ),
        Dataset(
            "usage",
            "모델 사용량",
            "usage_log",
            tuple(
                "id app_name created_at session_id user_id turn_id endpoint component model "
                "prompt_tokens response_tokens total_tokens thinking_tokens cached_tokens "
                "latency_ms outcome llm_calls tool_calls cited_sources".split()
            ),
            ("session_id", "user_id", "turn_id", "model", "component", "endpoint"),
            "created_at",
            ("created_at", "id"),
            "outcome",
            ("ok", "empty", "timeout", "error", "aborted"),
            nullable_status=True,
            exact_filters=("user_id", "session_id", "model", "component"),
        ),
        Dataset(
            "feedback",
            "피드백",
            "turn_feedback",
            tuple(
                "id created_at updated_at app_name user_id session_id turn_id "
                "rating comment".split()
            ),
            ("user_id", "session_id", "turn_id", "comment"),
            "updated_at",
            ("updated_at", "id"),
            "rating",
            ("up", "down"),
            exact_filters=("user_id", "session_id"),
            unsortable=("comment",),
        ),
        Dataset(
            "clicks",
            "링크 클릭",
            "turn_click",
            tuple(
                "id created_at app_name user_id session_id turn_id url "
                "source_id source_type label".split()
            ),
            ("user_id", "session_id", "turn_id", "url", "label"),
            "created_at",
            ("created_at", "id"),
            exact_filters=("user_id", "session_id"),
            unsortable=("url",),
        ),
        Dataset(
            "users",
            "사용자 활동",
            "user_activity",
            tuple(
                "id user_no user_login_id is_active rate_limit_rpm rate_limit_rpd active_keys "
                "requests_last_minute requests_last_day last_request_at "
                "created_at updated_at".split()
            ),
            ("user_no", "user_login_id"),
            "created_at",
            ("created_at", "id"),
            "is_active",
            ("1", "0"),
        ),
        Dataset(
            "starters",
            "초기 질문",
            "starters",
            tuple(
                "id slot text source goods_no source_url run_date pinned active "
                "valid_from valid_until "
                "created_at updated_at".split()
            ),
            ("slot", "text", "source", "source_url"),
            "created_at",
            ("created_at", "id"),
            "active",
            ("1", "0"),
            unsortable=("text", "source_url"),
        ),
        Dataset(
            "starter_runs",
            "초기 질문 생성 이력",
            "starter_runs",
            ("slot", "run_date", "status", "started_at"),
            ("slot",),
            "started_at",
            ("started_at", "slot", "run_date"),
            "status",
            ("running", "ok", "failed"),
        ),
        Dataset(
            "audit",
            "관리 감사",
            "admin_audit",
            tuple(
                "id created_at actor_id actor_name target_type target_id action "
                "before after ip session_id".split()
            ),
            ("actor_name", "target_id"),
            "created_at",
            ("created_at", "id"),
            "action",
            # admin_audit.action 어휘 — admin_auth에 새 action을 추가하면 여기 동기화한다.
            tuple(
                "ok failed logout create update deactivate "
                "password_reset password_change generate".split()
            ),
            exact_filters=("actor_name", "target_type", "target_id"),
            unsortable=("before", "after"),
            json_columns=("before", "after"),
        ),
    )
}

# 데이터 라우트가 받는 정확 일치 쿼리 파라미터 — 데이터셋 선언의 합집합(순서 보존).
EXACT_FILTER_KEYS = tuple(
    dict.fromkeys(key for dataset in DATASETS.values() for key in dataset.exact_filters)
)


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


def period_filter(
    column: str,
    period: tuple[date | None, date | None],
    app_name: str = "",
) -> tuple[list[str], list[Any]]:
    since, until = period
    where, params = [], []
    if since:
        where.append(f"{column} >= %s")
        params.append(since)
    if until and until < date.max:
        where.append(f"{column} < %s + INTERVAL 1 DAY")
        params.append(until)
    if app_name:
        where.append("app_name = %s")
        params.append(app_name)
    return where, params


def sql_where(conditions: list[str]) -> str:
    return " WHERE " + " AND ".join(conditions) if conditions else ""


def dataset_for(dataset_id: str, sort: str, status: str, app_name: str) -> Dataset:
    dataset = DATASETS.get(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="데이터셋을 찾을 수 없습니다.")
    if sort and sort not in dataset.sort_columns:
        raise HTTPException(status_code=422, detail="지원하지 않는 정렬 열입니다.")
    if status and status not in dataset.status_values:
        raise HTTPException(status_code=422, detail="지원하지 않는 상태 필터입니다.")
    if app_name and "app_name" not in dataset.columns:
        raise HTTPException(status_code=422, detail="이 데이터셋은 앱 필터를 지원하지 않습니다.")
    return dataset


def dataset_query(
    dataset: Dataset,
    *,
    query: str,
    period: tuple[date | None, date | None],
    app_name: str,
    status: str,
    status_null: bool,
    sort: str,
    direction: str,
    exact: dict[str, str],
) -> tuple[str, str, list[Any]]:
    conditions, params = period_filter(dataset.date_column, period, app_name)
    if status_null:
        if status or not dataset.nullable_status:
            raise HTTPException(status_code=422, detail="미측정 상태 필터를 사용할 수 없습니다.")
        conditions.append(f"{dataset.status_column} IS NULL")
    for column, value in exact.items():
        if not value:
            continue
        if column not in dataset.exact_filters:
            raise HTTPException(status_code=422, detail="지원하지 않는 정확한 일치 필터입니다.")
        conditions.append(f"{column} = %s")
        params.append(value)
    if query:
        conditions.append(
            "(" + " OR ".join(f"{column} LIKE %s" for column in dataset.search_columns) + ")"
        )
        params.extend([f"%{query}%"] * len(dataset.search_columns))
    if status:
        conditions.append(f"{dataset.status_column} = %s")
        params.append(status)
    where = sql_where(conditions)
    ordering = list(dict.fromkeys([sort or dataset.order[0], *dataset.order]))
    order_sql = ", ".join(f"{column} {direction}" for column in ordering)
    return (
        f"SELECT COUNT(*) AS total FROM {dataset.table}{where}",
        # 열 이름은 인용한다 — admin_audit의 `before`는 MySQL 예약어다.
        f"SELECT {', '.join(f'`{column}`' for column in dataset.columns)} "
        f"FROM {dataset.table}{where} ORDER BY {order_sql}",
        params,
    )


async def fetch_dataset(
    cur, dataset: Dataset, selection, *, page: int, size: int
) -> dict[str, Any]:
    count_sql, rows_sql, params = selection
    await cur.execute(count_sql, params)
    total = (await cur.fetchone())["total"]
    await cur.execute(rows_sql + " LIMIT %s OFFSET %s", [*params, size, page * size])
    return {
        "dataset": dataset.id,
        "total": total,
        "page": page,
        "page_size": size,
        "items": [jsonable(row, dataset.json_columns) for row in await cur.fetchall()],
    }


def csv_row(values) -> str:
    output = io.StringIO(newline="")
    cells = []
    for value in values:
        if isinstance(value, datetime):
            value = value.replace(tzinfo=timezone.utc).isoformat()
        text = "" if value is None else str(value)
        # 스프레드시트가 수식으로 해석하는 선두 문자. 탭·CR은 lstrip이 벗기므로 원문 첫 글자로 본다.
        if text.startswith(("\t", "\r")) or text.lstrip().startswith(("=", "+", "-", "@")):
            text = "'" + text
        cells.append(text)
    csv.writer(output).writerow(cells)
    return output.getvalue()


async def stream_csv(cur, dataset: Dataset, size: int):
    """접속 수명은 호출부 응답 객체가 소유한다(스트림이 시작되지 않아도 닫혀야 한다)."""
    yield "\ufeff" + csv_row(dataset.columns)
    while rows := await cur.fetchmany(size):
        yield "".join(csv_row(row[column] for column in dataset.columns) for row in rows)


# usage.py 계약: 턴당 1행(그 턴의 LLM 콜 합산)인 행의 component. 나머지는 콜당 1행인 서브콜이다.
MAIN_COMPONENT = "main"
# 과금 토큰 세 갈래의 합(admin_cost 모듈 docstring). 측정 불성립 행은 합에서 빼고 행 수로만 남긴다.
_MEASURED = "prompt_tokens IS NOT NULL AND total_tokens >= prompt_tokens"
_USAGE_SUMS = (
    f"COUNT(*) AS `rows`, COUNT(CASE WHEN {_MEASURED} THEN 1 END) AS measured_rows, "
    "SUM(llm_calls) AS llm_calls, "
    f"SUM(CASE WHEN {_MEASURED} THEN prompt_tokens - COALESCE(cached_tokens, 0) END) "
    "AS input_uncached, "
    f"SUM(CASE WHEN {_MEASURED} THEN COALESCE(cached_tokens, 0) END) AS input_cached, "
    f"SUM(CASE WHEN {_MEASURED} THEN total_tokens - prompt_tokens END) AS output_billed, "
    f"COUNT(CASE WHEN {_MEASURED} AND cached_tokens IS NULL THEN 1 END) AS cache_unknown_rows, "
    # tool_calls·cited_sources는 main 행에만 값이 있다(서브콜의 NULL은 세지 않는다).
    "COUNT(CASE WHEN tool_calls > 0 AND cited_sources = 0 THEN 1 END) AS ungrounded_suspect"
)
_TURN_STATUSES = DATASETS["turns"].status_values
_STATUS_COUNTS = ", ".join(
    f"COUNT(CASE WHEN status = '{status}' THEN 1 END) AS {status}" for status in _TURN_STATUSES
)
_LATENCY_QUANTILES = ", ".join(
    f"MAX(CASE WHEN rank_no = CEIL(samples * {share}) THEN elapsed_ms END) / 1000 AS {alias}"
    for share, alias in ((0.5, "p50_seconds"), (0.95, "p95_seconds"))
)
_COVERAGE = (
    "rows",
    "priced_rows",
    "unpriced_rows",
    "unpriced_tokens",
    "unmeasured_rows",
    "cache_unknown_rows",
)
_DAILY_COVERAGE = ("unpriced_rows", "cache_unknown_rows", "unmeasured_rows")


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


async def fetch_analytics(
    cur, settings: Settings, period: tuple[date | None, date | None], app_name: str
) -> dict[str, Any]:
    """대시보드 한 화면 — 모든 문장이 호출부(_query)의 같은 읽기 전용 스냅샷에서 돈다.

    비교 기간이 있으면 날짜로 가를 수 있는 집계(사용량·피드백·클릭)는 두 기간을 한 문장으로 읽어
    날짜로 나눈다. 금액은 SQL이 아니라 admin_cost가 행의 날짜(UTC)에 유효한 단가로 붙인다.
    """

    async def fetch(sql: str, params: list[Any]) -> list[dict[str, Any]]:
        await cur.execute(sql, params)
        return list(await cur.fetchall())

    since, until = period
    previous = None
    if since and until:
        length = (until - since).days + 1
        if (since - date.min).days >= length:
            previous = (since - timedelta(days=length), since - timedelta(days=1))
    spanned = (previous[0], until) if previous else period

    async def turn_summary(selected) -> dict[str, Any]:
        where, params = period_filter("asked_at", selected, app_name)
        [row] = await fetch(
            "SELECT COUNT(*) AS turns, COUNT(DISTINCT app_name, user_id, session_id) AS sessions, "
            f"COUNT(DISTINCT app_name, user_id) AS users, {_STATUS_COUNTS}, "
            "COUNT(elapsed_ms) AS elapsed_rows, AVG(elapsed_ms) / 1000 AS elapsed_avg_seconds, "
            f"{_LATENCY_QUANTILES}, MAX(elapsed_ms) / 1000 AS max_seconds FROM ("
            + _ranked_turns("app_name, user_id, session_id, status", sql_where(where))
            + ") ranked",
            params,
        )
        return jsonable(row)

    turn_where, turn_params = period_filter("asked_at", period, app_name)
    daily_turns = await fetch(
        f"SELECT day, COUNT(*) AS turns, {_STATUS_COUNTS}, COUNT(elapsed_ms) AS elapsed_rows, "
        f"{_LATENCY_QUANTILES} FROM ("
        + _ranked_turns("DATE(asked_at) AS day, status", sql_where(turn_where), "DATE(asked_at)")
        + ") ranked GROUP BY day",
        turn_params,
    )
    hourly = await fetch(
        "SELECT WEEKDAY(asked_at) AS weekday, HOUR(asked_at) AS hour, COUNT(*) AS turns "
        f"FROM chat_turns{sql_where(turn_where)} GROUP BY weekday, hour ORDER BY weekday, hour",
        turn_params,
    )
    feedback_where, feedback_params = period_filter("updated_at", spanned, app_name)
    click_where, click_params = period_filter("created_at", spanned, app_name)
    engagement = await fetch(
        "SELECT DATE(updated_at) AS day, COUNT(CASE WHEN rating = 'up' THEN 1 END) AS likes, "
        "COUNT(CASE WHEN rating = 'down' THEN 1 END) AS dislikes, 0 AS clicks "
        f"FROM turn_feedback{sql_where(feedback_where)} GROUP BY day "
        "UNION ALL SELECT DATE(created_at) AS day, 0, 0, COUNT(*) "
        f"FROM turn_click{sql_where(click_where)} GROUP BY day",
        [*feedback_params, *click_params],
    )
    usage_where, usage_params = period_filter("created_at", spanned, app_name)
    usage = attach_costs(
        await fetch(
            f"SELECT DATE(created_at) AS day, model, component, {_USAGE_SUMS} "
            f"FROM usage_log{sql_where(usage_where)} GROUP BY day, model, component",
            usage_params,
        ),
        settings.llm_prices,
    )
    # 사용자 귀속은 main 행뿐이다(서브콜은 턴 문맥이 없을 수 있다).
    user_where, user_params = period_filter("created_at", period, app_name)
    user_usage = attach_costs(
        await fetch(
            f"SELECT app_name, user_id, DATE(created_at) AS day, model, {_USAGE_SUMS} "
            f"FROM usage_log{sql_where([*user_where, 'component = %s', 'user_id IS NOT NULL'])} "
            "GROUP BY app_name, user_id, day, model",
            [*user_params, MAIN_COMPONENT],
        ),
        settings.llm_prices,
    )

    def split(rows) -> tuple[list, list]:
        """(이번 기간, 비교 기간) — 비교 기간이 없으면 전부 이번 기간이다."""
        if previous is None:
            return list(rows), []
        return [r for r in rows if r["day"] >= since], [r for r in rows if r["day"] < since]

    def summary_of(turns: dict[str, Any], usage_rows, engagement_rows) -> dict[str, Any]:
        main = total(r for r in usage_rows if r["component"] == MAIN_COMPONENT)
        return {
            **turns,
            **_sums(engagement_rows, ("likes", "dislikes", "clicks")),
            "ungrounded_suspect": main["ungrounded_suspect"],
            "main_rows": main["rows"],
            # 과금 턴 — 단가가 적용되고 측정된 main 행. 비용의 분모는 이것뿐이다(분자와 같은 모집단:
            # 대화 삭제는 chat_turn만 지우고, 미등록·미측정 행은 금액에 없으니 분모에도 없다).
            "priced_rows": main["priced_rows"],
            "cost_usd": total(usage_rows)["cost_usd"],
            "cost_per_turn_usd": per_turn(main["cost_usd"], main["priced_rows"]),
        }

    usage_now, usage_before = split(usage)
    engagement_now, engagement_before = split(engagement)
    comparison = None
    if previous:
        comparison = {
            "period": {"since": previous[0], "until": previous[1], "timezone": "UTC"},
            "summary": summary_of(await turn_summary(previous), usage_before, engagement_before),
        }

    turns_by_day = {row["day"]: jsonable(row) for row in daily_turns}
    usage_by_day = {day: rows for (day,), rows in _grouped(usage_now, "day").items()}
    engagement_by_day = {day: rows for (day,), rows in _grouped(engagement_now, "day").items()}
    daily = []
    for day in sorted({*turns_by_day, *usage_by_day, *engagement_by_day}):
        turns = turns_by_day.get(day, {})
        day_usage = usage_by_day.get(day, [])
        costs = total(day_usage)
        main = total(r for r in day_usage if r["component"] == MAIN_COMPONENT)
        model_costs = {
            model: total(rows)["cost_usd"]
            for (model,), rows in _grouped(day_usage, "model").items()
        }
        daily.append(
            {
                "day": day,
                "turns": turns.get("turns", 0),
                **{status: turns.get(status, 0) for status in _TURN_STATUSES},
                "p50_seconds": turns.get("p50_seconds"),
                "p95_seconds": turns.get("p95_seconds"),
                "elapsed_rows": turns.get("elapsed_rows", 0),
                **_sums(engagement_by_day.get(day, []), ("likes", "dislikes")),
                "ungrounded_suspect": main["ungrounded_suspect"],
                "main_rows": main["rows"],
                # 측정 턴 — 토큰이 측정된 main 행. main_tokens의 분모다(미등록 모델 토큰도 담긴다).
                "measured_main_rows": main["rows"] - main["unmeasured_rows"],
                "cost_usd": costs["cost_usd"],
                # 단가 미등록 모델은 금액 시리즈에서 빠지고 unpriced_rows로 남는다.
                "cost_by_model": {m: c for m, c in model_costs.items() if c is not None},
                "main_tokens": {kind: main[kind] for kind in TOKEN_KINDS},
                **{key: costs[key] for key in _DAILY_COVERAGE},
            }
        )

    model_components = {
        key: total(rows) for key, rows in _grouped(usage_now, "model", "component").items()
    }
    by_model_component = [
        {
            "model": model,
            "component": component,
            **{key: group[key] for key in ("rows", "llm_calls", *TOKEN_KINDS, "cost_usd")},
            "price_effective_from": group["price_effective_from"],
        }
        for (model, component), group in sorted(
            model_components.items(), key=lambda item: (item[0][0] or "", item[0][1])
        )
    ]
    user_totals = {
        key: total(rows) for key, rows in _grouped(user_usage, "app_name", "user_id").items()
    }
    users = [
        {
            "app_name": app,
            "user_id": user_id,
            "priced_rows": group["priced_rows"],
            "cost_usd": group["cost_usd"],
            "cost_per_turn_usd": per_turn(group["cost_usd"], group["priced_rows"]),
        }
        for (app, user_id), group in user_totals.items()
    ]
    # 비용 내림차순, 단가 미등록(None)은 뒤로, 동률은 앱·사용자 이름순.
    users.sort(
        key=lambda u: (u["cost_usd"] is None, -(u["cost_usd"] or 0), u["app_name"], u["user_id"])
    )
    totals = total(usage_now)
    coverage = {key: totals[key] for key in _COVERAGE}
    summary = summary_of(await turn_summary(period), usage_now, engagement_now)

    return {
        "period": {"since": since, "until": until, "timezone": "UTC"},
        "currency": {
            "ledger": "USD",
            "krw_per_usd": settings.krw_per_usd,
            "krw_as_of": settings.krw_per_usd_as_of,
        },
        "summary": summary,
        "coverage": coverage,
        "models": model_order(settings.llm_prices, (row["model"] for row in usage_now)),
        "daily": daily,
        "cost": {
            "by_model_component": by_model_component,
            "notes": cost_notes(coverage, summary["turns"], summary["main_rows"]),
        },
        "hourly": [jsonable(row) for row in hourly],
        "users": users[: settings.admin_top_users],
        "comparison": comparison,
    }
