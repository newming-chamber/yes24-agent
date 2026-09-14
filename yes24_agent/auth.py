"""ServiceCookies 인증: 사용자 정본과 해시 자격증명을 분리하고 사용자별 한도를 센다."""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from secrets import compare_digest
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from yes24_agent.config import get_settings
from yes24_agent.db import LazyAiomysqlPool
from yes24_agent.session_service import mysql_pool_kwargs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthenticatedUser:
    api_key: str = field(repr=False)
    user_no: str | None
    user_login_id: str | None
    rate_limit_rpm: int
    rate_limit_rpd: int
    user_id: int | None = None
    auth_key_id: int | None = None


def _unidentified() -> HTTPException:
    return HTTPException(
        status_code=403, detail="Yes24 회원 식별이 완료되지 않은 키입니다. 다시 로그인해 주세요."
    )


async def fetch_yes24_user_info(service_cookie: str) -> dict[str, Any]:
    """명시적인 인증 거절은 403, 회원 서비스 장애는 503으로 구별한다."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=settings.yes24_user_info_timeout_s) as client:
            response = await client.post(
                settings.yes24_user_info_url, json={"serviceCookies": service_cookie}
            )
        if response.status_code in (401, 403):
            raise _unidentified()
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "success" not in data:
            raise ValueError("invalid member response")
        if data["success"] is False:
            raise _unidentified()
        if data["success"] is not True:
            raise ValueError("invalid member response")
        return data
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Yes24 회원 API 실패: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Yes24 회원 정보를 확인할 수 없습니다."
        ) from exc


def _pool_kwargs(db_url: str) -> dict[str, Any] | None:
    return mysql_pool_kwargs(db_url, maxsize=get_settings().auth_pool_max)


def _user_fields(data: dict[str, Any]) -> tuple[str, str | None]:
    user_no = data.get("userNo")
    if user_no is None or isinstance(user_no, (bool, dict, list)) or not str(user_no).strip():
        raise _unidentified()
    return str(user_no), data.get("userId")


class AuthService:
    _instance: AuthService | None = None

    def __init__(self, pool_factory=None, fetch_user_info=fetch_yes24_user_info) -> None:
        self._pool_kwargs = _pool_kwargs(get_settings().session_db_url)
        self._db = (
            LazyAiomysqlPool(self._pool_kwargs, pool_factory)
            if self._pool_kwargs is not None
            else None
        )
        self._fetch_user_info = fetch_user_info

    @classmethod
    def get_instance(cls) -> AuthService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def enabled(self) -> bool:
        return self._pool_kwargs is not None

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()

    @asynccontextmanager
    async def _cursor(self, *, transaction: bool = False):
        try:
            pool = await self._db.get()
            async with pool.acquire() as connection:
                if transaction:
                    await connection.begin()
                try:
                    async with connection.cursor() as cursor:
                        yield cursor
                    if transaction:
                        await connection.commit()
                except BaseException:
                    if transaction:
                        await connection.rollback()
                    raise
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("인증 DB 실패: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="인증 DB 요청에 실패했습니다.") from exc

    async def _run(self, sql: str, params: tuple, *, fetch: bool = False):
        async with self._cursor() as cursor:
            await cursor.execute(sql, params)
            return await cursor.fetchone() if fetch else None

    @staticmethod
    def _is_dev_key(api_key: str) -> bool:
        configured = get_settings().dev_api_key
        return bool(configured) and compare_digest(api_key.encode(), configured.encode())

    async def _member_info(self, api_key: str) -> dict[str, Any]:
        data = await self._fetch_user_info(api_key)
        if not isinstance(data, dict):
            raise HTTPException(status_code=503, detail="Yes24 회원 정보를 확인할 수 없습니다.")
        if data.get("success") is False:
            raise _unidentified()
        return data

    async def _lock_identity(self, cursor, user_id: int, key_id: int) -> tuple[int, int]:
        await cursor.execute(
            "SELECT rate_limit_rpm, rate_limit_rpd, is_active FROM users WHERE id = %s FOR UPDATE",
            (user_id,),
        )
        limits = await cursor.fetchone()
        if limits is None or not limits[2]:
            raise HTTPException(status_code=401, detail="사용할 수 없는 API 키입니다.")
        await cursor.execute(
            "SELECT is_active FROM auth_keys WHERE id = %s AND user_id = %s FOR UPDATE",
            (key_id, user_id),
        )
        key = await cursor.fetchone()
        if key is None or not key[0]:
            raise HTTPException(status_code=401, detail="사용할 수 없는 API 키입니다.")
        return limits[0], limits[1]

    async def authenticate(self, api_key: str) -> AuthenticatedUser:
        key_hash = sha256(api_key.encode()).digest()
        row = await self._run(
            "SELECT u.id, k.id, u.user_no, u.user_login_id, u.rate_limit_rpm, "
            "u.rate_limit_rpd, u.is_active, k.is_active, k.kind, "
            "(k.user_cached_at IS NULL OR k.user_cached_at < NOW() - INTERVAL %s HOUR) "
            "FROM auth_keys k JOIN users u ON u.id = k.user_id WHERE k.key_hash = %s",
            (get_settings().yes24_user_cache_hours, key_hash),
            fetch=True,
        )
        if row is None:
            return await self._register(api_key, key_hash)
        user_id, key_id, user_no, login_id, rpm, rpd, active, key_active, kind, stale = row
        if not active or not key_active:
            raise HTTPException(status_code=401, detail="사용할 수 없는 API 키입니다.")
        if kind == "dev":
            if not self._is_dev_key(api_key) or user_no != get_settings().dev_api_user_no:
                raise _unidentified()
        elif kind == "member":
            if stale:
                data = await self._member_info(api_key)
                refreshed_no, login_id = _user_fields(data)
                if refreshed_no != user_no:
                    raise _unidentified()
                async with self._cursor(transaction=True) as cursor:
                    rpm, rpd = await self._lock_identity(cursor, user_id, key_id)
                    await cursor.execute(
                        "UPDATE users SET user_login_id = %s WHERE id = %s",
                        (login_id, user_id),
                    )
                    await cursor.execute(
                        "UPDATE auth_keys SET raw_user_info = %s, user_cached_at = NOW() "
                        "WHERE id = %s",
                        (json.dumps(data, ensure_ascii=False), key_id),
                    )
        else:
            raise _unidentified()
        return AuthenticatedUser(api_key, user_no, login_id, rpm, rpd, user_id, key_id)

    async def _register(self, api_key: str, key_hash: bytes) -> AuthenticatedUser:
        settings = get_settings()
        kind = "dev" if self._is_dev_key(api_key) else "member"
        data = (
            {"userNo": settings.dev_api_user_no, "userId": "dev"}
            if kind == "dev"
            else await self._member_info(api_key)
        )
        user_no, login_id = _user_fields(data)
        async with self._cursor(transaction=True) as cursor:
            await cursor.execute(
                "INSERT INTO users (user_no, user_login_id, rate_limit_rpm, rate_limit_rpd) "
                "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE id = id",
                (user_no, login_id, settings.rate_limit_rpm, settings.rate_limit_rpd),
            )
            await cursor.execute(
                "SELECT id, is_active, rate_limit_rpm, rate_limit_rpd FROM users "
                "WHERE user_no = %s FOR UPDATE",
                (user_no,),
            )
            user_id, active, rpm, rpd = await cursor.fetchone()
            if not active:
                raise HTTPException(status_code=401, detail="사용할 수 없는 API 키입니다.")
            await cursor.execute(
                "INSERT INTO auth_keys (key_hash, user_id, kind, raw_user_info, user_cached_at) "
                "VALUES (%s, %s, %s, %s, NOW()) ON DUPLICATE KEY UPDATE id = id",
                (key_hash, user_id, kind, json.dumps(data, ensure_ascii=False)),
            )
            await cursor.execute(
                "SELECT id, user_id, is_active, kind FROM auth_keys WHERE key_hash = %s FOR UPDATE",
                (key_hash,),
            )
            key_id, key_user_id, key_active, key_kind = await cursor.fetchone()
            if key_user_id != user_id or key_kind != kind:
                raise _unidentified()
            if not key_active:
                raise HTTPException(status_code=401, detail="사용할 수 없는 API 키입니다.")
            await cursor.execute(
                "UPDATE users SET user_login_id = %s WHERE id = %s", (login_id, user_id)
            )
        return AuthenticatedUser(api_key, user_no, login_id, rpm, rpd, user_id, key_id)

    async def consume_request(self, user: AuthenticatedUser, endpoint: str) -> None:
        """사용자 행 잠금 안에서 한도 확인과 기록을 원자적으로 처리한다."""
        async with self._cursor(transaction=True) as cursor:
            rpm, rpd = await self._lock_identity(cursor, user.user_id, user.auth_key_id)
            await cursor.execute(
                "SELECT COALESCE(SUM(requested_at > NOW(3) - INTERVAL 1 MINUTE), 0), COUNT(*) "
                "FROM rate_limit_log WHERE user_id = %s AND requested_at > NOW(3) - INTERVAL 1 DAY",
                (user.user_id,),
            )
            minute_count, day_count = (int(value) for value in await cursor.fetchone())
            if minute_count >= rpm:
                raise HTTPException(
                    status_code=429, detail=f"분당 요청 한도({rpm}회)를 초과했습니다."
                )
            if day_count >= rpd:
                raise HTTPException(
                    status_code=429, detail=f"일일 요청 한도({rpd}회)를 초과했습니다."
                )
            await cursor.execute(
                "INSERT INTO rate_limit_log (user_id, auth_key_id, endpoint) VALUES (%s, %s, %s)",
                (user.user_id, user.auth_key_id, endpoint),
            )


async def close_auth_service() -> None:
    """앱 종료 훅 — 인증 서비스가 실제로 만들어졌을 때만 풀을 닫는다."""
    if AuthService._instance is not None:
        await AuthService._instance.close()


# `x-api-key`를 **보안 스킴**으로 선언한다 — 평범한 Header 파라미터로 두면 OpenAPI에
# securitySchemes가 생기지 않아 Swagger UI에 "Authorize" 버튼이 없고, 프론트 개발자가 키를
# 엔드포인트마다 손으로 붙여 넣어야 한다(문서로 테스트가 안 된다). auto_error=False인 이유는
# 헤더 없는 익명 요청을 그대로 흘려보내야 하기 때문이다 — 거절 판정은 아래 본문이 소유한다.
API_KEY_HEADER = APIKeyHeader(
    name="x-api-key",
    auto_error=False,
    description="브라우저의 `ServiceCookies` 쿠키 값(Yes24 로그인 쿠키). 이 값 하나가 곧"
    " 사용자 식별자이며 서버가 Yes24 회원 API로 userNo를 조회한다 — crema-ai와 같은 계약이다."
    " 개발 환경에서는 별도 개발 키를 발급한다(값은 팀에 문의)."
    " 실패 코드: 헤더가 없으면 **401**, 식별되지 않는 키는 **모든 API에서 403**,"
    " 비활성 키는 401, 한도 초과는 429다.",
)


async def get_authenticated_user(
    request: Request,
    x_api_key: str | None = Security(API_KEY_HEADER),
) -> AuthenticatedUser | None:
    """FastAPI 의존성: `x-api-key` 검증(**한도는 세지 않는다**).

    헤더가 없으면 None(익명 허용 — 내장 UI·로컬 개발 경로가 그대로 돈다). 헤더가 있는데
    키가 비활성이면 401, 인증 DB가 죽었으면 503이다. 예외를 삼켜 익명으로 강등하지
    않는다 — 조용한 강등은 "인증됐다고 믿는 익명 세션"을 만든다.

    **요청 한도는 여기가 아니라 `enforce_rate_limit`이 센다.** 둘을 한 의존성에 묶었더니
    인증만 필요한 조회 라우트(세션 목록·읽음 표시·초기 질문)까지 한도를 깎았다 — 하루 500
    중 실제 대화가 205회인데 한도가 찬 실측이 그것이다(2026-09-09).
    """
    del request  # 라우트 템플릿은 한도 기록 쪽에서 쓴다
    if not x_api_key:
        return None

    service = AuthService.get_instance()
    if not service.enabled:
        # 세션 DB가 mysql이 아닌 환경(로컬 sqlite): 인증 테이블 자체가 없다 — 익명으로 흘린다.
        return None

    return await service.authenticate(x_api_key)


async def enforce_rate_limit(
    request: Request,
    user: Annotated[AuthenticatedUser | None, Depends(get_authenticated_user)] = None,
) -> None:
    """FastAPI 의존성: 요청 한도 검사 + 기록. **LLM을 호출하는 라우트에만** 붙인다.

    한도의 목적은 LLM 호출 비용 방어이므로 세는 대상도 그것이어야 한다 — 조회는 같은
    무게로 세면 안 된다. 익명(헤더 없음)·인증 스택 없는 구성에서는 셀 대상이 없어 무동작.

    `get_authenticated_user`를 의존성으로 받으므로 FastAPI가 요청당 한 번만 실행하고(의존성
    캐시) 라우트가 받는 user와 같은 객체다. 라우트 시그니처에도 그 의존성이 그대로 남아
    로그인월의 x-api-key 통과 집합 파생(main._key_checking_routes)은 영향받지 않는다.
    """
    # 셀 대상이 없으면 무동작. 익명(헤더 없음)이면 **서비스에 손대기 전에** 빠진다 —
    # get_instance()는 전역 싱글턴을 만들어 뒤따르는 테스트가 그것을 물려받는다.
    if user is None:
        return
    service = AuthService.get_instance()
    # 인증 테이블이 없는 구성(로컬 sqlite)에서도 셀 대상이 없다. 인증 의존성과 **같은
    # 방어**를 둔다 — 한쪽만 방어하면 스택 없는 구성에서 여기만 터진다.
    if not service.enabled:
        return
    await service.consume_request(user, _route_template(request))


def _route_template(request: Request) -> str:
    """요청이 실제로 매칭된 **라우트 템플릿**(`/chat/sessions/{session_id}`)을 돌려준다.

    구체 경로(`request.url.path`)를 그대로 남기면 rate_limit_log의 endpoint가 세션·턴 id마다
    고유해져 ① 엔드포인트별 집계가 불가능해지고(대화 수만큼 서로 다른 값) ② 컬럼 상한에서
    잘리며 ③ 식별자가 로그 테이블에 복제된다. 판정에는 쓰이지 않는 컬럼이라(일일 카운트는
    user_id만 본다) 동작은 그대로다 — 바뀌는 것은 기록의 쓸모뿐이다.

    매칭 라우트는 FastAPI가 scope에 심는다(fastapi.routing에서 child_scope["route"]).
    없으면(미들웨어 단계·404) 구체 경로로 떨어진다 — 기록이 비는 것보다 낫다.
    """
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def signed_access_token(password: str, message: bytes) -> str:
    """비밀번호에서 결정론적 접근 토큰(HMAC-SHA256 hex)을 만든다.

    같은 비밀번호는 항상 같은 토큰 → 세션 저장 없이 쿠키만으로 검증한다. message는
    용도·버전 구분자(비밀 아님)다. 채팅 로그인월(main)과 admin 게이트(admin)가 이 한
    구현을 공유하되, 쿠키명·비밀번호·message는 각자 유지한다(권한 분리는 값으로,
    구현은 한 벌로 — 2026-08-19 구조 감사 C3 통합).
    """
    return hmac.new(password.encode("utf-8"), message, sha256).hexdigest()


def token_matches(cookie_value: str | None, password: str, message: bytes) -> bool:
    """쿠키 토큰이 현재 비밀번호 파생값과 일치하는지 상수시간 비교로 판정한다."""
    return bool(cookie_value) and compare_digest(
        cookie_value, signed_access_token(password, message)
    )
