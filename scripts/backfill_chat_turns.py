"""ADK events를 공개 chat_turn으로 백필한다. 기본은 읽기 전용 dry-run이다.

    uv run python scripts/backfill_chat_turns.py --dry-run
    uv run python scripts/backfill_chat_turns.py --report
    uv run python scripts/backfill_chat_turns.py --commit --before 2026-09-09T08:00:00Z

실제 적용은 채팅 writer를 중지한 상태에서 한다. 진행 중 턴을 unknown으로 먼저 확정하면
나중의 정상 스냅샷과 불변 충돌이 생긴다. --before는 UTC 완료시각 상한이며 writer 중지를
대신하지 않는다. 원본 이벤트·스냅샷은 삭제하지 않는다. 재실행은 동일값을 확인하며 충돌은
오류로 종료한다. 스냅샷 없는 구 턴은 history_saved=false로 보존하며, 종료 상태는
명시적 중단·오류 증거만 반영한다. 정상 모델 STOP만으로 완료를 추측하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.events import Event  # noqa: E402
from google.adk.sessions import Session  # noqa: E402
from sqlalchemy import text as sql_text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yes24_agent.config import get_settings  # noqa: E402
from yes24_agent.event_translate import (  # noqa: E402
    PROCESS_TIMING_KEY,
    _sources_from_response,
    settle_sources,
)
from yes24_agent.history import _assemble_turns, _restore_turn_payload  # noqa: E402
from yes24_agent.sse import STREAM_ERROR_MESSAGE  # noqa: E402
from yes24_agent.user_data import UserDataService  # noqa: E402


def extract_session_turns(
    session: Session, main_outcomes: dict[str, str | None] | None = None
) -> list[dict[str, Any]]:
    """턴 시점별 출처 레지스트리와 원본 epoch로 공개 행을 재구성한다."""
    observations: dict[str, list[dict]] = collections.defaultdict(list)
    times: dict[str, list[float]] = collections.defaultdict(list)
    last_model: dict[str, Event] = {}
    for event in session.events:
        if not event.invocation_id or event.partial:
            continue
        times[event.invocation_id].append(event.timestamp)
        if event.author != "user" and PROCESS_TIMING_KEY not in (event.custom_metadata or {}):
            last_model[event.invocation_id] = event
        for response in event.get_function_responses() or []:
            payload = response.response or {}
            if isinstance(payload, dict) and payload.get("status") != "error":
                observations[event.invocation_id].extend(_sources_from_response(payload))
    registry: list[dict] = []
    rows: list[dict[str, Any]] = []
    for raw in _assemble_turns(session.events):
        turn_id = raw["turn_id"]
        registry = settle_sources(registry, observations[turn_id])
        payload = (
            dict(raw["snapshot"])
            if raw["snapshot"] is not None
            else (_restore_turn_payload(raw, registry, session.id))
        )
        if raw["snapshot"] is None:
            outcome = (main_outcomes or {}).get(turn_id)
            if outcome == "aborted":
                payload.update(
                    status="interrupted",
                    error={"code": "stream_interrupted", "message": "응답 수신이 중단됐어요."},
                )
            elif (
                outcome != "ok"
                and (terminal := last_model.get(turn_id)) is not None
                and terminal.error_code
            ):
                payload.update(
                    status="failed",
                    error={"code": "stream_error", "message": STREAM_ERROR_MESSAGE},
                )
        rows.append(
            {
                "app_name": session.app_name,
                "user_id": session.user_id,
                "session_id": session.id,
                "turn_id": turn_id,
                "user_text": "\n".join(raw["user"]),
                "started_at": min(times[turn_id]),
                "completed_at": max(times[turn_id]),
                "payload": payload,
            }
        )
    return rows


async def iter_all_turns(session_id: str | None, limit: int | None):
    """JSON 안의 원본 epoch를 읽는다. ADK DATETIME의 writer 시간대에 의존하지 않는다."""
    settings = get_settings()
    engine = create_async_engine(settings.session_db_url)
    try:
        async with engine.connect() as conn:
            if conn.dialect.name == "mysql":
                await conn.execute(sql_text("START TRANSACTION READ ONLY"))
            query = "SELECT app_name, user_id, id FROM sessions WHERE app_name = :app"
            params: dict[str, Any] = {"app": settings.app_name}
            if session_id:
                query += " AND id = :sid"
                params["sid"] = session_id
            query += " ORDER BY app_name, user_id, id"
            if limit is not None:
                query += " LIMIT :limit"
                params["limit"] = limit
            keys = (await conn.execute(sql_text(query), params)).all()
            for app_name, user_id, sid in keys:
                result = await conn.execute(
                    sql_text(
                        "SELECT event_data FROM events WHERE app_name = :app "
                        "AND user_id = :uid AND session_id = :sid"
                    ),
                    {"app": app_name, "uid": user_id, "sid": sid},
                )
                events = []
                for (raw,) in result:
                    payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                    if "timestamp" not in payload:
                        raise ValueError("원본 이벤트 epoch 누락: 백필을 중단합니다.")
                    events.append(Event.model_validate(payload))
                events.sort(key=lambda event: event.timestamp)
                session = Session(app_name=app_name, user_id=user_id, id=sid, events=events)
                main_outcomes = {}
                if conn.dialect.name == "mysql":
                    result = await conn.execute(
                        sql_text(
                            "SELECT turn_id, outcome FROM usage_log WHERE app_name = :app "
                            "AND user_id = :uid AND session_id = :sid "
                            "AND component = 'main' AND turn_id IS NOT NULL"
                        ),
                        {"app": app_name, "uid": user_id, "sid": sid},
                    )
                    for turn_id, outcome in result:
                        if turn_id in main_outcomes:
                            raise ValueError("동일 턴의 main 계측이 중복되어 백필을 중단합니다.")
                        main_outcomes[turn_id] = outcome
                for row in extract_session_turns(session, main_outcomes):
                    yield row
    finally:
        await engine.dispose()


def report(rows: list[dict[str, Any]]) -> None:
    counts = collections.Counter()
    for row in rows:
        payload = row["payload"]
        counts["turns"] += 1
        counts["snapshot" if payload["history_saved"] else "restored"] += 1
        counts["status_" + payload["status"]] += 1
        counts["with_answer"] += bool(payload["text"])
        counts["empty_restored"] += not payload["history_saved"] and not payload["text"]
    print(json.dumps(dict(counts), ensure_ascii=False))


async def write_rows(rows: list[dict[str, Any]], batch_size: int) -> int:
    data = UserDataService.get_instance()
    if not data.enabled:
        raise ValueError("chat_turn 백필 쓰기는 MySQL에서만 지원합니다.")
    written = 0
    try:
        for row in rows:
            if not await data.save_turn(**row, verify_completed_at=True):
                raise ValueError("기존 chat_turn과 백필 값이 다릅니다. 덮어쓰지 않고 중단합니다.")
            written += 1
            if written % batch_size == 0:
                print(f"저장/동일값 확인: {written}")
    finally:
        await data.close()
    return written


def utc_cutoff(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--before에는 UTC 오프셋 또는 Z가 필요합니다.")
    return parsed.timestamp()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--commit", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--session")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--before", type=utc_cutoff)
    args = parser.parse_args()
    if args.batch_size < 1 or (args.limit is not None and args.limit < 1):
        parser.error("batch-size와 limit은 양수여야 합니다.")
    if args.commit and args.before is None:
        parser.error("--commit에는 writer 중지 시점의 --before UTC 시각이 필요합니다.")
    rows = [
        row
        async for row in iter_all_turns(args.session, args.limit)
        if args.before is None or row["completed_at"] < args.before
    ]
    report(rows)
    if args.commit:
        print(f"저장/동일값 확인 완료: {await write_rows(rows, args.batch_size)}")
    else:
        print("dry-run: DB 변경 없음")


if __name__ == "__main__":
    asyncio.run(main())
