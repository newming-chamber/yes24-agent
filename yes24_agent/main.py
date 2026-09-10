"""FastAPI 서버 — `/chat/stream` SSE 엔드포인트.

라이브 소스(Yes24)를 검색해 인용 달린 답변을 스트리밍하는 대화 API. 실제 에이전트
루프와 SSE 변환은 `runner.run_agent_stream`이 담당하고, 이 모듈은 HTTP 계층
(라우팅·CORS·수명주기 훅)만 얇게 얹는다.
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from secrets import compare_digest
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from starlette.routing import Match

from yes24_agent.admin import client_ip, register_admin, require_admin
from yes24_agent.auth import (
    AuthenticatedUser,
    AuthService,
    close_auth_service,
    enforce_rate_limit,
    get_authenticated_user,
    signed_access_token,
    token_matches,
)
from yes24_agent.config import Settings, ensure_google_api_key_env, get_settings
from yes24_agent.history import register_history
from yes24_agent.logctx import ContextFilter, bound, new_request_id
from yes24_agent.matrix.matrix_runner import run_matrix_stream
from yes24_agent.overview import (
    ARM_YES24,
    OVERVIEW_ARMS,
    continue_overview,
    start_overview,
    warm_search,
)
from yes24_agent.rbti.profile import fetch_user_rbti
from yes24_agent.runner import run_agent_stream
from yes24_agent.session_service import SQLITE_DIALECT, db_dialect, persistence_mode
from yes24_agent.sse import OVERVIEW_EVENT_CONTRACT, SSE_EVENT_CONTRACT
from yes24_agent.starters import close_starter_service, register_starters
from yes24_agent.thought_translation import warmup_translation
from yes24_agent.toolsets import TOOLSETS, get_resolved_app, resolve_app_for
from yes24_agent.usage import close_usage_logger
from yes24_agent.user_data import UserDataService, close_user_data_service

logger = logging.getLogger(__name__)

# 웹 채팅 UI(단일 self-contained HTML).
_INDEX_HTML = Path(__file__).parent / "static" / "index.html"
# 16뷰 RBTI 매트릭스 시뮬레이터 UI(C4/matrix-ux 소유). 로그인월이 켜져 있으면 다른 보호
# 경로와 동일하게 월 뒤에 있다(_ACCESS_EXEMPT_PATHS에 없음 — "인증 없음"이던 옛 주석은
# 2026-08-19 감사에서 부패 판정).
_MATRIX_HTML = Path(__file__).parent / "static" / "matrix.html"
# 공유 패스워드 로그인월 페이지(access_password 설정 시 노출).
_LOGIN_HTML = Path(__file__).parent / "static" / "login.html"
# 두 UI가 공유하는 프론트 ES 모듈(마크다운·SSE·RBTI·출처 유틸). 페이지에 복제돼 갈라지던
# 코드를 이 디렉터리 한 사본으로 모으고 index/matrix가 /static/lib/*.js로 임포트한다.
_STATIC_LIB_DIR = Path(__file__).parent / "static" / "lib"


class _NoCacheStaticFiles(StaticFiles):
    """항상 재검증시키는 정적 파일 서버(Cache-Control: no-cache).

    페이지 HTML은 FileResponse라 매번 새로 읽히는데, 거기서 import한 ES 모듈만 브라우저
    캐시에 눌러앉아 구버전이 실행되는 문제가 있었다(마크다운 리터럴 누출의 정체). no-cache는
    조건부 요청(ETag/Last-Modified 304)으로 값싸게 최신을 보장한다 — 버전 쿼리를 파일마다
    붙여 관리하는 대신 서버 한 곳에서 끝낸다.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# 로그인월이 켜져도 통과시키는 예외 경로(헬스체크·로그인 페이지·로그아웃 자체).
# **API 문서 3종을 함께 연다**(2026-09-01 사용자 결정): 프론트 개발자가 클라이언트를 만들려면
# SSE 이벤트 계약을 봐야 하는데, 그 계약의 정본은 코드에서 생성되는 이 문서다(docs/는
# gitignore라 clone해도 안 온다). 여는 것은 **스키마이지 데이터가 아니다** — 엔드포인트는
# 그대로 월 뒤에 있고, admin 라우트는 애초에 OpenAPI에 실리지 않는다.
# 실제 호출·테스트에는 여전히 비밀번호가 필요하다(Swagger "Try it out" 포함).
_ACCESS_EXEMPT_PATHS = frozenset(
    {"/health", "/login", "/logout", "/docs", "/redoc", "/openapi.json"}
)


def _delegating_routes(app: FastAPI, judge) -> list:
    """`judge`를 의존성으로 가진 **라우트 객체** 목록 — 라우터에서 파생한다(손목록 금지).

    월 통과는 면제가 아니라 **판정 위임**이다. 그러므로 위임받을 판정자, 즉
    `get_authenticated_user` 의존성을 실제로 가진 라우트만 열 수 있다.

    종전엔 이 집합이 손으로 적은 상수였고 거기 `/chat/matrix`가 들어가 있었다 — 그런데 그
    라우트에는 인증 의존성이 없어서(등록 시그니처가 `request` 하나뿐) **아무 문자열이나
    x-api-key에 넣으면 16 페르소나 LLM 호출이 무검사로 열렸다**(2026-09-01 적대 검증 실측 →
    200, 스트림 시작). 목록을 지우고 의존성 그래프에서 파생하면 그 실수가 **구조적으로
    불가능**해진다 — 라우트에 의존성을 붙이는 것이 곧 여는 것이고, 안 붙이면 안 열린다.

    **경로 문자열이 아니라 라우트 객체를 돌려준다.** 문자열 집합으로 만들면 `route.path`가
    템플릿(`/chat/sessions/{id}`)인데 미들웨어가 비교하는 `request.url.path`는 구체 경로
    (`/chat/sessions/abc`)라 **영원히 일치하지 않는다** — 파라미터 라우트에 인증을 붙이는
    순간 그 라우트가 통째로 월에 막힌다(2026-09-01 적대 검증 5렌즈가 독립으로 같은 결함을
    지적). Starlette의 `route.matches(scope)`가 그 매칭을 소유하므로 그것을 쓴다.
    """
    def _walk(routes) -> list:
        found = []
        for route in routes:
            # FastAPI는 include_router로 얹은 라우트를 `_IncludedRouter` **프록시**로 감싸
            # app.routes에 넣는다 — 프록시에는 dependant가 없어서, 평면 순회만 하면 라우터로
            # 마운트한 라우트가 통째로 안 보인다(파생 집합이 비고 → 월이 그 기능을 죽인다).
            # 2026-09-01 실측으로 잡았다. "app에 직접 등록한다"는 관례로 우회할 수도 있지만
            # 그건 다음 사람이 include_router를 쓰는 순간 조용히 깨지므로, 여기서 **원 라우터로
            # 내려가** 구조가 마운트 방식과 무관하게 성립하도록 한다.
            inner = getattr(route, "original_router", None)
            if inner is not None:
                found.extend(_walk(inner.routes))
                continue
            dependant = getattr(route, "dependant", None)
            if dependant is not None and any(d.call is judge for d in dependant.dependencies):
                found.append(route)
        return found

    return _walk(app.routes)


def _key_checking_routes(app: FastAPI) -> list:
    """x-api-key로 월을 대신할 수 있는 라우트 — 판정자는 get_authenticated_user다."""
    return _delegating_routes(app, get_authenticated_user)


def _key_route_matches(routes: list, request: Request) -> bool:
    """이 요청이 위 라우트 중 하나에 실제로 매칭되는가 — 라우팅 규칙은 Starlette가 소유한다."""
    return any(route.matches(request.scope)[0] != Match.NONE for route in routes)


def _branded_html(path: Path, app_config=None) -> HTMLResponse:
    """페이지 HTML의 브랜딩 마커를 persona 문안으로 치환해 반환한다.

    치환 2종: `__BRAND_TITLE__`(제목·h1)과 `/*__BRANDING__*/null`(인사·부제·예시 칩 JSON).
    문안의 단일 출처는 toolsets.PERSONAS이고, 원본 파일은 마커를 유지한다(재하드코딩 금지
    — 가드는 test_toolsets). 매 요청 읽기라 dev 즉시 반영·성능은 FileResponse와 동급이다.
    app_config(ResolvedApp)를 주면 그 페르소나 문안을 쓴다 — 데모 세션의 역할별 브랜딩.
    """
    branding = (app_config or get_resolved_app()).persona.branding
    html = path.read_text(encoding="utf-8")
    payload = json.dumps(
        {
            "greeting": branding.greeting,
            "subtitle": branding.subtitle,
            "examples": list(branding.examples),
        },
        ensure_ascii=False,
        # json.dumps는 '/'를 이스케이프하지 않아 문안에 '</script>'가 들어오면 <script>
        # 블록이 조기 종료된다 — 표준 완화('</'→'<\/', JSON 의미 동일)로 구조적으로 막는다.
    ).replace("</", "<\\/")
    html = html.replace("__BRAND_TITLE__", branding.title)
    html = html.replace("/*__BRANDING__*/null", payload)
    return HTMLResponse(html)


# --- 공유 패스워드 로그인월(토큰·검증) ---
# 진짜 인증 시스템이 아니라 데모 접근을 막는 단일 공유 비밀번호 게이트다(config.access_password).
# 쿠키에는 비밀번호가 아니라 HMAC 토큰을 담아, 비밀번호 노출 없이 서버가 매 요청 재계산해 상수시간
# 비교로 확인한다 — 비밀번호가 키인 결정론 토큰이라 세션 저장소가 필요 없다.

# 로그인 성공 시 발급하는 쿠키 이름.
ACCESS_COOKIE = "yes24_access"
# 토큰 HMAC 메시지(비밀번호가 키). 값 자체는 비밀이 아니며 버전만 구분한다.
_TOKEN_MESSAGE = b"yes24-agent-access-v1"


def expected_token(password: str) -> str:
    """로그인월 토큰(auth.signed_access_token의 로그인월 message 바인딩)."""
    return signed_access_token(password, _TOKEN_MESSAGE)


def token_valid(cookie_value: str | None, password: str) -> bool:
    """쿠키 토큰 검증(auth.token_matches의 로그인월 message 바인딩)."""
    return token_matches(cookie_value, password, _TOKEN_MESSAGE)


def password_matches(candidate: str, password: str) -> bool:
    """입력 비밀번호가 설정값과 일치하는지 상수시간 비교로 판정한다(타이밍 공격 완화)."""
    return compare_digest(candidate.encode("utf-8"), password.encode("utf-8"))


def settings_unlocked(request: Request) -> bool:
    """이 요청이 세팅(모델 선택·도구 토글·모델명 노출)에 접근할 수 있는지 판정한다.

    로그인월이 꺼져 있거나(로컬 개발) admin_access_password가 미설정이면 전부 허용(기존 동작).
    둘 다 설정된 배포에선 admin 토큰 쿠키를 가진 세션만 허용한다 — 데모 공유 비밀번호
    (access_password)로 들어온 세션에는 모델명과 설정 UI를 숨긴다. 쿠키 토큰이 비밀번호별
    HMAC이라 별도 세션 저장 없이 토큰 재계산만으로 역할이 구분된다.
    """
    settings = get_settings()
    if not settings.access_password or not settings.admin_access_password:
        return True
    return token_valid(request.cookies.get(ACCESS_COOKIE), settings.admin_access_password)


def access_role(request: Request) -> str | None:
    """현재 요청의 로그인 역할 — 로그인월이 꺼져 있으면 None(배지·로그아웃 UI 비표시).

    "admin"(세팅 조정 가능) 또는 "demo"(세팅 잠금). 프론트는 `GET /me`로 조회한다 —
    내장 페이지든 외부 프론트든 같은 API 계약 하나만 쓴다(마커 주입 방식은 내장 페이지
    전용이라 API 분리 원칙에 따라 삭제).
    """
    if not get_settings().access_password:
        return None
    return "admin" if settings_unlocked(request) else "demo"


def app_for_request(request: Request):
    """이 요청이 쓸 앱 구성 — 데모 세션이면 config의 데모 전용 구성, 아니면 None(기본 위임).

    데모(access_password 로그인)는 서버 기본이 무엇이든 demo_persona·demo_enabled_toolsets로
    고정된다(브랜딩·정체성·도구 파생). None 반환은 "요청 지정 또는 서버 기본을 따르라"는
    기존 계약 그대로다. 해석·검증은 resolve_app_for 단일 경로(fail-loud·lru 캐시)를 탄다.
    """
    if settings_unlocked(request):
        return None
    settings = get_settings()
    return resolve_app_for(settings.demo_persona, frozenset(settings.demo_enabled_toolsets))


async def _hide_model_frames(stream):
    """SSE 스트림의 done 프레임에서 `model` 필드를 벗겨낸다(데모 로그인 모델명 비노출).

    프레임은 `event: {e}\\ndata: {json}\\n\\n` 문자열이라(sse.format_sse) done 이벤트만
    data를 재직렬화하고, 고빈도 delta 프레임은 파싱 없이 그대로 통과시킨다(스트리밍 무지연).
    """
    async for frame in stream:
        if frame.startswith("event: done\n"):
            head, _, body = frame.partition("data: ")
            payload = json.loads(body)
            payload.pop("model", None)
            frame = f"{head}data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield frame


# 요청 본문 텍스트 제약: 공백 트림 후 비어 있지 않고, config 상한(request_max_chars)을
# 넘지 않아야 한다. 초과 시 pydantic이 422를 내 초장문 입력을 입구에서 구조적으로 거절한다
# (키워드 탐지가 아니라 길이 제약). 상한은 하드코딩 대신 config에서 읽는다.
NonBlankText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=get_settings().request_max_chars,
    ),
]


# 스트리밍 라우트의 OpenAPI 응답 기술. FastAPI는 StreamingResponse의 미디어 타입을 추론하지
# 못해 기본 `application/json`으로 문서화한다 — 프론트가 그 문서를 믿고 `res.json()`을 쓰면
# 그냥 멈춘다(2026-09-01 지적). 계약 본문은 sse.py가 소유하고 여기선 싣기만 한다(사본 금지).
_SSE_RESPONSES: dict = {
    200: {
        "description": "SSE 이벤트 스트림 (application/json 아님)",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    }
}


# 요청 필드 중 **어드민 전용**임을 선언하는 표식. 공개 OpenAPI에서 그 필드를 빼는 근거이고,
# 감출 이름을 어딘가에 손으로 적어 두지 않기 위한 장치다(월 통과 집합을 의존성 그래프에서
# 파생한 것과 같은 이유 — 손목록은 갱신을 잊는 순간 조용히 어긋난다).
ADMIN_ONLY_MARK = "x-admin-only"


class ChatRequest(BaseModel):
    """`/chat/stream` 요청 본문 — 설명은 **OpenAPI로 나간다**(주석은 /docs에 안 보인다).

    프론트가 채우는 필드는 `message`·`session_id`·`use_rbti` 셋이다. RBTI **코드**는 사람에게
    붙는 값이라 **서버가 조회**하고(외부 RBTI API — rbti/profile.py), **이번 턴에 쓸지 말지**는
    화면의 선택이므로 프론트가 보낸다. 감춰진 `rbti`는 데모 UI의 선택기가 유형을 일시적으로
    덮어쓰기 위한 것이다.

    나머지 둘은 어드민(데모 로그인) 화면의 모델·도구 토글용이라 API 키 호출에서는
    무시되는데, 효과 없는 필드가 스키마에 보이는 것이 가장 헷갈리므로 `ADMIN_ONLY_MARK`를
    달아 공개 스키마에서 뺀다(감출 이름을 손목록으로 적지 않고 **선언에서 파생**한다 —
    필드가 늘어도 표식만 달면 되고, 목록을 갱신하지 않아 새는 일이 없다).
    """

    message: NonBlankText = Field(
        description="사용자 질문. 공백만이면 422, config `request_max_chars` 초과도 422다.",
        examples=["한강 작가 책 추천해줘"],
    )
    session_id: str | None = Field(
        default=None,
        description="이어갈 대화 id. **비우면 새 대화**가 만들어지고 그 id가 `done.session_id`로"
        " 돌아온다 — 다음 턴부터 그 값을 실어 보낸다. 클라이언트가 직접 만든 id(UUID 등)를"
        " 보내도 된다: 그 id의 대화가 없으면 **그 id 그대로** 새 대화가 만들어진다(crema 방식)."
        " 남의 대화 id를 넣어도 자기 것만 조회되므로 탈취는 성립하지 않는다.",
    )
    model_config = {
        "json_schema_extra": {
            "examples": [
                # Swagger UI의 "Try it out"이 이 본문을 그대로 채운다. 예시를 안 주면 스키마에서
                # 자동 생성한 `{"session_id": "string", ...}`이 들어가는데, 그 자리표시자가 실제
                # 값으로 전송돼 **"string"이라는 이름의 대화가 만들어진다**(2026-09-02 실측).
                # 그래서 예시는 "복사해서 바로 보내도 맞는" 최소 본문으로 둔다.
                {"message": "한강 작가 책 추천해줘"},
                {"message": "그 책 몇 쪽이야?", "session_id": "이전 응답의 done.session_id"},
                {"message": "요즘 읽을 만한 소설?", "use_rbti": True},
            ]
        }
    }

    use_rbti: bool = Field(
        default=True,
        description="이 턴에 RBTI 독서 유형을 적용할지. **기본 true** — 프론트가 아무것도 싣지"
        " 않아도 서버가 그 사용자의 유형을 외부 RBTI API로 조회해 적용한다(2026-09-10 결정)."
        " 유형이 없는 사용자이거나 조회에 실패하면 `done.rbti_applied`가 null이고 답변은 그대로"
        " 나간다(오류 아님). 배지는 `done.rbti_applied`가 null이 아닌지로 판단하고, 스트리밍"
        " 중에는 `status{stage:'rbti'}`가 턴 시작에 한 번 와서 그전에도 알 수 있다."
        " 성향을 끄고 싶은 화면은 `false`를 명시한다.",
    )
    rbti: str | None = Field(
        default=None,
        json_schema_extra={ADMIN_ONLY_MARK: True},
        description="어드민 전용 — 데모 UI의 페르소나 선택기가 그 사람의 유형을 일시적으로"
        " 덮어쓸 때만 쓴다. 일반 클라이언트는 실을 필요가 없다: 사용자의 유형은"
        " 서버가 외부 RBTI API로 조회해 적용한다.",
    )
    model: str | None = Field(
        default=None,
        json_schema_extra={ADMIN_ONLY_MARK: True},
        description="어드민 전용 — 데모 로그인 세션의 모델 선택. API 키 호출에서는 무시된다.",
    )
    enabled_toolsets: list[str] | None = Field(
        default=None,
        json_schema_extra={ADMIN_ONLY_MARK: True},
        description="어드민 전용 — 데모 로그인 세션의 도구 토글. API 키 호출에서는 무시된다."
        " 어드민 세션에서 무효 키를 주면 조용히 폴백하지 않고 400이다.",
    )


class OverviewRequest(BaseModel):
    """`/overview`·`/overview/warm`·`/overview/continue` 요청 본문(검색결과 AI 오버뷰).

    프론트가 채우는 것은 `query`(+선택적 `section`)뿐이다. `sources`는 접지원 비교 하네스용이라
    ADMIN_ONLY_MARK로 공개 문서에서 뺀다 — 프로덕션 요청은 이 필드를 모른다.
    """

    query: NonBlankText = Field(
        description="사용자가 친 **검색어**(대화 메시지가 아니다). 세 라우트가 같은 값을 쓰고,"
        " 그 값이 곧 오버뷰 캐시 키다 — /overview/warm으로 미리 데우고 /overview로 받은 뒤"
        " /overview/continue로 대화를 이어갈 때 **셋에 같은 문자열**을 보내야 캐시가 맞는다.",
        examples=["불편한 편의점"],
    )
    # 검색 범위. 빈 문자열·미지 값은 start_overview 입구가 urls.py 정본(SEARCH_SECTIONS)으로
    # 최광역 범위에 정규화한다 — 캐시 키·도구가 같은 값을 보므로 임의 변형이 키를 가르지
    # 않는다. 여기서는 길이만 기존 본문 상한으로 잠근다(무제한 문자열 입구 차단, query와 동일
    # config 상한 재사용).
    section: Annotated[
        str, StringConstraints(max_length=get_settings().request_max_chars)
    ] = Field(
        default="",
        description="검색 범위(Yes24 검색 페이지의 카테고리 탭). 비우면 최광역 범위다."
        " 모르는 값도 서버가 최광역으로 정규화하므로 오류가 아니다.",
    )
    # 접지원 팔(테스트 하네스 — config.overview_compare_enabled). 빈 문자열 = 미지정 =
    # 현행 기본 팔(overview.ARM_YES24)이라 **프로덕션 요청은 이 필드를 모른다**. 값이 실리면
    # 라우트가 스위치와 화이트리스트(overview.OVERVIEW_ARMS)를 검사해 400으로 거른다 —
    # 스위치 off면 우회 경로가 없다. /overview/warm·/overview/continue는 이 필드를 읽지
    # 않는다: 워밍은 Yes24 검색 선행일 뿐이고(팔 무관), 이어가기는 기본 팔 응답의 씨앗만
    # 잇는다(비교 팔 본문을 채팅으로 들고 가는 것은 하네스의 일이 아니다).
    sources: Annotated[
        str, StringConstraints(max_length=get_settings().request_max_chars)
    ] = Field(
        default="",
        json_schema_extra={ADMIN_ONLY_MARK: True},
        description="접지원 비교 하네스 전용 — 프로덕션 요청은 쓰지 않는다.",
    )


class MatrixRequest(BaseModel):
    """`/chat/matrix` 요청 본문(16뷰 매트릭스 시뮬레이터)."""

    question: NonBlankText
    session_id: str | None = None
    # 채팅과 동일한 화이트리스트 계약. selectable_models 값 밖(없음·임의 문자열)은
    # 기본(pro)으로 폴백한다(임의 모델 주입 차단) — /chat/stream과 동일 로직.
    model: str | None = None


def _configure_logging() -> None:
    """앱 로거(`yes24_agent.*`)의 INFO 로그가 콘솔에 나오게 설정한다.

    uvicorn 기본 설정은 root 로거에 핸들러를 달지 않아 도구 호출 기록·무효 인용
    경고 같은 앱 INFO 로그가 묻힌다. basicConfig로 콘솔 핸들러를 보장하고
    (핸들러가 이미 있으면 no-op) 앱 로거 레벨을 INFO로 명시한다. httpx 등
    서드파티 요청 소음은 WARNING으로 억제한다.

    config.log_file_path가 설정돼 있으면 같은 포맷의 RotatingFileHandler를 root에
    덧붙여 stdout+파일 이중 기록한다(배포 후 사후 디버깅). 크기·백업 수도 config에서
    읽어 하드코딩을 피한다. 파일 경로가 비면 stdout만(로컬 개발 기본).
    """
    settings = get_settings()
    # `%(ctx)s`는 logctx.ContextFilter가 채운다 — 요청 문맥(req·session·turn·user)이 모든
    # 줄에 자동으로 붙는다. 문맥 밖(기동 로그)에서는 빈 문자열이라 지저분해지지 않는다.
    log_format = "%(asctime)s %(levelname)s %(name)s:%(ctx)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_format)
    if settings.log_file_path:
        path = Path(settings.log_file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)
    # 필터는 **핸들러마다** 달아야 한다(로거에 달면 상위 로거로 전파된 레코드를 놓친다).
    for handler in logging.getLogger().handlers:
        handler.addFilter(ContextFilter())
    logging.getLogger("yes24_agent").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 수명주기 훅: 시작 시 로깅·API 키 매핑, 종료 시 공유 HTTP 클라이언트 정리."""
    _configure_logging()
    # ADK는 GOOGLE_API_KEY를 기대한다 — GEMINI_API_KEY를 매핑해 둔다.
    if not ensure_google_api_key_env():
        logger.warning("GEMINI/GOOGLE API 키가 설정되지 않았습니다. LLM 호출이 실패할 수 있어요.")
    await UserDataService.get_instance().verify_schema()
    # 사고 라벨 번역 경로를 백그라운드로 데운다(첫 채팅의 첫 한국어 라벨 ~0.3초 단축).
    # 기동을 막지 않도록 task로만 띄우고, 참조를 잡아 GC 취소를 막는다.
    app.state.translation_warmup = asyncio.create_task(warmup_translation())
    yield
    # 공유 HTTP 클라이언트를 정리해 열린 커넥션을 닫는다 — 훅 목록은 toolset 레지스트리
    # 파생이라 새 toolset이 생겨도 여기는 무수정이다(미생성 클라이언트는 no-op).
    for hook in get_resolved_app().aclose_hooks:
        await hook()
    # 인증 DB 커넥션 풀도 함께 닫는다(만들어진 적 없으면 no-op).
    await close_auth_service()
    # 사용자별 대화 데이터(피드백·읽음·제목) 풀도 같은 방식으로 닫는다(만들어진 적 없으면 no-op).
    await close_user_data_service()
    # 초기 질문 풀 서비스도 같은 방식으로 닫는다(만들어진 적 없으면 no-op).
    await close_starter_service()
    # 토큰 사용량 기록 풀도 나란히 정리한다 — 진행 중인 fire-and-forget INSERT를
    # 배수한 뒤 닫는다(만들어진 적 없으면 no-op).
    await close_usage_logger()


def _register_frontend(app: FastAPI, settings: Settings) -> None:
    """내장 프론트(정적 모듈·페이지·로그인월)를 등록한다 — serve_frontend=False면 미호출.

    로그인월도 여기 있다: 월은 내장 UI 접근을 가리는 프론트 관심사이고, 백엔드 전용
    배포에서는 API 앞에 다른 인증(외부 프론트·게이트웨이)이 서기 때문이다. API 라우트
    (/chat/stream ·/health ·/models ·/toolsets)는 게이트 밖에 남아 항상 등록된다.
    """
    # 공유 패스워드 로그인월. access_password가 빈 문자열이면 미들웨어가 전부 통과(무월).
    # 값이 있으면 보호 경로에서 유효 쿠키를 요구한다: HTML 내비게이션(GET+Accept:text/html)은
    # /login으로 302, 그 외(API·fetch)는 401. /health·/login은 예외.
    if settings.access_password:
        # 로그인월이 받는 비밀번호 목록(설정된 것만). 데모(access_password)와 세팅용
        # (admin_access_password) 어느 쪽 토큰이든 월은 통과시키고, 역할 구분(세팅 접근)은
        # settings_unlocked가 담당한다.
        wall_passwords = [
            pw for pw in (settings.access_password, settings.admin_access_password) if pw
        ]

        # 허용 경로는 **첫 요청 때 한 번** 파생해 캐시한다. 등록 시점에 계산하면 안 된다 —
        # _register_frontend는 create_app에서 API 라우트보다 **먼저** 돌아서 그때 app.routes가
        # 비어 있고, 그러면 모든 API가 월에 막힌다(실측으로 잡은 함정).
        route_cache: dict[str, list] = {}

        @app.middleware("http")
        async def access_gate(request: Request, call_next):
            path = request.url.path
            cookie = request.cookies.get(ACCESS_COOKIE)
            # **API 키를 들고 온 요청은 월이 막지 않는다**(2026-09-01). 이 앱의 인증은 두
            # 겹이다 — 데모 페이지용 공유 비밀번호 쿠키(이 월)와, 진짜 클라이언트용
            # x-api-key(auth.AuthService: 사용자 식별·레이트리밋). 그런데 월이 모든 요청
            # 앞에 서서 **후자를 가렸다**: 정상 API 키를 들고 와도 쿠키가 없으면 401이라
            # get_authenticated_user가 실행조차 안 됐고, 외부 프론트가 데모 비밀번호를
            # 알아야 하는 뒤집힌 상황이 됐다.
            #
            # 통과는 **판정 위임**이지 면제가 아니다 — 키의 유효성·한도는 라우트 의존성이
            # 그대로 검사해 무효 키는 401, 초과는 429가 된다. 그래서 **위임할 판정자가 있는
            # 경로만** 연다(_API_KEY_ROUTES): HTML 페이지 라우트에는 그 의존성이 없어,
            # 열어 주면 아무 문자열이나 헤더에 넣고 데모 UI를 통째로 받아 갈 수 있다
            # (실측으로 확인하고 되돌린 구멍이다). Accept 헤더로는 못 가른다 — 스푸핑된다.
            # 목록은 **통과 허용**이라 fail-safe다: 새 라우트를 여기 안 적으면 월이 그대로
            # 지키므로, 빠뜨림이 노출이 아니라 불편으로만 나타난다.
            # `service.enabled`(세션 DB가 mysql)를 함께 요구하는 이유: 인증 스택이 없는
            # 구성에서는 get_authenticated_user가 헤더를 무시하고 익명 허용으로 흘려보내
            # 판정자가 사실상 없어진다.
            # **CORS 프리플라이트는 월이 막지 않는다**(2026-09-02 외부 브라우저 실측).
            # 브라우저는 실제 요청 전에 OPTIONS를 먼저 보내는데, 그 프리플라이트에는 설계상
            # 인증 헤더가 실리지 않는다 — "x-api-key를 보내도 되냐"고 묻는 요청 자체이기
            # 때문이다. 월이 그것을 키 없는 요청으로 보고 401을 내면 CORS 헤더가 나가지
            # 않고, 브라우저는 본 요청을 아예 보내지 않는다. 즉 **키가 맞고 오리진이 허용
            # 목록에 있어도 브라우저에서는 이 API를 쓸 수 없었다**(curl은 프리플라이트를
            # 보내지 않아 전 점검이 이를 통과시켰다).
            # 판정은 `Access-Control-Request-Method` 헤더의 존재로 한다 — 브라우저만 붙이는
            # 프리플라이트의 정의 그 자체다(경로 목록·User-Agent 문자열이 아니다). 흘려보내면
            # CORSMiddleware가 허용 오리진일 때만 응답하므로 우회로가 되지 않는다: 프리플라이트
            # 응답에는 본문이 없고, 실제 요청은 다시 월과 라우트 의존성을 통과해야 한다.
            if request.method == "OPTIONS" and "access-control-request-method" in request.headers:
                return await call_next(request)

            key_routes = route_cache.get("routes")
            if key_routes is None:
                key_routes = route_cache["routes"] = _key_checking_routes(app)
                route_cache["admin"] = _delegating_routes(app, require_admin)
                logger.info(
                    "로그인월: x-api-key 통과 허용 경로 "
                    f"{sorted(r.path for r in key_routes)} / x-admin-key 통과 허용 경로 "
                    f"{sorted(r.path for r in route_cache['admin'])}"
                )
            if (
                request.headers.get("x-api-key")
                and _key_route_matches(key_routes, request)
                and AuthService.get_instance().enabled
            ):
                return await call_next(request)
            # 운영자 헤더도 같은 규칙이다 — 판정자(require_admin)를 가진 라우트만 열고, 키의
            # 유효성은 그 판정자가 그대로 검사한다(무효 헤더는 401). 월이 이것을 막으면 배포
            # 환경에서 어드민 API에 닿을 길이 없어진다(쿠키를 발급하는 /admin 로그인은 sqlite
            # 구성에서만 등록된다).
            if request.headers.get("x-admin-key") and _key_route_matches(
                route_cache["admin"], request
            ):
                return await call_next(request)
            if path in _ACCESS_EXEMPT_PATHS or any(
                token_valid(cookie, pw) for pw in wall_passwords
            ):
                return await call_next(request)
            accept = request.headers.get("accept", "")
            if request.method == "GET" and "text/html" in accept:
                return RedirectResponse("/login", status_code=302)
            return JSONResponse({"detail": "인증이 필요합니다."}, status_code=401)

        # 데모 UI 페이지는 **OpenAPI 스키마에서 뺀다**(include_in_schema=False, 2026-09-01).
        # /docs는 프론트 개발자가 클라이언트를 만들려고 보는 문서인데, 브라우저가 여는 HTML
        # 페이지(/ ·/matrix ·/login ·/logout)가 섞이면 "무엇을 호출해야 하는가"가 흐려진다.
        # 라우트는 그대로 살아 있고 문서에서만 감춘다.
        @app.get("/login", include_in_schema=False)
        async def login_page() -> HTMLResponse:
            """로그인월 페이지(공유 패스워드 입력) — 브랜딩 마커 치환 서빙."""
            return _branded_html(_LOGIN_HTML)

        @app.post("/login", include_in_schema=False)
        async def login_submit(request: Request):
            """패스워드를 검증해 성공 시 접근 쿠키를 발급하고 홈으로 보낸다."""
            form = await request.form()
            candidate = str(form.get("password", ""))
            # 일치한 비밀번호에서 파생된 토큰을 발급한다 — 쿠키 값 자체가 역할(데모/세팅)이다.
            for pw in wall_passwords:
                if password_matches(candidate, pw):
                    resp = RedirectResponse("/", status_code=303)
                    resp.set_cookie(
                        ACCESS_COOKIE,
                        expected_token(pw),
                        max_age=settings.access_cookie_max_age_s,
                        httponly=True,
                        samesite="lax",
                        secure=settings.cookie_secure,
                    )
                    return resp
            # 실패: 로그인 페이지로 되돌리며 에러 표시(?error=1). 공인 IP에 노출된 공유
            # 비밀번호라 반복 추측이 눈에 띄도록 실패를 남긴다(차단은 프록시 계층 몫).
            logger.warning(f"로그인월 인증 실패: ip={client_ip(request)}")
            return RedirectResponse("/login?error=1", status_code=303)

        @app.get("/logout", include_in_schema=False)
        async def logout() -> RedirectResponse:
            """접근 쿠키를 지우고 로그인 페이지로 보낸다(데모↔세팅 계정 전환용)."""
            resp = RedirectResponse("/login", status_code=303)
            resp.delete_cookie(ACCESS_COOKIE)
            return resp

        if settings.admin_access_password:
            # 데모 강제 구성은 첫 데모 요청이 아니라 기동 시점에 검증한다(fail-loud —
            # 무효 demo_persona·demo_enabled_toolsets로 배포되면 여기서 즉시 죽는다).
            resolve_app_for(
                settings.demo_persona, frozenset(settings.demo_enabled_toolsets)
            )

    # 공용 프론트 모듈만 노출한다(페이지 HTML은 각 라우트가 담당). 로그인월이 켜져 있으면 이
    # 경로도 미들웨어 게이트를 통과해야 한다(같은 출처 fetch라 쿠키가 함께 간다).
    # no-cache로 항상 재검증시킨다 — 페이지 HTML(FileResponse)은 매번 새로 읽는데 import된 ES
    # 모듈만 브라우저에 눌러앉아 구버전이 실행되던 문제를 막는다(버전 쿼리 없이 단일 지점 해결).
    app.mount("/static/lib", _NoCacheStaticFiles(directory=_STATIC_LIB_DIR), name="static-lib")

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        """웹 채팅 UI를 반환한다(로그인월 활성 시 쿠키 필요) — 브랜딩 마커 치환 서빙.

        데모 세션은 데모 전용 페르소나 문안(제목·인사·예시 칩)으로 서빙된다.
        """
        return _branded_html(_INDEX_HTML, app_config=app_for_request(request))

    # /me는 데모 UI의 역할 배지·세팅 노출 판단용이지 제품 API가 아니다 — /models·/toolsets와
    # 함께 OpenAPI에서 뺀다(include_in_schema=False, 2026-09-01 사용자 결정). 라우트·권한은
    # 그대로다(내장 프론트가 쓴다) — 문서에서만 감춘다.
    @app.get("/me", include_in_schema=False)
    async def me(request: Request) -> dict:
        """현재 로그인 역할: {"role": "admin"|"demo"|null}. null = 로그인월 비활성.

        프론트(내장·외부 공통)가 역할 배지·로그아웃 링크·세팅 UI 노출을 판단하는 단일
        계약이다. 세팅 강제 자체는 서버가 한다(/models·/toolsets 403, done.model 제거) —
        이 응답은 표시용이지 보안 경계가 아니다.
        """
        return {"role": access_role(request)}

    # RBTI 16뷰 매트릭스 UI도 배포 게이팅(matrix_enabled). off면 /matrix 라우트를 아예
    # 등록하지 않아 404가 된다(프로드 숨김) — 채팅 경로(/ ·/chat/stream ·/health)는 무영향.
    if settings.matrix_enabled:

        # GET+HEAD 둘 다 등록한다 — 프론트 네비 링크가 HEAD로 활성 여부를 게이팅하는데,
        # FastAPI GET 라우트는 HEAD를 자동 허용하지 않아(405; 프록시 뒤에선 503) 링크가 안 뜬다.
        @app.api_route("/matrix", methods=["GET", "HEAD"], include_in_schema=False)
        async def matrix_ui(request: Request) -> FileResponse:
            """16뷰 RBTI 매트릭스 시뮬레이터 UI — **어드민 역할 전용**.

            페이지만 열려 있어도 의미가 없다(호출하는 /chat/matrix가 403이다). 화면과 API의
            권한을 같은 술어로 묶어 "열리는데 안 되는" 상태를 만들지 않는다.
            """
            if not settings_unlocked(request):
                raise HTTPException(status_code=403, detail="설정 접근 권한이 없습니다.")
            return FileResponse(_MATRIX_HTML, media_type="text/html")


def _overview_arm(sources: str) -> str:
    """요청의 sources를 접지원 팔로 해석한다 — 미지정은 현행 기본 팔(동작 불변).

    비활성 스위치에서의 명시 지정은 **조용히 기본으로 강등하지 않고** 400으로 거절한다:
    하네스가 꺼진 서버에 3팔을 던지면 세 패널이 같은 답을 그려 "차이 없음"이라는 거짓
    관측이 되기 때문이다(빈 성공 위장 금지와 같은 계열).
    """
    if not sources:
        return ARM_YES24
    if not get_settings().overview_compare_enabled or sources not in OVERVIEW_ARMS:
        raise HTTPException(
            status_code=400, detail=f"지원하지 않는 접지원입니다: {sources!r}"
        )
    return sources


# `/docs` 첫 화면에 뜨는 안내 — **여기가 프론트 개발자의 진입점**이다.
# 엔드포인트 목록만 있고 "인증은 뭐로, 첫 호출은 뭘로, 응답은 어떻게 읽나"가 없으면 문서를
# 열어도 시작을 못 한다. 스트리밍 계약 본문은 /chat/stream 설명에 붙는 SSE_EVENT_CONTRACT가
# 정본이라 여기서 되풀이하지 않고 가리키기만 한다(같은 설명 두 벌 금지).
API_DESCRIPTION = r"""Yes24 책·상품에 밝은 AI 대화 어시스턴트 API.

**인증** — 모든 호출에 헤더 `x-api-key`를 넣는다. 값은 브라우저의 **`ServiceCookies` 쿠키
값**이다(Yes24 로그인 쿠키, HttpOnly가 아니라 JS로 읽힌다). 서버가 그 값으로 Yes24 회원
API를 조회해 userNo를 얻고, 그 userNo로 대화를 사람 단위로 가른다. crema-ai와 같은 계약이라
프론트 코드를 그대로 쓰면 된다:

```js
const key = document.cookie.match(/(?:^|;\s*)ServiceCookies=([^;]*)/)?.[1];
headers.set("x-api-key", decodeURIComponent(key));
```

**개발 환경**에서는 Yes24 로그인 없이 쓸 수 있는 별도 개발 키를 발급한다 — 값은 팀에
문의(이 문서에 적지 않는다). 위 **Authorize** 버튼에 넣으면 이 페이지에서 바로 호출해 볼 수
있다.

키 없이 부르면 **모든 API가 401**이고, 식별되지 않는 키는 **403**이다(임의 문자열은 키가
되지 않는다). 한도 초과는 429다.

**RBTI 독서 유형** — 유형 코드는 프론트가 싣는 값이 아니라 **서버가 사용자(userNo)로 외부
RBTI API에 조회**하는 값이다. 프론트는 요청에 `use_rbti`(불리언)만 싣고, 응답의
`done.rbti_applied`가 **null이 아니면** "✦ RBTI 데이터가 활용됨" 배지를 켠다.

`use_rbti: true`인데도 null이 나오는 경우가 둘 있고 **둘 다 오류가 아니다**: 그 사용자에게
아직 유형이 없거나, 조회에 실패한 경우다. 어느 쪽이든 답변 자체는 정상으로 나간다 — 성향은
답을 더 맞게 만드는 부가 정보이지 답의 전제가 아니다.

**첫 호출** — `POST /chat/stream`에 `{"message": "한강 작가 책 추천해줘"}`만 보내면 된다.
`session_id`를 비우면 새 대화가 만들어지고, 그 id가 스트림 마지막 `done` 이벤트의
`session_id`로 돌아온다. 다음 턴부터 그 값을 실어 보내면 대화가 이어진다.

**응답 읽는 법** — 답변은 JSON이 아니라 **SSE 스트림**이다. 이벤트 종류와 지켜지는 계약,
붙여 쓸 수 있는 예제는 아래 `POST /chat/stream` 설명에 전부 있다. 먼저 읽어라.

**화면 만들기** — 대화 목록·복원·이름 변경·삭제·좋아요·링크 클릭 기록은 `/chat/sessions*`
(history 태그)에 있다. 목록의 `unread`는 "답변이 끝났는데 아직 안 본 대화"이고, 그 대화를
`GET /chat/sessions/{session_id}`로 열면 자동으로 꺼진다(별도 읽음 API는 없다).

**검색결과 오버뷰** — `POST /overview`는 검색어 하나로 상품군을 정리해 주는 별도 기능이다
(대화가 아니다). 같은 SSE 계약을 쓰고, 사용자가 더 묻고 싶어 하면 `POST /overview/continue`가
그 내용을 이어받은 대화 세션을 만들어 준다.
"""


def _hide_admin_only_fields(app: FastAPI) -> None:
    """생성된 OpenAPI에서 `ADMIN_ONLY_MARK`가 달린 요청 필드를 지운다.

    어드민(데모 로그인) 전용 필드는 API 키 호출에서 무시되는데, 효과 없는 필드가 스키마에
    보이면 프론트가 "보내면 되는 값"으로 오해한다 — 보내도 아무 일이 없고 에러도 안 나서
    원인을 찾기 어렵다. **문서에서만** 감춘다(파싱은 원본 모델이 그대로 하므로 어드민
    화면은 계속 보낼 수 있다. 차단은 이미 라우트의 unlocked 판정이 한다).

    감출 이름을 손목록으로 적지 않고 **필드 선언의 표식에서 파생**한다 — 월 통과 집합을
    의존성 그래프에서 파생한 것과 같은 이유로, 목록은 갱신을 잊는 순간 조용히 어긋난다.
    요청 본문 스키마를 openapi_extra로 통째 교체하는 길은 막혀 있다: FastAPI가 그것을
    덮어쓰지 않고 **깊은 병합**을 해서 원래의 `$ref`가 그대로 남는다(실측).
    """
    schemas = app.openapi().get("components", {}).get("schemas", {})
    for schema in schemas.values():
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        hidden = [
            name
            for name, spec in properties.items()
            if isinstance(spec, dict) and spec.pop(ADMIN_ONLY_MARK, False)
        ]
        for name in hidden:
            del properties[name]


def create_app() -> FastAPI:
    """FastAPI 앱을 조립한다."""
    settings = get_settings()
    app = FastAPI(title="yes24-agent", description=API_DESCRIPTION, lifespan=lifespan)

    # CORS: 자격증명 동반 요청과 `*`의 조합은 브라우저가 거부하므로 패턴으로 허용한다
    # (조직 도메인 + 로컬). 명시 목록은 패턴 밖 예외용이고 둘은 OR로 합쳐진다.
    @app.middleware("http")
    async def request_log(request: Request, call_next):
        """요청 1건에 문맥을 부여하고 접근 로그를 남긴다 — **가장 바깥 미들웨어**.

        여기서 bind한 문맥은 이 요청이 만드는 모든 로그 줄(도구·러너·후처리)에 자동으로
        붙는다(logctx). 그래야 동시 사용자의 줄이 섞여도 가릴 수 있다.

        접근 로그가 따로 필요한 이유: uvicorn의 것은 **stdout에만** 나가고 파일 로그에는
        없어서, 컨테이너를 재기동하면 사라진다. 그리고 uvicorn 줄에는 소요 시간도 요청
        문맥도 없다. 여기서 남기면 파일에 함께 쌓이고 회전 정책도 같이 적용된다.

        **본문·질의어는 남기지 않는다** — 로그는 오래 남고 열람 범위가 넓다. 경로·상태·시간만
        남기고, 무엇을 물었는지는 events(대화 원문)에 이미 있으며 그쪽이 삭제 API의 대상이다.
        """
        started = time.perf_counter()
        route_path = request.url.path
        with bound(req=new_request_id()):
            response = await call_next(request)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            # 라우트 템플릿이 있으면 그것으로 — 구체 경로는 세션 id가 섞여 집계가 안 된다.
            matched = request.scope.get("route")
            path = getattr(matched, "path", None) or route_path
            logger.info(
                f"{request.method} {path} {response.status_code} {elapsed_ms}ms"
            )
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=settings.cors_origin_regex or None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if settings.serve_frontend:
        _register_frontend(app, settings)

    # 운영자 데이터 조회(admin). admin_password가 비어 있으면 라우트를 등록하지 않는다(404).
    # admin은 세션 sqlite 파일을 직접 여는 조회기라(mode=ro) 네트워크 DB에선 동작할 수
    # 없다 — sqlite일 때만 등록해, 열리지 않는 페이지를 노출하지 않는다.
    if db_dialect(settings.session_db_url) == SQLITE_DIALECT:
        register_admin(app, settings)

    @app.get("/health")
    async def health() -> dict:
        """헬스체크. persistence는 세션 서비스의 **실제** 영속 모드다.

        네트워크 DB에서 InMemory 폴백이 조용히 발동하면 응답은 정상인데 대화가 재시작마다
        증발한다 — 밖에서 관측 가능하게 노출한다(fail-fast는 session_fallback_allowed).
        """
        return {"status": "ok", "persistence": persistence_mode()}

    # /models·/toolsets는 데모 UI의 세팅 컨트롤이지 제품 API가 아니다 — OpenAPI에서만 뺀다
    # (include_in_schema=False, 2026-09-01 사용자 결정). 동작·권한(잠금 403)은 불변.
    @app.get("/models", include_in_schema=False)
    async def models(request: Request) -> dict:
        """UI 모델 선택기용 목록(라벨→모델ID)과 기본 모델. 화이트리스트가 단일 진실.

        데모 로그인(세팅 잠금)에는 403 — 프론트는 목록 조회 실패 시 선택기를 숨기고 서버
        기본 모델로 동작한다(기존 폴백 경로라 프론트 무수정).
        """
        if not settings_unlocked(request):
            raise HTTPException(status_code=403, detail="설정 접근 권한이 없습니다.")
        settings = get_settings()
        return {"models": settings.selectable_models, "default": settings.model_name}

    @app.get("/toolsets", include_in_schema=False)
    async def toolsets(request: Request) -> dict:
        """UI 도구 토글용 목록. 레지스트리(TOOLSETS)가 단일 진실이라 새 toolset이 추가되면
        프론트 수정 없이 따라온다. 잠금 항목은 없다 — 비어있지만 않으면 모든 조합이 유효하고
        (2026-08-06 사용자 방향) 정체성은 켜진 toolset에서 파생된다.

        데모 로그인(세팅 잠금)에는 403 — 프론트는 조회 실패 시 토글 메뉴를 숨긴다(/models 동일)."""
        if not settings_unlocked(request):
            raise HTTPException(status_code=403, detail="설정 접근 권한이 없습니다.")
        app_config = get_resolved_app()
        return {
            "toolsets": [
                {"key": key, "tools": [tool.__name__ for tool in tools]}
                for key, tools in TOOLSETS.items()
            ],
            "active": sorted(app_config.active),
        }

    @app.post(
        "/chat/stream",
        # 한도는 LLM 호출에만 건다 — 조회 라우트는 세지 않는다(auth.enforce_rate_limit).
        dependencies=[Depends(enforce_rate_limit)],
        responses=_SSE_RESPONSES,
        response_class=StreamingResponse,
        response_description="SSE 이벤트 스트림",
        description="사용자 메시지 1건에 대한 답변을 SSE로 스트리밍한다.\n" + SSE_EVENT_CONTRACT,
    )
    async def chat_stream(
        request: ChatRequest,
        http_request: Request,
        user: Annotated[AuthenticatedUser | None, Depends(get_authenticated_user)] = None,
    ) -> StreamingResponse:
        """사용자 메시지를 받아 SSE로 답변을 스트리밍한다.

        `x-api-key`(= Yes24 service_cookie) 헤더가 있으면 그 사용자의 userNo가 세션
        user_id가 되어 대화 기록이 사람 단위로 갈린다. 헤더가 없으면 익명(단일 POC
        사용자)으로 종전과 동일하게 동작한다.
        """
        # 세팅 잠금(데모 로그인) 세션은 모델·도구 구성을 조정할 수 없다 — 본문의 model·
        # enabled_toolsets를 무시하고 데모 전용 구성(demo_persona·demo_enabled_toolsets)으로
        # 고정 동작하며, done.model도 벗겨 보낸다.
        unlocked = settings_unlocked(http_request)
        # 화이트리스트 값만 통과 — 임의 모델 문자열은 여기서 걸러 기본(pro)으로 폴백한다.
        allowed = set(get_settings().selectable_models.values())
        model = request.model if unlocked and request.model in allowed else None
        # 도구 구성은 폴백하지 않는다: 무효 조합은 400으로 끊어 "요청과 다른 구성으로 답하는"
        # 조용한 부분 동작을 막는다. 검증은 resolve_app_for가 기동 경로와 같은 규칙으로 한다.
        app_config = app_for_request(http_request)
        if unlocked and request.enabled_toolsets is not None:
            try:
                app_config = resolve_app_for(
                    get_resolved_app().persona_key, frozenset(request.enabled_toolsets)
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        # RBTI: **코드는 서버가, 켜고 끄기는 프론트가** 소유한다(2026-09-02 사용자 결정).
        # 유형 자체는 사람에게 붙는 값이라 users.rbti에 저장돼 있고, 이번 턴에 그것을 쓸지는
        # 화면의 토글이라 요청이 정한다. use_rbti=false면 저장돼 있어도 적용하지 않는다 —
        # 저장된 유형 자체를 바꾸는 것과는 다른 층위다(이 토글은 이번 턴만).
        # request.rbti는 데모 UI 전용 덮어쓰기라 토글이 꺼져 있으면 그것도 무시한다(끄기가
        # 이긴다 — "껐는데 페르소나가 적용됐다"가 성립하면 안 된다). 그래서 데모 UI도 코드를
        # 실을 때 use_rbti=true를 함께 보낸다(index.html) — 계약을 불리언 하나로 유지하려고
        # 3상태(미지정/true/false)를 만들지 않고 호출부를 맞췄다.
        user_no = str(user.user_no) if user and user.user_no else None
        rbti = (request.rbti or await fetch_user_rbti(user_no)) if request.use_rbti else None
        stream = run_agent_stream(
            request.message,
            request.session_id,
            rbti=rbti,
            model=model,
            app=app_config,
            user_id=user_no,
            # persona status(턴 시작 신호)의 게이트 — 코드 유무와 별개로 "요청했는가"다
            # (요청했지만 코드가 없는 회원도 그 사실을 화면이 그린다, 피그마 10-C).
            use_rbti=request.use_rbti,
        )
        return StreamingResponse(
            stream if unlocked else _hide_model_frames(stream),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # 리버스 프록시(nginx)의 응답 버퍼링을 꺼 실시간 전달을 보장한다.
                "X-Accel-Buffering": "no",
            },
        )

    # 검색결과 AI 오버뷰 — 챗과 같은 SSE 스트리밍(delta/reset/done), 본체는 overview.py.
    # overview_model이 빈 문자열이면 라우트 미등록(404 — matrix_enabled 관례의 구조적 off 스위치).
    # [프론트 의존 계약] 프론트는 HEAD /overview의 상태가 **404가 아니면**(POST만 등록이라
    # Starlette가 405 반환) 기능 활성으로 판정한다 — 별도 HEAD 라우트를 만들지 않는다.
    if settings.overview_model:

        @app.post(
            "/overview",
            # 오버뷰는 LLM을 부른다 — 한도 대상. warm(검색만)·continue(캐시 시딩)는 아니다.
            dependencies=[Depends(enforce_rate_limit)],
            responses=_SSE_RESPONSES,
            response_class=StreamingResponse,
            response_description="SSE 이벤트 스트림",
            description=(
                "검색어 하나로 검색결과 상단 AI 오버뷰를 SSE로 스트리밍한다. 대화가 아니라 "
                "**검색결과 상단 요약**이라 세션이 생기지 않는다 — 사용자가 더 묻고 싶어 하면 "
                "`POST /overview/continue`가 이 내용을 이어받은 대화 세션을 만들어 준다.\n"
                + OVERVIEW_EVENT_CONTRACT
            ),
        )
        async def overview_endpoint(
            request: OverviewRequest,
            http_request: Request,
            user: Annotated[AuthenticatedUser | None, Depends(get_authenticated_user)] = None,
        ) -> StreamingResponse:
            """검색어 하나로 상단 AI 오버뷰를 SSE로 스트리밍한다(캐시·예산·degraded는 overview.py).

            인증은 챗과 동일 의존성 재사용 — 헤더 없으면 익명 허용, 키 비활성 401·한도
            429·인증 DB 503은 의존성이 처리한다. start_overview가 스트림 시작 전에 캐시
            조회·예산 선차단을 끝내므로 예산 429는 여기서 HTTP 상태로 나간다.
            """
            frames = await start_overview(
                request.query, request.section, get_settings(), _overview_arm(request.sources)
            )
            # 데모 역할(세팅 잠금)에는 done의 모델명 은닉 — 챗과 같은 프레임 필터 재사용.
            return StreamingResponse(
                frames if settings_unlocked(http_request) else _hide_model_frames(frames),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        # 접지원 비교 하네스(테스트 전용) — 스위치가 켜졌을 때만 등록한다. 프론트는 이
        # 라우트로 비교 토글 노출을 게이팅하고(HEAD 프로브와 같은 fail-closed 관례),
        # 팔 목록·순서를 여기서 받아 패널을 만든다(JS가 팔 이름을 다시 적지 않는다).
        if settings.overview_compare_enabled:

            @app.get("/overview/compare")
            async def overview_compare_arms() -> dict:
                """비교 뷰가 그릴 접지원 팔 목록(화면 왼→오 순서)."""
                return {"arms": list(OVERVIEW_ARMS)}

        @app.post("/overview/warm", status_code=202)
        async def overview_warm_endpoint(
            request: OverviewRequest,
            user: Annotated[AuthenticatedUser | None, Depends(get_authenticated_user)] = None,
        ) -> dict:
            """타이핑 중 검색 프리워밍(W1) — Yes24 검색만 백그라운드로 실행해 TextCache를
            데우고 즉시 202를 돌려준다. LLM 콜 0·예산 미소모·결과 본문 미반환(워밍 전용 —
            남용 상한은 검색 rps 리미터와 캐시 single-flight, 근거는 warm_search docstring).
            인증은 오버뷰와 동일 의존성이고 게이트(overview_model)도 동일이라 off면 함께
            404다.
            """
            warm_search(request.query, request.section, get_settings())
            return {"status": "warming"}

        @app.post(
            "/overview/continue",
            description=(
                "오버뷰 본문을 채팅 세션의 어시스턴트 턴으로 시딩한다(이어가기). "
                "응답은 JSON이며, 돌려받은 session_id로 `/chat/stream`을 이어 부른다."
            ),
        )
        async def overview_continue_endpoint(
            request: OverviewRequest,
            user: Annotated[AuthenticatedUser | None, Depends(get_authenticated_user)] = None,
        ) -> dict:
            """캐시된 오버뷰 교환(질의+본문+출처 레지스트리)을 챗 세션의 첫 턴으로 시딩하고
            session_id를 돌려준다 — 재생성·재조사 없음(LLM 콜 0, usage 기록 없음).

            캐시 미스·만료·degraded면 404 — 프론트는 현행 폴백(새 대화 + 질의 자동 전송)을
            탄다. 인증은 /overview와 동일 의존성이라 시딩 세션이 같은 user_id 밑에 생긴다
            (챗 후속 턴의 세션 조회 키와 일치).
            """
            user_no = str(user.user_no) if user and user.user_no else None
            session_id = await continue_overview(
                request.query, request.section, get_settings(), user_no
            )
            if session_id is None:
                raise HTTPException(
                    status_code=404, detail="이어갈 오버뷰가 캐시에 없습니다."
                )
            return {"session_id": session_id}

    # 히스토리·피드백 API(/chat/sessions*). 라우트·투영은 history.py가 소유하고 여기선 얹기만
    # 한다. 전 라우트가 get_authenticated_user를 직접 의존하므로 로그인월의 x-api-key 통과
    # 집합에 자동으로 파생된다(_key_checking_routes). include_router가 아니라 직접 등록인
    # 이유는 history.py 라우트 절 주석 참조(지연 프록시가 통과 집합 파생을 가린다).
    register_history(app)

    # 초기 질문 회전 풀(GET /chat/starters·/admin/starters/*). starter_model이 빈 값이면 미등록
    # (404 — overview_model 관례). 라우트·풀·생성의 소유자는 starters.py다.
    register_starters(app, settings)

    # 매트릭스 스트리밍 엔드포인트도 배포 게이팅(matrix_enabled) 대상 — off면 미등록(404).
    if settings.matrix_enabled:

        # **16유형 매트릭스는 프론트용 API가 아니라 어드민 도구다**(2026-09-01 사용자 결정).
        # 그래서 ① OpenAPI에서 빼고(프론트가 호출할 목록이 아니다) ② 어드민 역할을 요구한다.
        # 요구 근거는 비용이다 — 요청 1회가 16 페르소나 에이전트 루프를 동시에 돌려 실측
        # 6만 토큰이 나가고, 이 라우트에는 레이트리밋(인증 의존성 없음)도 일일 예산도 없으며
        # Yes24를 고처리량 모드(matrix_http_rps)로 16셀이 동시에 친다. 데모 비밀번호만으로
        # 열어 둘 표면이 아니다.
        @app.post("/chat/matrix", include_in_schema=False)
        async def chat_matrix(request: MatrixRequest, http_request: Request) -> StreamingResponse:
            """질문을 받아 16 RBTI 페르소나 답변을 열별 SSE로 스트리밍한다(retrieve-once)."""
            if not settings_unlocked(http_request):
                raise HTTPException(status_code=403, detail="설정 접근 권한이 없습니다.")
            # 화이트리스트 값만 통과 — /chat/stream과 동일(임의 문자열은 config 기본 모델 폴백).
            allowed = set(get_settings().selectable_models.values())
            model = request.model if request.model in allowed else None
            return StreamingResponse(
                run_matrix_stream(request.question, request.session_id, model=model),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

    # 문서 마감 — 어드민 전용 필드를 공개 스키마에서 걷어낸다(선언의 표식에서 파생).
    _hide_admin_only_fields(app)
    return app


app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    if settings.dev_reload:
        # 자동 리로드(개발 편의, 2026-07-29 사용자 요청): 소스 변경 시 uvicorn이 스스로
        # 재기동한다. reload 모드는 앱 객체가 아니라 임포트 문자열이 필요하다(워커 재생성).
        uvicorn.run(
            "yes24_agent.main:app", host=settings.host, port=settings.port, reload=True
        )
    else:
        uvicorn.run(app, host=settings.host, port=settings.port)
