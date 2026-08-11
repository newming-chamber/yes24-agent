"""FastAPI 서버 — `/chat/stream` SSE 엔드포인트.

라이브 소스(Yes24)를 검색해 인용 달린 답변을 스트리밍하는 대화 API. 실제 에이전트
루프와 SSE 변환은 `runner.run_agent_stream`이 담당하고, 이 모듈은 HTTP 계층
(라우팅·CORS·수명주기 훅)만 얇게 얹는다.
"""

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from hashlib import sha256
from logging.handlers import RotatingFileHandler
from pathlib import Path
from secrets import compare_digest
from typing import Annotated

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StringConstraints

from yes24_agent.admin import client_ip, register_admin
from yes24_agent.config import ensure_google_api_key_env, get_settings
from yes24_agent.matrix.matrix_runner import run_matrix_stream
from yes24_agent.runner import run_agent_stream
from yes24_agent.thought_translation import warmup_translation
from yes24_agent.toolsets import TOOLSETS, get_resolved_app, resolve_app_for

logger = logging.getLogger(__name__)

# 웹 채팅 UI(단일 self-contained HTML).
_INDEX_HTML = Path(__file__).parent / "static" / "index.html"
# 16뷰 RBTI 매트릭스 시뮬레이터 UI(C4/matrix-ux 소유). 인증 없음 — 개발/데모 용도.
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
_ACCESS_EXEMPT_PATHS = frozenset({"/health", "/login", "/logout"})

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
    """비밀번호로부터 결정론적 접근 토큰(HMAC-SHA256 hex)을 만든다.

    같은 비밀번호는 항상 같은 토큰을 낸다 → 세션 저장 없이 쿠키만으로 검증한다.
    """
    return hmac.new(password.encode("utf-8"), _TOKEN_MESSAGE, sha256).hexdigest()


def token_valid(cookie_value: str | None, password: str) -> bool:
    """쿠키 토큰이 현재 비밀번호에서 파생된 값과 일치하는지 상수시간 비교로 판정한다."""
    if not cookie_value:
        return False
    return compare_digest(cookie_value, expected_token(password))


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


class ChatRequest(BaseModel):
    """`/chat/stream` 요청 본문."""

    message: NonBlankText
    session_id: str | None = None
    # RBTI 독서 페르소나 코드(4글자, 예: "CADI"). 없거나 무효면 페르소나 미적용(기존 동작).
    rbti: str | None = None
    # 사용자가 UI에서 고른 Gemini 모델ID. selectable_models 화이트리스트 값만 허용하고
    # 그 밖(없음·임의 문자열)은 config 기본 모델로 폴백한다(임의 모델 주입 차단).
    model: str | None = None
    # 사용자가 UI에서 켠 toolset 키 목록. 미지정이면 config 기본 구성이다. 모델과 달리
    # 무효값을 조용히 폴백하지 않고 400으로 끊는다 — 도구 구성은 답변의 근거 범위를 바꾸므로
    # "요청과 다른 구성으로 답했다"가 조용히 성립하면 안 된다(resolve_app fail-loud 계승).
    enabled_toolsets: list[str] | None = None


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
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
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
    logging.getLogger("yes24_agent").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 수명주기 훅: 시작 시 로깅·API 키 매핑, 종료 시 공유 HTTP 클라이언트 정리."""
    _configure_logging()
    # ADK는 GOOGLE_API_KEY를 기대한다 — GEMINI_API_KEY를 매핑해 둔다.
    if not ensure_google_api_key_env():
        logger.warning("GEMINI/GOOGLE API 키가 설정되지 않았습니다. LLM 호출이 실패할 수 있어요.")
    # 사고 라벨 번역 경로를 백그라운드로 데운다(첫 채팅의 첫 한국어 라벨 ~0.3초 단축).
    # 기동을 막지 않도록 task로만 띄우고, 참조를 잡아 GC 취소를 막는다.
    app.state.translation_warmup = asyncio.create_task(warmup_translation())
    yield
    # 공유 HTTP 클라이언트를 정리해 열린 커넥션을 닫는다 — 훅 목록은 toolset 레지스트리
    # 파생이라 새 toolset이 생겨도 여기는 무수정이다(미생성 클라이언트는 no-op).
    for hook in get_resolved_app().aclose_hooks:
        await hook()


def create_app() -> FastAPI:
    """FastAPI 앱을 조립한다."""
    settings = get_settings()
    app = FastAPI(title="yes24-agent", lifespan=lifespan)

    # CORS: 자격증명 동반 요청과 `*`의 조합은 브라우저가 거부하므로 명시 목록만 허용.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

        @app.middleware("http")
        async def access_gate(request: Request, call_next):
            path = request.url.path
            cookie = request.cookies.get(ACCESS_COOKIE)
            if path in _ACCESS_EXEMPT_PATHS or any(
                token_valid(cookie, pw) for pw in wall_passwords
            ):
                return await call_next(request)
            accept = request.headers.get("accept", "")
            if request.method == "GET" and "text/html" in accept:
                return RedirectResponse("/login", status_code=302)
            return JSONResponse({"detail": "인증이 필요합니다."}, status_code=401)

        @app.get("/login")
        async def login_page() -> HTMLResponse:
            """로그인월 페이지(공유 패스워드 입력) — 브랜딩 마커 치환 서빙."""
            return _branded_html(_LOGIN_HTML)

        @app.post("/login")
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

        @app.get("/logout")
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

    @app.get("/")
    async def index(request: Request) -> HTMLResponse:
        """웹 채팅 UI를 반환한다(로그인월 활성 시 쿠키 필요) — 브랜딩 마커 치환 서빙.

        데모 세션은 데모 전용 페르소나 문안(제목·인사·예시 칩)으로 서빙된다.
        """
        return _branded_html(_INDEX_HTML, app_config=app_for_request(request))

    @app.get("/me")
    async def me(request: Request) -> dict:
        """현재 로그인 역할: {"role": "admin"|"demo"|null}. null = 로그인월 비활성.

        프론트(내장·외부 공통)가 역할 배지·로그아웃 링크·세팅 UI 노출을 판단하는 단일
        계약이다. 세팅 강제 자체는 서버가 한다(/models·/toolsets 403, done.model 제거) —
        이 응답은 표시용이지 보안 경계가 아니다.
        """
        return {"role": access_role(request)}

    # RBTI 16뷰 매트릭스는 배포 게이팅(matrix_enabled). off면 /matrix·/chat/matrix 라우트를
    # 아예 등록하지 않아 404가 된다(프로드 숨김) — 채팅 경로(/ ·/chat/stream ·/health)는 무영향.
    if settings.matrix_enabled:

        # GET+HEAD 둘 다 등록한다 — 프론트 네비 링크가 HEAD로 활성 여부를 게이팅하는데,
        # FastAPI GET 라우트는 HEAD를 자동 허용하지 않아(405; 프록시 뒤에선 503) 링크가 안 뜬다.
        @app.api_route("/matrix", methods=["GET", "HEAD"])
        async def matrix_ui() -> FileResponse:
            """16뷰 RBTI 매트릭스 시뮬레이터 UI를 반환한다(인증 없음)."""
            return FileResponse(_MATRIX_HTML, media_type="text/html")

    # 운영자 데이터 조회(admin). admin_password가 비어 있으면 라우트를 등록하지 않는다(404).
    register_admin(app, settings)

    @app.get("/health")
    async def health() -> dict:
        """헬스체크."""
        return {"status": "ok"}

    @app.get("/models")
    async def models(request: Request) -> dict:
        """UI 모델 선택기용 목록(라벨→모델ID)과 기본 모델. 화이트리스트가 단일 진실.

        데모 로그인(세팅 잠금)에는 403 — 프론트는 목록 조회 실패 시 선택기를 숨기고 서버
        기본 모델로 동작한다(기존 폴백 경로라 프론트 무수정).
        """
        if not settings_unlocked(request):
            raise HTTPException(status_code=403, detail="설정 접근 권한이 없습니다.")
        settings = get_settings()
        return {"models": settings.selectable_models, "default": settings.model_name}

    @app.get("/toolsets")
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

    @app.post("/chat/stream")
    async def chat_stream(request: ChatRequest, http_request: Request) -> StreamingResponse:
        """사용자 메시지를 받아 SSE로 답변을 스트리밍한다."""
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
        stream = run_agent_stream(
            request.message,
            request.session_id,
            rbti=request.rbti,
            model=model,
            app=app_config,
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

    # 매트릭스 스트리밍 엔드포인트도 배포 게이팅(matrix_enabled) 대상 — off면 미등록(404).
    if settings.matrix_enabled:

        @app.post("/chat/matrix")
        async def chat_matrix(request: MatrixRequest) -> StreamingResponse:
            """질문을 받아 16 RBTI 페르소나 답변을 열별 SSE로 스트리밍한다(retrieve-once)."""
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
