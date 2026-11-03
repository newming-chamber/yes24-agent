"""웹 검색 도구 — ADK FunctionTool로 노출되는 async 함수(Perplexity /search 직접 호출).

**역할 분리**: 이 도구는 Yes24 밖의 외부·최신 정보(뉴스·스포츠·주가·시사·인물·상식 등)를
확보하기 위한 것이다. 가격·구매·재고 등 상품 정보의 근거로는 쓰지 않는다 — 그것은
여전히 yes24_search/yes24_fetch(Yes24 출처)만 담당한다.

Perplexity /search는 요약(answer)이 아니라 **원시 검색 결과**(제목·URL·스니펫)를 준다.
각 결과의 snippet 필드에 페이지 콘텐츠(추출 본문, max_tokens_per_page로 분량 조절)가
직접 담기므로 Tavily의 snippet/raw_content 이원 구조가 단일 필드로 통합된다 — snippet이
곧 종합 재료다. 에이전트가 여러 결과를 직접 종합해 답하며, snippet보다 더 긴 전문이
필요하면 그 url을 web_fetch(Tavily /extract)로 읽는다. 각 결과에는 문서 발행 시점
published_at과 출처 갱신 시점 last_updated를 분리해 싣는다.

**퍼플렉시티식 멀티쿼리 병렬 검색**: 이 도구는 한 번에 여러 검색 각도(queries)를 받아
asyncio.gather로 **동시에** 검색하고 결과를 합쳐 돌려준다. 복합·시의성·비교 질문을 하나의
좁은 쿼리로 뭉개는 대신 서로 다른 각도로 분해해 폭넓게 수집한 뒤(질문 분해 → 병렬 검색 →
원시 결과를 에이전트가 직접 종합) 답하기 위함이다. 이는 fetch_many가 여러 상세 열람을 한 번의
LLM 왕복으로 병렬화하는 것과 같은 구조 — 모델이 N번 도구를 나눠 호출하길 기대(비결정적)하는
대신, N개 각도를 한 리스트로 받아 코드가 병렬 실행을 보장한다. 단순·단일 각도 질문은 원소
하나짜리 리스트로 그대로 처리된다(단일 검색과 동일 지연).

정확성 설계(레이스 0): 네트워크(/search POST)만 동시 실행하고, 출처 등록(register_source·
id 부여)은 **순차 루프**로 처리한다 — 단일 tool_context.state에 대한 등록이 await 없이 순차라
source_id가 유일·단조로 부여된다(fetch_many와 동일 규약, 병렬 도구 유실 방지). 같은 url이 여러
각도에서 걸리면 한 번만 등록하고 어느 각도에서 나왔는지(queries)를 합쳐 교차 확증 신호로 남긴다.

각 결과를 세션 state의 출처 레지스트리에 등록해 source_id를 부여하고, 인용에 쓸 수 있도록
반환 dict에 담는다. 실패는 예외를 밖으로 던지지 않고 구조화된 error dict로 반환한다(부분 실패는
성공 결과와 함께 각 각도의 상태를 searches로 fail-loud 노출 — 빈 성공으로 위장하지 않는다).
"""

import asyncio
import contextvars
import html
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from urllib.parse import urlsplit

import httpx
from google.adk.tools import ToolContext
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from yes24_agent import subcall
from yes24_agent.config import Settings, get_settings
from yes24_agent.model_routing import current_models, web_backend_configured
from yes24_agent.sources import cite_marker, now_checked_at, register_source
from yes24_agent.tools._planning import (
    angle_error_summary,
    dropped_queries_message,
    plan_queries,
)
from yes24_agent.tools._text import truncate
from yes24_agent.usage import record_usage, record_web_search

logger = logging.getLogger(__name__)


def _normalize_domain_filters(domains: list[str] | None) -> list[str]:
    """명시 도메인 필터를 Perplexity 문법의 단일 polarity 목록으로 검증한다."""
    if domains is None:
        return []
    if not isinstance(domains, list):
        raise ValueError("domains는 도메인 문자열 목록이어야 합니다")

    normalized: list[str] = []
    seen: set[str] = set()
    polarities: set[bool] = set()
    for raw in domains:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("domains의 각 항목은 비어 있지 않은 문자열이어야 합니다")
        value = raw.strip().lower()
        excluded = value.startswith("-")
        domain = value[1:] if excluded else value
        if any(marker in domain for marker in ("://", "/", "?", "#", "@", ":")):
            raise ValueError("domains에는 프로토콜·경로·포트 없이 hostname만 넣어야 합니다")
        domain = domain.rstrip(".")
        try:
            ascii_domain = domain.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("domains에 유효하지 않은 hostname이 있습니다") from exc
        labels = ascii_domain.split(".")
        if len(labels) < 2 or len(ascii_domain) > 253:
            raise ValueError("domains에는 완전한 hostname을 넣어야 합니다")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(not (character.isalnum() or character == "-") for character in label)
            for label in labels
        ):
            raise ValueError("domains에 유효하지 않은 hostname이 있습니다")

        polarities.add(excluded)
        normalized_filter = f"-{ascii_domain}" if excluded else ascii_domain
        if normalized_filter not in seen:
            seen.add(normalized_filter)
            normalized.append(normalized_filter)

    if len(polarities) > 1:
        raise ValueError("domains의 포함 필터와 제외 필터를 한 요청에 섞을 수 없습니다")
    return normalized


def _url_in_domain_scope(url: str, domain_filters: list[str]) -> bool:
    """반환 URL hostname이 요청한 allowlist/denylist 경계를 만족하는지 판정한다."""
    try:
        parsed = urlsplit(url)
        raw_hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or raw_hostname is None:
        return False
    try:
        hostname = raw_hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return False
    if not domain_filters:
        return True
    excluded = domain_filters[0].startswith("-")
    scoped_domains = [entry[1:] if excluded else entry for entry in domain_filters]
    matched = any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in scoped_domains
    )
    return not matched if excluded else matched


# Yes24 클라이언트와 별개인 범용 HTTP 클라이언트(도메인 제약 없음). 모듈 lazy 싱글턴.
# web_fetch도 이 클라이언트를 재사용한다(둘 다 외부 API 호출 — aclose 일원화). 두 도구가
# 서로 다른 인증(퍼플렉시티 Bearer 헤더 vs Tavily 바디 api_key)을 쓰므로, 클라이언트에는
# 기본 인증 헤더를 두지 않고 각 요청에서 헤더를 넘겨 키가 다른 API로 새지 않게 한다.
_shared_client: httpx.AsyncClient | None = None
# PoolTimeout으로 교체된 옛 공유 클라이언트를 유예 뒤 닫는 태스크(참조 유지·셧다운 정리용).
# 참조만 버리면 갇힌 연결의 소켓이 순환 GC 시점까지 남는다(실측).
_retire_tasks: set[asyncio.Task] = set()


@lru_cache(maxsize=1)
def _title_ssl_context():
    """제목 보강 묶음 클라이언트용 SSL context(1회 생성) — 묶음마다 만들면 루프가 ~6ms 막힌다."""
    return httpx.create_ssl_context()


# 백엔드별 쿼터 소진 표시(프로세스 전역, 백엔드 → monotonic 시각): 이 시각 전까지 그 백엔드를
# 건너뛴다. 2026-10-19 상용 장애 — 검색 그라운딩 한도가 소진되면 매 호출이 429라, 호출마다 429를
# 맞고 전환하면 왕복만 낭비한다. SearchApi도 크레딧·시간당 한도 초과가 429다. 쿨다운(config
# web_grounding_quota_cooldown_s)이 지나면 시각 비교만으로 풀린다. 표시·건너뛰기 지점은 각
# 백엔드 입구 하나다(그라운딩은 _grounding_raw_results — 프리페치·정상 경로 공통, SearchApi는
# searchapi_raw). 키가 있는 동안이 "전환 중"이다 — 시각이 지나도 그 백엔드가 성공할 때까지
# 남겨, 재시도 프로브의 재429를 새 전환(운영 Slack 알림 = ERROR)과 구분한다.
_quota_cooldown_until: dict[str, float] = {}


class _QuotaExhausted(RuntimeError):
    """소진 표시 중이라 백엔드를 호출하지 않았다(종단 실패와 같은 경로로 처리된다)."""


def _quota_exhausted(backend: str) -> bool:
    """백엔드의 쿼터 소진 표시가 살아 있는지(쿨다운 중인지) 판정한다."""
    until = _quota_cooldown_until.get(backend)
    return until is not None and time.monotonic() < until


def _mark_quota_exhausted(backend: str, settings: Settings) -> None:
    """쿨다운 동안 소진 표시를 건다(살아 있으면 무동작). 정상→전환은 ERROR, 재시도 프로브의
    재429는 WARNING이다."""
    if _quota_exhausted(backend):
        return
    log = logger.error if backend not in _quota_cooldown_until else logger.warning
    _quota_cooldown_until[backend] = time.monotonic() + settings.web_grounding_quota_cooldown_s
    log(
        f"web_search {backend} quota exhausted → switched to next backend "
        f"(retry every {settings.web_grounding_quota_cooldown_s:.0f}s)"
    )


def _clear_quota_mark(backend: str) -> None:
    """백엔드 호출 성공 = 전환 해제(표시가 있었을 때만 복귀 INFO 1회)."""
    if _quota_cooldown_until.pop(backend, None) is not None:
        logger.info(f"web_search {backend} recovered")


# ── 웹 선제 실행(prefetch) ─────────────────────────────────────────────────────
# TTFT 실측(2026-07-28): 시변 질의의 첫 본문 토큰 11.8s = 사고1(도구 결정) 3.8 → 도구 2.8 →
# 사고2 5.2의 직렬 합. 도구 실행에는 모델 판단이 필요 없으므로, 턴 시작 시 경량 모델이
# "웹 최신 정보가 필요한 질문인가"만 판단해 그라운딩 서브콜을 사고1과 **병렬로** 미리
# 시작한다. 계약(순수 지연 최적화):
#   - 판단 대체 금지: 프리페치는 힌트일 뿐 에이전트의 도구 선택·재검색 판단을 바꾸지 않는다.
#     서빙 결과의 searches에 실제 검색 질의(사용자 메시지)를 정직하게 실어, 모델이 자기
#     각도와 다르면 스스로 재검색한다(턴당 1회 서빙 — 두 번째 호출부터 항상 정상 경로).
#   - 실패는 무해: 힌트 오판·서브콜 실패·미소비 전부 정상 경로 폴백. 기능 저하 경로 없음.
#   - 캐시는 메시지 키·TTL 공유: 매트릭스 16셀이 같은 질문을 동시에 돌려도 힌트·서브콜은
#     1회다(셀은 채팅 runner를 그대로 쓰고, 공유는 여기서 일어난다 — 매트릭스 전용 로직 없음).

_HINT_INSTRUCTION = (
    "사용자 메시지에 정확히 답하려면 웹 실시간 검색(뉴스·시세·날씨·경기·순위·일정 등 "
    "시간에 따라 변하는 사실이나 웹에서 확인해야 하는 외부 사실)이 필요한지만 판단한다. "
    "잡담·창작·번역·개념 설명이나 도서·상품 검색으로 충분한 질문이면 필요 없음으로 판단한다."
)

# 턴 스코프 프리페치 핸들: (사용자 메시지, task) **1원소 list 홀더**. 러너가 턴 시작에
# set하고 이번 턴 첫 그라운딩 호출이 pop으로 소비한다. contextvar 값이 홀더(가변 list)인
# 이유: ADK 2.3.0은 도구를 단건이어도 create_task(컨텍스트 **사본**)로 실행하므로, 사본에서
# `set(None)`을 해도 부모·형제 task에는 안 보여 "턴당 1회"가 붕괴한다(적대 검증 실증
# 2026-07-28). 사본들이 **같은 list 객체**를 공유하므로 pop은 전 task에 즉시 보이고,
# check-pop 사이에 await가 없어 단일 스레드 이벤트 루프에서 원자적이다. 요청·매트릭스 셀
# 간 격리는 contextvar 그대로다(각 턴이 자기 홀더를 새로 만든다 — 교차 서빙 불가).
_turn_prefetch: contextvars.ContextVar[list[tuple[str, asyncio.Task]] | None] = (
    contextvars.ContextVar("web_prefetch", default=None)
)
# 메시지 키 공유 캐시: message → (시작 monotonic, task). TTL 내 같은 질문은 task를 공유한다.
_prefetch_cache: dict[str, tuple[float, asyncio.Task]] = {}


async def _prefetch_hint_needs_web(message: str, settings: Settings) -> bool:
    """경량 모델이 이 메시지에 웹 검색이 필요한지 판단한다(불리언 1개, 짧은 타임아웃)."""
    response = await subcall.generate(
        settings.web_grounding_model,
        message,
        system_instruction=_HINT_INSTRUCTION,
        temperature=0,
        response_schema={
            "type": "OBJECT",
            "properties": {"needs_web": {"type": "BOOLEAN"}},
            "required": ["needs_web"],
        },
        timeout_s=settings.web_prefetch_hint_timeout_s,
        http_timeout_ms=int(settings.web_grounding_timeout_s * 1000),
    )
    # 힌트도 과금되는 LLM 콜이다 — 콜당 1행(부가 채널·무실패, session 문맥 없음).
    record_usage("web_prefetch_hint", response.usage_metadata, model=settings.web_grounding_model)
    return json.loads(response.text or "{}").get("needs_web") is True


async def _primary_backend() -> str:
    """웹 검색 주 백엔드 — owner 어드민 런타임 설정(model_settings.web_search_backend)이 있으면 그
    값, 없으면 config. 읽기 실패는 current_models가 config로 접는다(요청을 실패시키지 않는다)."""
    return (await current_models()).web_search_backend


async def _prefetch_pipeline(message: str, settings: Settings) -> list[dict] | None:
    """힌트 판정 → (필요 시) 그라운딩 서브콜. 서빙 불가 상태는 None(정상 경로 폴백).

    주 백엔드가 그라운딩이 아니면 힌트도 부르지 않는다 — 서빙 표면이 없다(능력 기반). 판정이 이
    task 안에 있는 이유: 주 백엔드는 런타임 설정(비동기 읽기)이고, 프리페치 시작은 동기 훅이다.
    """
    if await _primary_backend() != "grounding":
        return None
    if not await _prefetch_hint_needs_web(message, settings):
        logger.info("web_search prefetch hint=no_web")
        return None
    raw_results = await _grounding_raw_results(
        _grounding_prompt([message]), settings, queries=[message]
    )
    logger.info(f"web_search prefetch ready results={len(raw_results)}")
    # 빈 근거는 서빙하지 않는다 — 정상 경로가 자기 재시도 규약대로 다시 시도하게 둔다.
    return raw_results or None


def _log_prefetch_outcome(task: asyncio.Task) -> None:
    """미소비 프리페치의 예외를 회수한다(경고 소음 방지) — 실패는 무해하므로 기록만 한다."""
    if not task.cancelled() and task.exception() is not None:
        logger.info(f"web_search prefetch 실패(정상 경로 폴백): {task.exception()!r:.120}")


def start_web_prefetch(message: str, active: frozenset[str]) -> None:
    """이번 턴의 웹 프리페치를 시작한다(러너가 턴 시작에 호출, 실패·미소비 무해).

    `active`는 **이번 요청의** 활성 toolset이다. 전역 구성을 여기서 다시 읽으면 UI에서 web을
    끈 요청도 프리페치가 돌아, 도구는 없는데 웹 서브콜만 나가는 어긋남이 된다 — 도구 등록·
    프롬프트 조립과 같은 구성을 봐야 한다.
    """
    if "web" not in active:
        return
    settings = get_settings()
    if not settings.web_prefetch_enabled:
        return
    message = message.strip()
    if not message:
        return
    now = time.monotonic()
    for key in [
        k
        for k, (started, _) in _prefetch_cache.items()
        if now - started > settings.web_prefetch_ttl_s
    ]:
        del _prefetch_cache[key]
    entry = _prefetch_cache.get(message)
    # 실패로 끝난 task가 TTL까지 캐시에 고정되면 같은 메시지의 후속 턴(매트릭스 셀 포함)이
    # 전부 폴백만 탄다 — 죽은 항목은 재생성한다(성공·실행 중 task만 공유 가치가 있다).
    if entry is not None and entry[1].done() and (
        entry[1].cancelled() or entry[1].exception() is not None
    ):
        entry = None
    if entry is None:
        task = asyncio.create_task(_prefetch_pipeline(message, settings))
        task.add_done_callback(_log_prefetch_outcome)
        _prefetch_cache[message] = (now, task)
    else:
        task = entry[1]
    _turn_prefetch.set([(message, task)])


def _take_turn_prefetch() -> tuple[str, asyncio.Task] | None:
    """이번 턴의 프리페치 핸들을 소비한다(턴당 1회 — 두 번째 호출부터 None).

    pop 소비인 이유는 _turn_prefetch 주석 참조(ADK 도구 task 컨텍스트 사본 간 공유).
    """
    holder = _turn_prefetch.get()
    if not holder:
        return None
    return holder.pop()


def _log_search(
    backend: str, query: str, started: float, *, ended: float | None = None,
    exc: Exception | None = None, results: list | None = None, response: dict | None = None,
    search_type: str | None = None, domain_filters: list[str] | None = None,
) -> None:
    """백엔드 요청 1건 → web_search_log 1행(세 백엔드 공용, 실패 무해). 지연은 started~ended
    (기본 지금) monotonic 구간 — 호출부가 백엔드 호출만 감싼다."""
    record_web_search(
        backend, query,
        outcome="ok" if exc is None else f"error:{type(exc).__name__}",
        error=None if exc is None else f"{exc!r:.120}",
        latency_ms=int(((ended or time.monotonic()) - started) * 1000),
        results=results, response=response, search_type=search_type,
        domain_filters=domain_filters,
    )


def _grounding_metadata(response):
    """응답 첫 후보의 grounding_metadata(없으면 None) — 출처 분해와 쿼리 수 기록의 단일 접근점."""
    candidates = getattr(response, "candidates", None) or []
    return getattr(candidates[0], "grounding_metadata", None) if candidates else None


def _grounding_response_to_results(response) -> list[dict]:
    """그라운딩 응답을 출처별 원시 재료 목록으로 분해한다.

    grounding_supports(문장 구간 ↔ 출처 청크 매핑)를 이용해 종합문을 **출처가 뒷받침한
    서술(claims)**로 되돌린다 — 벤더 종합을 그대로 삼키지 않고 출처 단위 재료로 낮춰,
    에이전트가 직접 종합·인용하는 기존 계약(원시 결과 철학)을 보존하기 위함이다. 근거 구간이
    하나도 연결되지 않은 청크는 인용 불가이므로 버리고, 종합문 자체도 반환하지 않는다 —
    supports가 연결되지 않은 문장은 정확히 "인용 불가한 벤더 주장"이라 도구 결과에 실으면
    마커 세탁 경로가 된다.

    **한 서술을 여러 청크가 함께 뒷받침하는 것이 API의 정상 형태다**(2026-08-04 실측:
    "오늘 코스피"는 supports 1건 → 청크 3개, "미움받을 용기 중고 가격"은 supports 3건이
    각각 청크 2·3·3개에 걸렸다 — 1:1 매핑이 오히려 드물다). 그래서 서술을 출처별 문자열로
    접어 넣으면 같은 문장이 출처마다 복제되고, 읽는 쪽은 그것을 서로 다른 출처의 독립 확증으로
    오독한다(실측: 중고가 답변에서 알라딘 2건 + 예스24 1건의 근거가 바이트 동일). 여기서는
    서술을 **원자 단위 목록**으로만 담고, "어느 서술을 어느 출처들이 함께 뒷받침하는가"는
    호출부가 도구 결과의 evidence에 한 번씩만 표현한다.
    """
    metadata = _grounding_metadata(response)
    chunks = getattr(metadata, "grounding_chunks", None) or []
    supports = getattr(metadata, "grounding_supports", None) or []

    segments_by_chunk: dict[int, list[str]] = {}
    for support in supports:
        segment_text = getattr(getattr(support, "segment", None), "text", None)
        if not segment_text:
            continue
        for chunk_index in getattr(support, "grounding_chunk_indices", None) or []:
            segments_by_chunk.setdefault(chunk_index, []).append(segment_text)

    raw_results: list[dict] = []
    for index, chunk in enumerate(chunks):
        web = getattr(chunk, "web", None)
        uri = getattr(web, "uri", None)
        segments = segments_by_chunk.get(index)
        if not uri or not segments:
            continue
        raw_results.append(
            {
                "url": uri,
                "title": getattr(web, "title", None) or uri,
                "claims": list(dict.fromkeys(segments)),  # 순서 보존 중복 제거
            }
        )
    return raw_results


def _grounding_prompt(questions: list[str]) -> str:
    """그라운딩 서브콜 프롬프트를 조립한다(도구 경로·선제 실행 경로가 문자 단위로 같아야 한다)."""
    return (
        "다음 질문(들)의 사실을 웹 검색으로 확인해, 확인된 사실만 간결하게 정리하세요. "
        "시간에 따라 변하는 값은 기준 시각을 함께 적으세요.\n"
        + "\n".join(f"- {q}" for q in questions)
    )


async def _fetch_page_meta(
    url: str, settings: Settings, client: httpx.AsyncClient
) -> tuple[str | None, str | None]:
    """리다이렉트를 따라가 **최종 URL과 페이지 <title>**을 가볍게 긁어온다(실패 무해).

    그라운딩 API가 청크마다 주는 값은 `uri`(불투명 리다이렉트)와 `title`(발행처 도메인)
    둘뿐이다 — `domain` 필드는 SDK 스키마에 있으나 항상 null이다(2026-08-03 실측). 그래서
    리다이렉트 URL을 그대로 두면 사용자는 카드를 클릭하기 전까지 뉴스핌인지 개인 블로그인지
    알 수 없다(발행처 신뢰 판단이 출처 표시의 목적인데 그게 막힌다).

    최종 URL은 **추가 비용이 0이다** — 제목을 읽으려 이미 follow_redirects로 열어 둔 응답의
    `response.url`이 곧 리다이렉트가 풀린 실제 주소다. 요청을 더 보내지 않으므로 지연이
    늘지 않는다. 본문 스트림은 제목 태그가 나올 만큼만 읽고 끊으며(대형 뉴스 페이지 전체
    다운로드 방지), 어떤 실패도 (None, None)으로 삼켜 원래의 리다이렉트 URL·도메인 표기를
    유지한다. 인용 근거가 아니라 순수 표시 메타다.
    """
    try:
        buf = b""
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return None, None
            final_url = str(response.url)
            async for chunk in response.aiter_bytes():
                buf += chunk
                if b"</title>" in buf.lower() or len(buf) > settings.web_title_fetch_max_bytes:
                    break
        match = re.search(rb"<title[^>]*>(.*?)</title>", buf, re.S | re.I)
        if not match:
            return final_url, None
        raw = match.group(1)
        for encoding in ("utf-8", "cp949"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return final_url, None
        title = html.unescape(" ".join(text.split()))
        return final_url, title[: settings.web_title_max_chars] or None
    except Exception:  # noqa: BLE001 — 표시 보강 전용: 어떤 실패도 원래 값 유지
        return None, None


async def _grounding_raw_results(
    prompt: str, settings: Settings, *, queries: list[str] | None = None
) -> list[dict]:
    """그라운딩 서브콜을 실행해 출처별 원시 재료를 돌려준다(빈 목록 가능, 종단 실패는 raise).

    web_search_log에는 호출 1건(재시도 포함)을 1행으로 남긴다 — 질의는 queries(계획된 각도),
    없으면 프롬프트. 결과는 제목·최종 URL 보강 후 값, 지연은 서브콜 구간만이다.

    일시 실패 2종을 재시도로 흡수한다(시도 횟수는 config web_grounding_max_attempts,
    Yes24Client의 전송 오류 재시도와 동일 철학):
    ① 벤더 5xx·타임아웃(ServerError — 504가 재시도에서 정상, 라이브 실측) ② **빈 근거**
    — 그라운딩 종합은 확률적이라 같은 질의가 다음 시도에서 출처를 붙인다(영화 질의 실측:
    E2E 1회 empty → 직호출 3/3 성공). 4xx(ClientError)는 반복해도 같으므로 즉시 실패.
    그중 429(쿼터 소진)는 소진 표시를 남기고(예외는 그대로 올린다), 표시 중에는 서브콜 없이
    즉시 실패한다 — 호출부는 다른 종단 실패와 똑같이 처리한다(백엔드 전환은 web_search).
    """
    if _quota_exhausted("grounding"):
        raise _QuotaExhausted("grounding quota cooldown")
    last_error: Exception | None = None
    raw_results: list[dict] = []
    query = " | ".join(queries) if queries else prompt
    started = time.monotonic()
    for _ in range(settings.web_grounding_max_attempts):
        try:
            response = await asyncio.to_thread(
                # 빌트인 google_search는 함수 선언과 같은 요청에 혼용이 금지되므로(400 실측)
                # 이 도구 내부의 **별도 요청**으로 실행한다 — 루프의 커스텀 도구와 충돌 없음.
                subcall._gemini_client(int(settings.web_grounding_timeout_s * 1000))
                .models.generate_content,
                model=settings.web_grounding_model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())]
                ),
            )
            # 재시도 루프 **안**에서 기록한다 — 실패·빈 결과로 버려진 시도도 각각 과금
            # 콜이라 시도별 1행이 정직하다(부가 채널·무실패). Gemini 3는 실행된 검색 쿼리
            # 1건당 따로 과금돼 그 수도 싣는다(목록 없음 = 0).
            record_usage(
                "web_grounding", response.usage_metadata, model=settings.web_grounding_model,
                search_queries=len(
                    getattr(_grounding_metadata(response), "web_search_queries", None) or []
                ),
            )
            raw_results = _grounding_response_to_results(response)
            last_error = None
            if raw_results:
                break
        except genai_errors.ServerError as exc:
            last_error = exc
        except genai_errors.ClientError as exc:
            if exc.code == 429:
                _mark_quota_exhausted("grounding", settings)
            last_error = exc
            break
        except Exception as exc:  # noqa: BLE001 — 비일시 오류: 재시도 없이 종단 실패로 올린다
            last_error = exc
            break
    ended = time.monotonic()
    if last_error is not None:
        _log_search("grounding", query, started, ended=ended, exc=last_error)
        raise last_error
    _clear_quota_mark("grounding")  # 서브콜 성공 = 전환 해제
    if raw_results:
        # 최종 URL·제목 보강(병렬·실패 무해, 요청 1회로 둘 다). 프리페치 파이프라인 안에서도
        # 실행되므로 대부분의 지연이 모델 사고 시간에 숨는다.
        #
        # 보강 **전체**에 wall-clock 예산 하나를 건다. 이것은 순수 표시 메타지 인용 근거가
        # 아닌데(`_fetch_page_meta` 참조), 프리페치가 안 숨겨주는 경로(턴 내 두 번째 호출,
        # 프리페치 미스·미소비)에서는 가장 느린 외부 사이트 한 곳이 도구 응답 전체를 붙잡는다.
        # 요청별 httpx 타임아웃은 **위상별**이라 청크가 시한 미만 간격으로 계속 흘러오는
        # 서버에서는 총 지연을 못 막는다 — 예산은 그 꼬리를 처음으로 진짜 벽에 가둔다.
        # 시한 내 끝난 것만 반영하고 나머지는 취소해 리다이렉트 URL·도메인 표기를 그대로
        # 둔다(개별 실패 폴백과 같은 형태라 경로가 갈라지지 않는다).
        #
        # 클라이언트는 묶음 전용으로 열고 닫는다 — 취소된 요청의 연결은 풀에 busy로 영구히
        # 남을 수 있고(httpcore 1.0.9: TLS 직후 HTTP/1.1 상태 전이 전 또는 응답 닫기 중 취소,
        # encode/httpcore#1093), 공유 풀이 그렇게 차면 같은 풀의 퍼플렉시티·Tavily까지 막힌다
        # (2026-11-01 상용 장애). 묶음 풀은 aclose가 상태와 무관하게 치운다.
        budget = settings.web_title_fetch_timeout_s
        tasks: list[asyncio.Future] = []
        try:
            async with httpx.AsyncClient(
                timeout=budget * settings.web_title_fetch_request_ratio,
                follow_redirects=True,
                verify=_title_ssl_context(),
            ) as client:
                tasks = [
                    asyncio.ensure_future(_fetch_page_meta(item["url"], settings, client))
                    for item in raw_results
                ]
                _, pending = await asyncio.wait(tasks, timeout=budget)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        finally:
            # 바깥(턴 취소·SSE 끊김)에서 취소되면 위 wait가 끊겨 태스크가 고아로 남는다
            # — 함께 끊는다.
            for task in tasks:
                if not task.done():
                    task.cancel()
        for item, task in zip(raw_results, tasks):
            if task.cancelled() or task.exception() is not None:
                continue
            final_url, title = task.result()
            if final_url:
                item["url"] = final_url
            if title:
                item["title"] = title
        # 서로 다른 리다이렉트 URI가 같은 원문으로 풀리면 출처는 하나다 — 등록 전에 서술을
        # 합친다(같은 url 재등록은 state에서 나중 관측이 이겨 앞 근거가 사라진다).
        merged: dict[str, dict] = {}
        for item in raw_results:
            kept = merged.setdefault(item["url"], item)
            if kept is not item:
                kept["claims"] = list(dict.fromkeys(kept["claims"] + item["claims"]))
        raw_results = list(merged.values())
    _log_search("grounding", query, started, ended=ended, results=raw_results)
    return raw_results


def _claims_snippet(claims: list[str], settings: Settings) -> tuple[list[str], str]:
    """출처가 뒷받침한 서술들 → (서술별로 자른 서술, 그 출처의 snippet).

    snippet은 **그 출처가 뒷받침한 서술들의 사영**이다. 다대다 관계를 출처별로 사영하면
    공동 근거는 반드시 겹치지만, 이 필드는 공개 출처 DTO의 근거 본문이라 출처마다 자기
    몫을 들고 있어야 한다(공개 DTO는 세션 레지스트리가 아니라 **이 도구 결과**에서
    조립되고, QA 심사관은 source id별 snippet을 읽어 인용을 판정한다 — 여기서 빼면
    웹 인용이 통째로 검증 불가가 된다. 2026-08-04 라이브 실측으로 확인).
    총량도 같은 상한으로 자른다 — 클레임별 truncate만 있으면 클레임 수에 비례해
    snippet이 자라 공개 DTO의 bounded-snippet 계약(상한 = 이 설정값)을 넘는다
    (실측 최대 4,472자/28클레임 — 아직 안 터졌지만 결정론 지뢰라 커밋 전 봉합).
    그라운딩·SearchApi 공용.
    """
    limit = settings.web_search_snippet_max_chars
    clipped = [truncate(claim, limit) for claim in claims]
    return clipped, truncate("\n".join(clipped), limit)


async def _grounded_search(
    planned: list[str], tool_context: ToolContext, settings: Settings
) -> dict:
    """google_search 그라운딩 서브콜 1회로 모든 각도를 검색·종합해 도구 결과를 조립한다.

    그라운딩은 질문에서 스스로 다중 검색 쿼리를 만들어 실행하므로(실측: 질문 1개 → 구글
    쿼리 3개) 각도별 병렬 호출이 필요 없다 — 서브콜 한 번이 각도 전체를 흡수한다.

    이번 턴의 선제 실행(prefetch) 결과가 있으면 서브콜 없이 그것을 서빙한다(턴당 1회).
    서빙 시 searches에는 **실제 검색된 질의(사용자 메시지)**를 정직하게 실어, 모델이 자기
    각도와 다르다고 판단하면 스스로 재검색하게 한다 — 두 번째 호출부터는 항상 정상 경로다.
    """
    raw_results: list[dict] | None = None
    searched_query = " | ".join(planned)
    prefetch_served = False
    prefetch = _take_turn_prefetch()
    if prefetch is not None:
        prefetch_message, prefetch_task = prefetch
        try:
            prefetched = await prefetch_task
        except Exception:  # noqa: BLE001 — 프리페치 실패는 무해: 정상 경로 폴백(로그는 콜백)
            prefetched = None
        if prefetched:
            raw_results = prefetched
            searched_query = prefetch_message
            prefetch_served = True

    if raw_results is None:
        try:
            raw_results = await _grounding_raw_results(
                _grounding_prompt(planned), settings, queries=planned
            )
        except Exception as exc:  # noqa: BLE001 — 도구 계약: 예외를 밖으로 던지지 않는다
            logger.info(
                f"web_search backend=grounding status=error error_type=fetch ({exc!r:.120})"
            )
            return {
                "status": "error",
                "error_type": "fetch",
                "message": "웹 검색(그라운딩)이 실패했습니다",
                "result_count": 0,
            }

    if not raw_results:
        # 근거 출처가 하나도 연결되지 않은 답은 인용 불가 — 빈 성공으로 위장하지 않는다.
        # 일시 오류(fetch)와 구분되는 별도 라벨: 재시도해도 같을 가능성이 큰 상태다.
        logger.info("web_search backend=grounding status=error error_type=empty_grounding")
        return {
            "status": "error",
            "error_type": "empty_grounding",
            "message": "웹 검색 결과에 인용 가능한 출처가 없습니다",
            "result_count": 0,
        }

    checked_at = now_checked_at()
    results: list[dict] = []
    # 서술 → 그 서술을 함께 뒷받침한 출처 id들(등장 순서 보존). 같은 서술이 여러 출처에
    # 걸리는 것이 API의 정상 형태이므로(_grounding_response_to_results 주석), 서술을 읽는
    # 쪽에는 **한 번만** 싣고 뒷받침한 출처를 묶어 보여준다. results의 출처별 snippet만
    # 있으면 같은 문장이 출처 수만큼 복제돼 서로 독립인 확증처럼 읽힌다 — 두 표현은
    # 소비자가 다르다(results.snippet=공개 DTO의 출처별 근거, evidence=종합·인용 재료).
    evidence: dict[str, list[int]] = {}
    # 출처 등록은 순차 루프(단일 state, source_id 유일·단조 — 기존 규약).
    for item in raw_results:
        claims, snippet = _claims_snippet(item["claims"], settings)
        source_id = register_source(
            tool_context.state,
            title=item["title"],
            url=item["url"],
            source_type="web",
            snippet=snippet,
            checked_at=checked_at,
            invocation_id=getattr(tool_context, "invocation_id", None),
        )
        results.append(
            {
                "source_id": source_id,
                "cite_as": cite_marker(source_id),
                "type": "web",
                "title": item["title"],
                "url": item["url"],
                "snippet": snippet,
                "checked_at": checked_at,
            }
        )
        for claim in claims:
            evidence.setdefault(claim, []).append(source_id)

    logger.info(
        f"web_search backend=grounding queries={len(planned)} status=ok "
        f"results={len(results)} prefetch={'served' if prefetch_served else 'none'}"
    )
    # searches는 **실제 관측 단위**로만 보고한다: 그라운딩은 서브콜 1회가 전 각도를
    # 흡수하므로 그 호출 1건이 유일한 관측이다. 각도별 성공을 제조하거나(빈 성공 위장)
    # 결과를 각도에 거짓 귀속(queries)하지 않는다 — 어느 각도가 어느 출처를 낳았는지는
    # 이 백엔드에서 관측 불가한 값이다. 프리페치가 서빙됐다면 query에는 실제 검색된
    # 질의(사용자 메시지)가 실린다(제조 금지 — 모델의 재검색 판단 재료).
    return {
        "status": "ok",
        "results": results,
        "evidence": [{"text": text, "source_ids": ids} for text, ids in evidence.items()],
        "searches": [{"query": searched_query, "status": "ok"}],
        "checked_at": checked_at,
        "result_count": len(results),
    }


def get_client(settings: Settings) -> httpx.AsyncClient:
    """외부 API 호출용 공유 httpx 클라이언트 싱글턴을 반환한다(최초 호출 시 생성)."""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=settings.web_search_timeout_s)
    return _shared_client


def _pool_stats(client: httpx.AsyncClient) -> str:
    """교체 로그용 풀 상태(연결 수·idle 수). 갇힌 연결이 원인이면 conns≈한도·idle=0으로 보인다.

    httpx 사설 속성(_transport._pool)에 기대므로 로그 보강 전용이고, 없거나 바뀌면 빈 문자열.
    """
    try:
        conns = client._transport._pool.connections  # type: ignore[attr-defined]
        return f" conns={len(conns)} idle={sum(c.is_idle() for c in conns)}"
    except Exception:  # noqa: BLE001 — 진단 보강 실패는 무해
        return ""


def discard_client_on_pool_timeout(
    exc: Exception, client: httpx.AsyncClient, settings: Settings
) -> None:
    """PoolTimeout이면 공유 클라이언트를 버려 다음 호출이 새로 만들게 한다(자가 회복).

    취소로 풀에 갇힌 연결은 저절로 풀리지 않아 재시작 전까지 매 요청이 실패한다. 진행 중
    요청을 깨지 않도록 즉시 닫지 않고 web_client_retire_delay_s 뒤에 닫는다 — 풀 aclose는
    상태 판정 없이 갇힌 연결까지 닫는다. 실패한 그 클라이언트일 때만 버린다 — 옛 풀의
    뒤늦은 실패가 새 클라이언트를 버리지 않게.
    """
    global _shared_client
    if not (isinstance(exc, httpx.PoolTimeout) and _shared_client is client):
        return
    logger.warning(
        f"web shared httpx client pool timeout → replacing{_pool_stats(client)} exc={exc!r:.120}"
    )
    _shared_client = None
    delay = settings.web_client_retire_delay_s

    async def _close_later() -> None:
        try:
            await asyncio.sleep(delay)
        finally:  # 셧다운 훅의 취소로 깨어나도 닫는다
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 — 정리 실패는 무해
                pass

    task = asyncio.get_running_loop().create_task(_close_later())
    _retire_tasks.add(task)
    task.add_done_callback(_retire_tasks.discard)


async def aclose_shared_client() -> None:
    """웹 검색·열람 공유 클라이언트(교체로 은퇴 대기 중인 것 포함)를 정리한다(shutdown 훅용)."""
    global _shared_client
    tasks = list(_retire_tasks)
    if tasks:
        await asyncio.sleep(0)  # 막 만든 태스크도 sleep에 진입시켜 취소 시 finally가 돌게 한다
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


def _record_request_usage(component: str, model: str) -> None:
    """요청 단위 과금 검색 API의 usage_log 1행(토큰 0의 측정 행, 검색 1건).

    2xx면 본문 검증 결과와 무관하게 요청 1건이 과금된다(4xx·5xx·전송 오류는 과금 없음 —
    호출부가 raise_for_status 뒤에만 부른다). 퍼플렉시티·SearchApi 공용.
    """
    record_usage(
        component,
        genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=0, candidates_token_count=0, total_token_count=0,
            cached_content_token_count=0,
        ),
        model=model, search_queries=1,
    )


async def search_raw(
    query: str, settings: Settings, domain_filters: list[str] | None = None,
    *, max_results: int | None = None,
) -> dict:
    """컨텍스트·출처 등록 없이 원시 검색과 URL·본문·날짜 정규화를 수행한다."""
    if not settings.perplexity_api_key:
        return {
            "query": query, "status": "error", "error_type": "not_configured",
            "message": "웹 검색이 설정되지 않았습니다",
        }
    try:
        domain_filters = _normalize_domain_filters(domain_filters)
    except ValueError as exc:
        return {
            "query": query, "status": "error", "error_type": "invalid_domains",
            "message": str(exc),
        }
    client = get_client(settings)
    headers = {"Authorization": f"Bearer {settings.perplexity_api_key}"}
    # owner 어드민 런타임 값(model_settings.perplexity_search_type), 없거나 읽기 실패면 config.
    search_type = (await current_models()).perplexity_search_type
    payload = {
        "query": query,
        "max_results": settings.web_search_max_results if max_results is None else max_results,
        "max_tokens_per_page": settings.web_search_max_tokens_per_page,
        "max_tokens": settings.web_search_max_tokens,
        "search_type": search_type,
    }
    if domain_filters:
        payload["search_domain_filter"] = domain_filters
    started = time.monotonic()
    try:
        response = await client.post(
            settings.perplexity_search_url, json=payload, headers=headers
        )
        response.raise_for_status()
        _record_request_usage("web_perplexity", (
            settings.perplexity_search_fast_model if search_type == "fast"
            else settings.perplexity_search_model
        ))
        data = response.json()
        if not isinstance(data, dict):
            # 유효 JSON이지만 객체가 아닌 본문(배열·null·스칼라)이면 data.get()이
            # AttributeError로 도구 밖 탈출한다 — 여기서 막아 fetch 에러로 처리.
            raise ValueError(f"응답이 JSON 객체가 아닙니다: {type(data).__name__}")
        raw = data.get("results")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ValueError(f"results가 목록이 아닙니다: {type(raw).__name__}")
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(
                    f"results[{index}]가 객체가 아닙니다: {type(item).__name__}"
                )
            url = item.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ValueError(f"results[{index}].url이 유효한 문자열이 아닙니다")
            for field in ("title", "snippet", "body", "last_updated", "date"):
                value = item.get(field)
                if value is not None and not isinstance(value, str):
                    raise ValueError(
                        f"results[{index}].{field}가 문자열이 아닙니다: "
                        f"{type(value).__name__}"
                    )
    except (httpx.HTTPError, ValueError) as exc:
        # httpx.HTTPError: 타임아웃·전송 오류·raise_for_status의 HTTPStatusError 포함.
        # ValueError: 응답이 JSON이 아니거나(response.json()) JSON 객체가 아닐 때.
        # 예외 repr은 status 앞에 둔다 — 감시기 정규식이 줄 끝(error_type=fetch$)을 묶는다.
        logger.info(f"web_search query={query!r} exc={exc!r:.120} status=error error_type=fetch")
        _log_search(
            "perplexity", query, started, exc=exc, search_type=search_type,
            domain_filters=domain_filters,
        )
        discard_client_on_pool_timeout(exc, client, settings)
        return {
            "query": query,
            "status": "error",
            "error_type": "fetch",
            "message": f"웹 검색 요청에 실패했습니다: {exc}",
        }
    _log_search(
        "perplexity", query, started, results=raw, search_type=search_type,
        domain_filters=domain_filters,
    )
    normalized = []
    for item in raw:
        url = item["url"]
        if not _url_in_domain_scope(url, domain_filters):
            continue
        content = next((
            value for value in (item.get("snippet"), item.get("body"))
            if isinstance(value, str) and value.strip()
        ), None)
        if content is None:
            continue
        normalized.append({
            "url": url, "title": item.get("title") or url,
            "snippet": truncate(content, settings.web_search_snippet_max_chars),
            "date": item.get("date"), "last_updated": item.get("last_updated"),
        })
    return {"query": query, "status": "ok", "raw": normalized, "raw_result_count": len(raw)}


def _ai_mode_claims(
    blocks: list, claims: dict[int, list[str]], inherited: list | None = None
) -> None:
    """AI 모드 text_blocks의 answer를 reference_indexes별 서술로 모은다(중첩 items 포함).

    출처가 연결되지 않은 블록(머리말·맺음말)은 버린다: 그라운딩 분해와 같은 규칙으로, 벤더
    종합문을 출처 없이 실으면 마커 세탁 경로가 된다. 목록의 refs(inherited)는 **자식이 하나도
    자기 refs를 달지 않고 목록 refs가 정확히 1개인 목록**에만 자식의 출처가 된다 — 출처 하나에
    목록 단위로 단 것이다(종가·시가·고가 목록, 실측 2026-10-30: 물려주지 않으면 숫자가 전부
    빠진다). 목록 refs가 여럿이면 어느 항목이 어느 출처 몫인지 알 수 없어 물려주면 오귀속이다
    (적대 검증 2026-10-30: 뉴스 목록 refs 4개 → 항목 2개가 출처 4개를 모두 받아 4/8 오귀속).
    자식 일부가 refs를 달면 부모 refs는 자식 합집합이라 독립 근거가 아니고, 물려주면 출처 없는
    자식 문장이 남의 출처를 얻는다(적대 검증 2026-10-29).
    """
    for block in blocks:
        if not isinstance(block, dict):
            continue
        refs = block.get("reference_indexes") or inherited or []
        answer = block.get("answer")
        if isinstance(answer, str) and answer.strip():
            for index in refs:
                claims.setdefault(index, []).append(answer.strip())
        items = block.get("items") or []
        child_cited = any(
            isinstance(item, dict) and item.get("reference_indexes") for item in items
        )
        _ai_mode_claims(items, claims, refs if not child_cited and len(refs) == 1 else None)


def _ai_mode_to_raw(data: dict) -> list[dict]:
    """AI 모드 응답 → 출처별 {url, title, claims} 원시 재료(서술이 실린 출처만, 링크 순서).

    같은 링크가 여러 index로 오면 출처 하나로 합친다. goto 리다이렉트 링크는 풀지 않는다.
    """
    claims_by_index: dict[int, list[str]] = {}
    _ai_mode_claims(data.get("text_blocks") or [], claims_by_index)
    merged: dict[str, dict] = {}
    for position, link in enumerate(data.get("reference_links") or []):
        if not isinstance(link, dict):
            continue
        url = link.get("link")
        claims = claims_by_index.get(link.get("index", position))
        if not (isinstance(url, str) and claims and _url_in_domain_scope(url, [])):
            continue
        title = link.get("title")
        if not (isinstance(title, str) and title.strip()):
            title = url
        item = merged.setdefault(url, {"url": url, "title": title, "claims": []})
        item["claims"] = list(dict.fromkeys(item["claims"] + claims))  # 순서 보존 중복 제거
    return list(merged.values())


# SearchApi 엔진. 파서(_ai_mode_to_raw)가 이 엔진의 응답 형식(text_blocks·reference_links)
# 전용이라 설정 레버가 아니다 — 다른 엔진이면 항상 empty_results가 된다. 바꾸지 말 것.
_SEARCHAPI_ENGINE = "google_ai_mode"
# SearchApi에 보내는 질의에서 뺄 site: 연산자 토큰(`site:x`, `-site:x`). AI 모드는 도메인 필터를
# 지원하지 않아(domains 포함 필터를 무시하는 것과 같은 근거) 연산자가 질의만 망친다 — 10-30
# 실측(site: 포함 실제 질의 16개): 원문 성공 11/16·중앙 12.3s → 제거 후 16/16·7.5s.
_SITE_OPERATOR_RE = re.compile(r"(?<!\S)-?site:\S+", re.IGNORECASE)


async def searchapi_raw(
    query: str, settings: Settings, domain_filters: list[str] | None = None
) -> dict:
    """SearchApi(Google AI 모드) 요청 1건 → search_raw 모양의 원시 결과(출처별 claims).

    결과 0건은 empty_results 오류다(빈 성공 위장 금지). 429는 쿨다운 표시를 걸고, 표시 중에는
    요청하지 않고 즉시 실패한다(_quota_cooldown_until). domain_filters(정규화된 목록)는 제외
    필터만 사후 적용하고, 포함 필터는 무시한 채 결과에 domains_ignored로 밝힌다(근거는 config
    web_search_backend 주석).
    """
    # 로그·searches의 query는 모델이 보낸 원문 그대로(정직 표기), 제거 후 비면 원문을 보낸다.
    sent = " ".join(_SITE_OPERATOR_RE.sub(" ", query).split()) or query

    def fail(error_type: str, message: str, cause: str = "") -> dict:
        detail = f" ({cause:.120})" if cause else ""
        sent_note = f" sent={sent!r}" if sent != query else ""
        logger.info(
            f"web_search backend=searchapi query={query!r}{sent_note} results=0 status=error "
            f"error_type={error_type}{detail}"
        )
        return {"query": query, "status": "error", "error_type": error_type, "message": message}

    if not settings.searchapi_api_key:
        return fail("not_configured", "웹 검색(SearchApi)이 설정되지 않았습니다")
    if _quota_exhausted("searchapi"):
        return fail("fetch", "웹 검색(SearchApi) 쿼터 쿨다운 중입니다", "quota cooldown")
    client = get_client(settings)
    started = time.monotonic()
    try:
        # wall-clock 시한 — httpx 타임아웃은 위상별이라 청크가 느리게 이어지면 총 지연을 못 막는다.
        response = await asyncio.wait_for(
            client.get(
                settings.searchapi_base_url,
                params={
                    "engine": _SEARCHAPI_ENGINE, "q": sent,
                    "gl": settings.searchapi_gl, "hl": settings.searchapi_hl,
                },
                headers={"Authorization": f"Bearer {settings.searchapi_api_key}"},
                timeout=settings.searchapi_timeout_s,
            ),
            settings.searchapi_timeout_s,
        )
        if response.status_code == 429:
            _mark_quota_exhausted("searchapi", settings)
        response.raise_for_status()
        _record_request_usage("web_searchapi", settings.searchapi_model)
        _clear_quota_mark("searchapi")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"응답이 JSON 객체가 아닙니다: {type(data).__name__}")
        if data.get("error"):
            raise ValueError(f"응답 error: {data['error']}")
        raw = _ai_mode_to_raw(data)
        raw_result_count = len(data.get("reference_links") or [])
    except Exception as exc:  # noqa: BLE001 — 도구 계약: 200의 이상 형식(타입·깊은 중첩)도 밖으로 던지지 않는다
        _log_search("searchapi", sent, started, exc=exc, domain_filters=domain_filters)
        discard_client_on_pool_timeout(exc, client, settings)
        return fail("fetch", f"웹 검색 요청에 실패했습니다: {exc!r:.120}", repr(exc))
    _log_search(
        "searchapi", sent, started, results=raw, response=data, domain_filters=domain_filters
    )
    domain_filters = domain_filters or []
    if domain_filters and domain_filters[0].startswith("-"):
        raw = [item for item in raw if _url_in_domain_scope(item["url"], domain_filters)]
    if not raw:
        return fail("empty_results", "웹 검색 결과에 인용 가능한 출처가 없습니다")
    outcome = {"query": query, "status": "ok", "raw": raw, "raw_result_count": raw_result_count}
    if domain_filters and not domain_filters[0].startswith("-"):
        outcome["domains_ignored"] = domain_filters
    return outcome


# 실패 전환 순서(제어 흐름 — config 값이 아니다). 실제 체인은 web_search_backend 위치부터 자른
# 것이다: grounding → perplexity / searchapi → grounding → perplexity / perplexity 단독.
_BACKEND_CHAIN = ("searchapi", "grounding", "perplexity")


async def web_search(
    queries: list[str],
    tool_context: ToolContext,
    domains: list[str] | None = None,
) -> dict:
    """웹을 검색해 최신 사실을 출처별 근거와 함께 가져온다(실시간 값 포함).

    확인된 사실은 evidence에 서술 단위로 담기고 각 서술에는 그것을 뒷받침한 출처 source_ids가
    붙는다 — 여러 출처가 한 서술을 함께 뒷받침하면 그 서술은 evidence에 한 번만 실리고 함께
    인용한다([1][2]).

    복합·시의성·비교 질문은 서로 독립적인 검색 각도를 queries에 함께 담을 수 있다. 단순
    질문은 하나의 원소만 전달한다. 같은 목적의 표현만 바꾼 중복 검색은 만들지 않고 서로
    다른 요구나 충돌 확인에 필요한 각도만 사용한다.

    Args:
        queries: 검색 각도 리스트. 각 원소는 독립적으로 확인할 질문이나 키워드다. 상한을 넘는
            각도는 dropped_queries로 알리고 처리에서 제외한다.
        domains: 사용자가 구체적으로 지정한 사이트 hostname의 선택적 목록. 포함 범위는
            `["news.example", "wire.example"]`, 특정 사이트 제외는 `["-example.com"]`처럼
            전달하며 포함과 제외를 섞지 않는다. 출처 범주를 임의 hostname 목록으로 바꾸지 않는다.
            도메인을 지정한 검색은 원시 검색 경로로 처리되며, 그 경로의 snippet은 검색엔진
            캐시라 시변 수치의 현재 값 근거로 쓰지 않는다.

    Returns:
        성공 시 status="ok"와 results 목록(각 결과에 인용용 source_id·title·url·snippet),
        evidence(확인된 서술 1건과 그것을 뒷받침한 source_ids — 그라운딩 경로만),
        searches(실제 수행된 검색 호출 단위의 성공/실패 — 그라운딩 경로는 서브콜 1건),
        검색 시각 checked_at을 담은 dict. 성공·실패 모두 result_count를 함께 담는다.
        실패 시 status="error"와 error_type("not_configured"|"empty_query"|"fetch"|
        "empty_grounding"), message, result_count=0을 담은 dict.
    """
    settings = get_settings()

    try:
        domain_filters = _normalize_domain_filters(domains)
    except ValueError as exc:
        logger.info("web_search status=error error_type=invalid_domains")
        return {
            "status": "error",
            "error_type": "invalid_domains",
            "message": str(exc),
            "result_count": 0,
        }

    # 각도 계획(관용 변환·중복 제거·상한 cap)은 yes24_search와 공용 헬퍼를 쓴다.
    planned, dropped_queries = plan_queries(queries, settings.web_search_max_queries)

    if not planned:
        # 유효한 검색 각도가 하나도 없다 — 빈 성공으로 위장하지 않고 명시적 실패.
        logger.info("web_search status=error error_type=empty_query")
        return {
            "status": "error",
            "error_type": "empty_query",
            "message": "검색할 유효한 검색어가 없습니다",
            "result_count": 0,
        }

    # 백엔드 체인(_BACKEND_CHAIN을 주 백엔드 — 런타임 설정 > config — 부터 자른 것): 앞에서부터
    # 시도해 첫 성공을 돌려준다(2026-10-19 상용 429 장애의 그라운딩 → 퍼플렉시티 전환을 일반화).
    # 건너뛰는 경우는
    # 둘이다. ① domains 지정 호출의 그라운딩: 구조적 필터가 없다 — 능력 기반 라우팅(콘텐츠 분기
    # 아님). SearchApi도 필터를 지키지 않지만 domains를 무시하고 받는다(config 근거 주석). ② 전환
    # 대상인데 키가 없는 백엔드: 전환할 곳이 없으므로 앞 백엔드의 오류를 그대로 돌려준다 — 키
    # 유무가 곧 전환 가용성이다. 첫 시도는 키가 없어도 실행해 not_configured를 정직하게 돌려준다.
    # 쿼터 소진 쿨다운은 각 백엔드 입구가 즉시 실패로 처리한다.
    runners = {
        "searchapi": lambda: _raw_search(
            planned, tool_context, settings, lambda q: searchapi_raw(q, settings, domain_filters),
            backend="searchapi", dropped=len(dropped_queries),
        ),
        "grounding": lambda: _grounded_search(planned, tool_context, settings),
        "perplexity": lambda: _raw_search(
            planned, tool_context, settings, lambda q: search_raw(q, settings, domain_filters),
            backend="perplexity", dropped=len(dropped_queries),
        ),
    }
    chain = _BACKEND_CHAIN[_BACKEND_CHAIN.index(await _primary_backend()):]
    outcome: dict | None = None
    failed: list[dict] = []  # 전환 전에 실패한 백엔드
    for name in chain:
        if name == "grounding" and domain_filters:
            continue
        if outcome is not None:
            if not web_backend_configured(settings, name):
                continue
            logger.info(
                f"web_search backend={failed[-1]['backend']} fallback={name} "
                f"reason={failed[-1]['error_type']}"
            )
        outcome = await runners[name]()
        if outcome["status"] == "ok":
            if failed:
                # 전환이 일어난 호출만 searches에 백엔드를 밝힌다(실패한 앞 백엔드 + 처리한 백엔드).
                outcome["searches"] = failed + [
                    {**entry, "backend": name} for entry in outcome["searches"]
                ]
            break
        failed.append(
            {**angle_error_summary(" | ".join(planned), outcome["error_type"]), "backend": name}
        )
    if outcome["status"] != "ok" and len(failed) > 1:
        outcome["searches"] = failed

    if dropped_queries and outcome["status"] == "ok":
        # 상한 초과 각도를 조용히 버리지 않는다(fail-loud) — 두 경로 공통 규약(가법 필드:
        # 드롭이 없으면 반환 형태에 이 키가 없다).
        outcome["dropped_count"] = len(dropped_queries)
        outcome["dropped_queries"] = dropped_queries
        outcome["message"] = dropped_queries_message(
            settings.web_search_max_queries, len(dropped_queries)
        )
    return outcome


async def _raw_search(
    planned: list[str],
    tool_context: ToolContext,
    settings: Settings,
    provider: Callable[[str], Awaitable[dict]],
    *,
    backend: str,
    dropped: int,
) -> dict:
    """원시 검색 경로 — 각도별 병렬 검색 후 출처를 순차 등록해 도구 결과를 조립한다.

    provider는 질의 하나를 받아 search_raw 모양({query,status,raw,raw_result_count})을 돌려주는
    벤더 함수다(퍼플렉시티 search_raw·SearchApi searchapi_raw). 원시 결과가 claims를 들고
    오면(SearchApi) 그라운딩과 같은 evidence 계약(서술 → 뒷받침한 source_ids, 서술당 1항목)과
    서술 사영 snippet을 따르고, claims가 없는 백엔드(퍼플렉시티)는 결과에 evidence 키가 없다.
    dropped는 로그용 카운트이고, dropped_* 필드는 호출부가 붙인다.
    """
    searched = await asyncio.gather(*(provider(q) for q in planned))

    checked_at = now_checked_at()

    # 1단계: url별로 모은다(각도 간 중복제거·교차확증 병합). 같은 url이 여러 각도에서 걸리면
    # 출처 하나에 어느 각도에서 나왔는지(queries)와 서술(claims)을 합친다.
    merged: dict[str, dict] = {}
    searches: list[dict] = []  # 각도별 성공/실패 요약(부분 실패 fail-loud)
    for outcome in searched:
        query = outcome["query"]
        if outcome["status"] == "error":
            searches.append(angle_error_summary(query, outcome["error_type"]))
            continue
        for item in outcome["raw"]:
            kept = merged.get(item["url"])
            if kept is None:
                merged[item["url"]] = {**item, "queries": [query]}
                continue
            if query not in kept["queries"]:
                kept["queries"].append(query)
            if "claims" in item:
                kept["claims"] = list(dict.fromkeys(kept["claims"] + item["claims"]))
        searches.append(
            {
                "query": query,
                "status": "ok",
                "result_count": len(outcome["raw"]),
                "raw_result_count": outcome["raw_result_count"],
                # 무시한 포함 필터(SearchApi) — 가법 필드, 정직 표기.
                **({"domains_ignored": outcome["domains_ignored"]}
                   if "domains_ignored" in outcome else {}),
            }
        )

    # 2단계: 출처 등록은 순차 루프(단일 state, source_id 유일·단조 — 기존 규약).
    results: list[dict] = []
    evidence: dict[str, list[int]] = {}
    for item in merged.values():
        claims: list[str] = []
        snippet = item.get("snippet")
        if "claims" in item:
            claims, snippet = _claims_snippet(item["claims"], settings)
        published_at = item.get("date")
        last_updated = item.get("last_updated")
        source_id = register_source(
            tool_context.state,
            title=item["title"],
            url=item["url"],
            source_type="web",
            snippet=snippet,
            checked_at=checked_at,
            meta={"published_at": published_at, "last_updated": last_updated},
            invocation_id=getattr(tool_context, "invocation_id", None),
        )
        results.append(
            {
                "source_id": source_id,
                # 원시 경로도 복사용 마커 동봉 — 그라운딩 경로와 같은 계약
                # (인용 가능한 결과는 모두 자기 마커를 들고 온다). 감사 지적 갭 봉합.
                "cite_as": cite_marker(source_id),
                "type": "web",
                "title": item["title"],
                "url": item["url"],
                "snippet": snippet,
                "checked_at": checked_at,
                "published_at": published_at,
                "last_updated": last_updated,
                "queries": item["queries"],
            }
        )
        for claim in claims:
            evidence.setdefault(claim, []).append(source_id)

    # 퍼플렉시티 줄은 접두를 바꾸지 않는다 — 운영 감시기가 "web_search queries="를 퍼플렉시티
    # 요청 수로 합산한다(ops/monitor config perplexity_requests).
    label = "web_search" if backend == "perplexity" else f"web_search backend={backend}"
    ok_count = sum(1 for s in searches if s["status"] == "ok")
    if ok_count == 0:
        # 모든 각도가 실패 — 단일 각도 실패의 기존 계약(status=error·error_type=fetch·
        # result_count=0)을 그대로 유지해, 에이전트가 "못 찾음"이 아니라 일시 오류로 처리하게 한다.
        logger.info(f"{label} queries={len(planned)} status=error error_type=fetch")
        return {
            "status": "error",
            "error_type": searched[0]["error_type"],
            "message": searched[0]["message"],
            "result_count": 0,
        }

    logger.info(
        f"{label} queries={len(planned)} angles_ok={ok_count} results={len(results)} "
        f"dropped={dropped}"
    )
    response = {
        "status": "ok",
        "queries": planned,
        "results": results,
        "searches": searches,
        "checked_at": checked_at,
        "result_count": len(results),
    }
    if evidence:
        response["evidence"] = [{"text": text, "source_ids": ids} for text, ids in evidence.items()]
    return response
