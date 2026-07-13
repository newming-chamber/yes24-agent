"""Yes24 전용 비동기 HTTP 클라이언트.

자사(Yes24 관계사) 트래픽이지만 예의 있는 클라이언트가 필수이므로 동시성·속도
상한과 지수 백오프를 적용한다. 인프라 오류(일시적 네트워크 문제·429/5xx)는
재시도 소진 시에만 `Yes24FetchError`로 올린다 — 파싱 실패 등 애플리케이션
오류는 이 모듈 밖(parsers 등)의 책임이다.
"""

import asyncio
import codecs
import re
import time
from types import TracebackType
from urllib.parse import urlparse

import httpx

from yes24_agent.config import Settings

# 재시도 대상 상태 코드. 5xx는 범위로 별도 판정한다.
_RETRYABLE_STATUS_CODES = {429}

# HTML <meta charset="..."> 및 <meta http-equiv="Content-Type" content="...charset=...">
# 두 형태를 모두 잡는 느슨한 패턴. 문서 앞부분(_META_SNIFF_WINDOW)만 스캔한다.
_META_CHARSET_RE = re.compile(rb"charset\s*=\s*[\"']?\s*([a-zA-Z0-9_\-]+)", re.IGNORECASE)
_META_SNIFF_WINDOW = 4096
# Content-Type 헤더도 메타 태그도 없을 때의 최종 폴백 인코딩 (Yes24 구버전 페이지는 EUC-KR 계열)
_FALLBACK_ENCODING = "cp949"


class Yes24FetchError(Exception):
    """Yes24 요청이 거부되었거나 재시도 끝에 실패했을 때 발생한다."""

    def __init__(self, message: str, *, url: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


def _hostname(url: str) -> str | None:
    """URL에서 호스트명(소문자)을 추출한다. 파싱 불가 시 None."""
    host = urlparse(url).hostname
    return host.lower() if host else None


def _strip_leading_www(host: str) -> str:
    """호스트명 맨 앞의 'www.' 라벨 하나만 제거한다.

    다른 서브도메인 라벨(cremaclub. 등)은 절대 건드리지 않는다 — 여기서 벗겨내는
    범위를 넓히면 화이트리스트가 과확장되어 도메인 검증이 무력화된다.
    """
    prefix = "www."
    return host[len(prefix) :] if host.startswith(prefix) else host


def _is_allowed_host(host: str, allowed_domain: str | None) -> bool:
    """host가 allowed_domain(등록 도메인) 본인이거나 그 서브도메인인지 확인한다."""
    if allowed_domain is None:
        return False
    return host == allowed_domain or host.endswith(f".{allowed_domain}")


def _is_known_encoding(encoding: str) -> bool:
    """`encoding`이 파이썬이 아는 코덱인지 확인한다."""
    try:
        codecs.lookup(encoding)
    except LookupError:
        return False
    return True


def _sniff_meta_charset(content: bytes) -> str | None:
    """HTML 앞부분에서 `<meta charset=...>` 계열 선언을 찾아 인코딩 이름을 반환한다."""
    match = _META_CHARSET_RE.search(content[:_META_SNIFF_WINDOW])
    if match is None:
        return None
    candidate = match.group(1).decode("ascii", errors="ignore")
    return candidate if _is_known_encoding(candidate) else None


def _decode_response(response: httpx.Response) -> str:
    """응답 본문을 인코딩 감지 후 텍스트로 디코드한다.

    1. HTTP `Content-Type` 헤더에 charset이 있으면 httpx 기본 처리(`response.text`)를
       그대로 신뢰한다 — 헤더 우선순위·미지 인코딩 폴백은 이미 httpx가 처리해 준다.
    2. 헤더에 없으면 **UTF-8 strict 디코드를 최우선으로 시도**한다. UTF-8은 바이트
       패턴이 자가 검증되어(EUC-KR 텍스트가 UTF-8 strict를 통과할 확률은 사실상
       0) `<meta charset>` 선언이 실제 바이트와 다른 경우(예: euc-kr로 선언해놓고
       실제로는 UTF-8인 페이지가 관측됨)를 원천 차단한다.
    3. UTF-8 strict 디코드가 실패하면 그때 HTML `<meta charset>` 선언을 스니핑해
       그 인코딩으로 디코드한다.
    4. meta 선언이 없거나 그 디코드마저 실패하면 `cp949`(errors="replace")로 최종
       폴백한다(본문 품질 판단은 이 클라이언트가 아니라 파서 계층의 책임이다).
    """
    if response.charset_encoding is not None:
        return response.text

    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        pass

    meta_encoding = _sniff_meta_charset(response.content)
    if meta_encoding is not None:
        try:
            return response.content.decode(meta_encoding)
        except UnicodeDecodeError:
            pass

    return response.content.decode(_FALLBACK_ENCODING, errors="replace")


class Yes24Client:
    """동시성·속도 상한, 지수 백오프, 도메인 화이트리스트를 적용하는 Yes24 클라이언트."""

    def __init__(
        self,
        base_url: str,
        user_agent: str,
        timeout_s: float,
        connect_timeout_s: float,
        concurrency: int,
        rps: float,
        max_retries: int,
        backoff_base_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base_host = _hostname(base_url)
        # 허용 기준은 등록 도메인(예: yes24.com) — www. 접두사만 벗겨내 base_url이
        # www.yes24.com이어도 cremaclub.yes24.com 등 다른 서브도메인을 허용한다.
        self._allowed_domain = _strip_leading_www(base_host) if base_host is not None else None
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._min_interval_s = 1.0 / rps if rps > 0 else 0.0

        self._semaphore = asyncio.Semaphore(concurrency)
        self._throttle_lock = asyncio.Lock()
        self._last_request_at: float | None = None

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            transport=transport,
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> "Yes24Client":
        """`Settings` 값을 생성자 파라미터로 매핑하는 편의 팩토리."""
        return cls(
            base_url=settings.yes24_base_url,
            user_agent=settings.user_agent,
            timeout_s=settings.http_timeout_s,
            connect_timeout_s=settings.http_connect_timeout_s,
            concurrency=settings.http_concurrency,
            rps=settings.http_rps,
            max_retries=settings.http_max_retries,
            transport=transport,
        )

    async def get_text(self, url: str) -> str:
        """Yes24 URL을 GET 요청해 응답 본문을 반환한다.

        도메인이 base_url과 다르면 요청을 보내지 않고 즉시 `Yes24FetchError`를
        던진다. 429·5xx·타임아웃·전송 오류는 지수 백오프 후 최대 `max_retries`회
        재시도하며, 그 외 4xx는 재시도 없이 즉시 실패한다. 응답 본문 인코딩은
        `_decode_response`가 헤더→utf-8 strict→meta 태그→cp949 순으로 감지해
        디코드한다(meta 선언이 실제 바이트와 다른 경우가 관측되어 utf-8 자가
        검증을 meta보다 우선한다).
        """
        host = _hostname(url)
        if host is None or not _is_allowed_host(host, self._allowed_domain):
            raise Yes24FetchError(f"허용되지 않은 도메인입니다: {url}", url=url)

        # urlparse(_hostname)는 httpx.URL보다 관대해 잘못된 포트 등을 통과시킬 수
        # 있다. httpx가 실제 요청 시점에 httpx.InvalidURL(Exception 직속이라
        # TimeoutException/TransportError로 안 잡힘)을 던지면 도구 밖으로 새어
        # 나가므로, 재시도 진입 전에 미리 검증해 결정론적으로 즉시 실패시킨다.
        try:
            httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise Yes24FetchError(f"잘못된 형식의 URL입니다: {url} ({exc!r})", url=url) from exc

        async with self._semaphore:
            for attempt in range(self._max_retries + 1):
                has_more_attempts = attempt < self._max_retries
                await self._throttle()

                try:
                    response = await self._client.get(url)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if has_more_attempts:
                        await self._sleep_backoff(attempt)
                        continue
                    raise Yes24FetchError(
                        f"Yes24 요청이 반복 실패했습니다: {url} ({exc!r})", url=url
                    ) from exc

                status = response.status_code
                if status < 400:
                    # SSRF 방어: follow_redirects=True라 최초 URL만 검증하면 yes24.com이
                    # 외부·내부망으로 302시키는 경로를 그대로 따라간다. 리다이렉트 체인의
                    # 모든 방문 host와 최종 host가 허용 도메인인지 재검증한다.
                    for hop in (*response.history, response):
                        hop_host = _hostname(str(hop.url))
                        if hop_host is None or not _is_allowed_host(
                            hop_host, self._allowed_domain
                        ):
                            raise Yes24FetchError(
                                f"허용되지 않은 도메인으로 리다이렉트되었습니다: {hop.url}",
                                url=url,
                            )
                    return _decode_response(response)

                if status in _RETRYABLE_STATUS_CODES or status >= 500:
                    if has_more_attempts:
                        await self._sleep_backoff(attempt)
                        continue
                    raise Yes24FetchError(
                        f"Yes24 요청이 재시도 끝에 실패했습니다: {url} (status={status})",
                        url=url,
                        status_code=status,
                    )

                raise Yes24FetchError(
                    f"Yes24 요청이 실패했습니다: {url} (status={status})",
                    url=url,
                    status_code=status,
                )

        # 도달 불가: 위 for 루프는 매 반복에서 return 또는 raise로 종료된다.
        raise AssertionError("get_text 재시도 루프가 값을 반환하지 않고 종료되었습니다")

    async def _throttle(self) -> None:
        """마지막 요청 시각 기준으로 최소 요청 간격(`1/rps`초)을 보장한다."""
        if self._min_interval_s <= 0:
            return
        async with self._throttle_lock:
            now = time.monotonic()
            if self._last_request_at is not None:
                wait = self._min_interval_s - (now - self._last_request_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
            self._last_request_at = now

    async def _sleep_backoff(self, attempt: int) -> None:
        """지수 백오프(`backoff_base_s * 2**attempt`)만큼 대기한다."""
        await asyncio.sleep(self._backoff_base_s * (2**attempt))

    async def aclose(self) -> None:
        """내부 httpx 클라이언트를 정리한다."""
        await self._client.aclose()

    async def __aenter__(self) -> "Yes24Client":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
