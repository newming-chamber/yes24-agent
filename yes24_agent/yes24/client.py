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
from urllib.parse import urljoin, urlparse

import httpx

from yes24_agent.config import Settings

# 재시도 대상 상태 코드. 5xx는 범위로 별도 판정한다.
_RETRYABLE_STATUS_CODES = {429}

# 리다이렉트 상태 코드. 응답을 받은 뒤 Location을 **요청 전에** 도메인 검증한다.
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}

# HTML <meta charset="..."> 및 <meta http-equiv="Content-Type" content="...charset=...">
# 두 형태를 모두 잡는 느슨한 패턴. 문서 앞부분(_META_SNIFF_WINDOW)만 스캔한다.
_META_CHARSET_RE = re.compile(rb"charset\s*=\s*[\"']?\s*([a-zA-Z0-9_\-]+)", re.IGNORECASE)
_META_SNIFF_WINDOW = 4096
# Content-Type 헤더도 메타 태그도 없을 때의 최종 폴백 인코딩 (Yes24 구버전 페이지는 EUC-KR 계열)
_FALLBACK_ENCODING = "cp949"
# 디코드 실패 시 채워지는 대체 문자(U+FFFD). 비율이 임계를 넘으면 fail-loud한다.
_REPLACEMENT_CHAR = "�"


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


def is_disallowed_path(url: str, disallowed_paths: tuple[str, ...]) -> bool:
    """robots.txt가 Disallow한 경로인지 판정한다(소문자 경로 **접두** 일치).

    접두 일치라 허용 경로(`/product/goods/...`)가 차단 경로(`/goods/`)를 부분 문자열로
    포함해도 오탐하지 않는다. 규칙은 config에서 주입한다(하드코딩 금지).
    """
    path = urlparse(url).path.lower()
    return any(path.startswith(prefix) for prefix in disallowed_paths)


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


def _decode_response(response: httpx.Response, *, url: str, max_replacement_ratio: float) -> str:
    """응답 본문을 **선언이 아니라 디코드 자가검증**으로 텍스트화한다.

    인코딩 선언(HTTP `Content-Type` charset·HTML `<meta charset>`)은 둘 다 실제 바이트와
    어긋나는 것이 관측된다 — 그래서 어느 선언도 무조건 신뢰하지 않고, **strict 디코드가
    성공하는지**로 검증한다. 순서:

    1. **UTF-8 strict 최우선.** UTF-8은 바이트 패턴이 자가 검증되어(EUC-KR 한국어 텍스트가
       UTF-8 strict를 통과할 확률은 사실상 0) 선언이 무엇이든 성공하면 그 결과가 옳다.
    2. 실패하면 선언된 인코딩(헤더 → meta 순)으로 **strict** 디코드를 시도한다. strict라
       선언이 틀리면 실패해 다음 후보로 넘어간다(거짓 선언이 조용히 채택되지 않는다).
    3. 전부 실패하면 `cp949`(errors="replace")로 폴백하되, 대체 문자(U+FFFD) 비율이
       `max_replacement_ratio`를 넘으면 `Yes24FetchError`로 **fail-loud**한다. 깨진
       텍스트는 길이가 멀쩡해 하류의 본문 길이 가드를 그대로 통과하므로("조용히 성공하는
       실패"), 여기서 끊지 않으면 문자 깨짐이 파싱 0건·환각으로 흘러간다.
    """
    content = response.content

    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        pass

    declared = (response.charset_encoding, _sniff_meta_charset(content))
    for encoding in declared:
        if not encoding or not _is_known_encoding(encoding):
            continue
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue

    text = content.decode(_FALLBACK_ENCODING, errors="replace")
    ratio = text.count(_REPLACEMENT_CHAR) / len(text) if text else 0.0
    if ratio > max_replacement_ratio:
        raise Yes24FetchError(
            f"응답 본문의 인코딩을 판별하지 못했습니다: {url} "
            f"(대체 문자 비율 {ratio:.1%} > 상한 {max_replacement_ratio:.1%})",
            url=url,
        )
    return text


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
        max_redirects: int = 5,
        max_replacement_ratio: float = 0.02,
        disallowed_paths: tuple[str, ...] = (),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base_host = _hostname(base_url)
        # 허용 기준은 등록 도메인(예: yes24.com) — www. 접두사만 벗겨내 base_url이
        # www.yes24.com이어도 cremaclub.yes24.com 등 다른 서브도메인을 허용한다.
        self._allowed_domain = _strip_leading_www(base_host) if base_host is not None else None
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._max_redirects = max_redirects
        self._max_replacement_ratio = max_replacement_ratio
        self._disallowed_paths = tuple(p.lower() for p in disallowed_paths)
        self._min_interval_s = 1.0 / rps if rps > 0 else 0.0

        self._semaphore = asyncio.Semaphore(concurrency)
        self._throttle_lock = asyncio.Lock()
        self._last_request_at: float | None = None

        # 리다이렉트는 httpx에 맡기지 않고 직접 따라간다 — follow_redirects=True면 다음 홉이
        # **이미 전송된 뒤에야** 검증할 수 있어(사후 차단) SSRF 요청 자체는 나가 버린다.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            headers={"User-Agent": user_agent},
            follow_redirects=False,
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
            backoff_base_s=settings.http_backoff_base_s,
            max_redirects=settings.http_max_redirects,
            max_replacement_ratio=settings.http_max_replacement_char_ratio,
            disallowed_paths=tuple(settings.yes24_disallowed_paths),
            transport=transport,
        )

    async def get_text(self, url: str) -> str:
        """Yes24 URL을 GET 요청해 응답 본문을 반환한다.

        도메인이 base_url과 다르면 요청을 보내지 않고 즉시 `Yes24FetchError`를 던진다.
        리다이렉트는 직접 따라가되 **다음 홉을 요청하기 전에** 같은 검증을 통과시킨다 —
        허용 밖 도메인으로의 요청은 아예 나가지 않는다(사전 차단). 홉 수가
        `max_redirects`를 넘으면 실패한다. 429·5xx·타임아웃·전송 오류는 홉마다 지수
        백오프 후 최대 `max_retries`회 재시도하며, 그 외 4xx는 재시도 없이 즉시 실패한다.
        본문 인코딩은 `_decode_response`가 선언이 아니라 strict 디코드 성공 여부로
        판별하고, 판별 실패 시 깨진 텍스트를 반환하지 않고 실패시킨다.
        """
        self._validate_target(url, original_url=url)

        async with self._semaphore:
            current = url
            for _ in range(self._max_redirects + 1):
                response = await self._fetch_once(current, original_url=url)

                next_url = self._redirect_target(response)
                if next_url is None:
                    return _decode_response(
                        response, url=url, max_replacement_ratio=self._max_replacement_ratio
                    )

                # 사전 검증: 다음 홉은 검증을 통과해야만 요청된다.
                self._validate_target(next_url, original_url=url, via_redirect=True)
                current = next_url

        raise Yes24FetchError(
            f"리다이렉트가 상한({self._max_redirects}회)을 초과했습니다: {url}", url=url
        )

    def _validate_target(
        self, url: str, *, original_url: str, via_redirect: bool = False
    ) -> None:
        """요청을 보내기 전에 대상 URL의 도메인·경로·형식을 검증한다(위반 시 Yes24FetchError).

        도메인 허용(SSRF 방어)과 robots.txt Disallow 경로를 **같은 게이트**에서 판정한다 —
        에이전트가 링크 팔로우로 얻은 차단 경로(예: 구경로 `/Goods/`)를 넣어도 요청이 나가지
        않는다. 리다이렉트 홉도 이 검증을 거친 뒤에만 요청된다(사전 차단).
        """
        host = _hostname(url)
        if host is None or not _is_allowed_host(host, self._allowed_domain):
            reason = (
                "허용되지 않은 도메인으로 리다이렉트되었습니다"
                if via_redirect
                else "허용되지 않은 도메인입니다"
            )
            raise Yes24FetchError(f"{reason}: {url}", url=original_url)

        if is_disallowed_path(url, self._disallowed_paths):
            raise Yes24FetchError(
                f"robots.txt가 수집을 금지한 경로입니다: {url}", url=original_url
            )

        # urlparse(_hostname)는 httpx.URL보다 관대해 잘못된 포트 등을 통과시킬 수
        # 있다. httpx가 실제 요청 시점에 httpx.InvalidURL(Exception 직속이라
        # TimeoutException/TransportError로 안 잡힘)을 던지면 도구 밖으로 새어
        # 나가므로, 재시도 진입 전에 미리 검증해 결정론적으로 즉시 실패시킨다.
        try:
            httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise Yes24FetchError(
                f"잘못된 형식의 URL입니다: {url} ({exc!r})", url=original_url
            ) from exc

    def _redirect_target(self, response: httpx.Response) -> str | None:
        """응답이 리다이렉트면 다음 홉의 절대 URL을, 아니면 None을 반환한다.

        Location은 상대 경로일 수 있어 응답 URL 기준으로 절대화한다.
        """
        if response.status_code not in _REDIRECT_STATUS_CODES:
            return None
        location = response.headers.get("Location")
        if not location:
            return None
        return urljoin(str(response.url), location)

    async def _fetch_once(self, url: str, *, original_url: str) -> httpx.Response:
        """단일 홉을 요청한다(재시도·스로틀 포함). 4xx/5xx는 Yes24FetchError.

        리다이렉트 응답(3xx)은 그대로 돌려주고 추종 여부는 호출자(get_text)가 검증 후 결정한다.
        """
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
                    f"Yes24 요청이 반복 실패했습니다: {url} ({exc!r})", url=original_url
                ) from exc

            status = response.status_code
            if status < 400:
                return response

            if status in _RETRYABLE_STATUS_CODES or status >= 500:
                if has_more_attempts:
                    await self._sleep_backoff(attempt)
                    continue
                raise Yes24FetchError(
                    f"Yes24 요청이 재시도 끝에 실패했습니다: {url} (status={status})",
                    url=original_url,
                    status_code=status,
                )

            raise Yes24FetchError(
                f"Yes24 요청이 실패했습니다: {url} (status={status})",
                url=original_url,
                status_code=status,
            )

        # 도달 불가: 위 for 루프는 매 반복에서 return 또는 raise로 종료된다.
        raise AssertionError("_fetch_once 재시도 루프가 값을 반환하지 않고 종료되었습니다")

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
