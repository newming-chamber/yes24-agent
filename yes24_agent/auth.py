"""`x-api-key` 인증 + rate limiting (crema-ai 계약 이식).

프론트는 crema-ai와 같은 계약으로 붙는다: 헤더 `x-api-key`의 값이 곧 **Yes24
service_cookie**이고, 그 값으로 Yes24 회원 API를 조회하면 userNo가 나온다. userNo는
ADK 세션 키 `(app_name, user_id, session_id)`의 user_id가 되어 대화 기록이 사람 단위로
갈린다(다른 사용자의 session_id로 요청해도 자기 user_id 밑에서만 조회되므로 세션 탈취가
구조적으로 성립하지 않는다).

인증 DB(`users`·`rate_limit_log`)는 세션 DB와 **같은 계정·database**를 쓴다 — 접속 정보를
따로 두지 않고 `config.session_db_url`을 파싱한다(설정 단일 출처). 그래서 세션 DB가 mysql이
아니면(로컬 sqlite 개발) 인증 스택이 통째로 비활성이 되어 모든 요청이 익명으로 흐른다.
키워드 분기가 아니라 접속 가능성에서 나오는 구조 분기다.

테이블은 배포 환경에 이미 만들어져 있다고 전제한다. 없거나 DB가 죽었으면 조용히 익명으로
떨어지지 않고 503으로 끊는다 — "인증된 줄 알았는데 익명으로 답하고 있었다"가 조용히
성립하면 안 된다(fail-loud).
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from dataclasses import dataclass
from hashlib import sha256
from secrets import compare_digest
from typing import Any

import httpx
from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from yes24_agent.config import get_settings
from yes24_agent.db import LazyAiomysqlPool
from yes24_agent.session_service import mysql_pool_kwargs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthenticatedUser:
    """인증된 요청 컨텍스트."""

    api_key: str
    user_no: str | None
    user_login_id: str | None
    # crema 전용 권한 플래그. 우리는 **저장만 하고 게이트하지 않는다** — crema club AI
    # 이용 자격이지 이 서비스의 차단 근거가 아니다(컬럼명도 crema 계약 그대로 유지).
    canUseCremaclubAI: bool
    rate_limit_rpm: int
    rate_limit_rpd: int


async def fetch_yes24_user_info(service_cookie: str) -> dict[str, Any] | None:
    """service_cookie로 Yes24 회원 정보를 조회한다 — 실패·비정상 응답이면 None.

    성공 응답: `{"success": True, "userNo": …, "userId": …, "SelfCert": …,
    "canUseCremaclubAI": …}`. 회원 정보 조회 실패는 인증 실패가 아니다(키는 유효한데
    Yes24가 잠깐 죽은 경우) — 호출부가 user_no 없는 사용자로 진행한다.
    """
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=settings.yes24_user_info_timeout_s) as client:
            resp = await client.post(
                settings.yes24_user_info_url,
                json={"serviceCookies": service_cookie},
            )
        if resp.status_code != 200:
            logger.warning(f"Yes24 회원 API status={resp.status_code} body={resp.text[:200]}")
            return None
        data = resp.json()
        if not data.get("success"):
            logger.warning(f"Yes24 회원 API success=false: {data}")
            return None
        return data
    except Exception as exc:  # noqa: BLE001 — 회원 조회 실패는 인증을 막지 않는다
        logger.warning(f"Yes24 회원 API 호출 실패: {exc}")
        return None


def _pool_kwargs(db_url: str) -> dict[str, Any] | None:
    """인증 풀 접속 kwargs — 파싱은 session_service.mysql_pool_kwargs 단일 출처.

    mysql이 아니면 None(인증 비활성). 풀 크기만 인증 용도의 config 필드를 쓴다.
    """
    return mysql_pool_kwargs(db_url, maxsize=get_settings().auth_pool_max)


def _user_fields(data: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    """Yes24 회원 응답에서 (user_no, user_login_id, canUseCremaclubAI)를 뽑는다."""
    user_no = data.get("userNo")
    return (
        str(user_no) if user_no is not None else None,
        data.get("userId"),
        bool(data.get("canUseCremaclubAI")),
    )


class AuthService:
    """API key 검증 + rate limiting 서비스(프로세스 싱글턴).

    풀·Yes24 클라이언트를 생성자로 주입할 수 있다 — 테스트는 실 DB·실 네트워크 없이
    스텁을 넣어 전 경로를 돈다.
    """

    _instance: AuthService | None = None

    def __init__(
        self,
        pool_factory=None,
        fetch_user_info=fetch_yes24_user_info,
    ) -> None:
        self._pool_kwargs = _pool_kwargs(get_settings().session_db_url)
        # 풀 생성 기계(지연 생성·태스크 공유·shield)는 db.LazyAiomysqlPool 단일 구현이다 —
        # 여기 남는 것은 실패 정책(503 fail-loud)뿐이다. mysql이 아니면 풀 자체가 없다.
        self._db = (
            LazyAiomysqlPool(self._pool_kwargs, pool_factory)
            if self._pool_kwargs is not None
            else None
        )
        self._fetch_user_info = fetch_user_info
        # api_key → (사용자, 캐시 시각). TTL 안에서는 users 조회를 건너뛴다.
        # is_active 회수도 최대 TTL만큼 늦게 반영된다(rate limit은 캐시와 무관하게 매번 DB).
        self._cache: dict[str, tuple[AuthenticatedUser, float]] = {}

    @classmethod
    def get_instance(cls) -> AuthService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def enabled(self) -> bool:
        """인증 스택이 성립하는가 — 세션 DB가 mysql일 때만 True."""
        return self._pool_kwargs is not None

    async def close(self) -> None:
        """커넥션 풀을 정리한다(앱 종료 훅) — 마감은 공유 풀 구현이 소유한다."""
        if self._db is not None:
            await self._db.close()

    # ----- DB -----

    async def _get_pool(self):
        """풀 획득 — 생성 기계는 db.LazyAiomysqlPool, 여기는 실패 정책(503)만 얹는다."""
        try:
            return await self._db.get()
        except Exception as exc:  # noqa: BLE001 — 어떤 드라이버 오류든 503으로 정직하게
            logger.error(f"인증 DB 풀 생성 실패: {exc}")
            raise HTTPException(status_code=503, detail="인증 DB에 연결할 수 없습니다.") from exc

    async def _run(self, sql: str, params: tuple, *, fetch: bool = False):
        """질의 1건 실행(필요하면 1행 반환). DB 오류는 삼키지 않고 503으로 올린다."""
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    return await cur.fetchone() if fetch else None
        except Exception as exc:  # noqa: BLE001 — 테이블 부재·연결 끊김 전부 정직하게 노출
            logger.error(f"인증 DB 질의 실패: {exc}")
            raise HTTPException(status_code=503, detail="인증 DB 질의에 실패했습니다.") from exc

    # ----- 인증 -----

    async def authenticate(self, api_key: str) -> AuthenticatedUser:
        """API key → AuthenticatedUser. 캐시 히트면 DB를 보지 않는다."""
        cached = self._cache.get(api_key)
        if cached and (time.time() - cached[1]) < get_settings().auth_cache_ttl_s:
            return cached[0]

        # 회원 정보 만료 판정을 DB에서 한다 — timestamp 컬럼은 타임존 없는 값이라
        # 파이썬 로컬 시각과 빼면 서버 타임존 차이만큼 통째로 어긋난다(같은 DB 시계끼리 비교).
        row = await self._run(
            "SELECT user_no, user_login_id, canUseCremaclubAI, is_active, "
            "rate_limit_rpm, rate_limit_rpd, "
            "(user_cached_at IS NULL OR user_cached_at < NOW() - INTERVAL %s HOUR) "
            "FROM users WHERE api_key = %s",
            (get_settings().yes24_user_cache_hours, api_key),
            fetch=True,
        )
        if row is None:
            return await self._register(api_key)

        user_no, login_id, can_use_ai, is_active, rpm, rpd, stale = row
        if not is_active:
            raise HTTPException(status_code=401, detail="사용할 수 없는 API 키입니다.")

        if user_no is None or stale:
            data = await self._refresh_yes24_user(api_key)
            if data is not None:
                user_no, login_id, can_use_ai = _user_fields(data)

        return self._remember(
            AuthenticatedUser(
                api_key=api_key,
                user_no=user_no,
                user_login_id=login_id,
                canUseCremaclubAI=bool(can_use_ai),
                rate_limit_rpm=rpm,
                rate_limit_rpd=rpd,
            )
        )

    async def _register(self, api_key: str) -> AuthenticatedUser:
        """미등록 키를 자동 등록하고 Yes24 회원 정보를 채운다.

        헤더 값이 곧 service_cookie라, 등록 직후 같은 값으로 회원 API를 조회한다.
        """
        settings = get_settings()
        await self._run(
            "INSERT INTO users (api_key, rate_limit_rpm, rate_limit_rpd) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE updated_at = NOW()",
            (api_key, settings.rate_limit_rpm, settings.rate_limit_rpd),
        )
        logger.info(f"신규 api_key 자동 등록: {api_key[:8]}…")

        data = await self._refresh_yes24_user(api_key)
        user_no, login_id, can_use_ai = _user_fields(data) if data else (None, None, False)
        return self._remember(
            AuthenticatedUser(
                api_key=api_key,
                user_no=user_no,
                user_login_id=login_id,
                canUseCremaclubAI=can_use_ai,
                rate_limit_rpm=settings.rate_limit_rpm,
                rate_limit_rpd=settings.rate_limit_rpd,
            )
        )

    async def _refresh_yes24_user(self, api_key: str) -> dict[str, Any] | None:
        """Yes24 회원 정보를 조회해 users에 캐시하고 그 응답을 돌려준다(실패면 None).

        조회 결과를 그대로 반환하므로 호출부가 users를 다시 SELECT하지 않는다.
        """
        data = await self._fetch_user_info(api_key)
        if data is None:
            logger.warning(f"Yes24 회원 정보 갱신 실패: key={api_key[:8]}…")
            return None

        user_no, login_id, can_use_ai = _user_fields(data)
        await self._run(
            "UPDATE users SET user_no = %s, user_login_id = %s, self_cert = %s, "
            "canUseCremaclubAI = %s, raw_user_info = %s, user_cached_at = NOW() "
            "WHERE api_key = %s",
            (
                user_no,
                login_id,
                1 if data.get("SelfCert") else 0,
                1 if can_use_ai else 0,
                json.dumps(data, ensure_ascii=False),
                api_key,
            ),
        )
        logger.info(f"Yes24 회원 정보 캐시: userNo={user_no} userId={login_id}")
        return data

    def _remember(self, user: AuthenticatedUser) -> AuthenticatedUser:
        self._cache[user.api_key] = (user, time.time())
        return user

    # ----- Rate limiting -----

    async def check_rate_limit(self, user: AuthenticatedUser) -> None:
        """슬라이딩 윈도우(분·일) 요청 수를 세고 초과면 429.

        두 창을 한 질의로 센다 — 일 단위 행을 이미 훑으므로 분 단위는 그 안의 조건 합이다.
        """
        row = await self._run(
            "SELECT COALESCE(SUM(requested_at > NOW(3) - INTERVAL 1 MINUTE), 0), COUNT(*) "
            "FROM rate_limit_log WHERE api_key = %s AND requested_at > NOW(3) - INTERVAL 1 DAY",
            (user.api_key,),
            fetch=True,
        )
        minute_count, day_count = (int(value) for value in row)
        if minute_count >= user.rate_limit_rpm:
            raise HTTPException(
                status_code=429,
                detail=f"분당 요청 한도({user.rate_limit_rpm}회)를 초과했습니다.",
            )
        if day_count >= user.rate_limit_rpd:
            raise HTTPException(
                status_code=429,
                detail=f"일일 요청 한도({user.rate_limit_rpd}회)를 초과했습니다.",
            )

    async def record_request(self, api_key: str, endpoint: str) -> None:
        """요청을 rate_limit_log에 남긴다(다음 요청의 슬라이딩 윈도우 재료)."""
        await self._run(
            "INSERT INTO rate_limit_log (api_key, endpoint) VALUES (%s, %s)",
            (api_key, endpoint),
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
    description="Yes24 service_cookie 값. 이 값 하나가 곧 사용자 식별자이며(서버가 Yes24 회원"
    " API로 userNo를 조회한다) crema-ai와 같은 계약이라 쓰던 키를 그대로 쓰면 된다."
    " 실패 코드: 헤더가 없으면 **401**, 키는 있으나 Yes24 회원 식별(userNo)이 안 되면 **403**,"
    " 비활성 키도 401, 한도 초과는 429다.",
)


async def get_authenticated_user(
    request: Request,
    x_api_key: str | None = Security(API_KEY_HEADER),
) -> AuthenticatedUser | None:
    """FastAPI 의존성: `x-api-key` 검증 + rate limit 기록.

    헤더가 없으면 None(익명 허용 — 내장 UI·로컬 개발 경로가 그대로 돈다). 헤더가 있는데
    키가 비활성이면 401, 한도를 넘으면 429, 인증 DB가 죽었으면 503이다. 예외를 삼켜
    익명으로 강등하지 않는다 — 조용한 강등은 "인증됐다고 믿는 익명 세션"을 만든다.
    """
    if not x_api_key:
        return None

    service = AuthService.get_instance()
    if not service.enabled:
        # 세션 DB가 mysql이 아닌 환경(로컬 sqlite): 인증 테이블 자체가 없다 — 익명으로 흘린다.
        return None

    user = await service.authenticate(x_api_key)
    await service.check_rate_limit(user)
    await service.record_request(x_api_key, _route_template(request))
    return user


def _route_template(request: Request) -> str:
    """요청이 실제로 매칭된 **라우트 템플릿**(`/chat/sessions/{session_id}`)을 돌려준다.

    구체 경로(`request.url.path`)를 그대로 남기면 rate_limit_log의 endpoint가 세션·턴 id마다
    고유해져 ① 엔드포인트별 집계가 불가능해지고(대화 수만큼 서로 다른 값) ② 컬럼 상한에서
    잘리며 ③ 식별자가 로그 테이블에 복제된다. 판정에는 쓰이지 않는 컬럼이라(일일 카운트는
    api_key만 본다) 동작은 그대로다 — 바뀌는 것은 기록의 쓸모뿐이다.

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
