"""긴 대화의 이력 압축 — ADK 네이티브 token-threshold compaction을 턴 마감 뒤 백그라운드로 돌린다.

왜 필요한가: ADK는 매 턴 세션 이벤트 전체를 프롬프트로 재전송한다. 운영 DB 실측(2026-10-06) —
회원 1명이 한 대화에서 343턴·320턴을 써 턴당 프롬프트가 1만→165만 토큰으로 자랐고, 10-03 이후
main 비용 $167 중 $106(64%)이 이 한 명이었다. 정상 사용자는 대화당 1~3턴, 턴당 프롬프트 중앙값
5~6만·p90 15~18만 토큰이라 임계값(config)은 그 위에만 둔다.

무엇을 쓰는가: ADK 2.3.0의 `EventsCompactionConfig(token_threshold, event_retention_size)` +
`LlmEventSummarizer` 그대로다. 마지막 LLM 콜의 prompt_token_count가 임계값 이상이면 최근
`event_retention_size`건을 뺀 오래된 이벤트(함수콜↔응답 짝은 가르지 않는다)를 경량 모델 요약
1건으로 접어 `actions.compaction` 이벤트로 append한다. 원시 이벤트는 DB에 그대로 남고(히스토리
복원·admin·턴 스냅샷 무영향), 다음 턴부터 ADK contents 조립(`_process_compaction_events`)이
압축 범위의 원시 이벤트 대신 요약을 싣는다. 재압축은 직전 요약을 seed로 받는 롤링 요약이다.

왜 App 경로가 아닌가: ADK는 compaction config를 App에 실으면 `Runner.run_async` 안의 두 지점에서
돌린다 — (1) 모델 콜 직전 요청 프로세서(flows/llm_flows/compaction.py: 첫 토큰이 요약 콜만큼
늦어진다), (2) invocation 마감 뒤(runners.py: 마지막 이벤트 뒤 제너레이터 종료가 늦어진다 =
우리 done이 요약 콜 시간만큼 밀린다). 둘 다 사용자 대기에 얹힌다. 그래서 App을 쓰지 않고
(Runner에 config를 넘기지 않으면 두 지점 모두 무동작) **같은 네이티브 함수**를 done 직전에
예약한 백그라운드 task가 세션 락 안에서 돌린다 — 스트림은 즉시 닫히고, 같은 세션의 다음 턴만
락에서 요약 시간을 기다린다(다른 세션·사용자 무영향). 압축이 실패·중단되면 다음 턴이 같은
임계값으로 다시 시도한다(멱등).

인용 계약(원칙 4)과의 관계: 출처 레지스트리는 세션 **state**(sources.py)라 이벤트 압축과 무관하게
전부 남는다. 요약은 `[n]` 마커를 보존하도록 지시해, 모델이 요약 속 책을 다시 인용하면
`_citable_sources`(레지스트리 ∪ 이번 턴 관측)가 그대로 검증한다.
"""

import asyncio
import logging
import time
from functools import lru_cache

from google.adk.agents import LlmAgent
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.compaction import _run_compaction_for_token_threshold_config
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.events import Event
from google.adk.models import Gemini
from google.adk.sessions import BaseSessionService
from google.genai import types

from yes24_agent.config import get_settings
from yes24_agent.session_service import _get_session_lock
from yes24_agent.usage import record_usage

logger = logging.getLogger(__name__)

# usage_log.component 값 — 요약 콜 1건당 1행(비용 사각 금지).
USAGE_COMPONENT = "history_compaction"

# ADK 기본 템플릿(영어·범용)을 대신한다. `{conversation_history}` 자리는 ADK 계약. 보존 대상을
# 종류로만 말한다 — 완성 예문을 주면 내용까지 복사된다(prompt-style-example-as-skeleton).
#
# **지시는 기록 뒤에 둔다.** 지시→기록 순서(ADK 기본 템플릿도 같다)로 8.5만 토큰짜리 운영 세션
# 기록을 flash-lite에 보내면 요약 대신 기록의 마지막 질문에 답했다(현행·ADK 기본 각 3/3, [n] 0개 —
# 2026-10-06 구조 감사). 긴 기록이 앞의 지시를 밀어내는 순서 효과라, 지시를 기록 뒤에 두면 3/3
# 정상 요약(prompt-order-is-priority의 같은 원리를 반대 방향으로 쓴다).
_SUMMARY_PROMPT = (
    "다음은 사용자와 AI 어시스턴트(Yes24 책·상품 안내)의 대화 기록이다. 앞부분이 이미 요약본일 수 "
    "있다.\n\n"
    "[대화 기록 시작]\n{conversation_history}\n[대화 기록 끝]\n\n"
    "위 대화 기록에 답하지 말고 **요약만** 작성하라. 이어질 대화에서 맥락을 잇는 데 필요한 것만 "
    "한국어로 간결하게 정리한다: 사용자가 밝힌 관심사·조건·취향, 지금까지 추천하거나 안내한 "
    "책·상품(제목·저자와 본문에 붙은 [n] 출처 번호를 그대로 유지), 확인된 사실과 결정, 아직 "
    "해결되지 않은 질문. 도구 결과 원문이나 가격표 전체는 옮기지 말고 결론만 남긴다."
)

# 응답 없는 함수콜을 닫는 자리표시 응답. 중단 사실만 담는다 — 모델이 사실로 인용할 내용이 없다.
_ABORTED_RESPONSE = {"error": "aborted"}

# 진행 중인 압축 task 참조 — GC 취소를 막는다(UsageLogger._tasks 관례). 종료 훅은 두지 않는다:
# 중단된 압축은 아무것도 쓰지 않았고 다음 턴이 같은 판정으로 다시 예약한다.
_tasks: set[asyncio.Task] = set()


@lru_cache(maxsize=1)
def _config() -> EventsCompactionConfig:
    """압축 설정 + 요약기(프로세스당 1개 — genai 클라이언트·연결 재사용, 실험 기능 경고도 1회).

    요약은 ADK contents 조립에서 author=model의 발화로 실린다 — 에이전트 이름과 다르므로 ADK가
    `For context: [model] said: …` 꼴의 user 역할 문맥으로 바꿔 넣는다(ADK 공통 동작, 모델은
    요약을 '앞선 대화의 요지'로 받는다).
    """
    settings = get_settings()
    return EventsCompactionConfig(
        # 슬라이딩 윈도(N턴마다 압축)는 쓰지 않는다. 두 필드는 ADK 모델에서 필수라 값을 두지만,
        # 우리는 token-threshold 함수만 부르므로 어디서도 읽히지 않는다(트레이스 속성뿐).
        compaction_interval=0,
        overlap_size=0,
        token_threshold=settings.history_compaction_token_threshold,
        event_retention_size=settings.history_compaction_retention_events,
        summarizer=LlmEventSummarizer(
            llm=Gemini(model=settings.history_compaction_model), prompt_template=_SUMMARY_PROMPT
        ),
    )


def close_dangling_calls(events: list[Event]) -> list[Event]:
    """응답 없는 함수콜 바로 뒤에 중단 응답을 끼운 **압축 입력용** 이벤트 목록(세션엔 쓰지 않는다).

    클라이언트가 도구 실행 중 끊은 턴(outcome=aborted)은 function_call만 남고 응답이 없다. ADK
    압축은 그 콜을 "열린 의무"로 보고 `_longest_self_contained_prefix`가 그 앞에서 멈추므로, 그 뒤
    이벤트는 영영 접히지 않는다(운영 1위 세션: 893건 중 312건만 접히고 재압축 0회, 미결 콜 보유
    세션 5.5% — 2026-10-06 구조 감사). 여기서 닫아 주면 ADK의 짝 판정이 그대로 성립한다.

    세션에 이벤트를 **쓰지 않고** 압축 함수에 넘길 목록만 고치는 이유: ① 이미 운영 DB에 있는
    세션도 다음 압축에서 바로 고쳐진다(소급), ② 닫는 응답을 실제로 append하면 시각이 콜과 멀어
    ADK 분할(`_safe_token_compaction_split_index`)이 콜까지 거슬러 올라가 다시 멈추고, 시각을
    과거로 두면 세션 update_time이 뒤로 간다, ③ 압축 범위(start~end 시각)가 미결 콜을 덮으므로
    다음 턴 contents에서 그 콜은 요약으로 대체된다 — 모델은 자리표시 응답을 보지 않는다.
    """
    answered = {
        response.id
        for event in events
        for response in event.get_function_responses()
        if response.id
    }
    patched: list[Event] = []
    for event in events:
        patched.append(event)
        dangling = [
            call for call in event.get_function_calls() if call.id and call.id not in answered
        ]
        if dangling:
            patched.append(
                Event(
                    author=event.author,
                    invocation_id=event.invocation_id,
                    branch=event.branch,
                    timestamp=event.timestamp,
                    content=types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    id=call.id, name=call.name, response=_ABORTED_RESPONSE
                                )
                            )
                            for call in dangling
                        ],
                    ),
                )
            )
    return patched


async def compact_history(
    service: BaseSessionService, session_id: str, user_id: str, agent: LlmAgent
) -> bool:
    """세션 락 안에서 네이티브 token-threshold compaction을 1회 시도한다(압축했으면 True).

    세션은 **여기서 다시 읽는다** — 턴 스냅샷은 Runner가 이벤트를 영속한 순간 stale이라
    DatabaseSessionService 낙관적 락이 append를 거부한다(runner.py의 재조회 관례와 같다).
    부가 채널이라 어떤 실패도 밖으로 던지지 않는다 — 다음 턴이 같은 판정으로 재시도한다.
    """
    settings = get_settings()
    try:
        async with _get_session_lock(session_id):
            session = await service.get_session(
                app_name=settings.app_name, user_id=user_id, session_id=session_id
            )
            if session is None:
                return False
            session.events = close_dangling_calls(session.events)
            started = time.monotonic()
            compacted = await _run_compaction_for_token_threshold_config(
                config=_config(),
                session=session,
                session_service=service,
                agent=agent,
                agent_name=agent.name,
            )
            if not compacted:
                return False
            elapsed_ms = int((time.monotonic() - started) * 1000)
            # append_event가 인메모리 세션에도 더하므로 방금 쓴 요약 이벤트는 여기서 보인다.
            summary = next(
                event for event in reversed(session.events) if event.actions.compaction
            )
            record_usage(
                USAGE_COMPONENT,
                summary.usage_metadata,
                model=settings.history_compaction_model,
                session_id=session_id,
                user_id=user_id,
                latency_ms=elapsed_ms,
            )
            logger.info(
                f"이력 압축 완료 {elapsed_ms}ms "
                f"(events={len(session.events)} session_id={session_id})"
            )
            return True
    except Exception as exc:  # noqa: BLE001 — 부가 채널: 압축 실패가 어떤 턴도 막지 않는다
        logger.warning(f"이력 압축 실패(다음 턴에 재시도, session_id={session_id}): {exc}")
        return False


def schedule_history_compaction(
    service: BaseSessionService,
    session_id: str,
    user_id: str,
    agent: LlmAgent,
    *,
    prompt_tokens: int,
) -> None:
    """턴 마감에서 호출 — 마지막 LLM 콜의 프롬프트가 임계값 이상일 때만 압축 task를 예약한다.

    판정을 여기서 먼저 내는 이유: 네이티브 함수도 같은 임계값을 세션 이벤트에서 다시 확인하지만
    그러려면 세션을 한 번 더 읽어야 한다 — 정상 사용자(대화당 1~3턴)의 턴마다 DB 왕복과 task를
    붙이지 않는다. 예약된 task는 호출 턴이 세션 락을 놓은 뒤에야 돈다(asyncio.Lock 대기 FIFO —
    같은 세션의 다음 턴 요청보다 먼저 줄을 선다).
    """
    settings = get_settings()
    if (
        not settings.history_compaction_model
        or prompt_tokens < settings.history_compaction_token_threshold
    ):
        return
    task = asyncio.get_running_loop().create_task(
        compact_history(service, session_id, user_id, agent)
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
