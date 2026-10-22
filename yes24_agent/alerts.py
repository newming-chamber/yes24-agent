"""ERROR 로그를 Slack incoming webhook으로 즉시 알린다.

10-17 상용 장애에서 채팅 77%가 실패했는데 SSE는 200이라 ALB 지표에 안 잡혔고, 실패는 ERROR
로그로만 남았다. 이 핸들러가 그 로그를 사람에게 바로 보낸다.

- emit은 막지 않는다: 요약을 bounded 큐에 put_nowait(가득 차면 버림). 전송은 daemon 스레드가
  urllib로 한다 — 이벤트 루프와 무관하다.
- 폭주 억제: 로거명+첫 줄(숫자·hex를 #로 치환) 앞부분이 같은 키. 키별 첫 건은 즉시, 창 안의
  후속 건은 세기만 하다 창이 끝나면 요약 1건 — 키당 창마다 최대 2건.
- 전송 실패는 삼킨다(WARNING으로만 남김 — ERROR면 자기 자신을 다시 부른다).
- 길이를 자르고 스택은 싣지 않는다: 메시지에 사용자 질문 원문 같은 개인정보가 섞일 수 있다.
- ADK(`google_adk.*`)의 ERROR는 보내지 않는다(파일 로그에는 남는다): 모델·도구 실패마다 ADK가
  자체 ERROR를 2건 남기는데, 턴 실패 여부와 심각도(정상→전환 ERROR, 재프로브 WARNING, 실패 마감
  ERROR)는 runner·model_routing이 판정해 따로 남긴다 — 그대로 보내면 폴백 1회가 알림 3건이 되고
  재프로브(WARNING)도 ADK ERROR 2건으로 새 장애와 구분되지 않는다(2026-10-20 실 Runner 실측).
"""

from __future__ import annotations

import json
import logging
import queue
import re
import socket
import threading
import time
import traceback
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from yes24_agent.config import Settings

logger = logging.getLogger(__name__)

_VARIABLE = re.compile(r"[0-9a-f]*\d[0-9a-f]*", re.IGNORECASE)
_KEY_CHARS = 80
_MESSAGE_CHARS = 500
_EXC_CHARS = 300
_STOP = object()
_ADK_LOGGER = "google_adk"


def _escape(text: str) -> str:
    """Slack mrkdwn 제어 문자를 막는다(`<!channel>` 같은 멘션 주입 차단)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackAlertHandler(logging.Handler):
    def __init__(
        self,
        url: str,
        *,
        window_s: float,
        timeout_s: float,
        instance: str,
        tz: timezone,
        tz_label: str,
        queue_size: int,
        send: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(logging.ERROR)
        self._url, self._window_s, self._timeout_s = url, window_s, timeout_s
        self._instance, self._tz, self._tz_label = instance, tz, tz_label
        self._send = send or self._post
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._thread = threading.Thread(target=self._run, name="slack-alerts", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == logger.name or record.name.split(".", 1)[0] == _ADK_LOGGER:
            return
        try:
            first = record.getMessage().strip().split("\n", 1)[0]
            exc = ""
            if record.exc_info and record.exc_info[1] is not None:
                # 첫 줄만: SQLAlchemy 예외는 뒤 줄에 SQL·바인딩 값(사용자 데이터)을 싣는다.
                exc = traceback.format_exception_only(*record.exc_info[:2])[-1].strip()
                exc = exc.splitlines()[0]
            when = datetime.fromtimestamp(record.created, self._tz).strftime("%m-%d %H:%M:%S")
            text = (
                f":rotating_light: *ERROR* `{self._instance}` {when} {self._tz_label}\n"
                f"`{record.name}`{_escape(getattr(record, 'ctx', ''))}\n"
                f"> {_escape(first[:_MESSAGE_CHARS])}"
                + (f"\n`{_escape(exc[:_EXC_CHARS])}`" if exc else "")
            )
            key = f"{record.name}|{_VARIABLE.sub('#', first)[:_KEY_CHARS]}"
            self._queue.put_nowait((key, first[:_KEY_CHARS], text))
        except queue.Full:
            pass
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        """창에 남은 요약을 짧게 내보낸다(logging.shutdown이 종료 시 부른다)."""
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass
        self._thread.join(self._timeout_s)
        super().close()

    def _run(self) -> None:
        windows: dict[str, list] = {}  # key -> [창 끝(monotonic), 추가 건수, 첫 줄]
        while True:
            now = time.monotonic()
            for key, (end, extra, first) in list(windows.items()):
                if end <= now:
                    del windows[key]
                    if extra:
                        self._summary(extra, first)
            wait = min((w[0] for w in windows.values()), default=now + 3600) - now
            try:
                item = self._queue.get(timeout=max(wait, 0.01))
            except queue.Empty:
                continue
            if item is _STOP:
                for _, extra, first in windows.values():
                    if extra:
                        self._summary(extra, first)
                return
            key, first, text = item
            if key in windows:
                windows[key][1] += 1
            else:
                windows[key] = [time.monotonic() + self._window_s, 0, first]
                self._safe_send(text)

    def _summary(self, extra: int, first: str) -> None:
        self._safe_send(
            f":repeat: `{self._instance}` 최근 {self._window_s:g}초 같은 에러 {extra}건 추가"
            f" — {_escape(first)}"
        )

    def _safe_send(self, text: str) -> None:
        try:
            self._send(text)
        except Exception as exc:  # 삼킨다 — ERROR로 남기면 자기 자신을 다시 트리거한다
            logger.warning(f"Slack 알림 전송 실패: {type(exc).__name__}")

    def _post(self, text: str) -> None:
        req = urllib.request.Request(
            self._url,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=self._timeout_s).close()


def install_slack_alerts(settings: Settings) -> SlackAlertHandler | None:
    """웹훅 URL이 있을 때만 root 로거에 핸들러를 단다(빈 값이면 기능 전체 비활성)."""
    if not settings.slack_webhook_url:
        return None
    handler = SlackAlertHandler(
        settings.slack_webhook_url,
        window_s=settings.slack_alert_window_s,
        timeout_s=settings.slack_alert_timeout_s,
        instance=settings.alert_instance or socket.gethostname(),
        tz=timezone(timedelta(hours=settings.admin_utc_offset_hours)),
        tz_label=settings.admin_timezone_label,
        queue_size=settings.slack_alert_queue_size,
    )
    logging.getLogger().addHandler(handler)
    return handler
