"""관리자 정체성 — 개인 계정·서버측 세션·로그인 시도 제한·변경 감사 트랜잭션(누가 했는가).

설계 정본: docs/admin-management-design-20260914.md. 이 모듈은 **누가**를 소유하고, 무엇을
보고 바꾸는지는 admin.py(데이터)·starters.py(초기 질문)가 소유한다 — 그 둘이 이 모듈을
import하고 역방향은 없다.

- 비밀번호는 `hashlib.scrypt`(stdlib, 메모리 하드). 저장 문자열이 파라미터를 품어
  config를 바꿔도 기존 해시가 검증된다. import 자체가 scrypt를 요구하므로 OpenSSL이
  scrypt를 못 주는 환경에선 앱 기동이 즉시 실패한다(fail-loud).
- 세션 토큰은 무작위 원문을 쿠키에만 두고 DB엔 SHA-256만 둔다. 폐기가 행 UPDATE라 같은
  RDS를 쓰는 서버 전부에 즉시 적용된다(옛 HMAC 쿠키는 폐기가 불가능했다).
- 로그인 시도 제한은 별도 테이블 없이 `admin_audit`의 `login/failed` 행을 창 안에서 센다.
- 변경과 감사는 같은 커넥션·같은 트랜잭션이다(`AdminService.transaction`).
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import scrypt, sha256
from typing import Annotated, Any, Literal

import aiomysql
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)
from pydantic_core import PydanticCustomError

from yes24_agent.admin_data import BOOL_DECODERS, jsonable
from yes24_agent.config import Settings, get_settings
from yes24_agent.db import MysqlBackedService
from yes24_agent.session_service import mysql_pool_kwargs

logger = logging.getLogger(__name__)

# 순서 = 권한 순위. 역할의 유일한 정의다(DDL CHECK가 같은 값을 강제한다).
ROLES: tuple[str, ...] = ("viewer", "editor", "owner")
# 쿠키 이름·path는 옛 게이트와 같다 — /admin/starters도 path 안에 든다.
ADMIN_COOKIE = "yes24_admin"
_COOKIE_PATH = "/admin"

# 해시 문자열 형식 `scrypt$<log2 n>$<r>$<p>$<salt b64>$<dk b64>`의 구성 상수. 조정 대상인
# 비용 파라미터(n·r·p)는 config이고, salt·dk 길이는 형식의 일부라 여기 둔다(검증은 저장된
# dk 길이를 따르므로 바꿔도 기존 해시가 깨지지 않는다).
_HASH_SCHEME = "scrypt"
_SALT_BYTES = 16
_DK_BYTES = 32
# 세션 토큰 원문 엔트로피(바이트). SHA-256 digest(BINARY(32))로만 저장된다.
_TOKEN_BYTES = 32
# admin_sessions.ip·admin_audit.ip `VARCHAR(45)`(scripts/admin_management.sql, IPv6 텍스트 최대
# 길이). 기록 IP는 위조 가능한 XFF라 임의 길이다 — 넘치면 실패 예약 INSERT가 503이 돼 시도
# 제한을 우회한다.
_IP_COLUMN_CHARS = 45

_ROLE = Literal[ROLES]  # type: ignore[valid-type]
# DDL VARCHAR(64)와 같은 상한. 소문자 고정 — 대소문자 변형 중복 계정을 막는다.
_USERNAME = Annotated[str, StringConstraints(pattern=r"^[a-z0-9._-]{3,64}$")]

_UNAUTHORIZED = "인증이 필요합니다."
_LOGIN_FAILED = "아이디 또는 비밀번호가 올바르지 않습니다."
_LOCKED = "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요."
_CONFLICT = "다른 관리자가 먼저 바꿨습니다."
_LAST_OWNER = "마지막 활성 owner는 강등·비활성화할 수 없습니다."

AUDIT_INSERT = (
    "INSERT INTO admin_audit (actor_id, actor_name, target_type, target_id, action, "
    "`before`, `after`, ip, session_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

# 비밀번호 교체 — 본인 변경·owner 재설정·CLI 재설정이 같은 문장을 쓴다. password_changed_at은
# 로그인 실패 카운트의 기준점이라(재설정 = 잠금 해제) 해시와 반드시 함께 바뀐다.
PASSWORD_UPDATE = (
    "UPDATE admin_users SET password_hash = %s, password_changed_at = NOW(3) WHERE id = %s"
)

# 계정의 살아 있는 세션 전부 폐기 — 앱(owner 조치)·CLI(재설정)가 같은 문장을 쓴다.
REVOKE_SESSIONS = (
    "UPDATE admin_sessions SET revoked_at = NOW(3) WHERE admin_user_id = %s AND revoked_at IS NULL"
)

# 실패 예약 취소 — 잠긴 시도·성공한 시도의 예약 행만 지운다(id로만, 다른 행에 닿지 않는다).
_RESERVATION_DELETE = "DELETE FROM admin_audit WHERE id = %s"

# 로그인 실패 카운트(§3.4 R2). 기준점 = max(창 시작, 비밀번호 변경 시각, 마지막 성공 로그인)
# — 재설정과 성공 로그인이 잠금을 자연히 풀어 "해제" 상태·API가 따로 없다.
_FAILURE_COUNT = (
    "SELECT COUNT(*) AS failures FROM admin_audit "
    "WHERE target_type = 'login' AND action = 'failed' AND target_id = %s "
    "AND created_at > GREATEST("
    "NOW(3) - INTERVAL %s SECOND, "
    "COALESCE(%s, '1970-01-01'), "
    "COALESCE((SELECT MAX(created_at) FROM admin_audit "
    "WHERE target_type = 'login' AND action = 'ok' AND target_id = %s), '1970-01-01'))"
)


def audit_params(
    actor: AdminActor | None,
    target_type: str,
    target_id: str | int,
    action: str,
    before: dict | None = None,
    after: dict | None = None,
    *,
    ip: str | None = None,
) -> tuple:
    """`AUDIT_INSERT` 바인딩 — 앱·CLI가 같은 행 모양을 쓴다(actor None = 인증 전·CLI)."""

    def dump(value: dict | None) -> str | None:
        return None if value is None else json.dumps(jsonable(value), ensure_ascii=False)

    return (
        actor.user_id if actor else None,
        actor.username if actor else None,
        target_type,
        str(target_id),
        action,
        dump(before),
        dump(after),
        _recorded_ip(ip),
        actor.session_id if actor else None,
    )


def _recorded_ip(ip: str | None) -> str | None:
    return None if ip is None else ip[:_IP_COLUMN_CHARS]


@dataclass(frozen=True)
class AdminActor:
    session_id: int
    user_id: int
    username: str
    role: str

    def at_least(self, role: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)


@dataclass(frozen=True)
class LoginResult:
    status: Literal["ok", "failed", "locked"]
    token: str | None = None
    actor: AdminActor | None = None
    retry_after_s: int | None = None


class EditBody(BaseModel):
    """편집 프로토콜 본문(§5.3). 라우트는 서브클래스로 `changes`를 `EditChanges` 모델로 좁힌다."""

    changes: dict[str, Any]
    expected: dict[str, Any] | None = None


class EditChanges(BaseModel):
    """PATCH의 changes 공통 규칙 — 모르는 필드·빈 본문은 422, 준 필드만 바꾼다.

    null 허용 여부는 **필드 타입**이 판정한다: NOT NULL 컬럼은 `T = None`(생략 허용, 입력된
    null은 Pydantic이 422), 기간 해제처럼 null이 정상 입력인 컬럼만 `T | None = None`.
    호출부는 `model_dump(exclude_unset=True)`를 넘긴다("안 보냄" = 안 바꿈).
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.model_fields_set:
            raise PydanticCustomError("empty_changes", "바꿀 항목이 없습니다.")
        return self


class EditConflict(HTTPException):
    """expected 불일치 409 — 본문에 현재값을 함께 싣는다(`{"detail", "current"}`)."""

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__(status_code=409, detail=_CONFLICT)
        self.current = current


# ── 해시 ───────────────────────────────────────────────────────────────────


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(password: str, salt: bytes, log2_n: int, r: int, p: int, dklen: int) -> bytes:
    n = 1 << log2_n
    # maxmem을 명시하지 않으면 OpenSSL 기본 상한(32 MiB)에 걸려 기본 파라미터부터 예외다.
    return scrypt(
        password.encode("utf-8", "surrogatepass"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=2 * 128 * r * n,
        dklen=dklen,
    )


def hash_password(password: str, settings: Settings) -> str:
    """동기·CPU 바운드(기본 수십 ms) — 이벤트 루프에선 `asyncio.to_thread`로 부른다."""
    log2_n, r, p = settings.admin_scrypt_log2_n, settings.admin_scrypt_r, settings.admin_scrypt_p
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _derive(password, salt, log2_n, r, p, _DK_BYTES)
    return "$".join((_HASH_SCHEME, str(log2_n), str(r), str(p), _b64(salt), _b64(derived)))


def verify_password(password: str, stored: str) -> bool:
    """저장 문자열의 파라미터로 다시 유도해 상수시간 비교. 형식 오류는 예외 없이 False."""
    try:
        scheme, log2_n, r, p, salt, expected = stored.split("$")
        if scheme != _HASH_SCHEME:
            return False
        expected_raw = base64.b64decode(expected, validate=True)
        derived = _derive(
            password,
            base64.b64decode(salt, validate=True),
            int(log2_n),
            int(r),
            int(p),
            len(expected_raw),
        )
    except (ValueError, OverflowError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected_raw)


# ── 요청 판정 ──────────────────────────────────────────────────────────────


def client_ip(request: Request) -> str:
    """요청 출발지 IP — X-Forwarded-For 첫 항목 우선.

    프록시 없이 직결 배포라 위조 가능한 헤더다. 그래서 차단 근거가 아니라 **기록**으로만
    쓴다(로그인 잠금은 계정명 기준, 로그인월 실패 로그도 관측 신호).
    """
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def require_same_origin(request: Request) -> None:
    """`Sec-Fetch-Site`가 same-site·cross-site면 403.

    Origin을 `scheme://netloc`과 비교하던 판정은 지웠다 — TLS 프록시 뒤에선 request.url.scheme이
    http로 보여 정상 로그인까지 403이 된다. 헤더가 없는 클라이언트(curl)는 통과하지만 쿠키
    CSRF의 주체인 브라우저는 이 헤더를 항상 싣는다. JSON 본문 모델·SameSite=Lax가 나머지 두 겹이다.
    """
    if request.headers.get("sec-fetch-site") in {"same-site", "cross-site"}:
        raise HTTPException(status_code=403, detail="같은 출처에서 요청해 주세요.")


def admin_enabled(settings: Settings) -> bool:
    """어드민 스택이 성립하는가 — 세션 DB가 MySQL이면 등록한다(admin·starters 공통 판정)."""
    return mysql_pool_kwargs(settings.session_db_url, maxsize=settings.admin_pool_max) is not None


# ── 트랜잭션 ───────────────────────────────────────────────────────────────


class AdminTx:
    """한 트랜잭션의 커서 + 감사 기록기. actor·ip·session_id는 여기서 채운다."""

    def __init__(self, cur: aiomysql.DictCursor, actor: AdminActor, ip: str) -> None:
        self.cur = cur
        self.actor = actor
        self._ip = ip

    async def audit(
        self,
        target_type: str,
        target_id: str | int,
        action: str,
        before: dict | None = None,
        after: dict | None = None,
    ) -> None:
        await self.cur.execute(
            AUDIT_INSERT,
            audit_params(self.actor, target_type, target_id, action, before, after, ip=self._ip),
        )

    async def edit_row(
        self,
        table: str,
        row_id: int,
        editable: tuple[str, ...],
        changes: dict,
        expected: dict | None,
        *,
        scope: dict[str, Any] | None = None,
    ) -> tuple[dict, dict]:
        """편집 프로토콜 한 벌(§5.3): 잠금 선조회 → expected 대조 → 같은 값 제거 → UPDATE.

        rowcount로 판정하지 않는다(값이 같으면 0). 비교는 행 값과 `==`다 — expected·changes는
        호출부 모델이 컬럼 타입으로 맞춘 값이다(TINYINT 1 == True, date == date). `scope`는
        소속 조건(열 = 값 동등, 예: 키가 그 회원 것인가)이라 어긋나면 행이 없는 것과 같이 404다.
        컬럼명(editable·scope 키)은 SQL에 보간되므로 호출부 상수여야 하고, changes·expected의
        `editable` 밖 키는 여기서 422로 끊는다. 감사는 target_type을 아는 호출부 몫.
        """
        stray = (set(changes) | set(expected or ())) - set(editable)
        if stray:
            raise HTTPException(status_code=422, detail=f"편집할 수 없는 필드: {sorted(stray)}")
        scope = scope or {}
        columns = ", ".join(f"`{column}`" for column in editable)
        conditions = "".join(f" AND `{column}` = %s" for column in scope)
        await self.cur.execute(
            f"SELECT {columns} FROM `{table}` WHERE id = %s{conditions} FOR UPDATE",
            (row_id, *scope.values()),
        )
        row = await self.cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="대상을 찾을 수 없습니다.")
        if expected is not None and any(row[k] != v for k, v in expected.items()):
            raise EditConflict(jsonable(row))
        after = {k: v for k, v in changes.items() if row[k] != v}
        if not after:
            return {}, {}
        assignments = ", ".join(f"`{column}` = %s" for column in after)
        await self.cur.execute(
            f"UPDATE `{table}` SET {assignments} WHERE id = %s", (*after.values(), row_id)
        )
        return {k: row[k] for k in after}, after


class AdminService(MysqlBackedService):
    """admin_users·admin_sessions·admin_audit 쓰기 풀(프로세스 싱글턴).

    읽기 전용 조회(admin.py)는 요청별 READ ONLY 접속을 그대로 쓴다 — 이 풀을 읽기에 빌려
    쓰면 "읽기 전용은 접속의 속성"이 규율로 퇴화한다.
    """

    _instance: AdminService | None = None

    def __init__(self, pool_factory=None) -> None:
        settings = get_settings()
        pool_kwargs = mysql_pool_kwargs(settings.session_db_url, maxsize=settings.admin_pool_max)
        if pool_kwargs is not None:
            # BOOLEAN→bool은 읽기 전용 접속과 같은 드라이버 디코더 한 벌(409 current·감사 before).
            pool_kwargs["conv"] = BOOL_DECODERS
        super().__init__(
            pool_kwargs,
            pool_factory,
            unavailable_detail="관리자 저장소가 없는 구성입니다(세션 DB가 MySQL이 아님).",
            failure_detail="관리자 DB 요청에 실패했습니다.",
        )
        # 없는 계정명에도 해시를 한 번 계산해 응답 시간으로 계정 존재를 못 가리게 한다.
        # 현재 파라미터로 만들어 실제 계정 검증과 비용이 같다.
        self._dummy_hash = hash_password(secrets.token_urlsafe(_TOKEN_BYTES), settings)
        self._hash_slots = asyncio.Semaphore(settings.admin_password_hash_concurrency)

    @classmethod
    def get_instance(cls) -> AdminService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """풀 커넥션 1개. 드라이버 실패는 503으로, HTTPException은 그대로 올린다."""
        if self._db is None:
            raise HTTPException(status_code=503, detail=self._unavailable_detail)
        try:
            pool = await self._db.get()
            async with pool.acquire() as conn:
                yield conn
        except HTTPException:
            raise
        except (aiomysql.Error, OSError, asyncio.TimeoutError) as exc:
            logger.error(f"{self._failure_detail} ({type(exc).__name__}): {exc}")
            raise HTTPException(status_code=503, detail=self._failure_detail) from exc

    async def scrypt(self, fn, *args):
        """scrypt(해시·검증)를 스레드에서 돌리되 프로세스 동시 수를 config 상한으로 묶는다.

        1회에 128·r·n(기본 32 MiB)이라 인증 없는 로그인 폭주의 메모리 상한이 이 세마포어다.
        호출부는 DB 커넥션을 쥔 채 여기서 기다리지 않는다(풀 고갈 → 전 관리자 세션 판정 대기).
        대기자가 취소돼도 이미 시작한 스레드는 끝까지 돈다 — 상한은 "시작 수" 기준이다.
        """
        async with self._hash_slots:
            return await asyncio.to_thread(fn, *args)

    async def _attempt(self, username: str, password: str, ip: str):
        """시도 제한의 **유일한** 판정 — 실패 예약 → 카운트 → 검증 → 예약 확정/취소.

        로그인과 본인 비밀번호 변경(현재 비밀번호 확인)이 같은 카운트·같은 잠금을 쓴다.

        **실패 행을 검증 전에 예약한다**(보안 M-A). 카운트를 먼저 보고 실패를 나중에 적으면 한
        계정에 동시에 들어온 요청 전원이 같은 낮은 카운트를 보고 전원 검증한다(실측: 동시 100건
        중 오답 401이 99건). 예약은 autocommit이라 즉시 보이고, 동시 요청은 서로의 예약을 센다.
        - 카운트(자기 예약 포함) > max → 예약 삭제 후 locked. 잠긴 시도는 적재하지 않는다(R2 —
          쌓으면 창이 계속 밀려 잠금이 무기한이 되고 테이블이 팽창한다).
        - 검증 실패 → 예약이 곧 `login/failed` 행이다(커밋된 채 반환 — R1).
        - 검증 성공 → 예약은 호출부가 지운다(login은 `login/ok`로 대체, check_password는 삭제만 —
          성공한 확인이 실패로 남으면 본인 변경 한 번이 카운트를 깎는다).
        동시 예약이 서로를 세므로 실제 실패가 max에 못 미쳐도 잠길 수 있다 — fail-closed로 허용한다.
        예약 뒤 검증이 예외·취소로 끊겨도 예약은 실패로 남는다(같은 방향의 허용).

        커넥션은 조회·예약에만 짧게 쓰고 **반납한 뒤** scrypt를 돈다. 검증 동안 쥐면 인증 없는 동시
        로그인 몇 개로 풀이 고갈돼 모든 관리자 요청이 대기한다(결함 판정 2026-09-14).
        반환: (LoginResult, 성공 시 계정 행, 성공 시 지울 예약 id).
        """
        settings = get_settings()
        window = settings.admin_login_window_s
        async with self._connection() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, username, password_hash, role, is_active, password_changed_at "
                "FROM admin_users WHERE username = %s",
                (username,),
            )
            account = await cur.fetchone()
            await cur.execute(AUDIT_INSERT, audit_params(None, "login", username, "failed", ip=ip))
            reservation = cur.lastrowid
            await cur.execute(
                _FAILURE_COUNT,
                (username, window, account["password_changed_at"] if account else None, username),
            )
            if (await cur.fetchone())["failures"] > settings.admin_login_max_failures:
                await cur.execute(_RESERVATION_DELETE, (reservation,))
                logger.warning(f"admin 로그인 잠김: username={username!r} ip={ip}")
                return LoginResult("locked", retry_after_s=window), None, None
        stored = account["password_hash"] if account else self._dummy_hash
        matched = await self.scrypt(verify_password, password, stored)
        if matched and account and account["is_active"]:
            return LoginResult("ok"), account, reservation
        logger.warning(f"admin 로그인 실패: username={username!r} ip={ip}")
        return LoginResult("failed"), None, None

    async def check_password(self, username: str, password: str, ip: str) -> LoginResult:
        """세션 발급 없이 비밀번호만 확인한다(본인 비밀번호 변경) — 판정은 로그인과 한 벌."""
        result, _, reservation = await self._attempt(username, password, ip)
        if reservation is not None:
            await self._run(_RESERVATION_DELETE, (reservation,))
        return result

    async def login(self, username: str, password: str, ip: str) -> LoginResult:
        """예외 대신 결과를 돌려준다 — 성공이면 예약 삭제·세션 INSERT·`login/ok`를 한 커밋으로."""
        result, account, reservation = await self._attempt(username, password, ip)
        if account is None:
            return result
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        async with self._connection() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await conn.begin()
            try:
                await cur.execute(_RESERVATION_DELETE, (reservation,))
                await cur.execute(
                    "INSERT INTO admin_sessions (admin_user_id, token_hash, expires_at, ip) "
                    "VALUES (%s, %s, NOW(3) + INTERVAL %s SECOND, %s)",
                    (
                        account["id"],
                        sha256(token.encode()).digest(),
                        get_settings().admin_session_ttl_s,
                        _recorded_ip(ip),
                    ),
                )
                actor = AdminActor(
                    cur.lastrowid, account["id"], account["username"], account["role"]
                )
                await cur.execute(
                    AUDIT_INSERT, audit_params(actor, "login", actor.username, "ok", ip=ip)
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return LoginResult("ok", token=token, actor=actor)

    async def authenticate(self, token: str) -> AdminActor | None:
        """쿠키 원문 → 유효 세션의 actor. 만료·유휴·폐기·비활성 계정은 전부 None(§3.2).

        `u.is_active` JOIN 조건이 세션 폐기 누락의 이중 방어다. last_seen_at 갱신은 touch 간격이
        지났을 때만 낸다(요청마다 쓰지 않는다).
        """
        settings = get_settings()
        rows = await self._run(
            "SELECT s.id, u.id, u.username, u.role, "
            "s.last_seen_at < NOW(3) - INTERVAL %s SECOND "
            "FROM admin_sessions s JOIN admin_users u ON u.id = s.admin_user_id "
            "WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > NOW(3) "
            "AND s.last_seen_at > NOW(3) - INTERVAL %s SECOND AND u.is_active = 1",
            (
                settings.admin_session_touch_s,
                sha256(token.encode()).digest(),
                settings.admin_session_idle_s,
            ),
            fetch_all=True,
        )
        if not rows:
            return None
        session_id, user_id, username, role, stale = rows[0]
        if stale:
            await self._run(
                "UPDATE admin_sessions SET last_seen_at = NOW(3) WHERE id = %s", (session_id,)
            )
        return AdminActor(session_id, user_id, username, role)

    async def logout(self, actor: AdminActor, ip: str | None = None) -> None:
        await self._run_all(
            [
                (
                    "UPDATE admin_sessions SET revoked_at = NOW(3) "
                    "WHERE id = %s AND revoked_at IS NULL",
                    (actor.session_id,),
                ),
                (AUDIT_INSERT, audit_params(actor, "login", actor.username, "logout", ip=ip)),
            ]
        )

    async def revoke_sessions(self, cur, user_id: int, *, keep: int | None = None) -> int:
        """대상 계정의 살아 있는 세션을 호출부 트랜잭션 안에서 폐기한다(keep = 남길 세션 id)."""
        sql = REVOKE_SESSIONS
        params: tuple = (user_id,)
        if keep is not None:
            sql += " AND id <> %s"
            params = (user_id, keep)
        await cur.execute(sql, params)
        return cur.rowcount

    @asynccontextmanager
    async def transaction(self, actor: AdminActor, request: Request) -> AsyncIterator[AdminTx]:
        """변경과 감사를 한 트랜잭션으로 — 본문이 끝나면 commit, 예외면 rollback 후 재전파.

        HTTPException(404·409·422)은 그대로, 드라이버 예외는 503이 된다. 풀이 autocommit이라
        begin으로 명시 트랜잭션을 연다.
        """
        async with self._connection() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    yield AdminTx(cur, actor, client_ip(request))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise


async def close_admin_service() -> None:
    """앱 종료 훅 — 서비스가 실제로 만들어졌을 때만 풀을 닫는다."""
    if AdminService._instance is not None:
        await AdminService._instance.close()


# ── 의존성 ─────────────────────────────────────────────────────────────────


async def require_admin(request: Request) -> AdminActor:
    """모든 admin 라우트의 판정자 — 출처 검사 + 세션(401).

    로그인월(main._delegating_routes)은 이 함수를 의존성 체인 **어디에든** 가진 라우트를
    위임으로 통과시킨다(require_editor·require_owner를 거쳐도 추이적으로).
    """
    require_same_origin(request)
    token = request.cookies.get(ADMIN_COOKIE)
    actor = await AdminService.get_instance().authenticate(token) if token else None
    if actor is None:
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
    return actor


def _require_role(actor: AdminActor, role: str) -> AdminActor:
    if not actor.at_least(role):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")
    return actor


async def require_editor(actor: AdminActor = Depends(require_admin)) -> AdminActor:
    return _require_role(actor, "editor")


async def require_owner(actor: AdminActor = Depends(require_admin)) -> AdminActor:
    return _require_role(actor, "owner")


# ── 라우트 ─────────────────────────────────────────────────────────────────


def _password_max(value: str) -> str:
    """모든 비밀번호 입력의 상한 — 거대 본문이 scrypt 입력·로그로 흘러가지 않게."""
    maximum = get_settings().admin_password_max_length
    if len(value) > maximum:
        raise PydanticCustomError(
            "password_too_long", "비밀번호는 {maximum}자 이하여야 합니다.", {"maximum": maximum}
        )
    return value


def _password_min(value: str) -> str:
    minimum = get_settings().admin_password_min_length
    if len(value) < minimum:
        raise PydanticCustomError(
            "password_too_short", "비밀번호는 {minimum}자 이상이어야 합니다.", {"minimum": minimum}
        )
    return value


# 길이 상·하한은 config라 검증 시점에 읽는다(모델은 모듈 전역이어야 FastAPI가 문자열 주석을
# 풀 수 있다). 기존 비밀번호 입력(로그인·현재 비밀번호)은 상한만, 새 비밀번호는 둘 다.
_PASSWORD = Annotated[str, AfterValidator(_password_max)]
_NEW_PASSWORD = Annotated[str, AfterValidator(_password_max), AfterValidator(_password_min)]


class _Login(BaseModel):
    username: Annotated[str, StringConstraints(max_length=64)]
    password: _PASSWORD


class _PasswordChange(BaseModel):
    current_password: _PASSWORD
    new_password: _NEW_PASSWORD


class PasswordReset(BaseModel):
    """재설정 본문 — owner API와 CLI `reset-password`가 같은 검증을 쓴다."""

    new_password: _NEW_PASSWORD


class AdminCreate(BaseModel):
    """계정 생성 본문 — owner API와 CLI `create`가 같은 검증을 쓴다."""

    username: _USERNAME
    password: _NEW_PASSWORD
    role: _ROLE


class _AdminChanges(EditChanges):
    # NOT NULL 컬럼 — 생략은 허용(default), 입력된 null은 타입이 422로 거부한다(`| None` 금지).
    role: _ROLE = Field(default=None)
    is_active: StrictBool = Field(default=None)


class _AdminEdit(EditBody):
    changes: _AdminChanges


def _actor_body(actor: AdminActor) -> dict[str, Any]:
    return {"id": actor.user_id, "username": actor.username, "role": actor.role}


def register_admin_auth(app: FastAPI, settings: Settings) -> None:
    """관리자 인증·계정 라우트 등록 — `admin_enabled(settings)`일 때만."""
    if not admin_enabled(settings):
        return
    router = APIRouter(prefix=_COOKIE_PATH, include_in_schema=False)

    @app.exception_handler(RequestValidationError)
    async def admin_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """/admin 422는 거부된 입력(`input`)을 되돌려 보내지 않는다 — 나머지 경로는 FastAPI 기본.

        FastAPI는 `errors(include_url=False)`로 본문을 만들어 모델의 `hide_input_in_errors`가
        먹지 않고(그 설정은 str/repr만 가린다), 필드 누락 오류는 **본문 전체**를 input으로 싣는다
        — 오입력한 실제 비밀번호·현재 비밀번호가 응답으로 반사된다(보안 검증 2026-09-14).
        """
        if not request.url.path.startswith(_COOKIE_PATH):
            return await request_validation_exception_handler(request, exc)
        errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
        # 인코딩은 FastAPI 기본 핸들러와 같게(ctx에 예외 객체가 실릴 수 있다) — 다른 것은 input뿐.
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)

    @app.exception_handler(EditConflict)
    async def edit_conflict(request: Request, exc: EditConflict) -> JSONResponse:
        return JSONResponse({"detail": exc.detail, "current": exc.current}, status_code=409)

    async def _hash(password: str) -> str:
        return await AdminService.get_instance().scrypt(hash_password, password, get_settings())

    @router.post("/api/login")
    async def login(request: Request, body: _Login) -> JSONResponse:
        require_same_origin(request)
        result = await AdminService.get_instance().login(
            body.username, body.password, client_ip(request)
        )
        if result.status == "locked":
            return JSONResponse(
                {"detail": _LOCKED},
                status_code=429,
                headers={"Retry-After": str(result.retry_after_s)},
            )
        if result.status == "failed":
            return JSONResponse({"detail": _LOGIN_FAILED}, status_code=401)
        response = JSONResponse(_actor_body(result.actor))
        response.set_cookie(
            ADMIN_COOKIE,
            result.token,
            max_age=get_settings().admin_session_ttl_s,
            httponly=True,
            samesite="lax",
            secure=get_settings().cookie_secure,
            path=_COOKIE_PATH,
        )
        return response

    @router.post("/api/logout")
    async def logout(request: Request) -> JSONResponse:
        """세션이 없거나 이미 무효여도 200 — 쿠키는 항상 지운다."""
        require_same_origin(request)
        token = request.cookies.get(ADMIN_COOKIE)
        service = AdminService.get_instance()
        actor = await service.authenticate(token) if token else None
        if actor is not None:
            await service.logout(actor, client_ip(request))
        response = JSONResponse({"ok": True})
        response.delete_cookie(ADMIN_COOKIE, path=_COOKIE_PATH)
        return response

    @router.get("/api/me")
    async def me(actor: AdminActor = Depends(require_admin)) -> dict[str, Any]:
        """roles = 권한 순서(낮음→높음). 프론트가 역할 비교표를 따로 들지 않게 정본을 싣는다."""
        return {**_actor_body(actor), "roles": list(ROLES)}

    @router.post("/api/me/password")
    async def change_password(
        body: _PasswordChange, request: Request, actor: AdminActor = Depends(require_admin)
    ) -> dict[str, Any]:
        """본인 비밀번호 변경 — 현재 세션만 남기고 나머지를 폐기한다.

        현재 비밀번호 확인은 로그인과 같은 시도 제한을 탄다(`check_password`) — 틀리면 그
        계정의 `login/failed`로 커밋되고 422(세션은 유효하므로 401이 아니다), 잠겼으면 429다.
        탈취한 세션으로 현재 비밀번호를 무제한 대입하는 통로를 막는다.
        """
        service = AdminService.get_instance()
        checked = await service.check_password(
            actor.username, body.current_password, client_ip(request)
        )
        if checked.status == "locked":
            raise HTTPException(
                status_code=429,
                detail=_LOCKED,
                headers={"Retry-After": str(checked.retry_after_s)},
            )
        if checked.status == "failed":
            raise HTTPException(status_code=422, detail="현재 비밀번호가 올바르지 않습니다.")
        new_hash = await _hash(body.new_password)
        async with service.transaction(actor, request) as tx:
            await tx.cur.execute(PASSWORD_UPDATE, (new_hash, actor.user_id))
            await service.revoke_sessions(tx.cur, actor.user_id, keep=actor.session_id)
            await tx.audit("admin", actor.user_id, "password_change")
        return {"ok": True}

    @router.get("/api/admins")
    async def list_admins(
        request: Request, actor: AdminActor = Depends(require_owner)
    ) -> dict[str, Any]:
        async with AdminService.get_instance().transaction(actor, request) as tx:
            await tx.cur.execute(
                "SELECT id, username, role, is_active, password_changed_at, created_at, "
                "created_by FROM admin_users ORDER BY id"
            )
            rows = await tx.cur.fetchall()
        return {"items": [jsonable(row) for row in rows]}

    @router.post("/api/admins", status_code=201)
    async def create_admin(
        body: AdminCreate, request: Request, actor: AdminActor = Depends(require_owner)
    ) -> dict[str, Any]:
        password_hash = await _hash(body.password)
        async with AdminService.get_instance().transaction(actor, request) as tx:
            try:
                await tx.cur.execute(
                    "INSERT INTO admin_users (username, password_hash, role, created_by) "
                    "VALUES (%s, %s, %s, %s)",
                    (body.username, password_hash, body.role, actor.user_id),
                )
            except aiomysql.IntegrityError:
                raise HTTPException(status_code=409, detail="이미 있는 계정명입니다.") from None
            admin_id = tx.cur.lastrowid
            await tx.audit(
                "admin", admin_id, "create", after={"username": body.username, "role": body.role}
            )
        return {"id": admin_id, "username": body.username, "role": body.role}

    @router.patch("/api/admins/{admin_id}")
    async def patch_admin(
        admin_id: int,
        body: _AdminEdit,
        request: Request,
        actor: AdminActor = Depends(require_owner),
    ) -> dict[str, Any]:
        """역할·활성 변경. 바뀌면 대상의 세션을 전부 폐기한다(같은 트랜잭션).

        최후 owner 규칙: 계정 행 **전체를 PK로 먼저** 잠근 뒤 활성 owner를 센다. 두 owner가
        동시에 서로를 강등하면 뒤 요청이 이 잠금에서 기다렸다가 앞 요청이 커밋한 상태를 보고
        409가 된다. (role, is_active) 인덱스로 잠그면(WHERE로 좁히거나, 커버링이라 옵티마이저가
        고르거나) 격리 MySQL에서 교착(1213 → 503)이 실측됐다 — 앞 요청의 UPDATE가 그 인덱스에 새
        항목을 넣으려 뒤 요청이 기다리는 레코드 앞 갭을 요구한다. 그래서 PRIMARY를 강제한다.
        운영자 한 자릿수라 전체 잠금이 싸다.
        """
        changes = body.changes.model_dump(exclude_unset=True)
        service = AdminService.get_instance()
        async with service.transaction(actor, request) as tx:
            await tx.cur.execute(
                "SELECT id, role, is_active FROM admin_users FORCE INDEX (PRIMARY) FOR UPDATE"
            )
            owners = {
                row["id"]
                for row in await tx.cur.fetchall()
                if row["role"] == "owner" and row["is_active"]
            }
            before, after = await tx.edit_row(
                "admin_users", admin_id, ("role", "is_active"), changes, body.expected
            )
            if after:
                demoted = after.get("role", "owner") != "owner" or after.get("is_active") is False
                if admin_id in owners and demoted and not owners - {admin_id}:
                    raise HTTPException(status_code=409, detail=_LAST_OWNER)
                await service.revoke_sessions(tx.cur, admin_id)
                await tx.audit("admin", admin_id, "update", before, after)
        return {"id": admin_id, "updated": sorted(after)}

    @router.post("/api/admins/{admin_id}/password")
    async def reset_password(
        admin_id: int,
        body: PasswordReset,
        request: Request,
        actor: AdminActor = Depends(require_owner),
    ) -> dict[str, Any]:
        password_hash = await _hash(body.new_password)
        service = AdminService.get_instance()
        async with service.transaction(actor, request) as tx:
            await tx.cur.execute(PASSWORD_UPDATE, (password_hash, admin_id))
            if tx.cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="대상을 찾을 수 없습니다.")
            revoked = await service.revoke_sessions(tx.cur, admin_id)
            await tx.audit("admin", admin_id, "password_reset")
        return {"ok": True, "revoked_sessions": revoked}

    app.include_router(router)
