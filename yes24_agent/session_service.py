"""세션 서비스 획득·세션별 직렬화 락·세션 조회/생성.

`runner.py`에서 SSE 스트리밍의 순수 세션 관심사만 추출한 모듈이다(동작 불변).
DatabaseSessionService(sqlite) 싱글턴을 lazy 생성하되 디렉토리·드라이버 오류 시
InMemorySessionService로 폴백해 서버 기동을 항상 보장하고, 같은 session_id로 들어온
동시 요청을 세션별 asyncio.Lock으로 순차화한다.
"""

import asyncio
import logging
from pathlib import Path

from google.adk.sessions import (
    BaseSessionService,
    DatabaseSessionService,
    InMemorySessionService,
)
from google.adk.sessions.session import Session

from yes24_agent.config import get_settings

logger = logging.getLogger(__name__)

# POC 단계에서는 단일 사용자로 고정한다(인증 없음). 멀티유저는 이후 마일스톤.
_POC_USER_ID = "poc-user"

# 세션 서비스 싱글턴(lazy). 프로세스 전체가 하나의 DB 연결 풀을 공유한다.
_session_service: BaseSessionService | None = None

# 세션별 직렬화 락. 같은 session_id로 동시 요청(전송 버튼 더블클릭 등)이 들어오면
# 두 run_async가 같은 세션에 동시에 이벤트를 append해 DatabaseSessionService의
# stale-writer 검출(ValueError)로 스트림이 중간에 죽는다. 세션별 락으로 순차 처리한다.
# POC 스코프에서 락 dict는 무한정 커질 수 있으나(세션당 1개) 실사용 규모에서 무시 가능.
_session_locks: dict[str, asyncio.Lock] = {}


def _get_session_lock(session_id: str) -> asyncio.Lock:
    """session_id별 asyncio.Lock을 반환한다(최초 접근 시 생성).

    asyncio 단일 스레드라 setdefault 구간에 await가 없어 경합이 없다.
    """
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    return lock


def _sqlite_dir(db_url: str) -> Path | None:
    """sqlite 파일 URL에서 DB 파일이 놓일 디렉토리를 추출한다.

    `sqlite+aiosqlite:///./data/sessions.db` → `./data`. 인메모리(`:memory:`)나
    sqlite가 아닌 URL이면 None을 반환한다(디렉토리 생성 불필요).
    """
    if "sqlite" not in db_url or ":memory:" in db_url:
        return None
    # 스킴 구분자 `:///` 뒤가 파일 경로.
    _, _, path_part = db_url.partition(":///")
    if not path_part:
        return None
    return Path(path_part).parent


def _get_session_service() -> BaseSessionService:
    """세션 서비스 싱글턴을 반환한다(최초 호출 시 생성).

    DatabaseSessionService(sqlite) 생성을 시도하되, sqlite 파일 디렉토리를 먼저
    보장한다. 드라이버·URL 오류로 생성이 실패하면 InMemorySessionService로 폴백해
    서버 기동 자체는 항상 가능하게 한다(멀티턴 영속만 포기).
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
            logger.warning(
                "세션 DB 디렉토리(%s) 생성 실패(%s). InMemorySessionService로 폴백합니다.",
                data_dir,
                exc,
            )
            _session_service = InMemorySessionService()
            return _session_service

    try:
        _session_service = DatabaseSessionService(db_url=db_url)
    except (ValueError, ImportError) as exc:
        # DatabaseSessionService는 드라이버 미설치·URL 오류를 ValueError/ImportError로
        # 감싸 던진다. 영속을 포기하고 인메모리로 폴백한다.
        logger.warning(
            "DatabaseSessionService 생성 실패(%s). InMemorySessionService로 폴백합니다. "
            "멀티턴 히스토리가 프로세스 재시작 시 사라집니다.",
            exc,
        )
        _session_service = InMemorySessionService()
    return _session_service


async def _resolve_session(service: BaseSessionService, session_id: str | None) -> Session:
    """기존 세션을 조회하거나, 없으면 새로 만든다.

    session_id가 주어졌지만 조회에 실패(만료·오타·재시작 후 인메모리 유실)하면
    클라이언트가 준 id를 그대로 재사용해 신규 세션을 만든다.
    """
    if session_id:
        existing = await service.get_session(
            app_name=get_settings().app_name,
            user_id=_POC_USER_ID,
            session_id=session_id,
        )
        if existing is not None:
            return existing

    return await service.create_session(
        app_name=get_settings().app_name,
        user_id=_POC_USER_ID,
        session_id=session_id,
    )
