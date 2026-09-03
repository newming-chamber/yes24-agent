"""요청 문맥을 로그 줄에 자동으로 싣는 장치 — contextvars 기반.

**문제**: 도구·러너·후처리가 각자 로그를 남기는데 어느 요청/대화의 것인지 표시가 없었다.
사용자가 하나일 땐 시간순으로 읽히지만 **동시에 둘이 대화하면 줄이 뒤섞여** 가릴 수 없다
(2026-09-03 실서비스 가정 점검에서 확인). 실서비스에서는 "이 사용자가 느리다/이상하다"는
신고를 로그만으로 추적할 수 있어야 한다.

**해법**: 파라미터를 모든 함수에 꿰지 않고 `contextvars`에 담는다. asyncio는 태스크 생성 시
문맥을 복사하므로, 요청 진입에서 한 번 bind하면 그 요청이 만드는 모든 로그(도구·서브콜 포함)에
자동으로 붙는다. 로깅 필터가 그 값을 레코드에 얹고 포맷터가 찍는다 — **호출부는 아무것도
하지 않는다**(로그 줄마다 id를 손으로 넣는 방식은 빠뜨리는 곳이 반드시 생긴다).

**안 담는 것**: 사용자가 친 질문·답변 본문. 식별자와 수치만 담는다(로그는 오래 남고 열람
범위가 넓다 — 대화 원문은 이미 DB에 있고 그쪽이 삭제 API의 대상이다).
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from contextlib import contextmanager

_CTX: contextvars.ContextVar[dict[str, object]] = contextvars.ContextVar("log_ctx", default={})


def new_request_id() -> str:
    """요청 1건을 가리키는 짧은 id — 로그에서 한 요청의 줄들을 묶는 열쇠."""
    return uuid.uuid4().hex[:8]


@contextmanager
def bound(**fields: object):
    """이 블록 안에서 남는 모든 로그에 필드를 붙인다(None 값은 무시)."""
    clean = {k: v for k, v in fields.items() if v is not None}
    token = _CTX.set({**_CTX.get(), **clean})
    try:
        yield
    finally:
        _CTX.reset(token)


def update(**fields: object) -> None:
    """현재 문맥에 필드를 더한다 — 요청 도중에 알게 되는 값(session·turn)을 위한 것."""
    clean = {k: v for k, v in fields.items() if v is not None}
    if clean:
        _CTX.set({**_CTX.get(), **clean})


def current() -> dict[str, object]:
    return dict(_CTX.get())


class ContextFilter(logging.Filter):
    """레코드에 `ctx` 속성을 채운다 — 포맷터가 `%(ctx)s`로 찍는다.

    문맥이 비면 빈 문자열이라 기동 로그처럼 요청 밖에서 나는 줄은 지저분해지지 않는다.
    """

    # 로그 줄에서만 줄여 보여줄 필드(전체 값은 문맥에 그대로 둔다 — 잘라 담으면 DB·API의
    # 같은 값과 조인이 안 된다. 줄 길이는 표시의 문제이지 저장의 문제가 아니다).
    _ABBREV = {"session": 8, "turn": 10}

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _CTX.get()
        if not ctx:
            record.ctx = ""
            return True
        parts = []
        for key, value in ctx.items():
            text = str(value)
            width = self._ABBREV.get(key)
            parts.append(f"{key}={text[:width] if width else text}")
        record.ctx = " [" + " ".join(parts) + "]"
        return True
