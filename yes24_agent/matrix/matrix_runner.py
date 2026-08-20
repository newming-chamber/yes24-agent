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

from google.adk.sessions import InMemorySessionService

from yes24_agent.postprocess import MARKER_PATTERN, code_span_ranges, renumber_markers
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
from yes24_agent.toolsets import TOOLSET_SOURCE_TYPES

logger = logging.getLogger(__name__)

# 대표책 줄에서 제외할 출처 타입 — 도구 레지스트리(toolsets.TOOLSET_SOURCE_TYPES)의 web
# toolset 선언에서 파생한다(손 사본 금지). 프론트 static/lib/sources.js WEB_TYPES는 런타임이
# 달라 불가피한 JS 사본이다. 웹 출처는 책이 아니라 하단 인용 칩으로만 표시되므로
# picks(고른 책)에서 뺀다.
_WEB_TYPES = frozenset(TOOLSET_SOURCE_TYPES["web"])

# 셀 로컬 id → 매트릭스 전역 유일 id의 네임스페이스 폭. 각 셀은 독립 세션이라 로컬 id가
# 1,2,3…으로 재시작(sources.py register_source)해, 전역 디둡·프론트 레지스트리(String(id))에서
# 나중 셀의 id=1이 먼저 방출한 셀의 책으로 해소되는 오염이 난다. 전역 id = col*STRIDE + local로
# 셀마다 유일화한다(셀당 인용 출처 < STRIDE 가정 안전 — 실사용 채팅 한 턴 인용은 한 자릿수).
_CELL_ID_STRIDE = 1000

# 마커 치환·패턴은 postprocess가 단일 정의처다(renumber_markers·MARKER_PATTERN) — 셀 로컬
# id → 전역 id 치환도 채팅 경로와 같은 함수로 한다(이스케이프 가드·코드 스팬 제외까지 동일).
# 사본이던 _MARKER_RE·_remap_marker_text는 2026-08-19 삭제(구조 감사 C1 — matrix 사본에만
# 이스케이프 가드·코드 스팬 제외가 없어 채팅과 동작이 갈렸다).

# 정렬 비교에서 건너뛸 것: 마커 토큰과 가로 공백. 인용 검증이 정본에서 바꾸는 것은 마커
# 표기뿐인데, 무효 마커를 지울 때 seam의 공백까지 흡수하므로(postprocess) 마커만 눈감으면
# 공백 한 칸에 대조가 깨진다. 줄바꿈은 남긴다(검증이 건드리지 않는다).
_ALIGN_SKIP_RE = re.compile(MARKER_PATTERN.pattern + r"|[ \t]+")


def _normalize_for_align(text: str) -> tuple[str, list[int]]:
    """마커·가로공백을 걷어낸 본문과, 남은 글자의 원문 인덱스를 돌려준다."""
    kept: list[str] = []
    index_map: list[int] = []
    i = 0
    while i < len(text):
        skip = _ALIGN_SKIP_RE.match(text, i)
        if skip:
            i = skip.end()
            continue
        kept.append(text[i])
        index_map.append(i)
        i += 1
    return "".join(kept), index_map


def _aligned_offset(text: str, streamed: str) -> int:
    """정본 text에서 이미 흐른 조각(streamed)이 끝나는 위치. 못 맞추면 -1.

    표시 번호를 스트리밍 시점에 배정하고 빈 근거 제거를 삭제한 뒤로(2026-08-04) 정본과
    스트리밍 본문은 **재생 47턴 전부에서 바이트 동일**하다. 이 정렬이 실제로 일할 일은
    존재하지 않는 출처를 가리킨 **유령 마커**가 지워진 셀뿐이고, 그 경로는 살아 있으므로
    (4a의 실제 방어선) 이 대조도 남긴다 — 마커 차이에 눈감지 않으면 그 셀은 접힘을 포기해
    답이 조사 로그로 시작한다. 프론트의 같은 판정(md.js redistributeCanonical)과 한 쌍이라
    한쪽만 지우면 채팅과 매트릭스가 갈린다(런타임이 달라 사본이 둘일 뿐, 규칙은 하나다).
    단 이스케이프 가드(`(?<!\\)`)는 서버 쪽에만 있다 — md.js 사본에는 없어 본문에 `\\[숫자]`
    리터럴이 있을 때만 미세 분기한다(2026-08-19 동등성 검증 관측, 서버 쪽이 더 정확한 방향).
    """
    norm_text, index_map = _normalize_for_align(text)
    norm_streamed, _ = _normalize_for_align(streamed)
    if not norm_text.startswith(norm_streamed):
        return -1
    position = len(norm_streamed)
    return index_map[position] if position < len(index_map) else len(text)


def _display_cell_fields(text: str, cut: int, id_map: dict[int, int]) -> tuple[str, int]:
    """셀 본문을 전역 id로 치환하고, 접힘 오프셋(process_chars)을 치환 후 기준으로 센다.

    슬라이스 치환에도 **정본 전체의 코드스팬 눈**(code_ranges)을 넘긴다 — 슬라이스 위에서
    새로 계산하면 슬라이스 밖에서 닫히는 인라인 코드스팬이 코드스팬으로 안 보여 접두
    치환이 전체 치환의 접두가 아니게 되고(remap(prefix) ⊄ remap(full)), 그 오버슛만큼
    셀 답변 앞부분이 접힘에 삼켜진다(2026-08-19 동등성 적대 검증 실결함 — 가드:
    test_process_chars_remap_keeps_the_prefix_invariant).
    """
    ranges = code_span_ranges(text)
    return (
        renumber_markers(text, id_map, code_ranges=ranges),
        len(renumber_markers(text[:cut], id_map, code_ranges=ranges)),
    )


def _parse_frame(frame: str) -> tuple[str, dict]:
    """SSE 프레임 문자열(event:/data:)을 (event, payload)로 되돌린다."""
    lines = frame.strip().split("\n")
    event = lines[0].removeprefix("event: ")
    data = json.loads(lines[1].removeprefix("data: ")) if len(lines) > 1 else {}
    return event, data


async def _run_cell(
    question: str, code: str, model: str | None, session_service: InMemorySessionService
) -> dict:
    """페르소나 코드 하나로 채팅을 끝까지 돌려 그 셀의 구조화 결과를 모은다.

    **채팅 runner를 그대로 소비한다 — 모듈·로직 동일, 유일한 차이는 rbti(페르소나) 주입뿐.**
    모델도 채팅에서 고른 것을 그대로 따른다(매트릭스 전용 모델 없음). 각 셀은 독립
    세션(session_id=None → runner가 고유 id 부여)이라 서로의 맥락을 오염시키지 않는다.
    세션 서비스는 런 스코프 인메모리를 주입받는다 — 셀 세션은 1회성(후속 턴 없음)인데
    16셀이 sqlite 한 파일에 동시 append하면 `database is locked`로 셀이 죽는다
    (2026-08-04 상용 실측 6~8셀 사망). 영속이 필요 없으므로 공유 쓰기 자원을 뺀다.
    최종 done 프레임의 text·sources만 취해 열 프레임으로 재조립한다. 여기에
    **라운드 경계 오프셋(process_chars)**을 더한다: 채팅 화면이 조사 과정을 접는 근거는
    텍스트가 아니라 **도구 스텝 프레임이 본문을 끊는다는 이벤트 순서**인데, 셀은 본문 통째
    1회로 방출돼 그 순서 정보가 프론트에 남지 않는다. 본문에 라운드 구분자가 들어가긴
    하지만(runner _ROUND_SEPARATOR) 모델이 스스로 쓴 문단 경계와 구별되지 않아 텍스트만으로는
    어느 문단 경계가 라운드 경계인지 특정할 수 없다. 그래서 여기서 한 번 세어 넘긴다 —
    본문은 한 글자도 바꾸지 않는다(표시용 부가 필드).
    """
    text = ""
    sources: list[dict] = []
    streamed: list[str] = []  # 흐른 본문 조각(라운드 경계를 세기 위한 것 — 방출하지 않는다)
    process_prefix = ""       # 마지막 도구 스텝까지 흐른 본문 = 경계의 정본
    settled = False           # reset 이후 = 정본 재전송, 라운드 경계를 더 셀 수 없다
    # 이 셀 태스크(및 그 안에서 파생되는 Yes24 도구 하위 태스크)를 고처리량 경로로 표시한다.
    # 16셀이 동시에 도는 매트릭스에서 전역 채팅 클라이언트(rps=1.5)의 단일 throttle_lock이 모든
    # 셀의 Yes24 요청을 0.667초 간격으로 직렬화하던 병목을 없앤다(2026-07-24 실측: 89→~52초).
    # 채팅 단일 경로는 이 컨텍스트가 꺼져 있어 rps=1.5 그대로 — 매트릭스만 빨라진다.
    with high_throughput_client():
        # enrich=False: 셀 세션은 1회성이라 제목·추천 구조화의 소비자가 없다 — 16셀이
        # meta 서브콜을 16번 쏘는 낭비를 끈다.
        async for frame in run_agent_stream(
            question,
            session_id=None,
            rbti=code,
            model=model,
            session_service=session_service,
            enrich=False,
        ):
            event, data = _parse_frame(frame)
            if event == "delta" and not settled:
                streamed.append(data.get("text", ""))
            elif event == "reset":
                # 인용 검증이 본문을 바꿔 정본이 통째로 다시 온다. 그 덩어리엔 경계가 없지만
                # **이미 센 경계는 버리지 않는다** — 바뀌는 것은 마커 표기뿐이라 과정 접두는
                # 정본에서도 찾을 수 있다(아래 _aligned_offset 대조가 판정한다).
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
    # 오프셋은 **정본 위에서 유효해야** 쓴다: 과정 접두를 정본에서 못 찾거나(인용 검증이 그
    # 구간을 고친 경우) 경계 뒤에 남는 답이 없으면(도구 호출 후 본문 없이 끝난 셀) 접지
    # 않는다 — 과정만 남고 답이 사라지는 표시는 어떤 경우에도 만들지 않는다.
    # 대조는 마커 표기에 눈감는다: 재번호는 접두의 **표기만** 바꾸므로 그걸로 접힘을
    # 포기하면 인용 있는 셀이 죄다 조사 로그로 시작한다(채팅 쪽 reset 회귀와 같은 뿌리).
    cut = _aligned_offset(text, process_prefix) if process_prefix else 0
    if cut < 0 or not text[cut:].strip():
        cut = 0
    return {
        "code": code,
        "text": text,
        "sources": sources,
        "process_chars": cut,
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

        # 런 스코프 인메모리 세션 서비스 — 16셀이 공유하되 런이 끝나면 통째로 GC된다
        # (프로세스 싱글턴이면 런마다 16세션의 도구 결과가 메모리에 영구 누적된다).
        # 수용한 트레이드오프: 셀 세션이 sqlite에 안 남으므로 admin 세션 브라우저에서
        # 매트릭스 실행 이력(셀별 도구 호출 타임라인)이 보이지 않는다 — 매트릭스는 개발
        # 확인용이고 셀 결과는 SSE로 전량 프론트에 가므로, 상용 셀 사망과 맞바꿀 가치가 없다.
        cell_sessions = InMemorySessionService()
        tasks = [
            asyncio.ensure_future(_run_cell(question, code, model, cell_sessions))
            for code in codes
        ]
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
            # 조회하므로 title·sale_price·image_url 등 전체 정보를 이때 싣는다.
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

            display_text, display_cut = _display_cell_fields(
                cell["text"], cell["process_chars"], id_map
            )
            identity = {
                "code": cell["code"],
                "name": get_archetype_name(cell["code"]),
                "axis_label": axis_label(cell["code"]),
                # 마커 전역화가 [2]→[2002]로 길이를 바꾸므로 오프셋도 **치환 후 기준**으로 센다.
                # 경계가 마커 안에 걸리면 그 마커만 치환에서 빠져 몇 글자 짧게 잡히는데, 과정
                # 꼬리가 조금 더 보이는 방향이라 무해하다(경계는 신호가 정본 — 문구 보정 없음).
                "process_chars": display_cut,
            }
            yield sse_delta(display_text, col=col, extra=identity)

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
        logger.exception(f"매트릭스 스트림 처리 중 예외: {exc}")
        yield sse_error(STREAM_ERROR_MESSAGE)
        yield _terminal_done()
    finally:
        # 예외·클라이언트 중단(GeneratorExit)으로 as_completed 소비가 조기 종료되면 남은
        # 셀 태스크가 백그라운드로 계속 LLM 스트림을 돈다 — 미완 태스크를 취소해 누수를 막는다.
        for task in tasks:
            if not task.done():
                task.cancel()
