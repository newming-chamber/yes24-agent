"""대화 히스토리 API — 목록(검색·unread)·복원(=읽음)·이름 변경·삭제·턴 피드백 (`/chat/sessions*`).

**새 저장소가 없다.** 히스토리는 ADK 세션 서비스가 이미 영속한 `events`에서 투영하고
(sessions·events의 소유자는 ADK다), 턴 경계는 ADK가 턴마다 부여하는 `invocation_id`다 —
`/chat/stream` done의 `turn_id`, admin 턴 집계와 같은 열쇠. 이 모듈이 이력 투영의 단일
소유자다(같은 판정 두 곳 금지 — 본문 재조립·인용 마감은 runner·postprocess의 그 함수를
그대로 빌려 쓴다).

**소유권은 x-api-key다(401).** 익명(단일 poc 사용자) 풀은 사실상 전 방문자의 공유 기록이라
"내 대화 목록"으로 내줄 수 없다 — 키 없이 열면 남의 세션이 섞인다(2026-09-01 실측: 929
세션 중 928이 익명). 키가 있어도 Yes24 회원 식별(userNo)이 아직 없으면 소유 판정 자체가
불가능해 403이다. ADK 세션 키가 (app_name, user_id, session_id) 복합이라 남의 session_id로
조회·삭제해도 자기 user_id 밑에서만 성립한다(404) — 접근 제어가 키 구조에서 나온다.

**복원 계약 — 과거 턴의 출처는 '그때 값'이 아니라 '지금 값'이다.** 세션 출처 레지스트리는
같은 URL을 최신 관측으로 갱신하므로(CLAUDE.md 결정 4) 복원된 출처 카드의 가격·평점은
마지막 관측 시점(checked_at)의 값이다. 턴별 본문은 저장된 원시 본문을 스트림과 **같은
조립기**(postprocess.finalize_answer: 검증→표시 재번호→인용분 투영)로 다시 마감한 것이다.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints

from yes24_agent.auth import AuthenticatedUser, get_authenticated_user
from yes24_agent.config import get_settings
from yes24_agent.enrichment import SESSION_TITLE_STATE_KEY
from yes24_agent.postprocess import finalize_answer
from yes24_agent.runner import _event_text, _round_boundary_prefix
from yes24_agent.session_service import _get_session_service
from yes24_agent.sources import get_sources
from yes24_agent.user_data import UserDataService

logger = logging.getLogger(__name__)


# ── 응답/요청 모델 (OpenAPI가 목적 — /docs만 보고 클라이언트를 만들 수 있어야 한다) ──


class ErrorDetail(BaseModel):
    """실패 응답 본문(FastAPI HTTPException 표준형)."""

    detail: str = Field(description="사용자에게 보여줄 실패 사유")


# 전 라우트 공통의 인증 실패 응답 기술. 401/403 사유는 _require_identified가 소유한다.
_AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorDetail, "description": "x-api-key 헤더 없음(익명 요청)"},
    403: {
        "model": ErrorDetail,
        "description": "키는 유효하나 Yes24 회원 식별(userNo)이 아직 없어 소유 판정 불가",
    },
}
_SESSION_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_AUTH_RESPONSES,
    404: {"model": ErrorDetail, "description": "이 사용자의 세션이 아니거나 존재하지 않음"},
}


class SessionSummary(BaseModel):
    """대화 목록 한 줄."""

    session_id: str = Field(description="후속 /chat/stream·복원·삭제에 쓰는 세션 id")
    title: str | None = Field(
        description="서버가 생성한 세션 제목 — **첫 턴이 완료되면** 잡담이든 아니든 자동"
        " 생성된다. null인 것은 아직 완료된 턴이 없는 세션(오버뷰 이어가기로 막 만들어진"
        " 세션 등)이거나 제목 생성이 실패한 세션이다"
    )
    last_update_time: float = Field(description="마지막 활동 시각(epoch 초)")
    unread: bool = Field(
        description="답변 생성 완료 후 아직 열어보지 않은 세션이면 true(LNB 파란 점)."
        " 대화를 복원(GET /chat/sessions/{id})하면 읽음이 된다. 이미 화면에 떠 있는 대화의"
        " 배지만 끄려면 POST /chat/sessions/{id}/read를 쓴다(대화를 다시 받아오지 않는다)."
    )


class SessionListResponse(BaseModel):
    """`GET /chat/sessions` 응답."""

    sessions: list[SessionSummary] = Field(
        description="내(x-api-key 소유자) 세션, 최근 활동순. 상한은 서버 설정"
    )


class TurnFeedbackState(BaseModel):
    """턴에 남아 있는 피드백(없으면 복원 응답에서 null)."""

    rating: Literal["up", "down"] | None = Field(description="'up' | 'down', 철회됐으면 null")
    comment: str | None = Field(default=None, description="선택 코멘트")


class TurnView(BaseModel):
    """복원된 턴 1건 — 사용자 발화와 마감된 답변."""

    turn_id: str = Field(description="ADK invocation_id — /chat/stream done.turn_id와 같은 값")
    started_at: float | None = Field(description="턴 첫 이벤트 시각(epoch 초)")
    user_text: str = Field(description="사용자 발화 원문")
    text: str = Field(
        description="답변 본문(마크다운). [n] 마커는 sources의 id에 매핑된다 — 저장된 원시"
        " 본문을 스트림과 같은 검증·표시 재번호로 다시 마감한 결과다"
    )
    sources: list[dict] = Field(
        description="이 턴이 인용한 출처(등장 순서). 값은 관측 시점이 아니라 **현재** 세션"
        " 레지스트리의 최신 관측이다(같은 URL 재관측이 가격·평점을 갱신함 — checked_at 참조)"
    )
    cited_ids: list[int] = Field(description="본문 마커와 sources를 잇는 인용 id(등장 순서)")
    feedback: TurnFeedbackState | None = Field(
        description="내가 이 턴에 남긴 피드백(없으면 null)"
    )


class SessionDetailResponse(BaseModel):
    """`GET /chat/sessions/{session_id}` 응답 — 대화 복원 재료 전부."""

    session_id: str
    title: str | None = Field(description="세션 제목(없으면 null)")
    turns: list[TurnView] = Field(description="시간순 턴 목록")


# 피드백 코멘트 상한은 채팅 입력과 같은 축(사용자 자유 텍스트 입구 제약)이라
# request_max_chars를 재사용한다 — 별도 필드를 만들면 같은 결정이 두 곳이 된다.
_CommentText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=get_settings().request_max_chars),
]


class SessionRenameRequest(BaseModel):
    """`PATCH /chat/sessions/{id}` 요청 본문 — ⋯ 메뉴의 '이름 변경'."""

    # 상한은 서버 생성 제목과 같은 축(목록 한 줄에 담기는 길이)이라 session_title_max_chars를
    # 재사용한다. 빈 문자열·공백만은 422로 거절한다(트림 후 min_length) — 제목을 지우는
    # 경로는 디자인에 없고, 지우면 다음 턴의 want_title 판정이 제목을 다시 생성해 버린다.
    title: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=get_settings().session_title_max_chars,
        ),
    ] = Field(description="새 세션 제목(트림 후 1자 이상)")


class TurnFeedbackRequest(BaseModel):
    """`PUT …/feedback` 요청 본문. PUT은 멱등 — 같은 턴 재전송은 최신값으로 덮는다."""

    rating: Literal["up", "down", "none"] = Field(
        description="'up'(좋아요) | 'down'(싫어요) | 'none'(철회 — 남긴 피드백을 지운다)"
    )
    comment: _CommentText | None = Field(default=None, description="선택 코멘트")


# ── 공통 판정 ───────────────────────────────────────────────────────────────


def _require_identified(user: AuthenticatedUser | None) -> str:
    """히스토리·피드백의 소유자 열쇠(user_no)를 뽑는다 — 없으면 여기서 끊는다.

    401 detail은 로그인월의 문구("인증이 필요합니다.")와 다르게 둔다 — 월 차단과 라우트
    판정을 응답만 보고 구분할 수 있어야 한다(테스트·운영 디버깅 공통).
    """
    if user is None:
        raise HTTPException(status_code=401, detail="x-api-key 인증이 필요합니다.")
    if not user.user_no:
        raise HTTPException(
            status_code=403,
            detail="Yes24 회원 식별이 완료되지 않은 키입니다. 잠시 후 다시 시도해 주세요.",
        )
    return str(user.user_no)


async def _owned_session(user_no: str, session_id: str):
    """이 사용자 소유의 세션을 조회한다 — 남의 것·없는 것은 같은 404다(존재 노출 금지)."""
    service = _get_session_service()
    session = await service.get_session(
        app_name=get_settings().app_name, user_id=user_no, session_id=session_id
    )
    if session is None:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return service, session


def _normalized(text: str) -> str:
    """검색 비교용 정규화 — casefold + 공백 압축. 최소한만 한다(키워드 목록·형태소 금지)."""
    return " ".join(text.casefold().split())


async def _mark_read_best_effort(data, user_no: str, session, last_read_at: float | None) -> None:
    """읽음 기록 — 복원(GET)과 읽음 표시(POST)가 **같은 판정·같은 실패 정책**을 쓴다.

    이미 읽은 상태면 쓰지 않는다(반복 조회가 행을 갱신하지 않는다). 기록 실패는 응답을 막지
    않는다: 배지는 부가 채널이고 복원이 제품이다 — 로그로 정직하게 남긴다.

    저장하는 값이 벽시계가 아니라 `session.last_update_time`인 이유는 비교 대상과 **같은
    값**이어야 시계 오차로 unread가 되살아나지 않기 때문이다(user_data.mark_read 참조).
    """
    if not _is_unread(session.last_update_time, last_read_at):
        return
    try:
        await data.mark_read(
            user_id=user_no, session_id=session.id, read_at=session.last_update_time
        )
    except Exception as exc:  # noqa: BLE001 — 부가 채널(응답 보호)
        logger.warning(f"읽음 기록 실패(session_id={session.id}): {exc}")


def _is_unread(last_update_time: float, last_read_at: float | None) -> bool:
    """읽지 않음 판정의 단일 소유자 — 목록(unread 필드)과 복원(읽음 기록 여부)이 같이 쓴다.

    한 번도 열어본 적 없는 세션(last_read_at 없음)은 true다. 두 값은 같은 시간 도메인이다
    (읽음 기록이 벽시계가 아니라 그때의 last_update_time을 그대로 저장한다 — UserDataService).
    """
    return last_read_at is None or last_update_time > last_read_at


# ── 이력 투영 (이 모듈이 단일 소유자) ───────────────────────────────────────


def _assemble_turns(events: list) -> list[dict[str, Any]]:
    """영속 이벤트를 턴(invocation) 단위 원시 재료로 묶는다 — 마감 전 단계.

    ADK는 non-partial 이벤트만 영속하므로 모델 텍스트 이벤트는 LLM 콜(라운드)당 1건이다.
    라운드 사이 문단 구분은 스트림과 **같은 규칙**(_round_boundary_prefix — 도구가 돈 뒤의
    이어붙는 텍스트에만, 경계에 공백이 없을 때만)을 쓴다. invocation_id 없는 이벤트(러너의
    state_delta 시스템 write)는 대화가 아니므로 건너뛴다.
    """
    turns: dict[str, dict[str, Any]] = {}
    for event in events:
        invocation_id = event.invocation_id
        if not invocation_id or event.partial:
            continue
        turn = turns.get(invocation_id)
        if turn is None:
            turn = turns[invocation_id] = {
                "turn_id": invocation_id,
                "started_at": event.timestamp,
                "user": [],
                "body": [],
                "tool_ran": False,
            }
        chunk = _event_text(event)
        if event.author == "user":
            if chunk:
                turn["user"].append(chunk)
        elif chunk:
            # 한 이벤트에 서술 텍스트와 함수콜이 함께 실릴 수 있다(ADK 집계 형태) —
            # 텍스트가 콜보다 앞이므로 경계 판정을 먼저 하고 tool_ran을 아래에서 올린다.
            if turn["body"] and turn["tool_ran"]:
                chunk = _round_boundary_prefix(turn["body"], chunk) + chunk
            turn["tool_ran"] = False
            turn["body"].append(chunk)
        if event.get_function_calls() or event.get_function_responses():
            turn["tool_ran"] = True
    return list(turns.values())  # dict 삽입 순서 = 턴 등장 순서(이벤트는 시간순)


def _title_of(session, user_title: str | None) -> str | None:
    """표시할 제목의 **단일 판정** — 목록·복원이 같이 쓴다.

    사용자가 지은 것이 있으면 그것이, 없으면 서버 자동 생성분이 이긴다. 두 자리에 나눠 둔
    이유는 소유권이다: 자동 제목은 대화에서 파생된 값이라 ADK 세션 state에 남고(runner가
    쓴다), 사용자 제목은 사용자 데이터라 우리 테이블에 남는다. runner의 `want_title`은
    여전히 state만 보므로 자동 생성 로직은 이 변경을 모른다.
    """
    return user_title or session.state.get(SESSION_TITLE_STATE_KEY)


def _project_session_detail(
    session, feedback_by_turn: dict[str, dict[str, Any]], user_title: str | None = None
) -> SessionDetailResponse:
    """세션 이벤트·레지스트리·피드백을 복원 응답으로 투영한다.

    본문 마감은 스트림의 그 조립기(finalize_answer) 그대로다 — 검증 집합은 세션 누적
    레지스트리(원칙 4), 표시 번호는 턴마다 1..n이라 라이브 스트림과 같은 번호가 나온다.
    """
    registry = get_sources(session.state)
    turns: list[TurnView] = []
    for raw in _assemble_turns(session.events):
        _, payload = finalize_answer("".join(raw["body"]), registry, session.id)
        feedback = feedback_by_turn.get(raw["turn_id"])
        turns.append(
            TurnView(
                turn_id=raw["turn_id"],
                started_at=raw["started_at"],
                user_text="\n".join(raw["user"]),
                text=payload["text"] if raw["body"] else "",
                sources=payload["sources"],
                cited_ids=payload["cited_ids"],
                feedback=TurnFeedbackState(**feedback) if feedback else None,
            )
        )
    return SessionDetailResponse(
        session_id=session.id,
        title=_title_of(session, user_title),
        turns=turns,
    )


# ── 라우트 ─────────────────────────────────────────────────────────────────
# 주의 1: 인증 의존성은 라우트에 **직접** 단다(중첩 의존성 금지) — 로그인월의 x-api-key 통과
# 집합이 라우트의 1단계 의존성 그래프에서 파생되므로(main._key_checking_routes), 한 겹
# 감싸면 월이 이 라우트를 영영 열지 않는다.
# 주의 2: APIRouter + include_router가 아니라 **앱에 직접 등록**한다(register_admin 관례).
# 현행 FastAPI는 include_router를 지연 프록시(_IncludedRouter)로 얹어 app.routes에 APIRoute가
# 실체화되지 않고, 그러면 월의 통과 집합 파생이 이 라우트들을 영영 못 본다(실측 —
# test_history의 월 파생 가드가 빨강으로 잡았다). fail-closed라 노출은 아니지만 기능이 죽는다.

_UserDep = Annotated[AuthenticatedUser | None, Depends(get_authenticated_user)]


def register_history(app: FastAPI) -> None:
    """히스토리·피드백 라우트를 앱에 등록한다 — 라우트·투영의 소유자는 이 모듈이다."""

    @app.get(
        "/chat/sessions",
        tags=["history"],
        response_model=SessionListResponse,
        responses=_AUTH_RESPONSES,
        summary="내 대화 목록(+검색)",
        description="x-api-key 소유자의 세션을 최근 활동순으로 돌려준다. 제목은 첫 턴이"
        " 끝날 때 자동 생성되므로 title이 null인 것은 **아직 완료된 턴이 없는 세션**이다."
        " `q`는 **제목 기준**"
        " 부분일치다(대소문자·공백 정규화) — 본문 전문 검색은 지원하지 않는다: 본문은"
        " events의 JSON 안에 있어 검색이 사용자 전 세션·전 이벤트 스캔이 되고, 목록 API의"
        " 비용 축이 달라진다(제목이 이미 대화 내용의 요약이라 히스토리 패널 용도로 충분).",
    )
    async def list_sessions(
        q: Annotated[
            str | None,
            Query(description="제목 부분일치 검색어(대소문자·공백 정규화). 없으면 전체 목록"),
        ] = None,
        user: _UserDep = None,
    ) -> SessionListResponse:
        user_no = _require_identified(user)
        service = _get_session_service()
        listing = await service.list_sessions(app_name=get_settings().app_name, user_id=user_no)
        recent = sorted(listing.sessions, key=lambda s: s.last_update_time, reverse=True)
        # 사용자 제목·읽음 시각을 **한 번에** 읽는다(세션당 질의는 N+1이 되는 자리다).
        ui = await UserDataService.get_instance().ui_for_user(user_id=user_no)
        rows = [(s, *ui.get(s.id, (None, None))) for s in recent]
        if q is not None and (needle := _normalized(q)):
            # 제목 없는 세션(title=null)은 어떤 검색어에도 잡히지 않는다 — 보여줄 실마리가
            # 없는 항목을 부분일치로 내주면 목록이 왜 나왔는지 설명 불가능해진다.
            rows = [row for row in rows if needle in _normalized(_title_of(row[0], row[1]) or "")]
        return SessionListResponse(
            sessions=[
                SessionSummary(
                    session_id=session.id,
                    title=_title_of(session, user_title),
                    last_update_time=session.last_update_time,
                    unread=_is_unread(session.last_update_time, last_read_at),
                )
                for session, user_title, last_read_at in rows[
                    : get_settings().history_sessions_limit
                ]
            ]
        )

    @app.get(
        "/chat/sessions/{session_id}",
        tags=["history"],
        response_model=SessionDetailResponse,
        responses=_SESSION_RESPONSES,
        summary="대화 복원(턴·본문·출처)",
        description="세션의 턴들을 시간순으로 돌려준다. 본문 [n] 마커는 각 턴 sources의 id에"
        " 매핑된다(/chat/stream done과 같은 계약). **출처 값은 관측 스냅샷이 아니라 세션"
        " 레지스트리의 현재(최신 관측) 값이다** — 같은 상품을 나중 턴이 다시 관측했다면 이전"
        " 턴의 카드도 그 최신 가격·평점으로 보인다(checked_at이 그 관측 시각)."
        " 이 조회가 곧 **읽음 처리**다 — 복원해 화면에 그린 것이 '봤다'의 자연스러운 정의다."
        " 대화를 다시 받지 않고 배지만 끄려면 POST /chat/sessions/{session_id}/read를 쓴다.",
    )
    async def session_detail(session_id: str, user: _UserDep = None) -> SessionDetailResponse:
        user_no = _require_identified(user)
        service, session = await _owned_session(user_no, session_id)
        data = UserDataService.get_instance()
        feedback_by_turn = await data.feedback_for_session(
            user_id=user_no, session_id=session.id
        )
        user_title, last_read_at = await data.ui_get(user_id=user_no, session_id=session.id)
        detail = _project_session_detail(session, feedback_by_turn, user_title)
        await _mark_read_best_effort(data, user_no, session, last_read_at)
        return detail

    @app.post(
        "/chat/sessions/{session_id}/read",
        tags=["history"],
        response_model=SessionSummary,
        responses=_SESSION_RESPONSES,
        summary="읽음 표시",
        description="이 대화를 **읽은 것으로 표시**한다(멱등). 복원(GET)도 읽음 처리를 하므로"
        " 대화를 열어 그리는 흐름에서는 따로 부를 필요가 없다. 이 라우트는 **이미 화면에 있는"
        " 대화**를 위한 것이다 — 방금 답변을 받아 다 읽은 창에서 배지만 끄려고 대화 전체를"
        " 다시 받아오는 낭비를 없앤다. 응답은 갱신된 목록 항목이라 그대로 목록에 반영하면 된다.",
    )
    async def mark_session_read(session_id: str, user: _UserDep = None) -> SessionSummary:
        user_no = _require_identified(user)
        _, session = await _owned_session(user_no, session_id)
        data = UserDataService.get_instance()
        user_title, last_read_at = await data.ui_get(user_id=user_no, session_id=session.id)
        # 읽은 시점은 **서버가 정한다**(클라이언트가 보낸 시각을 믿지 않는다). 미래 시각을
        # 실어 보내면 이후 어떤 새 답변도 영영 읽음으로 보이게 만들 수 있기 때문이다.
        await _mark_read_best_effort(data, user_no, session, last_read_at)
        return SessionSummary(
            session_id=session.id,
            title=_title_of(session, user_title),
            last_update_time=session.last_update_time,
            unread=False,
        )

    @app.patch(
        "/chat/sessions/{session_id}",
        tags=["history"],
        response_model=SessionSummary,
        responses=_SESSION_RESPONSES,
        summary="대화 이름 변경",
        description="⋯ 메뉴의 '이름 변경'. 세션 제목을 사용자가 준 값으로 바꾼다. 사용자 제목은"
        " 서버의 자동 제목보다 **항상 우선**이라 이후 턴의 자동 생성이 화면을 되덮지 않는다."
        " 빈 문자열·공백만인 제목은 422다(제목을 지우는 경로는 없다)."
        " 이름 변경은 활동이 아니다 — last_update_time(목록 순서)과 unread를 바꾸지 않는다.",
    )
    async def rename_session(
        session_id: str, request: SessionRenameRequest, user: _UserDep = None
    ) -> SessionSummary:
        user_no = _require_identified(user)
        _, session = await _owned_session(user_no, session_id)
        data = UserDataService.get_instance()
        # 사용자 제목은 **우리 테이블에** 쓴다(ADK 세션 state가 아니라). 그래야 이름 변경이
        # 목록의 활동 시각을 밀지 않는다 — ADK 세션은 state를 쓰는 순간 update_time이
        # onupdate로 현재가 된다(user_data.py 독스트링). 자동 제목은 여전히 ADK state에
        # 남고, 표시할 때 사용자 제목이 그것을 덮는다(_title_of).
        await data.set_title(user_id=user_no, session_id=session.id, title=request.title)
        _, last_read_at = await data.ui_get(user_id=user_no, session_id=session.id)
        return SessionSummary(
            session_id=session.id,
            title=request.title,
            last_update_time=session.last_update_time,
            unread=_is_unread(session.last_update_time, last_read_at),
        )

    @app.delete(
        "/chat/sessions/{session_id}",
        tags=["history"],
        status_code=204,
        responses={**_SESSION_RESPONSES, 204: {"description": "삭제 완료(응답 본문 없음)"}},
        summary="대화 삭제",
        description="세션과 그 이벤트, 그리고 **그 대화에 남긴 것 전부**(좋아요/싫어요·코멘트·"
        "직접 지은 제목)를 지운다. 되돌릴 수 없다. 집계 신호를 잃는 대가는 치른다 — 사용자가"
        " 지우겠다고 한 것이 우선이다(삭제 API의 존재 이유가 프라이버시인데 코멘트가 남으면"
        " 삭제가 아니다).",
    )
    async def delete_session(session_id: str, user: _UserDep = None) -> None:
        user_no = _require_identified(user)
        service, session = await _owned_session(user_no, session_id)
        # **피드백을 먼저 지운다.** 세션만 지우면 사용자가 쓴 코멘트가 session_id·user_id와
        # 함께 남는다 — 이 API의 존재 이유가 프라이버시인데 그러면 삭제가 아니다(2026-09-01
        # 라이브 검증에서 고아 행 관측). 순서가 이쪽인 이유: 피드백 삭제가 실패하면 5xx로
        # 끊겨 세션이 남고 사용자가 다시 누를 수 있다. 반대로 하면 세션은 사라졌는데 코멘트만
        # 남아 되지울 방법이 없어진다(soft-fail보다 재시도 가능한 실패가 낫다).
        data = UserDataService.get_instance()
        if data.enabled:
            await data.purge_session(user_id=user_no, session_id=session.id)
        try:
            await service.delete_session(
                app_name=get_settings().app_name, user_id=user_no, session_id=session.id
            )
        except Exception as exc:  # noqa: BLE001 — 500이 아니라 **재시도 가능한** 실패로 알린다
            # 여기까지 왔으면 우리 쪽 데이터는 이미 지워졌다. 그대로 500을 내면 프론트는
            # 재시도 가능한 실패인지 알 수 없다 — 다시 누르면 세션까지 지워져 최종 상태가
            # 맞으므로(우리 쪽 삭제는 멱등) 503으로 정직하게 알린다.
            logger.error(f"세션 삭제 실패(session_id={session.id}): {exc}")
            raise HTTPException(
                status_code=503, detail="대화를 지우지 못했습니다. 잠시 후 다시 시도해 주세요."
            ) from exc
        logger.info(f"세션 삭제: session_id={session.id} user_no={user_no}")

    @app.put(
        "/chat/sessions/{session_id}/turns/{turn_id}/feedback",
        tags=["history"],
        response_model=TurnFeedbackState,
        responses={
            **_SESSION_RESPONSES,
            404: {
                "model": ErrorDetail,
                "description": "세션 또는 턴(turn_id)이 이 사용자의 것으로 존재하지 않음",
            },
            503: {
                "model": ErrorDetail,
                "description": "피드백 DB 실패 — 저장되지 않았다(재시도 대상)",
            },
        },
        summary="턴 피드백",
        description="턴(=/chat/stream done.turn_id)에 좋아요/싫어요와 선택 코멘트를 남긴다."
        " PUT은 멱등 — 재전송은 덮어쓰고, rating='none'은 철회다. 저장 실패는 5xx로 정직하게"
        " 끊는다(성공 응답 = 실제로 저장됨).",
    )
    async def put_turn_feedback(
        session_id: str,
        turn_id: str,
        request: TurnFeedbackRequest,
        user: _UserDep = None,
    ) -> TurnFeedbackState:
        user_no = _require_identified(user)
        _, session = await _owned_session(user_no, session_id)
        # 턴 실재 검증 — 존재하지 않는 turn_id로 쓰레기 행이 쌓이지 않게 한다. 열쇠는 이벤트에
        # 이미 있는 invocation_id뿐이라 별도 인덱스·스캔 구조가 필요 없다.
        if not any(event.invocation_id == turn_id for event in session.events):
            raise HTTPException(status_code=404, detail="해당 턴을 찾을 수 없습니다.")
        data = UserDataService.get_instance()
        comment = request.comment or None
        if request.rating == "none":
            await data.withdraw_feedback(
                user_id=user_no, session_id=session.id, turn_id=turn_id
            )
            return TurnFeedbackState(rating=None, comment=None)
        await data.upsert_feedback(
            user_id=user_no,
            session_id=session.id,
            turn_id=turn_id,
            rating=request.rating,
            comment=comment,
        )
        return TurnFeedbackState(rating=request.rating, comment=comment)
