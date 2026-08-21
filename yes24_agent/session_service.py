"""세션 서비스 획득·세션별 직렬화 락·세션 조회/생성.

`runner.py`에서 SSE 스트리밍의 순수 세션 관심사만 추출한 모듈이다(동작 불변).
DatabaseSessionService(sqlite·MySQL) 싱글턴을 lazy 생성하되 디렉토리·드라이버 오류 시
InMemorySessionService로 폴백해 서버 기동을 항상 보장하고(폴백은 config로 끌 수 있다),
같은 session_id로 들어온 동시 요청을 세션별 asyncio.Lock으로 순차화한다.
"""

import asyncio
import logging
from pathlib import Path
from urllib.parse import unquote, urlparse
from weakref import WeakValueDictionary

from google.adk.sessions import (
    BaseSessionService,
    DatabaseSessionService,
    InMemorySessionService,
)
from google.adk.sessions.session import Session

from yes24_agent.config import get_settings

logger = logging.getLogger(__name__)

# 인증 없는 요청(x-api-key 헤더 없음·로컬 개발·매트릭스)의 단일 사용자 id. 인증된 요청은
# Yes24 userNo가 user_id가 된다(auth.py) — ADK 세션 키가 (app_name, user_id, session_id)
# 복합이라 사용자별로 대화가 갈린다.
_POC_USER_ID = "poc-user"

# 파일 기반 DB dialect. "파일이 있어야 성립하는 기능"(sqlite 디렉토리 보장, admin의 파일
# 직접 열람)의 판정을 여기 한 곳에서 낸다 — 각자 URL 문자열을 훑으면 판정이 갈라진다.
SQLITE_DIALECT = "sqlite"

# 네트워크 DB dialect. "MySQL이라야 성립하는 기능"(인증 스택 auth.py, 사용량 기록
# usage.py)의 활성 판정이 이 값 하나를 공유한다 — 키워드 분기가 아니라 접속 가능성에서
# 나오는 구조 분기다.
MYSQL_DIALECT = "mysql"

# 세션 서비스 싱글턴(lazy). 프로세스 전체가 하나의 DB 연결 풀을 공유한다.
_session_service: BaseSessionService | None = None

# 세션별 직렬화 락. 같은 session_id로 동시 요청(전송 버튼 더블클릭 등)이 들어오면
# 두 run_async가 같은 세션에 동시에 이벤트를 append해 DatabaseSessionService의
# stale-writer 검출(ValueError)로 스트림이 중간에 죽는다. 세션별 락으로 순차 처리한다.
# 실행 중이거나 대기 중인 코루틴이 lock을 강하게 참조하며, 사용이 끝난 lock은
# 레지스트리에서 자동 제거된다.
_session_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _get_session_lock(session_id: str) -> asyncio.Lock:
    """session_id별 asyncio.Lock을 반환한다(최초 접근 시 생성).

    asyncio 단일 스레드라 setdefault 구간에 await가 없어 경합이 없다.
    """
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    return lock


def db_dialect(db_url: str) -> str:
    """DB URL의 dialect 이름 — `mysql+aiomysql://…` → `mysql`, `sqlite+aiosqlite:///…` → `sqlite`.

    URL 어딘가에 이름이 들어 있는지(부분 문자열) 대신 스킴만 본다 — 파일 경로에 우연히
    섞인 이름으로 판정이 뒤집히지 않게 한다.
    """
    scheme, _, _ = db_url.partition("://")
    return scheme.partition("+")[0].lower()


def mysql_pool_kwargs(db_url: str, *, maxsize: int) -> dict | None:
    """세션 DB URL에서 aiomysql 접속 kwargs를 뽑는다 — mysql이 아니면 None(기능 비활성).

    인증(auth.py)·사용량 기록(usage.py)이 세션 DB와 **같은 계정·database**를 쓰므로
    접속 정보를 따로 두지 않고 여기 한 곳에서 파싱한다(설정 단일 출처). 포트가 URL에
    없으면 키를 아예 넣지 않아 드라이버 기본값을 쓴다(우리가 포트 상수를 새로 만들지
    않는다). 사용자·비밀번호는 URL 인코딩돼 있을 수 있어 디코드한다. maxsize는 용도별
    config 필드를 호출부가 넘긴다 — 풀 크기는 기능마다 다른 결정이다.
    """
    if db_dialect(db_url) != MYSQL_DIALECT:
        return None
    parsed = urlparse(db_url)
    kwargs: dict = {
        "host": parsed.hostname,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "db": parsed.path.lstrip("/"),
        "charset": "utf8mb4",
        "autocommit": True,
        "minsize": 1,
        "maxsize": maxsize,
        # 접속 수립 상한. aiomysql 기본은 None = OS TCP 타임아웃(~75s+)이라, RDS가
        # TCP 블랙홀(SG 오설정 등)이면 실패 **판정 자체**가 분 단위로 늘어진다 —
        # auth의 503도 usage의 무시 판정·종료 배수도 전부 이 값에 물리므로, 장애를
        # 초 단위로 끊어 정직하게 노출한다(포트와 달리 드라이버 기본값이 쓸 수 없는 값).
        "connect_timeout": get_settings().mysql_connect_timeout_s,
    }
    if parsed.port:
        kwargs["port"] = parsed.port
    return kwargs


def _sqlite_dir(db_url: str) -> Path | None:
    """sqlite 파일 URL에서 DB 파일이 놓일 디렉토리를 추출한다.

    `sqlite+aiosqlite:///./data/sessions.db` → `./data`. 인메모리(`:memory:`)나
    sqlite가 아닌 URL(MySQL 등)이면 None을 반환한다(디렉토리 생성 불필요).
    """
    if db_dialect(db_url) != SQLITE_DIALECT or ":memory:" in db_url:
        return None
    # 스킴 구분자 `:///` 뒤가 파일 경로.
    _, _, path_part = db_url.partition(":///")
    if not path_part:
        return None
    return Path(path_part).parent


def _get_session_service() -> BaseSessionService:
    """세션 서비스 싱글턴을 반환한다(최초 호출 시 생성).

    DatabaseSessionService 생성을 시도하되, sqlite면 파일 디렉토리를 먼저 보장한다.
    드라이버·URL 오류로 생성이 실패하면 InMemorySessionService로 폴백해 서버 기동
    자체는 항상 가능하게 한다(멀티턴 영속만 포기). `session_fallback_allowed=False`면
    폴백하지 않고 예외를 그대로 올려 기동을 실패시킨다.
    """
    global _session_service
    if _session_service is not None:
        return _session_service

    settings = get_settings()
    db_url = settings.session_db_url

    data_dir = _sqlite_dir(db_url)
    if data_dir is not None:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # ./data가 파일로 존재(FileExistsError)하거나 읽기 전용 컨테이너
            # (PermissionError)면 DB 파일을 만들 수 없다. 인메모리로 폴백해
            # 서버 기동·응답은 유지한다(멀티턴 영속만 포기).
            if not settings.session_fallback_allowed:
                raise
            logger.warning(
                f"세션 DB 디렉토리({data_dir}) 생성 실패({exc}). "
                "InMemorySessionService로 폴백합니다."
            )
            _session_service = InMemorySessionService()
            return _session_service

    try:
        _session_service = DatabaseSessionService(db_url=db_url)
    except (ValueError, ImportError) as exc:
        # DatabaseSessionService는 드라이버 미설치·URL 오류를 ValueError/ImportError로
        # 감싸 던진다. 영속을 포기하고 인메모리로 폴백한다.
        if not settings.session_fallback_allowed:
            raise
        logger.warning(
            f"DatabaseSessionService 생성 실패({exc}). InMemorySessionService로 폴백합니다. "
            "멀티턴 히스토리가 프로세스 재시작 시 사라집니다."
        )
        _session_service = InMemorySessionService()
    return _session_service


def persistence_mode() -> str:
    """현재 세션 서비스의 영속 모드 — `"sqlite"` | `"mysql"` | `"in-memory"`.

    /health가 노출한다. 폴백이 발동하면 URL은 그대로여도 서비스는 비영속이므로,
    설정값이 아니라 **실제로 만들어진 서비스**를 보고 판정한다.
    """
    service = _get_session_service()
    if isinstance(service, InMemorySessionService):
        return "in-memory"
    return db_dialect(get_settings().session_db_url)


async def _resolve_session(
    service: BaseSessionService, session_id: str | None, user_id: str | None = None
) -> Session:
    """기존 세션을 조회하거나, 없으면 새로 만든다.

    session_id가 주어졌지만 조회에 실패(만료·오타·재시작 후 인메모리 유실)하면
    클라이언트가 준 id를 그대로 재사용해 신규 세션을 만든다. user_id가 None이면
    인증 없는 단일 사용자(_POC_USER_ID)로 취급한다 — 조회·생성이 같은 키를 써야
    남의 session_id로 요청해도 자기 user_id 밑에서만 세션이 성립한다.
    """
    if session_id:
        existing = await service.get_session(
            app_name=get_settings().app_name,
            user_id=user_id or _POC_USER_ID,
            session_id=session_id,
        )
        if existing is not None:
            return existing

    return await service.create_session(
        app_name=get_settings().app_name,
        user_id=user_id or _POC_USER_ID,
        session_id=session_id,
    )
