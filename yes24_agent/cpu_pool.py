"""CPU 바운드 순수 계산(Yes24 HTML 파싱)을 프로세스 풀로 내린다.

근거(2026-10-17 상용 장애): 유입이 평소의 ~24배가 되자 t4g.medium(2 vCPU)의 단일 프로세스가
CPU 110~120%로 포화됐고, 이벤트 루프가 밀려 세션 DB 풀 타임아웃으로 채팅이 대량 실패했다.
py-spy상 CPU의 55.6%가 BeautifulSoup 생성자였다. 파싱은 이미 `asyncio.to_thread`로 루프
밖에 있었지만 GIL에 묶여 두 번째 코어를 못 썼다 — 프로세스로 내려야 코어가 늘어난다.

계약:
- fn과 인자·반환은 pickle 가능해야 한다(모듈 최상위 함수 + 원시값). 설정 객체는 통째로
  넘기지 않는다 — 시크릿까지 실려 가고 매 호출 직렬화 비용이 붙는다. settings_view로 그 함수가
  읽는 필드만 뽑아 넘긴다.
- fn은 상태를 만지지 않는 순수 계산이어야 한다. 워커는 별도 프로세스라 부모의 monkeypatch·
  전역 상태·로깅 설정이 닿지 않는다(워커 안의 logger 호출은 부모 로그 파일에 남지 않으므로
  실패 기록은 호출부가 결과를 보고 남긴다).
- 예외(ParseError 등)는 워커에서 pickle돼 호출부에서 그대로 다시 던져진다.
"""

import asyncio
import functools
import logging
import multiprocessing
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from types import SimpleNamespace
from typing import Any, TypeVar

from yes24_agent.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# spawn: 이 프로세스엔 이벤트 루프·스레드(HTTP 풀·DB 풀·ADK)가 돌고 있다 — fork는 그 잠금
# 상태까지 복제해 자식이 교착할 수 있다(Python 3.12도 이 경우 DeprecationWarning). 워커는
# 오래 살고(재사용) 수가 적어 spawn의 기동 비용(파서 모듈 import, 로컬 ~0.5초)은 워커당 한 번이다.
# 조정 대상이 아니라 정확성 조건이라 설정 필드로 두지 않는다(fork로 바꾸는 길을 열지 않는다).
_MP_CONTEXT = "spawn"

_pool: ProcessPoolExecutor | None = None
# 풀이 한 번 깨지면 True — 이 프로세스가 사는 동안 스레드 모드로 고정한다(run_cpu 독스트링).
_broken = False


def settings_view(settings: Any, *names: str) -> SimpleNamespace:
    """설정에서 순수 계산이 읽는 필드만 원시값으로 뽑는다(프로세스 경계를 넘는 pickle 단위).

    빠뜨린 필드는 워커에서 AttributeError로 드러난다(조용한 기본값 대체 없음).
    """
    return SimpleNamespace(**{name: getattr(settings, name) for name in names})


def _get_pool(workers: int) -> ProcessPoolExecutor:
    """풀을 처음 쓸 때 만든다(lazy). 루프 스레드에서만 불리므로 확인과 대입 사이에 경합이 없다."""
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context(_MP_CONTEXT)
        )
        logger.info(f"cpu_pool 생성 workers={workers} context={_MP_CONTEXT}")
    return _pool


async def run_cpu(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """fn(*args, **kwargs)를 프로세스 풀에서 실행하고 결과를 돌려준다.

    워커가 죽어 풀이 깨지면(BrokenProcessPool — OOM kill·세그폴트·워커 기동 실패) 이 호출은
    워커 스레드(to_thread)에서 다시 돌리고, **이 프로세스가 사는 동안 스레드 모드로 고정한다**
    (풀 재생성 없음, error 로그 1회). 근거: 깨지는 원인 대부분이 입력이 아니라 환경(메모리 압박·
    기동 실패)이라 다시 풀을 세우면 같은 실패가 반복된다 — 기동 실패 환경에서 호출마다 폐기→
    재spawn→폴백이 돌아 호출당 3,223ms(스레드 40ms)가 재현됐다(2026-10-17 감사). 시간 기반
    재시도는 두지 않는다 — 복구 경로는 배포·재시작이다. 스레드 모드는 종전 동작(같은 함수·같은
    결과)이라 느려질 뿐 틀리지 않으며, 빈 결과로 위장하지 않는다.
    workers=0이면 풀 없이 처음부터 스레드에서 돈다.
    """
    call = functools.partial(fn, *args, **kwargs)
    workers = get_settings().cpu_pool_workers
    if workers <= 0 or _broken:
        return await asyncio.to_thread(call)
    pool = _get_pool(workers)
    try:
        return await asyncio.get_running_loop().run_in_executor(pool, call)
    except BrokenProcessPool as exc:
        _pin_thread_mode(pool, fn, exc)
        return await asyncio.to_thread(call)


def _pin_thread_mode(pool: ProcessPoolExecutor, fn: Callable, exc: BaseException) -> None:
    """깨진 풀을 닫고 스레드 모드로 고정한다 — 동시에 깨진 호출이 여럿이어도 로그·정리는 한 번."""
    global _broken, _pool
    if _broken:
        return
    _broken, _pool = True, None
    logger.error(
        f"cpu_pool 깨짐 — 이 프로세스는 재시작 전까지 스레드 모드로 고정 fn={fn.__name__}: {exc!r}"
    )
    pool.shutdown(wait=False, cancel_futures=True)


def shutdown_cpu_pool() -> None:
    """앱 종료 시 풀을 닫는다(만들어진 적 없으면 no-op). 진행 중인 작업은 끝까지 기다린다."""
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)
