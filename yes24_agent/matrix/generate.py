"""C2 — 16 fan-out 생성(도구 없음, 병렬, 각 열 인용 검증 + 게이트).

공유 풀 하나를 16 RBTI 페르소나가 나눠 쓰며 **선택·프레이밍만** 달리해 생성한다. 각 열은:
1. build_persona_block(code) + render_pool(사실만) → 매트릭스 프롬프트(도구 없음).
2. genai flash generate_content(thinking=0)로 생성.
3. validate_citations로 무효 마커 제거(채팅 postprocess 재사용).
4. 게이트 — evaluate_product_answer(mismap/unsourced) + **assert_pool_confined**(매트릭스 고유):
   풀에 상품 출처가 있어 기존 unsourced가 못 잡는 "풀 밖 책+가격" 홀을, 주장 제목이 풀 후보
   제목 집합에 있는지 대조해 차단한다(product_gate의 title 정규화·_title_supported 재사용).
5. 사유가 있으면 그 셀만 정직 폴백(재검색 안 함 — 비용 가드). build_done_payload로 마감.

16을 케이스로 박지 않는다 — matrix_codes()가 AXIS_ORDER의 축별 허용값을 곱해 16을 파생한다.
생성은 ADK Runner를 쓰지 않고 google.genai async를 직접 fan-out한다(병렬·경량).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types
from google.genai.errors import APIError

from yes24_agent.config import Settings
from yes24_agent.matrix.genai_runtime import get_genai_client
from yes24_agent.matrix.prompt import (
    MATRIX_EMPTY_NOTICE,
    MATRIX_FALLBACK_NOTICE,
    MATRIX_WEB_EMPTY_NOTICE,
    build_matrix_prompt,
)
from yes24_agent.matrix.retrieval import SharedPool
from yes24_agent.postprocess import CitationResult, build_done_payload, validate_citations
from yes24_agent.product_gate import (
    _ASSERTED_TITLE,
    _clean_asserted,
    _is_title_candidate,
    _title_supported,
    evaluate_product_answer,
)
from yes24_agent.rbti.persona import AXIS_ORDER, build_persona_block

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

# 요지 한 줄에서 걷어낼 마크다운/인용 접두. 카드의 "한 줄 요지"용 정리.
_SUMMARY_STRIP = re.compile(r"^[#>*\-\s]+|\[\d+(?:\s*,\s*\d+)*\]")

@dataclass(frozen=True)
class ColumnResult:
    """한 열(RBTI 코드) 생성 결과.

    - code: RBTI 4글자. col: 0~15 열 인덱스(matrix_codes 순서).
    - summary: 카드 상단 한 줄 요지. text: 카드 본문(인용 마커 포함, 무효 마커 제거됨).
    - done_payload: 이 열의 인용 검증된 출처·grounding_supports(build_done_payload 산출).
    - gate_reason: 게이트 발동 사유(None이면 정상). "mismap"|"unsourced"|"pool_escape"|
      "empty"|"error" — 발동 시 text는 정직 폴백으로 대체된다.
    """

    code: str
    col: int
    summary: str
    text: str
    done_payload: dict
    gate_reason: str | None


def matrix_codes() -> list[str]:
    """16 RBTI 코드를 AXIS_ORDER의 축별 허용값 데카르트 곱으로 파생한다(하드코딩 아님)."""
    return ["".join(combo) for combo in itertools.product(*(vals for _axis, vals in AXIS_ORDER))]


def _today_kst() -> str:
    """오늘 날짜를 KST 기준 "YYYY년 M월 D일"로 반환한다(프롬프트 시제 기준)."""
    now = datetime.now(_KST)
    return f"{now.year}년 {now.month}월 {now.day}일"


def _get_genai_client() -> genai.Client:
    """공유 genai 클라이언트 싱글턴을 반환한다(retrieval과 동일 클라이언트)."""
    return get_genai_client()


def _summarize(text: str) -> str:
    """본문 첫 유의미 줄을 한 줄 요지로 뽑는다(마크다운·인용 마커 제거, 80자 상한)."""
    for line in text.splitlines():
        stripped = _SUMMARY_STRIP.sub("", line).strip()
        if stripped:
            return stripped[:80]
    return ""


def _pool_confined(text: str, pool_titles: list[str]) -> bool:
    """본문이 주장한 모든 책 제목이 풀 후보 제목 집합에 의해 뒷받침되는지 판정한다.

    매트릭스 고유 가드: 풀에 상품 출처가 있어(공유 검색) product_gate의 unsourced가 발동하지
    않는 상황에서, 모델이 풀에 없는 책을 지어내(가격까지) 그럴듯하게 추천하는 "풀 밖" 환각을
    막는다. 제목 매칭은 product_gate의 관대 매칭(_title_supported: 축약·부제 변형 허용)을
    그대로 재사용해 정상 인용을 오탐하지 않는다. 하나라도 풀 밖 제목이면 False.
    """
    for line in text.splitlines():
        for match in _ASSERTED_TITLE.finditer(line):
            asserted = _clean_asserted(match)
            if not _is_title_candidate(asserted):
                continue
            if not any(_title_supported(asserted, title) for title in pool_titles):
                return False
    return True


def _gate_reason(text: str, citation: CitationResult, pool: SharedPool) -> str | None:
    """열 답변의 게이트 사유를 판정한다(정상이면 None).

    evaluate_product_answer(mismap: cited-but-fabricated / unsourced: 상품 접지 없음)는 kind와
    무관하게 항상 적용한다 — web/none 답변이 지어낸 책+가격을 주장하면 상품 접지가 없어
    unsourced가 발동해 무출처 상품 사실(4a)을 차단한다. assert_pool_confined("풀 밖 책")는
    **product 풀에서만** 적용한다(web/none은 대조할 도서 후보 집합이 없다).
    """
    by_id = {source["id"]: source for source in pool.sources}
    cited = [by_id[sid] for sid in citation.used_source_ids if sid in by_id]
    reason = evaluate_product_answer(
        text, cited_sources=cited, observed_sources=pool.sources
    )
    if reason is not None:
        return reason
    if pool.kind == "product" and not _pool_confined(
        text, [c["title"] for c in pool.candidates]
    ):
        return "pool_escape"
    return None


async def _call_model(client: genai.Client, settings: Settings, system: str, user: str) -> str:
    """genai flash로 한 열을 생성한다(도구 없음, thinking=matrix budget)."""
    config = types.GenerateContentConfig(
        system_instruction=system,
        thinking_config=types.ThinkingConfig(
            thinking_budget=settings.matrix_generation_thinking_budget
        ),
    )
    response = await client.aio.models.generate_content(
        model=settings.matrix_generation_model,
        contents=user,
        config=config,
    )
    return (response.text or "").strip()


def _fallback_column(
    pool: SharedPool, code: str, col: int, reason: str, session_id: str
) -> ColumnResult:
    """정직 폴백 열을 만든다(재검색 없음). kind·풀 유무에 맞는 정직 문구를 고른다."""
    if pool.kind == "web":
        notice = MATRIX_WEB_EMPTY_NOTICE
    elif not pool.candidates:
        notice = MATRIX_EMPTY_NOTICE
    else:
        notice = MATRIX_FALLBACK_NOTICE
    citation = validate_citations(notice, pool.sources)
    done_payload = build_done_payload(
        pool.sources, citation.used_source_ids, session_id, citation.supports
    )
    return ColumnResult(code, col, _summarize(notice), notice, done_payload, reason)


async def generate_column(
    pool: SharedPool,
    code: str,
    col: int,
    settings: Settings,
    *,
    genai_client: genai.Client,
    session_id: str = "",
) -> ColumnResult:
    """한 열(RBTI 코드)의 답을 생성·검증·게이트한다."""
    persona = build_persona_block(code)
    # 열별 후보 회전(product 풀·설정 on) — primacy 편향으로 열마다 다른 책을 앞세워 리드 수렴 완화.
    lead_offset = col if (settings.matrix_pool_rotate and pool.kind == "product") else 0
    system, user = build_matrix_prompt(pool, persona, today=_today_kst(), lead_offset=lead_offset)

    try:
        raw = await _call_model(genai_client, settings, system, user)
    except APIError as exc:
        logger.warning("matrix 생성 실패 code=%s: %s", code, exc)
        return _fallback_column(pool, code, col, "error", session_id)

    citation = validate_citations(raw, pool.sources)
    reason = _gate_reason(citation.text, citation, pool)
    if reason is not None:
        logger.info("matrix 게이트 발동 code=%s reason=%s", code, reason)
        return _fallback_column(pool, code, col, reason, session_id)

    done_payload = build_done_payload(
        pool.sources, citation.used_source_ids, session_id, citation.supports
    )
    return ColumnResult(code, col, _summarize(citation.text), citation.text, done_payload, None)


async def generate_matrix(
    pool: SharedPool,
    settings: Settings,
    *,
    session_id: str = "",
    genai_client: genai.Client | None = None,
) -> AsyncIterator[ColumnResult]:
    """16열을 병렬 생성하며 완료된 열부터 yield한다(스트리밍 UX).

    풀이 ok가 아니면(empty/error) genai를 전혀 호출하지 않고 16열 모두 정직 폴백으로 즉시
    산출한다(비용 가드 — 사실이 없으니 생성할 것도 없다). ok면 Semaphore로 동시성을 제한한
    fan-out을 asyncio.as_completed로 소비해 먼저 끝난 열부터 흘린다.
    """
    codes = matrix_codes()

    if pool.status != "ok":
        for col, code in enumerate(codes):
            yield _fallback_column(pool, code, col, pool.status, session_id)
        return

    client = genai_client or _get_genai_client()
    semaphore = asyncio.Semaphore(settings.matrix_generation_concurrency)

    async def _run(col: int, code: str) -> ColumnResult:
        async with semaphore:
            return await generate_column(
                pool, code, col, settings, genai_client=client, session_id=session_id
            )

    tasks = [asyncio.create_task(_run(col, code)) for col, code in enumerate(codes)]
    for future in asyncio.as_completed(tasks):
        yield await future
