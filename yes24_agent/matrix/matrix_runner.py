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
- 열 `delta` {text, col, code, name, axis_label, process_chars}: 그 페르소나 채팅의 완결 본문
  (카드 통째 1회). `process_chars`는 **본문을 자르지 않는 표시용 오프셋**이다 — text[:n]이
  조사 과정(내레이션), text[n:]이 최종 답이다(0이면 접을 과정이 없다). 본문 자체는 불변.
- 열 `done` {sources:[{id}], picks:[{id}], fallback, gate_reason, col}: 그 셀이 인용한 출처 id와
  고른 책 id(picks=비-web 출처, 대표책=picks[0]). fallback=true는 셀이 **답을 못 낸**(빈 본문)
  경우이며 인용 유무와 무관하다 — 범용 질문은 인용 없이 답하는 게 정상이라 폴백이 아니다.
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
from yes24_agent.tools.yes24_search import high_throughput_client

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
    최종 done 프레임의 text·sources·cited_ids만 취해 열 프레임으로 재조립한다. 여기에
    **라운드 경계 오프셋(process_chars)**을 더한다: 채팅 화면이 조사 과정을 접는 근거는
    텍스트가 아니라 **도구 스텝 프레임이 본문을 끊는다는 이벤트 순서**인데, 셀은 본문 통째
    1회로 방출돼 그 순서 정보가 프론트에 남지 않는다. 본문에 라운드 구분자가 들어가긴
    하지만(runner _ROUND_SEPARATOR) 모델이 스스로 쓴 문단 경계와 구별되지 않아 텍스트만으로는
    어느 문단 경계가 라운드 경계인지 특정할 수 없다. 그래서 여기서 한 번 세어 넘긴다 —
    본문은 한 글자도 바꾸지 않는다(표시용 부가 필드).
    """
    text = ""
    sources: list[dict] = []
    cited_ids: list = []
    streamed: list[str] = []  # 흐른 본문 조각(라운드 경계를 세기 위한 것 — 방출하지 않는다)
    process_prefix = ""       # 마지막 도구 스텝까지 흐른 본문 = 경계의 정본
    settled = False           # reset 이후 = 정본 재전송, 라운드 경계를 더 셀 수 없다
    # 이 셀 태스크(및 그 안에서 파생되는 Yes24 도구 하위 태스크)를 고처리량 경로로 표시한다.
    # 16셀이 동시에 도는 매트릭스에서 전역 채팅 클라이언트(rps=1.5)의 단일 throttle_lock이 모든
    # 셀의 Yes24 요청을 0.667초 간격으로 직렬화하던 병목을 없앤다(2026-07-24 실측: 89→~52초).
    # 채팅 단일 경로는 이 컨텍스트가 꺼져 있어 rps=1.5 그대로 — 매트릭스만 빨라진다.
    with high_throughput_client():
        async for frame in run_agent_stream(question, session_id=None, rbti=code, model=model):
            event, data = _parse_frame(frame)
            if event == "delta" and not settled:
                streamed.append(data.get("text", ""))
            elif event == "reset":
                # 인용 검증이 본문을 바꿔 정본이 통째로 다시 온다. 그 덩어리엔 경계가 없지만
                # **이미 센 경계는 버리지 않는다** — 지워진 마커가 답 구간에 있었다면 과정
                # 접두는 정본에서도 그대로다(아래 대조가 판정한다).
                settled = True
            elif (
                not settled
                and event == "status"
                and data.get("stage") != "thinking"
                and data.get("detail")
            ):
                # 채팅 프론트가 블록을 나누는 조건과 같은 프레임이다(thinking은 본문을 끊지
                # 않고, 문구 없는 프레임은 표시할 단계가 없다 — index.html setStatus 참조).
                process_prefix = "".join(streamed)
            # 채팅의 글로벌 done(col 없음)이 이 셀의 최종 결과다. 공개 source·done 규율은 원칙 4대로
            # runner가 이미 적용해 두었으므로(인용분만), 여기서는 그대로 옮기기만 한다.
            elif event == "done":
                text = data.get("text", "") or text
                sources = data.get("sources", sources)
                cited_ids = data.get("cited_ids", cited_ids)
    # 오프셋은 **정본 위에서 유효해야** 쓴다: 과정 접두가 정본의 접두와 다르거나(인용 검증이
    # 그 구간을 고친 경우) 경계 뒤에 남는 답이 없으면(도구 호출 후 본문 없이 끝난 셀) 접지
    # 않는다 — 과정만 남고 답이 사라지는 표시는 어떤 경우에도 만들지 않는다.
    if not text.startswith(process_prefix) or not text[len(process_prefix) :].strip():
        process_prefix = ""
    return {
        "code": code,
        "text": text,
        "sources": sources,
        "cited_ids": cited_ids,
        "process_chars": len(process_prefix),
    }


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
                # 마커 전역화가 [2]→[2002]로 길이를 바꾸므로 오프셋도 **치환 후 기준**으로 센다.
                # 경계가 마커 안에 걸리면 그 마커만 치환에서 빠져 몇 글자 짧게 잡히는데, 과정
                # 꼬리가 조금 더 보이는 방향이라 무해하다(경계는 신호가 정본 — 문구 보정 없음).
                "process_chars": len(
                    _remap_marker_text(cell["text"][: cell["process_chars"]], id_map)
                ),
            }
            yield sse_delta(_remap_marker_text(cell["text"], id_map), col=col, extra=identity)

            # picks = 인용한 **책**(비-web) 출처, 등장 순서 보존(대표책=picks[0]). 인용 유무는
            # 대표책 줄 표시에만 쓰이고 폴백 판정과는 분리한다 — 프론트는 이 명시 필드만 보고
            # 본문을 파싱하지 않는다(백엔드 인용 검증과의 이중 구현 금지 — 기존 계약 유지).
            book_ids = [
                id_map[source["id"]]
                for source in sources
                if source.get("id") is not None and source.get("type") not in _WEB_TYPES
            ]
            # 폴백 = **셀이 답을 못 낸 것**(빈 본문)이지 인용이 없는 것이 아니다. 매트릭스는 책
            # 추천 그리드가 아니라 "같은 질문을 16 페르소나가 어떻게 답하나" 비교 뷰이고, 앱은
            # 범용 어시스턴트라 월드컵·번아웃 등 대다수 질문은 Yes24 인용이 없는 게 정상이다.
            # 인용 0을 실패로 찍으면 멀쩡한 답이 흐림(opacity 0.62)·비교 제외돼 뷰 목적이 깨진다.
            fallback = not cell["text"].strip()
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
