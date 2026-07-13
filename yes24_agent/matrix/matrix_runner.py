"""C3 — 16뷰 매트릭스 SSE 스트림 오케스트레이션.

`/chat/matrix` 엔드포인트의 심장. 공유 검색 1회(build_shared_pool) → 16 fan-out 생성
(generate_matrix)을 SSE 프레임으로 번역해 yield한다. 채팅 runner를 재사용하지 않는 별도
경로다(도구 루프·게이트 재진입 없음 — 매트릭스는 생성 단계에 도구가 없고, 셀 게이트는
재검색 대신 정직 폴백).

SSE 계약(프론트 C4/matrix-ux 계약):
- **글로벌 프레임**(col 필드 없음 — /chat/stream과 동형):
  - `event: status` {stage, detail}: 진행 상태(thinking/generating).
  - `event: source` {id,title,url,type}: 공유 풀 출처. 생성 시작 전 N회 방출(16열이 공유).
  - `event: done` {sources, grounding_supports, session_id}: **스트림 종료 신호**(col 없음).
    sources=공유 풀 전체. 이 프레임 하나로 매트릭스 완료를 판정한다.
- **열 프레임**(col=0~15):
  - `event: delta` {text, col, code, name, axis_label}: 그 열 카드 본문 전체(열 단위 입도 —
    카드 통째로 1회) + 카드 정체성(code="CADI", name=아키타입명, axis_label="완독-분석-깊이-정보").
    프론트가 첫 페인트에서 카드 제목·부제를 확보하도록 정체성을 프레임에 싣는다(프론트가
    매핑표를 중복 보유하지 않게 — persona.py가 단일 소스, JS는 import 못 함).
  - `event: done` {sources, grounding_supports, session_id, col, fallback, gate_reason}: 그 열의
    인용 검증된 출처(공유 풀의 부분집합)·grounding_supports + **폴백 플래그**. fallback(bool)이
    정직 폴백 셀 여부를 명시하고(프론트는 본문 정규식이 아니라 이 필드로 판정), gate_reason은
    사유(정상 null|"mismap"|"unsourced"|"pool_escape"|"empty"|"error"). 완료 순서로 col 라우팅.

열은 생성 완료 순서로 도착하므로 col 인덱스로 카드를 채운다(순차 아님). 상품 사실은 공유 풀
(Yes24 출처)만 근거하며, 각 열은 인용 검증·cited-fabricated·풀밖 게이트를 통과한 것만 나온다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from yes24_agent.config import get_settings
from yes24_agent.matrix.generate import generate_matrix
from yes24_agent.matrix.retrieval import build_shared_pool
from yes24_agent.rbti.persona import axis_label, get_archetype_name
from yes24_agent.sse import sse_delta, sse_done, sse_error, sse_source, sse_status

logger = logging.getLogger(__name__)


async def run_matrix_stream(
    question: str, session_id: str | None = None
) -> AsyncIterator[str]:
    """질문 1건에 대한 16뷰 매트릭스를 SSE 프레임 문자열로 스트리밍한다.

    어떤 예외가 나도 제너레이터가 예외로 죽지 않고 error + 글로벌 done으로 마감한다
    ("글로벌 done 정확히 1회" 불변식 — 채팅 runner와 동일 정신).
    """
    settings = get_settings()
    resolved_session_id = session_id or uuid.uuid4().hex

    def _terminal_done(sources: list[dict]) -> str:
        return sse_done(
            {
                "sources": sources,
                "grounding_supports": [],
                "session_id": resolved_session_id,
            }
        )

    try:
        yield sse_status("thinking", "질문을 확인하고 있어요")

        pool = await build_shared_pool(question, settings)

        # 공유 풀 출처를 글로벌로 먼저 방출한다(16열이 공유 — "출처 먼저 → 본문").
        for source in pool.sources:
            yield sse_source(source)

        yield sse_status("generating", "16가지 독서 성향으로 살펴보고 있어요")

        async for column in generate_matrix(
            pool, settings, session_id=resolved_session_id
        ):
            # 카드 정체성(제목=아키타입명, 부제=축라벨)을 delta에 실어 첫 페인트에서 확보하게
            # 한다. persona.py 헬퍼가 단일 소스 — 프론트가 col↔코드 매핑표를 중복 보유하지 않음.
            identity = {
                "code": column.code,
                "name": get_archetype_name(column.code),
                "axis_label": axis_label(column.code),
            }
            yield sse_delta(column.text, col=column.col, extra=identity)
            # 폴백 여부를 명시 필드로 싣는다(가법) — 프론트가 본문 정규식으로 폴백을 추정하지
            # 않게(잡담이 우연히 폴백 문구를 포함해 오마킹되던 문제). gate_reason(None|사유)도
            # 함께 실어 muted 사유 표시에 쓴다. 정상 열은 fallback=false·gate_reason=null.
            col_done = {
                **column.done_payload,
                "fallback": column.gate_reason is not None,
                "gate_reason": column.gate_reason,
            }
            yield sse_done(col_done, col=column.col)

        yield _terminal_done(pool.sources)

    except Exception as exc:  # noqa: BLE001 — SSE 스트림 최상위 방어선(글로벌 done 1회 불변식)
        logger.exception("매트릭스 스트림 처리 중 예외: %s", exc)
        yield sse_error("일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요.")
        yield _terminal_done([])
