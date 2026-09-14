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
                "password_reset password_change generate restore purge".split()
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


_USAGE_AGGREGATES = (
    "COUNT(*) AS `rows`, COUNT(total_tokens) AS known_token_rows, "
    "SUM(total_tokens) AS total_tokens, "
    "COUNT(latency_ms) AS latency_rows, AVG(latency_ms) / 1000 AS latency_avg_seconds"
)


async def fetch_analytics(
    cur, period: tuple[date | None, date | None], app_name: str
) -> dict[str, Any]:
    async def query(table, columns, date_column, *, group_by="", selected_period=period):
        conditions, params = period_filter(date_column, selected_period, app_name)
        grouping = f" GROUP BY {group_by} ORDER BY {group_by}" if group_by else ""
        await cur.execute(f"SELECT {columns} FROM {table}{sql_where(conditions)}{grouping}", params)
        if group_by:
            return [jsonable(row) for row in await cur.fetchall()]
        return jsonable(await cur.fetchone())

    summary_columns = (
        "COUNT(*) AS turns, COUNT(DISTINCT app_name, user_id, session_id) AS sessions, "
        "COUNT(DISTINCT app_name, user_id) AS users, "
        "COUNT(CASE WHEN status = 'completed' THEN 1 END) AS completed, "
        "COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed, "
        "COUNT(CASE WHEN status = 'interrupted' THEN 1 END) AS interrupted, "
        "COUNT(CASE WHEN status = 'unknown' THEN 1 END) AS unknown, "
        "COUNT(CASE WHEN history_saved = 1 THEN 1 END) AS history_saved, "
        "COUNT(elapsed_ms) AS elapsed_rows, AVG(elapsed_ms) / 1000 AS elapsed_avg_seconds"
    )
    summary = await query("chat_turns", summary_columns, "asked_at")

    daily = await query(
        "chat_turns",
        "DATE(asked_at) AS day, COUNT(*) AS turns, "
        "COUNT(CASE WHEN status = 'completed' THEN 1 END) AS completed, "
        "COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed, "
        "AVG(elapsed_ms) / 1000 AS elapsed_avg_seconds",
        "asked_at",
        group_by="DATE(asked_at)",
    )
    usage = await query("usage_log", _USAGE_AGGREGATES, "created_at")
    models = await query("usage_log", "model, " + _USAGE_AGGREGATES, "created_at", group_by="model")
    components = await query(
        "usage_log", "component, " + _USAGE_AGGREGATES, "created_at", group_by="component"
    )
    feedback = await query(
        "turn_feedback",
        "COUNT(CASE WHEN rating = 'up' THEN 1 END) AS likes, "
        "COUNT(CASE WHEN rating = 'down' THEN 1 END) AS dislikes",
        "updated_at",
    )
    clicks = await query("turn_click", "COUNT(*) AS clicks", "created_at")
    conditions, params = period_filter("asked_at", period, app_name)
    conditions.append("elapsed_ms IS NOT NULL")
    await cur.execute(
        "SELECT COUNT(*) AS elapsed_rows, "
        "MAX(CASE WHEN rank_no = CEIL(samples * 0.5) THEN elapsed_ms END) / 1000 AS p50_seconds, "
        "MAX(CASE WHEN rank_no = CEIL(samples * 0.95) THEN elapsed_ms END) / 1000 AS p95_seconds, "
        "MAX(elapsed_ms) / 1000 AS max_seconds FROM ("
        "SELECT elapsed_ms, ROW_NUMBER() OVER (ORDER BY elapsed_ms) AS rank_no, "
        "COUNT(*) OVER () AS samples FROM chat_turns" + sql_where(conditions) + ") measured",
        params,
    )
    latency = jsonable(await cur.fetchone())
    comparison = None
    since, until = period
    if since and until:
        length = (until - since).days + 1
        if (since - date.min).days >= length:
            previous = (since - timedelta(days=length), since - timedelta(days=1))
            comparison = {
                "period": {"since": previous[0], "until": previous[1], "timezone": "UTC"},
                "summary": await query(
                    "chat_turns", summary_columns, "asked_at", selected_period=previous
                ),
                "usage": await query(
                    "usage_log", _USAGE_AGGREGATES, "created_at", selected_period=previous
                ),
            }
    return {
        "period": {"since": period[0], "until": period[1], "timezone": "UTC"},
        "summary": summary,
        "daily": daily,
        "usage": usage,
        "models": models,
        "components": components,
        "feedback": feedback,
        "clicks": clicks,
        "latency": latency,
        "comparison": comparison,
    }
