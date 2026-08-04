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
from functools import lru_cache
from urllib.parse import urlsplit

import httpx
from google import genai
from google.adk.tools import ToolContext
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from yes24_agent.config import Settings, get_settings
from yes24_agent.sources import now_checked_at, register_source
from yes24_agent.tools._planning import (
    angle_error_summary,
    dropped_queries_message,
    plan_queries,
)
from yes24_agent.tools.yes24_fetch import truncate

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


@lru_cache(maxsize=1)
def _grounding_client(timeout_ms: int) -> genai.Client:
    """그라운딩 서브콜 전용 genai 클라이언트(프로세스당 1개, thought_translation._client 관례).

    빌트인 google_search는 함수 선언과 같은 요청에 혼용이 금지되므로(400 실측), 이 도구
    내부의 **별도 요청**으로 실행한다 — 에이전트 루프의 커스텀 도구 7종과 충돌하지 않는다.
    """
    return genai.Client(http_options=genai_types.HttpOptions(timeout=timeout_ms))


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
    response = await asyncio.wait_for(
        _grounding_client(int(settings.web_grounding_timeout_s * 1000))
        .aio.models.generate_content(
            model=settings.web_grounding_model,
            contents=message,
            config=genai_types.GenerateContentConfig(
                system_instruction=_HINT_INSTRUCTION,
                temperature=0,
                response_mime_type="application/json",
                response_schema={
                    "type": "OBJECT",
                    "properties": {"needs_web": {"type": "BOOLEAN"}},
                    "required": ["needs_web"],
                },
            ),
        ),
        timeout=settings.web_prefetch_hint_timeout_s,
    )
    return json.loads(response.text or "{}").get("needs_web") is True


async def _prefetch_pipeline(message: str, settings: Settings) -> list[dict] | None:
    """힌트 판정 → (필요 시) 그라운딩 서브콜. 서빙 불가 상태는 None(정상 경로 폴백)."""
    if not await _prefetch_hint_needs_web(message, settings):
        logger.info("web_search prefetch hint=no_web")
        return None
    raw_results = await _grounding_raw_results(_grounding_prompt([message]), settings)
    logger.info(f"web_search prefetch ready results={len(raw_results)}")
    # 빈 근거는 서빙하지 않는다 — 정상 경로가 자기 재시도 규약대로 다시 시도하게 둔다.
    return raw_results or None


def _log_prefetch_outcome(task: asyncio.Task) -> None:
    """미소비 프리페치의 예외를 회수한다(경고 소음 방지) — 실패는 무해하므로 기록만 한다."""
    if not task.cancelled() and task.exception() is not None:
        logger.info(f"web_search prefetch 실패(정상 경로 폴백): {task.exception()!r:.120}")


def start_web_prefetch(message: str) -> None:
    """이번 턴의 웹 프리페치를 시작한다(러너가 턴 시작에 호출, 실패·미소비 무해)."""
    settings = get_settings()
    if not (settings.web_prefetch_enabled and settings.web_search_backend == "grounding"):
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
    candidates = getattr(response, "candidates", None) or []
    metadata = getattr(candidates[0], "grounding_metadata", None) if candidates else None
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


async def _fetch_page_meta(url: str, settings: Settings) -> tuple[str | None, str | None]:
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
        client = _get_client(settings)
        buf = b""
        async with client.stream(
            "GET", url, follow_redirects=True,
            timeout=settings.web_title_fetch_timeout_s,
        ) as response:
            if response.status_code != 200:
                return None, None
            final_url = str(response.url)
            async for chunk in response.aiter_bytes():
                buf += chunk
                if b"</title>" in buf.lower() or len(buf) > 65536:
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
        return final_url, title[:120] or None
    except Exception:  # noqa: BLE001 — 표시 보강 전용: 어떤 실패도 원래 값 유지
        return None, None


async def _grounding_raw_results(prompt: str, settings: Settings) -> list[dict]:
    """그라운딩 서브콜을 실행해 출처별 원시 재료를 돌려준다(빈 목록 가능, 종단 실패는 raise).

    일시 실패 2종을 1회 재시도로 흡수한다(Yes24Client의 전송 오류 재시도와 동일 철학):
    ① 벤더 5xx·타임아웃(ServerError — 504가 재시도에서 정상, 라이브 실측) ② **빈 근거**
    — 그라운딩 종합은 확률적이라 같은 질의가 다음 시도에서 출처를 붙인다(영화 질의 실측:
    E2E 1회 empty → 직호출 3/3 성공). 4xx(ClientError)는 반복해도 같으므로 즉시 실패.
    """
    last_error: Exception | None = None
    raw_results: list[dict] = []
    for _ in range(2):
        try:
            response = await asyncio.to_thread(
                _grounding_client(int(settings.web_grounding_timeout_s * 1000))
                .models.generate_content,
                model=settings.web_grounding_model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())]
                ),
            )
            raw_results = _grounding_response_to_results(response)
            last_error = None
            if raw_results:
                break
        except genai_errors.ServerError as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001 — 비일시 오류: 재시도 없이 종단 실패로 올린다
            last_error = exc
            break
    if last_error is not None:
        raise last_error
    if raw_results:
        # 최종 URL·제목 보강(병렬·실패 무해, 요청 1회로 둘 다). 프리페치 파이프라인 안에서도
        # 실행되므로 대부분의 지연이 모델 사고 시간에 숨는다.
        metas = await asyncio.gather(
            *(_fetch_page_meta(item["url"], settings) for item in raw_results),
            return_exceptions=True,
        )
        for item, meta in zip(raw_results, metas):
            if isinstance(meta, BaseException):
                continue
            final_url, title = meta
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
    return raw_results


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
            raw_results = await _grounding_raw_results(_grounding_prompt(planned), settings)
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
        claims = [
            truncate(claim, settings.web_search_snippet_max_chars) for claim in item["claims"]
        ]
        # snippet은 **그 출처가 뒷받침한 서술들의 사영**이다. 다대다 관계를 출처별로 사영하면
        # 공동 근거는 반드시 겹치지만, 이 필드는 공개 출처 DTO의 근거 본문이라 출처마다 자기
        # 몫을 들고 있어야 한다(공개 DTO는 세션 레지스트리가 아니라 **이 도구 결과**에서
        # 조립되고, QA 심사관은 source id별 snippet을 읽어 인용을 판정한다 — 여기서 빼면
        # 웹 인용이 통째로 검증 불가가 된다. 2026-08-04 라이브 실측으로 확인).
        # 총량도 같은 상한으로 자른다 — 클레임별 truncate만 있으면 클레임 수에 비례해
        # snippet이 자라 공개 DTO의 bounded-snippet 계약(상한 = 이 설정값)을 넘는다
        # (실측 최대 4,472자/28클레임 — 아직 안 터졌지만 결정론 지뢰라 커밋 전 봉합).
        snippet = truncate("\n".join(claims), settings.web_search_snippet_max_chars)
        source_id = register_source(
            tool_context.state,
            title=item["title"],
            url=item["url"],
            source_type="web",
            snippet=snippet,
            checked_at=checked_at,
            meta={"published_at": None, "last_updated": None},
        )
        results.append(
            {
                "source_id": source_id,
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


def _get_client(settings: Settings) -> httpx.AsyncClient:
    """외부 API 호출용 공유 httpx 클라이언트 싱글턴을 반환한다(최초 호출 시 생성)."""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=settings.web_search_timeout_s)
    return _shared_client


async def aclose_shared_client() -> None:
    """웹 검색·열람 공유 클라이언트를 정리한다(서버 shutdown 훅용). 미생성 상태면 무동작."""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


async def _search_one(
    query: str,
    client: httpx.AsyncClient,
    headers: dict,
    settings: Settings,
    domain_filters: list[str],
) -> dict:
    """한 검색 각도(query)로 Perplexity /search를 호출해 **원시 결과만** 돌려준다(등록 없음).

    출처 등록(register_source)은 여기서 하지 않는다 — 여러 각도를 gather로 동시 실행할 때
    등록을 병렬로 돌리면 source_id 부여에 레이스가 생기므로, 네트워크만 여기서 하고 등록은
    호출부의 순차 루프에서 처리한다(레이스 0). 예상된 오류(전송·HTTP·JSON)만 잡아 구조화된
    error dict로 반환하고, 예상 밖 예외는 삼키지 않고 그대로 올려보낸다(fail-loud).

    반환: {"query", "status": "ok", "raw": [원시 item...]} 또는
          {"query", "status": "error", "error_type": "fetch", "message"}.
    """
    payload = {
        "query": query,
        "max_results": settings.web_search_max_results,
        "max_tokens_per_page": settings.web_search_max_tokens_per_page,
        "max_tokens": settings.web_search_max_tokens,
    }
    if domain_filters:
        payload["search_domain_filter"] = domain_filters
    try:
        response = await client.post(
            settings.perplexity_search_url, json=payload, headers=headers
        )
        response.raise_for_status()
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
        logger.info("web_search query=%r status=error error_type=fetch", query)
        return {
            "query": query,
            "status": "error",
            "error_type": "fetch",
            "message": f"웹 검색 요청에 실패했습니다: {exc}",
        }
    return {"query": query, "status": "ok", "raw": raw}


async def web_search(
    queries: list[str],
    tool_context: ToolContext,
    domains: list[str] | None = None,
) -> dict:
    """웹을 검색해 최신 사실을 출처별 근거와 함께 가져온다(실시간 값 포함).

    Yes24로 답할 수 없는 외부·최신 정보(뉴스·스포츠·주가·환율·날씨·시사·인물·상식 등)가
    필요할 때 쓴다. 시시각각 변하는 수치의 현재 값도 이 도구가 신선하게 가져온다. 확인된
    사실은 evidence에 서술 단위로 담기고 각 서술에는 그것을 뒷받침한 출처 source_ids가
    붙는다 — 여러 출처가 한 서술을 함께 뒷받침하면 그 서술은 evidence에 한 번만 실리고
    (결과마다 실리는 snippet에는 겹쳐 보인다) 함께 인용한다([1][2]). 그 재료를 직접 종합해
    각 사실에 출처의 source_id를 [n]으로 인용해 답한다(도메인 지정 검색은 원시 경로라
    evidence 없이 결과마다 페이지 발췌 snippet만 실린다).
    특정 출처의 더 긴 전문이 필요하면 그 url을 web_fetch로 읽는다(리다이렉트 url도 원문으로
    열린다 — 실측). 가격·재고·구매 링크 같은 상품 정보를 얻는 용도가 아니다
    (그것은 Yes24 검색으로). 잡담이나 Yes24로 충분한 질문엔 쓰지 않는다.

    복합·시의성·비교 질문은 서로 독립적인 검색 각도를 queries에 함께 담을 수 있다. 단순
    질문은 하나의 원소만 전달한다. 같은 목적의 표현만 바꾼 중복 검색은 만들지 않고 서로
    다른 요구나 충돌 확인에 필요한 각도만 사용한다. 사용자가 여러 사이트를 함께 근거로
    요구했다면 각 사이트를 실제로 찾는 독립 검색 각도를 두고, 결과가 반환된 사이트만
    사용했다고 말한다.

    Args:
        queries: 검색 각도 리스트. 각 원소는 독립적으로 확인할 질문이나 키워드다. 상한을 넘는
            각도는 dropped_queries로 알리고 처리에서 제외한다. 현재·상대 시점 질문에는 해소한
            절대 날짜를 각 검색어에 포함한다.
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

    # 백엔드 라우팅(config web_search_backend): 그라운딩이 기본. 도메인 필터는 그라운딩에
    # 구조적 필터가 없어 원시(퍼플렉시티) 경로로 처리한다 — 능력 기반 라우팅(콘텐츠 분기 아님).
    if settings.web_search_backend == "grounding" and not domain_filters:
        outcome = await _grounded_search(planned, tool_context, settings)
        if dropped_queries and outcome.get("status") == "ok":
            # 상한 초과 각도를 조용히 버리지 않는다(fail-loud) — 퍼플렉시티 경로와 동일 규약.
            outcome["dropped_count"] = len(dropped_queries)
            outcome["dropped_queries"] = dropped_queries
            outcome["message"] = dropped_queries_message(
                settings.web_search_max_queries, len(dropped_queries)
            )
        return outcome

    if not settings.perplexity_api_key:
        logger.info("web_search status=error error_type=not_configured")
        return {
            "status": "error",
            "error_type": "not_configured",
            "message": "웹 검색이 설정되지 않았습니다",
            "result_count": 0,
        }

    client = _get_client(settings)
    # 퍼플렉시티는 Bearer 헤더 인증(바디 api_key 아님). 헤더는 요청마다 넘겨 공유 클라이언트를
    # 인증 중립으로 유지한다. snippet 콘텐츠 분량은 토큰 예산으로 조절(snippet이 종합 재료).
    headers = {"Authorization": f"Bearer {settings.perplexity_api_key}"}

    # 네트워크(/search POST)만 동시 실행한다(각도별 병렬). 등록은 아래 순차 루프에서 — 레이스 0.
    # _search_one이 예상 오류를 이미 error dict로 삼키므로 예상 밖 예외만 gather 밖으로 올라온다.
    searched = await asyncio.gather(
        *(_search_one(q, client, headers, settings, domain_filters) for q in planned)
    )

    checked_at = now_checked_at()

    results: list[dict] = []
    url_to_index: dict[str, int] = {}  # url → results 인덱스(각도 간 중복제거·교차확증 병합용)
    searches: list[dict] = []  # 각도별 성공/실패 요약(부분 실패 fail-loud)
    for outcome in searched:
        query = outcome["query"]
        if outcome["status"] == "error":
            searches.append(angle_error_summary(query, outcome["error_type"]))
            continue
        matched = 0
        for item in outcome["raw"]:
            url = item.get("url")
            if not _url_in_domain_scope(url, domain_filters):
                continue
            content = next(
                (
                    value
                    for value in (item.get("snippet"), item.get("body"))
                    if isinstance(value, str) and value.strip()
                ),
                None,
            )
            if content is None:
                continue
            # 절단(상한에서 자르고 끝 공백 정리 후 표식)은 세 도구가 같아야 하므로
            # yes24_fetch의 truncate를 공유한다.
            snippet = truncate(content, settings.web_search_snippet_max_chars)
            matched += 1
            existing = url_to_index.get(url)
            if existing is not None:
                # 같은 url이 다른 각도에서도 걸렸다 — 재등록하지 않고 어느 각도에서 나왔는지만
                # 합쳐 교차 확증 신호로 남긴다(source_id 중복 방지).
                if query not in results[existing]["queries"]:
                    results[existing]["queries"].append(query)
                continue
            title = item.get("title") or url
            # snippet 로컬 하드 상한(벤더 토큰 예산 초과분 방어). 등록·반환 모두 절단본으로
            # 통일해 세션 출처와 도구 결과의 snippet이 어긋나지 않게 한다.
            published_at = item.get("date")
            last_updated = item.get("last_updated")
            source_id = register_source(
                tool_context.state,
                title=title,
                url=url,
                source_type="web",
                snippet=snippet,
                checked_at=checked_at,
                meta={"published_at": published_at, "last_updated": last_updated},
            )
            url_to_index[url] = len(results)
            results.append(
                {
                    "source_id": source_id,
                    "type": "web",
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "checked_at": checked_at,
                    "published_at": published_at,
                    "last_updated": last_updated,
                    "queries": [query],
                }
            )
        searches.append(
            {
                "query": query,
                "status": "ok",
                "result_count": matched,
                "raw_result_count": len(outcome["raw"]),
            }
        )

    ok_count = sum(1 for s in searches if s["status"] == "ok")
    if ok_count == 0:
        # 모든 각도가 실패 — 단일 각도 실패의 기존 계약(status=error·error_type=fetch·
        # result_count=0)을 그대로 유지해, 에이전트가 "못 찾음"이 아니라 일시 오류로 처리하게 한다.
        logger.info("web_search queries=%d status=error error_type=fetch", len(planned))
        return {
            "status": "error",
            "error_type": "fetch",
            "message": searched[0]["message"],
            "result_count": 0,
        }

    logger.info(
        "web_search queries=%d angles_ok=%d results=%d dropped=%d",
        len(planned), ok_count, len(results), len(dropped_queries),
    )
    response = {
        "status": "ok",
        "queries": planned,
        "results": results,
        "searches": searches,
        "checked_at": checked_at,
        "result_count": len(results),
    }
    if dropped_queries:
        # 가법 필드: 드롭이 없으면 반환 형태는 단일/다중 각도 모두 이 키가 없다.
        response["dropped_count"] = len(dropped_queries)
        response["dropped_queries"] = dropped_queries
        response["message"] = dropped_queries_message(
            settings.web_search_max_queries, len(dropped_queries)
        )
    return response
