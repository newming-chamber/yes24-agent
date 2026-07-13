"""FastAPI 서버 — `/chat/stream` SSE 엔드포인트.

라이브 소스(Yes24)를 검색해 인용 달린 답변을 스트리밍하는 대화 API. 실제 에이전트
루프와 SSE 변환은 `runner.run_agent_stream`이 담당하고, 이 모듈은 HTTP 계층
(라우팅·CORS·수명주기 훅)만 얇게 얹는다.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel

from yes24_agent.auth import (
    ACCESS_COOKIE,
    expected_token,
    password_matches,
    token_valid,
)
from yes24_agent.config import ensure_google_api_key_env, get_settings
from yes24_agent.matrix.matrix_runner import run_matrix_stream
from yes24_agent.runner import run_agent_stream
from yes24_agent.tools.web_search import aclose_shared_client as aclose_web_search_client
from yes24_agent.tools.yes24_search import aclose_shared_client as aclose_yes24_client

logger = logging.getLogger(__name__)

# 테스트용 웹 채팅 UI(단일 self-contained HTML). 인증 없음 — 개발/데모 용도.
_INDEX_HTML = Path(__file__).parent / "static" / "index.html"
# 16뷰 RBTI 매트릭스 시뮬레이터 UI(C4/matrix-ux 소유). 인증 없음 — 개발/데모 용도.
_MATRIX_HTML = Path(__file__).parent / "static" / "matrix.html"
# 공유 패스워드 로그인월 페이지(access_password 설정 시 노출).
_LOGIN_HTML = Path(__file__).parent / "static" / "login.html"

# 로그인월이 켜져도 통과시키는 예외 경로(헬스체크·로그인 페이지 자체).
_ACCESS_EXEMPT_PATHS = frozenset({"/health", "/login"})
# 로그인 쿠키 유효기간(초). 데모 접근 게이트라 재로그인 성가심을 줄이되 무한은 아니게 7일.
_ACCESS_COOKIE_MAX_AGE = 7 * 24 * 60 * 60


class ChatRequest(BaseModel):
    """`/chat/stream` 요청 본문."""

    message: str
    session_id: str | None = None
    # RBTI 독서 페르소나 코드(4글자, 예: "CADI"). 없거나 무효면 페르소나 미적용(기존 동작).
    rbti: str | None = None


class MatrixRequest(BaseModel):
    """`/chat/matrix` 요청 본문(16뷰 매트릭스 시뮬레이터)."""

    question: str
    session_id: str | None = None


def _configure_logging() -> None:
    """앱 로거(`yes24_agent.*`)의 INFO 로그가 콘솔에 나오게 설정한다.

    uvicorn 기본 설정은 root 로거에 핸들러를 달지 않아 도구 호출 기록·무효 인용
    경고 같은 앱 INFO 로그가 묻힌다. basicConfig로 콘솔 핸들러를 보장하고
    (핸들러가 이미 있으면 no-op) 앱 로거 레벨을 INFO로 명시한다. httpx 등
    서드파티 요청 소음은 WARNING으로 억제한다.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("yes24_agent").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 수명주기 훅: 시작 시 로깅·API 키 매핑, 종료 시 공유 HTTP 클라이언트 정리."""
    _configure_logging()
    # ADK는 GOOGLE_API_KEY를 기대한다 — GEMINI_API_KEY를 매핑해 둔다.
    if not ensure_google_api_key_env():
        logger.warning("GEMINI/GOOGLE API 키가 설정되지 않았습니다. LLM 호출이 실패할 수 있어요.")
    yield
    # 공유 HTTP 클라이언트(Yes24·웹서치)를 정리해 열린 커넥션을 닫는다.
    await aclose_yes24_client()
    await aclose_web_search_client()


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

        @app.middleware("http")
        async def access_gate(request: Request, call_next):
            path = request.url.path
            if path in _ACCESS_EXEMPT_PATHS or token_valid(
                request.cookies.get(ACCESS_COOKIE), settings.access_password
            ):
                return await call_next(request)
            accept = request.headers.get("accept", "")
            if request.method == "GET" and "text/html" in accept:
                return RedirectResponse("/login", status_code=302)
            return JSONResponse({"detail": "인증이 필요합니다."}, status_code=401)

        @app.get("/login")
        async def login_page() -> FileResponse:
            """로그인월 페이지(공유 패스워드 입력)."""
            return FileResponse(_LOGIN_HTML, media_type="text/html")

        @app.post("/login")
        async def login_submit(request: Request):
            """패스워드를 검증해 성공 시 접근 쿠키를 발급하고 홈으로 보낸다."""
            form = await request.form()
            candidate = str(form.get("password", ""))
            if password_matches(candidate, settings.access_password):
                resp = RedirectResponse("/", status_code=303)
                resp.set_cookie(
                    ACCESS_COOKIE,
                    expected_token(settings.access_password),
                    max_age=_ACCESS_COOKIE_MAX_AGE,
                    httponly=True,
                    samesite="lax",
                )
                return resp
            # 실패: 로그인 페이지로 되돌리며 에러 표시(?error=1).
            return RedirectResponse("/login?error=1", status_code=303)

    @app.get("/")
    async def index() -> FileResponse:
        """테스트용 웹 채팅 UI를 반환한다(로그인월 활성 시 쿠키 필요)."""
        return FileResponse(_INDEX_HTML, media_type="text/html")

    # RBTI 16뷰 매트릭스는 배포 게이팅(matrix_enabled). off면 /matrix·/chat/matrix 라우트를
    # 아예 등록하지 않아 404가 된다(프로드 숨김) — 채팅 경로(/ ·/chat/stream ·/health)는 무영향.
    if settings.matrix_enabled:

        # GET+HEAD 둘 다 등록한다 — 프론트 네비 링크가 HEAD로 활성 여부를 게이팅하는데,
        # FastAPI GET 라우트는 HEAD를 자동 허용하지 않아(405; 프록시 뒤에선 503) 링크가 안 뜬다.
        @app.api_route("/matrix", methods=["GET", "HEAD"])
        async def matrix_ui() -> FileResponse:
            """16뷰 RBTI 매트릭스 시뮬레이터 UI를 반환한다(인증 없음)."""
            return FileResponse(_MATRIX_HTML, media_type="text/html")

    @app.get("/health")
    async def health() -> dict:
        """헬스체크."""
        return {"status": "ok"}

    @app.post("/chat/stream")
    async def chat_stream(request: ChatRequest) -> StreamingResponse:
        """사용자 메시지를 받아 SSE로 답변을 스트리밍한다."""
        return StreamingResponse(
            run_agent_stream(request.message, request.session_id, rbti=request.rbti),
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
            return StreamingResponse(
                run_matrix_stream(request.question, request.session_id),
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
    uvicorn.run(app, host=settings.host, port=settings.port)
