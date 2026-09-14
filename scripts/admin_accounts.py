"""관리자 계정 CLI — 첫 owner 생성(`create`)과 잠긴 운영자 복구(`reset-password`) 전용.

설계 §2.3·D6.

운영 DB 쓰기는 사람이 승인한 순간에만 일어나야 하므로 앱 기동 시 자동 부트스트랩을 두지
않는다. 그 밖의 계정 관리(목록·역할·비활성)는 owner API가 소유한다 — 최후 owner 규칙이 거기
있어서, CLI에 같은 기능을 두면 규칙 없는 우회로가 된다. 접속은 `SESSION_DB_URL`
(migrate_database.connect)이며, 쓰기 전에 대상 host/db를 stderr에 찍는다. 변경은
`admin_audit`에 actor NULL·`after.by="cli"`로 같은 트랜잭션에 남는다.

  uv run python scripts/admin_accounts.py create --username alice --role owner
  uv run python scripts/admin_accounts.py reset-password --username alice   # 로그인 잠금도 풀린다

비밀번호는 에코 없는 프롬프트(두 번) 또는 env `ADMIN_BOOTSTRAP_PASSWORD`(1회용)로 받는다.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import pymysql
from pydantic import ValidationError
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migrate_database import connect  # noqa: E402

from yes24_agent.admin_auth import (  # noqa: E402
    AUDIT_INSERT,
    PASSWORD_UPDATE,
    REVOKE_SESSIONS,
    ROLES,
    AdminCreate,
    PasswordReset,
    audit_params,
    hash_password,
)
from yes24_agent.config import get_settings  # noqa: E402

_CLI = {"by": "cli"}


def _password() -> str:
    password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD")
    if not password:
        password = getpass.getpass("새 비밀번호: ")
        if getpass.getpass("한 번 더: ") != password:
            sys.exit("비밀번호가 서로 다릅니다.")
    return password


def _validated(model, **fields):
    """앱 API와 같은 본문 모델로 검증 — 규칙(계정명 형식·비밀번호 길이)이 한 벌이다."""
    try:
        return model(**fields)
    except ValidationError as exc:
        sys.exit("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))


def _write(conn, work) -> None:
    """한 트랜잭션: 변경과 감사가 함께 커밋되거나 함께 사라진다."""
    conn.begin()
    try:
        with conn.cursor() as cur:
            work(cur)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def create(conn, args) -> None:
    body = _validated(AdminCreate, username=args.username, password=_password(), role=args.role)
    password_hash = hash_password(body.password, get_settings())

    def work(cur):
        try:
            cur.execute(
                "INSERT INTO admin_users (username, password_hash, role) VALUES (%s, %s, %s)",
                (body.username, password_hash, body.role),
            )
        except pymysql.IntegrityError:
            raise SystemExit(f"이미 있는 계정명입니다: {body.username}") from None
        admin_id = cur.lastrowid
        after = {**_CLI, "username": body.username, "role": body.role}
        cur.execute(AUDIT_INSERT, audit_params(None, "admin", admin_id, "create", after=after))
        print(f"생성: id={admin_id} username={body.username} role={body.role}")

    _write(conn, work)


def reset_password(conn, args) -> None:
    body = _validated(PasswordReset, new_password=_password())
    password_hash = hash_password(body.new_password, get_settings())

    def work(cur):
        cur.execute("SELECT id FROM admin_users WHERE username = %s FOR UPDATE", (args.username,))
        account = cur.fetchone()
        if account is None:
            raise SystemExit(f"계정이 없습니다: {args.username}")
        cur.execute(PASSWORD_UPDATE, (password_hash, account["id"]))
        cur.execute(REVOKE_SESSIONS, (account["id"],))
        revoked = cur.rowcount
        cur.execute(
            AUDIT_INSERT, audit_params(None, "admin", account["id"], "password_reset", after=_CLI)
        )
        print(f"재설정: username={args.username} 폐기한 세션={revoked} (로그인 잠금도 풀린다)")

    _write(conn, work)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("create")
    sub.add_argument("--username", required=True)
    sub.add_argument("--role", required=True, choices=ROLES)
    sub.set_defaults(handler=create)
    sub = commands.add_parser("reset-password")
    sub.add_argument("--username", required=True)
    sub.set_defaults(handler=reset_password)
    args = parser.parse_args()

    url = make_url(get_settings().session_db_url)
    print(f"대상 DB: {url.host}:{url.port}/{url.database}", file=sys.stderr)
    conn = connect()
    try:
        args.handler(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
