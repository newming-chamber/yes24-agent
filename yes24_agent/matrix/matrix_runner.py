"""16뷰 매트릭스 SSE 스트림 — **채팅 파이프라인을 16개 페르소나로 병렬 실행**.

매트릭스는 실사용자 기능이 아니라 **개발 확인용**이다: 실사용자는 자기 RBTI 페르소나
하나만 겪으므로, 16뷰는 "그 실제 채팅을 16개 페르소나로 한 번에 보는" 검증 화면이어야
한다. 그래서 이 모듈은 별도 검색·선택 엔진을 두지 않고 `run_agent_stream`(채팅 runner)을
페르소나 코드마다 그대로 돌린다 — 로직은 채팅과 100% 동일하고, 차이는 rbti 주입뿐이다.

과거의 공유풀(build_shared_pool)·일괄배정(plan_selection)·쿼리정제(refine)는 "16셀을 서로
다른 책으로 조율"하려는 매트릭스 전용 로직이었으나, 실사용자가 겪지 않는 인위적 산출물이라
삭제했다. 책이 셀 간 겹치는 것은 실사용자가 그 페르소나로 실제 받는 답 그대로라 정직하다.

SSE 계약(프론트 matrix.html 불변):
- 글로벌 `source` {id,title,url,type,...}: 어느 셀이든 인용한 출처. id로 프론트 레지스트리에
  등록되고, col done의 sources·picks가 id로 참조한다(중복 id는 한 번만 방출).
- 열 `delta` {text, col, code, name, axis_label}: 그 페르소나 채팅의 완결 본문(카드 통째 1회).
- 열 `done` {sources:[{id}], picks:[{id}], fallback, gate_reason, col}: 그 셀이 인용한 출처 id와
  고른 책 id(picks=비-web 출처, 대표책=picks[0]). 인용 0이면 fallback=true.
- 글로벌 `done`: 스트림 종료 신호(col 없음). sources=전체 방출 출처 합집합.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator

from yes24_agent.rbti.persona import axis_label, get_archetype_name, matrix_codes
from yes24_agent.runner import run_agent_stream
from yes24_agent.sse import (
    STREAM_ERROR_MESSAGE,
    sse_delta,
    sse_done,
    sse_error,
    sse_source,
    sse_status,
)

logger = logging.getLogger(__name__)

# 대표책 줄에서 제외할 출처 타입(프론트 WEB_TYPES와 동일 단일 진실). 웹 출처는 책이 아니라
# 하단 인용 칩으로만 표시되므로 picks(고른 책)에서 뺀다.
_WEB_TYPES = frozenset({"web"})

# 셀 로컬 id → 매트릭스 전역 유일 id의 네임스페이스 폭. 각 셀은 독립 세션이라 로컬 id가
# 1,2,3…으로 재시작(sources.py register_source)해, 전역 디둡·프론트 레지스트리(String(id))에서
# 나중 셀의 id=1이 먼저 방출한 셀의 책으로 해소되는 오염이 난다. 전역 id = col*STRIDE + local로
# 셀마다 유일화한다(셀당 인용 출처 < STRIDE 가정 안전 — 실사용 채팅 한 턴 인용은 한 자릿수).
_CELL_ID_STRIDE = 1000

# 본문 [n]·[n, m] 인용 마커(md.js MARKER_RE와 동일 형태). 대괄호 안 숫자만 전역 id로 치환한다.
_MARKER_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def _remap_marker_text(text: str, id_map: dict[int, int]) -> str:
    """본문의 [n]·[n, m] 마커 안 로컬 id를 셀 전역 id로 치환한다(구분자·공백 보존).

    id_map에 없는 마커 숫자는 이번 턴 인용 출처가 아니므로(프론트 풀에도 없어 평문으로
    남는다) 그대로 둔다.
    """

    def _sub_group(match: re.Match) -> str:
        inner = re.sub(
            r"\d+",
            lambda m: str(id_map.get(int(m.group()), int(m.group()))),
            match.group(1),
        )
        return f"[{inner}]"

    return _MARKER_RE.sub(_sub_group, text)


def _parse_frame(frame: str) -> tuple[str, dict]:
    """SSE 프레임 문자열(event:/data:)을 (event, payload)로 되돌린다."""
    lines = frame.strip().split("\n")
    event = lines[0].removeprefix("event: ")
    data = json.loads(lines[1].removeprefix("data: ")) if len(lines) > 1 else {}
    return event, data


async def _run_cell(question: str, code: str, model: str | None) -> dict:
    """페르소나 코드 하나로 채팅을 끝까지 돌려 그 셀의 구조화 결과를 모은다.

    **채팅 runner를 그대로 소비한다 — 모듈·로직 동일, 유일한 차이는 rbti(페르소나) 주입뿐.**
    모델도 채팅에서 고른 것을 그대로 따른다(매트릭스 전용 모델 없음). 각 셀은 독립
    세션(session_id=None → runner가 고유 id 부여)이라 서로의 맥락을 오염시키지 않는다.
    최종 done 프레임의 text·sources·cited_ids만 취해 열 프레임으로 재조립한다.
    """
    text = ""
    sources: list[dict] = []
    cited_ids: list = []
    async for frame in run_agent_stream(question, session_id=None, rbti=code, model=model):
        event, data = _parse_frame(frame)
        # 채팅의 글로벌 done(col 없음)이 이 셀의 최종 결과다. 공개 source·done 규율은 원칙 4대로
        # runner가 이미 적용해 두었으므로(인용분만), 여기서는 그대로 옮기기만 한다.
        if event == "done":
            text = data.get("text", "") or text
            sources = data.get("sources", sources)
            cited_ids = data.get("cited_ids", cited_ids)
    return {"code": code, "text": text, "sources": sources, "cited_ids": cited_ids}


async def run_matrix_stream(
    question: str, session_id: str | None = None, model: str | None = None
) -> AsyncIterator[str]:
    """질문 1건을 16개 RBTI 페르소나로 병렬 채팅해 매트릭스 SSE로 스트리밍한다.

    셀은 완료되는 대로(as_completed) 방출해 빠른 셀이 먼저 채워진다. model은 채팅에서
    고른 것을 그대로 16셀에 적용한다(채팅과 동일 — 페르소나만 다르다). 어떤 예외가 나도
    제너레이터가 예외로 죽지 않고 글로벌 done 1회로 마감한다(채팅 runner와 동일 정신).
    """
    resolved_session_id = session_id or uuid.uuid4().hex
    codes = matrix_codes()
    col_of = {code: index for index, code in enumerate(codes)}

    emitted_source_ids: set = set()
    all_visible: list[dict] = []

    def _terminal_done() -> str:
        return sse_done(
            {
                "sources": all_visible,
                "grounding_supports": [],
                "session_id": resolved_session_id,
                "models": {},
            }
        )

    tasks: list[asyncio.Future] = []
    try:
        yield sse_status("generating", "16가지 독서 성향으로 살펴보고 있어요")

        tasks = [asyncio.ensure_future(_run_cell(question, code, model)) for code in codes]
        for finished in asyncio.as_completed(tasks):
            cell = await finished
            col = col_of[cell["code"]]
            sources = cell["sources"]

            # 셀 로컬 id → 매트릭스 전역 유일 id. 이 매핑을 아래 세 곳(source 방출·본문 [n]
            # 마커·done sources/picks)에 동일 적용해야 프론트가 셀마다 올바른 책을 그린다.
            id_map = {
                source["id"]: col * _CELL_ID_STRIDE + source["id"]
                for source in sources
                if source.get("id") is not None
            }

            # 이 셀이 인용한 출처를 전역 id로 등록한다(같은 전역 id는 한 번만). 프론트가 id로
            # 조회하므로 title·price·image_url 등 전체 정보를 이때 싣는다.
            for source in sources:
                sid = source.get("id")
                if sid is None:
                    continue
                gid = id_map[sid]
                if gid in emitted_source_ids:
                    continue
                emitted_source_ids.add(gid)
                remapped = {**source, "id": gid}
                all_visible.append(remapped)
                yield sse_source(remapped)

            identity = {
                "code": cell["code"],
                "name": get_archetype_name(cell["code"]),
                "axis_label": axis_label(cell["code"]),
            }
            yield sse_delta(_remap_marker_text(cell["text"], id_map), col=col, extra=identity)

            # picks = 인용한 **책**(비-web) 출처, 등장 순서 보존(대표책=picks[0]). 인용이 하나도
            # 없으면 정직 폴백 셀이다(fallback=true). 프론트는 이 명시 필드만 보고 본문을 파싱하지
            # 않는다(백엔드 인용 검증과의 이중 구현 금지 — 기존 계약 유지).
            book_ids = [
                id_map[source["id"]]
                for source in sources
                if source.get("id") is not None and source.get("type") not in _WEB_TYPES
            ]
            fallback = not cell["cited_ids"]
            yield sse_done(
                {
                    "sources": [
                        {"id": id_map[source["id"]]}
                        for source in sources
                        if source.get("id") is not None
                    ],
                    "picks": [{"id": book_id} for book_id in book_ids],
                    "grounding_supports": [],
                    "session_id": resolved_session_id,
                    "fallback": fallback,
                    "gate_reason": "empty" if fallback else None,
                },
                col=col,
            )

        yield _terminal_done()

    except Exception as exc:  # noqa: BLE001 — SSE 스트림 최상위 방어선(글로벌 done 1회 불변식)
        logger.exception("매트릭스 스트림 처리 중 예외: %s", exc)
        yield sse_error(STREAM_ERROR_MESSAGE)
        yield _terminal_done()
    finally:
        # 예외·클라이언트 중단(GeneratorExit)으로 as_completed 소비가 조기 종료되면 남은
        # 셀 태스크가 백그라운드로 계속 LLM 스트림을 돈다 — 미완 태스크를 취소해 누수를 막는다.
        for task in tasks:
            if not task.done():
                task.cancel()
