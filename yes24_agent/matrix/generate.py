"""C2 — 16 fan-out 생성(도구 없음, 병렬, 각 열 인용 검증 + 게이트).

공유 풀 하나를 16 RBTI 페르소나가 나눠 쓰며 **선택·프레이밍만** 달리해 생성한다. 각 열은:
1. build_persona_block(code) + render_pool(사실만) → 매트릭스 프롬프트(도구 없음).
2. genai flash generate_content(thinking=0)로 생성.
3. validate_citations로 무효 마커 제거(채팅 postprocess 재사용).
4. 게이트 — evaluate_product_answer(mismap/unsourced) + **assert_pool_confined**(매트릭스 고유):
   풀에 상품 출처가 있어 기존 unsourced가 못 잡는 "풀 밖 책+가격" 홀을, 주장 제목이 풀 후보
   제목 집합에 있는지 대조해 차단한다(product_gate의 title 정규화·title_supported 재사용).
5. 사유가 있으면 그 셀만 정직 폴백(재검색 안 함 — 비용 가드). build_done_payload로 마감.

16을 케이스로 박지 않는다 — matrix_codes()가 AXIS_ORDER의 축별 허용값을 곱해 16을 파생한다.
생성은 ADK Runner를 쓰지 않고 google.genai async를 직접 fan-out한다(병렬·경량).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types
from google.genai.errors import APIError

from yes24_agent.config import Settings, get_genai_client
from yes24_agent.grounding import evaluate_product_answer, title_claims, title_supported
from yes24_agent.matrix.prompt import build_axis_guard, build_matrix_prompt, fallback_notice
from yes24_agent.matrix.retrieval import SharedPool
from yes24_agent.postprocess import CitationResult, build_done_payload, validate_citations
from yes24_agent.rbti.persona import AXIS_ORDER, build_persona_block

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class ColumnResult:
    """한 열(RBTI 코드) 생성 결과.

    - code: RBTI 4글자. col: 0~15 열 인덱스(matrix_codes 순서).
    - text: 카드 본문(인용 마커 포함, 무효 마커 제거됨).
    - picks: 이 셀이 **실제로 고른 책**(인용 등장 순서, 중복 제거). 각 원소는 공유 풀의 후보
      레코드 그대로 — source_id·title·url + product_fields(price·rating·image_url·author·
      publisher·pub_status…). picks[0]이 대표책이다.
    - done_payload: 이 열의 인용 검증된 출처·grounding_supports(build_done_payload 산출).
    - gate_reason: 게이트 발동 사유(None이면 정상). "mismap"|"unsourced"|"pool_escape"|
      "empty"|"error" — 발동 시 text는 **사유에 맞는** 정직 폴백으로 대체되고 picks는 빈다.
    """

    code: str
    col: int
    text: str
    picks: list[dict]
    done_payload: dict
    gate_reason: str | None


def _picks(citation: CitationResult, pool: SharedPool) -> list[dict]:
    """이 셀이 고른 책을 **구조화된 레코드**로 뽑는다(인용 등장 순서).

    프론트가 카드 산문을 정규식으로 재파싱해 『제목』+인접 가격을 복원할 필요가 없게 하는 것이
    목적이다 — 같은 판정이 백엔드(인용 검증·풀 접지)와 프론트(정규식)에 이중 구현되면 둘이
    어긋나고, 프론트 파서가 실패하면 대표책·표지가 통째로 사라진다. 근거는 이미 여기 있다:
    validate_citations가 검증한 source_id(등장 순서·중복 제거)를 풀 후보에 그대로 매핑한다.
    본문에 남은 [n]은 게이트를 통과한 것뿐이므로 picks도 접지된 책만 담는다.

    web/none 풀은 도서 후보가 없어 자연히 빈 목록이다(source_id가 풀 후보에 매핑되지 않음).
    """
    by_id = {c["source_id"]: c for c in pool.candidates}
    return [by_id[sid] for sid in citation.used_source_ids if sid in by_id]


def matrix_codes() -> list[str]:
    """16 RBTI 코드를 AXIS_ORDER의 축별 허용값 데카르트 곱으로 파생한다(하드코딩 아님)."""
    return ["".join(combo) for combo in itertools.product(*(vals for _axis, vals in AXIS_ORDER))]


def _axis_value(code: str, axis: str) -> str:
    """RBTI 코드에서 지정 축의 값 글자를 뽑는다(AXIS_ORDER 파생 — 자리 인덱스 하드코딩 없음)."""
    for ch, (name, _allowed) in zip(code, AXIS_ORDER):
        if name == axis:
            return ch
    return ""


def _today_kst() -> str:
    """오늘 날짜를 KST 기준 "YYYY년 M월 D일"로 반환한다(프롬프트 시제 기준)."""
    now = datetime.now(_KST)
    return f"{now.year}년 {now.month}월 {now.day}일"


def _pool_confined(text: str, pool_titles: list[str]) -> bool:
    """본문이 주장한 모든 책 제목이 풀 후보 제목 집합에 의해 뒷받침되는지 판정한다.

    매트릭스 고유 가드: 풀에 상품 출처가 있어(공유 검색) product_gate의 unsourced가 발동하지
    않는 상황에서, 모델이 풀에 없는 책을 지어내(가격까지) 그럴듯하게 추천하는 "풀 밖" 환각을
    막는다. 무엇이 **제목 주장**인지는 product_gate.title_claims가 판정한다 — 볼드는 범용 강조
    서식이라 항목 머리·저자 표기·출처 제목 대응 같은 구조 신호가 있을 때만 제목 주장이다. 이
    가드를 건너뛰고 마커를 직접 훑으면 문장 중간의 강조 볼드("**압도적인 밀도**")를 책 제목으로
    오인해 **정상 셀이 폴백으로 폐기**된다(실측). 제목 매칭도 product_gate의 관대 매칭
    (title_supported: 축약·부제 변형 허용)을 그대로 재사용한다. 하나라도 풀 밖 제목이면 False.
    """
    return all(
        any(title_supported(asserted, title) for title in pool_titles)
        for asserted in title_claims(text, pool_titles)
    )


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
    """정직 폴백 열을 만든다(재검색 없음). 문구는 **사유**에 맞는 것을 고른다(fallback_notice).

    사유를 무시하고 kind로만 고르면, 잡담 셀이 게이트에 걸렸을 때 "Yes24에서 이 질문에 맞는 책을
    찾지 못했어요"(검색 실패 문구)가 나간다 — 찾을 책이 애초에 없던 질문인데.
    """
    notice = fallback_notice(reason, kind=pool.kind, has_candidates=bool(pool.candidates))
    citation = validate_citations(notice, pool.sources)
    done_payload = build_done_payload(
        pool.sources, citation.used_source_ids, session_id, citation.supports
    )
    # 폴백 셀은 고른 책이 없다(picks=[]) — 프론트가 대표책·표지를 그리지 않는 단일 신호.
    return ColumnResult(code, col, notice, [], done_payload, reason)


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
    # D/B 축 추천 구성 구조 가드(product 풀 전용) — 권수 경계로 축 정체성을 구성에서부터 가른다.
    axis_guard = (
        build_axis_guard(
            _axis_value(code, "breadth"),
            depth_max_picks=settings.matrix_depth_max_picks,
            breadth_min_picks=settings.matrix_breadth_min_picks,
        )
        if pool.kind == "product"
        else ""
    )
    system, user = build_matrix_prompt(
        pool, persona, today=_today_kst(), lead_offset=lead_offset, axis_guard=axis_guard
    )

    # 게이트 발동 시 재생성한다(재검색 아님 — 같은 풀로 flash만 한 번 더, Yes24 트래픽 0).
    # 게이트는 이번 초안의 환각(풀 밖 책·무출처 상품사실)에 발동하는데 생성은 비결정적이라, 두 번째
    # 초안은 접지된 답을 낼 확률이 높다 — 셀을 곧장 dim 폴백으로 방치("다시 살펴볼게요" 약속만 하고
    # 이행 안 함)하는 대신 실제 한 번 더 시도해 셀 성공률을 높인다(비용 가드: 발동 셀만, 최대 N회).
    # kind와 무관하게 재시도한다 — 잡담(none) 셀이 책을 지어내 게이트에 걸리는 경우야말로 재생성이
    # 가장 필요한 자리인데(폴백 문구밖에 남지 않는다), 예전엔 그 경로만 재시도가 0회였다.
    attempts = 1 + settings.matrix_cell_retries
    reason: str | None = None
    citation: CitationResult | None = None
    for attempt in range(attempts):
        try:
            raw = await _call_model(genai_client, settings, system, user)
        except APIError as exc:
            logger.warning("matrix 생성 실패 code=%s: %s", code, exc)
            return _fallback_column(pool, code, col, "error", session_id)

        citation = validate_citations(raw, pool.sources)
        reason = _gate_reason(citation.text, citation, pool)
        if reason is None:
            break
        logger.info(
            "matrix 게이트 발동 code=%s reason=%s attempt=%d/%d",
            code, reason, attempt + 1, attempts,
        )

    if reason is not None:
        return _fallback_column(pool, code, col, reason, session_id)

    done_payload = build_done_payload(
        pool.sources, citation.used_source_ids, session_id, citation.supports
    )
    return ColumnResult(code, col, citation.text, _picks(citation, pool), done_payload, None)


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

    client = genai_client or get_genai_client()
    semaphore = asyncio.Semaphore(settings.matrix_generation_concurrency)

    async def _run(col: int, code: str) -> ColumnResult:
        async with semaphore:
            return await generate_column(
                pool, code, col, settings, genai_client=client, session_id=session_id
            )

    tasks = [asyncio.create_task(_run(col, code)) for col, code in enumerate(codes)]
    for future in asyncio.as_completed(tasks):
        yield await future
