"""Normalize the existing MySQL database during a drained writer maintenance window.

Run without --apply for a read-only inventory. Back up with mysqldump before applying.
Legacy authentication tables remain under archive names until the verified cutover.
The migration is restartable; existing staged data is never silently overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pymysql
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yes24_agent.config import get_settings  # noqa: E402

ROOT = Path(__file__).resolve().parent
VERSION = "20260909_normalize"


def connect():
    url = make_url(get_settings().session_db_url)
    if url.get_backend_name() != "mysql":
        raise ValueError("MySQL required")
    return pymysql.connect(
        host=url.host,
        port=url.port or 3306,
        user=url.username,
        password=url.password,
        database=url.database,
        charset="utf8mb4",
        autocommit=True,
        init_command="SET time_zone = '+00:00'",
        cursorclass=pymysql.cursors.DictCursor,
    )


def exists(cur, table):
    cur.execute(
        "SELECT COUNT(*) n FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND table_name=%s",
        (table,),
    )
    return bool(cur.fetchone()["n"])


def statements(path):
    sql = "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("--")
    )
    return [part.strip() for part in sql.split(";") if part.strip()]


def phase_done(cur, phase):
    cur.execute("SELECT 1 FROM schema_migrations WHERE version=%s", (VERSION + ":" + phase,))
    return cur.fetchone() is not None


def mark(cur, phase, details):
    cur.execute(
        "INSERT INTO schema_migrations(version,details) VALUES(%s,%s)",
        (VERSION + ":" + phase, json.dumps(details, ensure_ascii=False)),
    )


def auth_migration(conn, cur):
    if phase_done(cur, "auth"):
        return
    if exists(cur, "auth_keys") and exists(cur, "legacy_users_20260909"):
        verify_auth(cur, "", "legacy_users_20260909", "legacy_rate_limit_log_20260909")
        mark(cur, "auth", {"recovered_after_atomic_rename": True})
        return
    cur.execute("SELECT * FROM users ORDER BY updated_at,id")
    legacy = cur.fetchall()
    groups = defaultdict(list)
    for row in legacy:
        if not row["user_no"]:
            raise ValueError("Unidentified legacy user: explicit disposition required")
        groups[row["user_no"]].append(row)
    for rows in groups.values():
        for field in ("rate_limit_rpm", "rate_limit_rpd", "rbti"):
            values = {row[field] for row in rows if row[field] is not None}
            if len(values) > 1:
                raise ValueError(f"Conflicting user attribute: {field}")
    for statement in statements(ROOT / "auth_schema.sql"):
        for table in ("rate_limit_log", "auth_keys", "users"):
            statement = statement.replace(f"TABLE {table} (", f"TABLE IF NOT EXISTS next_{table} (")
            statement = statement.replace(f"REFERENCES {table}(", f"REFERENCES next_{table}(")
        cur.execute(statement)
    conn.begin()
    try:
        for user_no, rows in groups.items():
            latest = rows[-1]
            rbti = next((r["rbti"] for r in reversed(rows) if r["rbti"] is not None), None)
            cur.execute(
                "INSERT INTO next_users(user_no,user_login_id,rate_limit_rpm,rate_limit_rpd,"
                "rbti,created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id=id",
                (
                    user_no,
                    latest["user_login_id"],
                    latest["rate_limit_rpm"],
                    latest["rate_limit_rpd"],
                    rbti,
                    min(r["created_at"] for r in rows),
                    latest["updated_at"],
                ),
            )
            cur.execute("SELECT id FROM next_users WHERE user_no=%s", (user_no,))
            user_id = cur.fetchone()["id"]
            for row in rows:
                raw = row["raw_user_info"]
                if raw is not None:
                    raw = json.dumps(json.loads(raw), ensure_ascii=False)
                cur.execute(
                    "INSERT INTO next_auth_keys(key_hash,user_id,is_active,kind,raw_user_info,"
                    "user_cached_at,created_at,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id=id",
                    (
                        hashlib.sha256(row["api_key"].encode()).digest(),
                        user_id,
                        row["is_active"],
                        "dev" if row["api_key"] == get_settings().dev_api_key else "member",
                        raw,
                        row["user_cached_at"],
                        row["created_at"],
                        row["updated_at"],
                    ),
                )
        cur.execute(
            "INSERT INTO next_rate_limit_log(id,user_id,auth_key_id,requested_at,endpoint) "
            "SELECT l.id,k.user_id,k.id,l.requested_at,l.endpoint FROM rate_limit_log l "
            "JOIN next_auth_keys k ON k.key_hash=UNHEX(SHA2(l.api_key,256)) "
            "ON DUPLICATE KEY UPDATE id=next_rate_limit_log.id"
        )
        for table, expected in (("next_users", len(groups)), ("next_auth_keys", len(legacy))):
            cur.execute(f"SELECT COUNT(*) n FROM {table}")
            if cur.fetchone()["n"] != expected:
                raise ValueError(f"Count mismatch: {table}")
        cur.execute(
            "SELECT (SELECT COUNT(*) FROM next_rate_limit_log)="
            "(SELECT COUNT(*) FROM rate_limit_log) ok"
        )
        if not cur.fetchone()["ok"]:
            raise ValueError("Unmapped rate request")
        verify_auth(cur, "next_", "users", "rate_limit_log")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    cur.execute(
        "RENAME TABLE users TO legacy_users_20260909, "
        "rate_limit_log TO legacy_rate_limit_log_20260909, "
        "next_users TO users, next_auth_keys TO auth_keys, next_rate_limit_log TO rate_limit_log"
    )
    mark(cur, "auth", {"users": len(groups), "keys": len(legacy)})


def verify_auth(cur, prefix, source_users, source_rates):
    cur.execute(f"SELECT * FROM {source_users} ORDER BY updated_at,id")
    source = cur.fetchall()
    grouped = defaultdict(list)
    for row in source:
        grouped[row["user_no"]].append(row)
    cur.execute(f"SELECT * FROM {prefix}users")
    users = {row["user_no"]: row for row in cur.fetchall()}
    if len(users) != len(grouped):
        raise ValueError("User mapping mismatch")
    for no, rows in grouped.items():
        expected = rows[-1]
        actual = users[no]
        for field in ("rate_limit_rpm", "rate_limit_rpd", "user_login_id"):
            if actual[field] != expected[field]:
                raise ValueError(f"Staged user content differs: {field}")
        expected_rbti = next((r["rbti"] for r in reversed(rows) if r["rbti"] is not None), None)
        if actual["rbti"] != expected_rbti or not actual["is_active"]:
            raise ValueError("Staged user state differs")
    cur.execute(f"SELECT * FROM {prefix}auth_keys")
    keys = {row["key_hash"]: row for row in cur.fetchall()}
    if len(keys) != len(source):
        raise ValueError("Key mapping mismatch")
    for row in source:
        key = keys[hashlib.sha256(row["api_key"].encode()).digest()]
        raw = json.loads(row["raw_user_info"]) if row["raw_user_info"] is not None else None
        staged_raw = json.loads(key["raw_user_info"]) if key["raw_user_info"] is not None else None
        kind = "dev" if row["api_key"] == get_settings().dev_api_key else "member"
        if (
            key["user_id"] != users[row["user_no"]]["id"]
            or key["is_active"] != row["is_active"]
            or key["kind"] != kind
            or staged_raw != raw
            or key["user_cached_at"] != row["user_cached_at"]
        ):
            raise ValueError("Staged credential content differs")
    cur.execute(
        f"SELECT COUNT(*) n FROM {source_rates} l "
        f"LEFT JOIN {prefix}rate_limit_log n ON n.id=l.id "
        f"LEFT JOIN {prefix}auth_keys k ON k.key_hash=UNHEX(SHA2(l.api_key,256)) "
        "WHERE n.id IS NULL OR k.id IS NULL OR NOT(n.auth_key_id <=> k.id) "
        "OR n.user_id<>k.user_id "
        "OR NOT(n.requested_at <=> l.requested_at) OR NOT(BINARY n.endpoint <=> BINARY l.endpoint)"
    )
    if cur.fetchone()["n"]:
        raise ValueError("Staged request content differs")


def clocks_and_json(conn, cur):
    if phase_done(cur, "clock_json"):
        return
    cur.execute("SELECT id,app_name,user_id,session_id,event_data,timestamp FROM events")
    events = cur.fetchall()
    latest = {}
    changes = []
    for event in events:
        data = json.loads(event["event_data"])
        epoch = data.get("timestamp")
        if not isinstance(epoch, (int, float)):
            raise ValueError("Event missing original epoch")
        utc = datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)
        key = (event["app_name"], event["user_id"], event["session_id"])
        latest[key] = max(latest.get(key, utc), utc)
        changes.append((utc, json.dumps(data, ensure_ascii=False), event["id"], *key))
    conn.begin()
    try:
        for key, utc in latest.items():
            cur.execute(
                "SELECT update_time FROM sessions "
                "WHERE app_name=%s AND user_id=%s AND id=%s FOR UPDATE",
                key,
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("Event without session")
            old_epoch = row["update_time"].replace(tzinfo=timezone.utc).timestamp()
            new_epoch = utc.replace(tzinfo=timezone.utc).timestamp()
            cur.execute(
                "UPDATE session_ui SET last_read_at=LEAST(last_read_at + %s,%s) "
                "WHERE user_id=%s AND session_id=%s",
                (new_epoch - old_epoch, new_epoch, key[1], key[2]),
            )
            if old_epoch != new_epoch:
                cur.execute(
                    "UPDATE sessions SET update_time=%s WHERE app_name=%s AND user_id=%s AND id=%s",
                    (utc, *key),
                )
        cur.executemany(
            "UPDATE events SET timestamp=%s,event_data=%s "
            "WHERE id=%s AND app_name=%s AND user_id=%s AND session_id=%s",
            changes,
        )
        cur.execute("SELECT app_name,user_id,id,state FROM sessions")
        for row in cur.fetchall():
            cur.execute(
                "UPDATE sessions SET state=%s WHERE app_name=%s AND user_id=%s AND id=%s",
                (
                    json.dumps(json.loads(row["state"]), ensure_ascii=False),
                    row["app_name"],
                    row["user_id"],
                    row["id"],
                ),
            )
        mark(cur, "clock_json", {"events": len(events), "sessions_with_events": len(latest)})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def apply_named_constraints(cur, filename):
    for statement in statements(ROOT / filename):
        match = re.fullmatch(
            r"ALTER TABLE\s+(\w+)\s+ADD CONSTRAINT\s+(\w+)\s+.+", statement, re.DOTALL
        )
        if not match:
            raise ValueError("Expected a named integrity constraint")
        table, constraint = match.groups()
        cur.execute(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_schema=DATABASE() AND table_name=%s AND constraint_name=%s",
            (table, constraint),
        )
        if not cur.fetchone():
            cur.execute(statement)


def session_integrity(cur):
    if phase_done(cur, "session_integrity"):
        return
    apply_named_constraints(cur, "session_integrity.sql")
    mark(cur, "session_integrity", {"session_foreign_keys": 3, "feedback_rating_check": True})


def history_saved_schema(cur):
    if phase_done(cur, "history_saved_schema"):
        return
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() "
        "AND table_name='chat_turn' AND column_name='history_saved'"
    )
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE chat_turn ADD COLUMN history_saved BOOLEAN NOT NULL DEFAULT TRUE"
        )
    cur.execute("UPDATE chat_turn SET history_saved=FALSE WHERE status='unknown'")
    cur.execute(
        "SELECT 1 FROM information_schema.table_constraints WHERE constraint_schema=DATABASE() "
        "AND table_name='chat_turn' AND constraint_name='ck_chat_turn_history_saved'"
    )
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE chat_turn ADD CONSTRAINT ck_chat_turn_history_saved "
            "CHECK (history_saved IN (0, 1))"
        )
    mark(cur, "history_saved_schema", {"independent_of_status": True})


def usage_scope_integrity(cur):
    if phase_done(cur, "usage_scope_integrity"):
        return
    apply_named_constraints(cur, "usage_scope_integrity.sql")
    mark(cur, "usage_scope_integrity", {"nonempty_app_name": True})


def migrate(conn, cur):
    cur.execute("SELECT GET_LOCK(%s,0) locked", (VERSION,))
    if not cur.fetchone()["locked"]:
        raise RuntimeError("Another migration owns the database")
    try:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version VARCHAR(96) PRIMARY KEY, "
            "applied_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),"
            "details JSON NOT NULL) ENGINE=InnoDB "
            "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        auth_migration(conn, cur)
        if not phase_done(cur, "collation"):
            for table in ("usage_log", "turn_feedback", "turn_click"):
                cur.execute(
                    f"ALTER TABLE {table} CONVERT TO CHARACTER SET utf8mb4 "
                    "COLLATE utf8mb4_unicode_ci"
                )
            mark(cur, "collation", {"tables": 3})
        if not phase_done(cur, "scope"):
            app_name = get_settings().app_name
            cur.execute("SELECT DISTINCT app_name FROM sessions")
            apps = {row["app_name"] for row in cur.fetchall()}
            if apps and apps != {app_name}:
                raise ValueError("Legacy app differs: explicit sidecar ownership mapping required")
            for table in ("usage_log", "turn_feedback", "turn_click", "session_ui"):
                cur.execute(
                    "SELECT COUNT(*) n FROM information_schema.columns "
                    "WHERE table_schema=DATABASE() AND table_name=%s AND column_name='app_name'",
                    (table,),
                )
                if not cur.fetchone()["n"]:
                    cur.execute(
                        f"ALTER TABLE {table} ADD COLUMN app_name VARCHAR(128) NOT NULL DEFAULT %s",
                        (app_name,),
                    )
                cur.execute(f"ALTER TABLE {table} ALTER COLUMN app_name DROP DEFAULT")
                nullable = "NULL" if table == "usage_log" else "NOT NULL"
                cur.execute(f"ALTER TABLE {table} MODIFY user_id VARCHAR(128) {nullable}")
                if table != "session_ui":
                    cur.execute(f"ALTER TABLE {table} MODIFY turn_id VARCHAR(256) {nullable}")
            indexes = (
                ("session_ui", "uk_session_ui_owner", "UNIQUE", "app_name,user_id,session_id"),
                (
                    "turn_feedback",
                    "uq_turn_feedback_user_turn",
                    "UNIQUE",
                    "app_name,user_id,session_id,turn_id",
                ),
                (
                    "turn_click",
                    "idx_turn_click_owner_turn",
                    "",
                    "app_name,user_id,session_id,turn_id",
                ),
            )
            for table, index, unique, columns in indexes:
                cur.execute(
                    f"ALTER TABLE {table} DROP INDEX {index}, "
                    f"ADD {unique} INDEX {index} ({columns})"
                )
            mark(cur, "scope", {"app_name": app_name})
        clocks_and_json(conn, cur)
        if not phase_done(cur, "turn_schema"):
            for statement in statements(ROOT / "chat_turn.sql"):
                cur.execute(statement)
            cur.execute(
                "SELECT app_name,user_id,session_id,turn_id,started_at,completed_at,"
                "user_text,text,status,sources,process,meta,error,rbti_applied "
                "FROM chat_turn WHERE 1=0"
            )
            cur.execute(
                "SELECT GROUP_CONCAT(column_name ORDER BY seq_in_index) cols "
                "FROM information_schema.statistics WHERE table_schema=DATABASE() "
                "AND table_name='chat_turn' AND non_unique=0 GROUP BY index_name"
            )
            if "app_name,user_id,session_id,turn_id" not in {r["cols"] for r in cur.fetchall()}:
                raise ValueError("chat_turn scoped unique key missing")
            mark(cur, "turn_schema", {})
        history_saved_schema(cur)
        session_integrity(cur)
        usage_scope_integrity(cur)
    finally:
        cur.execute("SELECT RELEASE_LOCK(%s)", (VERSION,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.writers_stopped:
        parser.error("--apply requires a drained writer window (--writers-stopped)")
    conn = connect()
    try:
        with conn.cursor() as cur:
            if args.apply:
                migrate(conn, cur)
            cur.execute(
                "SELECT table_name,table_type FROM information_schema.tables "
                "WHERE table_schema=DATABASE() ORDER BY table_name"
            )
            print(json.dumps(cur.fetchall(), ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
