"""대화 히스토리 API — 목록(검색·unread)·복원(=읽음)·이름 변경·삭제·턴 피드백·클릭 기록
(`/chat/sessions*`).

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

**복원 계약 — 공개 턴 스냅샷을 우선한다.** 본문·출처·메타·종료 상태·조사 과정은 완료 당시
값을 복원한다. 스냅샷이 없는 구 턴만 원시 이벤트를 다시 조립하고 현재 출처 레지스트리를
참조한다. 구 턴의 종료 상태는 추측하지 않고 unknown으로, history_saved는 false로 표시한다.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    UrlConstraints,
    model_serializer,
)
from typing_extensions import TypedDict

from yes24_agent.auth import AuthenticatedUser, AuthService, get_authenticated_user
from yes24_agent.config import get_settings
from yes24_agent.enrichment import SESSION_TITLE_STATE_KEY
from yes24_agent.event_translate import PROCESS_TIMING_KEY, TurnProcess
from yes24_agent.postprocess import finalize_answer, finalize_text
from yes24_agent.runner import _event_text, _round_boundary_prefix
from yes24_agent.session_service import _POC_USER_ID, _get_session_service
from yes24_agent.sources import get_sources
from yes24_agent.toolsets import TOOLSET_PUBLIC_SOURCE_TYPE, TOOLSET_SOURCE_TYPES
from yes24_agent.turn_snapshot import TURN_SNAPSHOT_KEY
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


class StepSource(BaseModel):
    """found 스텝이 찾은 출처 1건 — 키는 url이다(표시 번호 없음: `[n]`은 인용된 출처만 받는다)."""

    url: str = Field(
        description="출처 url — 인용되면 status refs{id,url}·출처 카드가 이 url로 잇는다"
    )
    title: str = Field(description="도구가 돌려준 제목(가격·평점 등 상품 사실은 싣지 않는다)")


class TurnProcessStep(BaseModel):
    """턴 과정의 도구 스텝 1건 — /chat/stream status 프레임과 같은 stage·detail(·sources)."""

    round: int = Field(description="이 스텝이 속한 LLM 라운드(0부터)")
    stage: str = Field(description="searching·searching_web·reading·browsing·working·found·notice")
    detail: str = Field(description="검색 각도·상세 제목·코너명·'N건 찾았어요'·안내 문구")
    step_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="도구 호출 식별자. 같은 호출의 시작·완료·실패 상태를 연결한다",
    )
    state: Literal["running", "completed", "failed"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="도구 단계 상태. 턴 전체의 종료 상태와는 별개다",
    )
    result_count: int | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="이 호출의 결과 건수. 0도 유효하며 문구를 파싱할 필요가 없다",
    )
    sources: list[StepSource] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="완료·실패 스텝의 관측 출처 url·title(응답 순서). 결과가 없으면 []."
        " 실행 시작 스텝과 일부 구 턴에는 키가 없다",
    )


class TurnProcessView(BaseModel):
    """턴 과정 요약 — /chat/stream `done.process`와 동형(같은 누적기가 만든다)."""

    elapsed_ms: int = Field(
        description="라이브 done.process.elapsed_ms와 같은 값(라이브가 턴에 영속한 것)."
        " 타이밍 영속 이전의 구 턴만 영속 timestamp(첫 이벤트 → 마지막 이벤트)로 근사"
    )
    answer_at_ms: int = Field(
        description="라이브 done.process.answer_at_ms와 같은 값 — 접힌 헤더 'N초'의 재료."
        " 구 턴만 마지막 라운드 텍스트 이벤트의 timestamp로 근사(텍스트가 없으면 elapsed_ms)"
    )
    sources_reviewed: int = Field(
        description="이 턴 도구 응답으로 관측한 고유 출처 수(인용 수와 다르다)"
    )
    offset_unit: Literal["unicode_codepoint"] = Field(
        default="unicode_codepoint",
        description="answer_start와 round_starts의 단위. JavaScript UTF-16 slice 인덱스가"
        " 아니다. Array.from(text)로 코드포인트 배열을 만든 뒤 slice한다",
    )
    answer_start: int = Field(
        description="text에서 최종 답(마지막 라운드)이 시작하는 문자 오프셋 — text[:n]이 조사"
        " 경과, text[n:]이 답. 단일 라운드면 0. 항상 round_starts[-1]과 같다"
    )
    round_starts: list[int] = Field(
        description="라운드 r의 텍스트가 text에서 시작하는 오프셋(첫 원소 0, 단조 비감소, 마지막"
        " = answer_start). 라운드 r 텍스트 = text[round_starts[r]:round_starts[r+1]](마지막은"
        " 끝까지 = 최종 답), round==r인 스텝은 그 텍스트 뒤에 온다 — 라이브 순서의 복원 재료"
    )
    steps: list[TurnProcessStep] = Field(
        description="도구 스텝(순서대로). 사고 요약(thinking)은 영속되지 않아 복원엔 없다"
    )


class SourceFormat(TypedDict, total=False):
    """관측한 다른 판형. 생략·null·0은 서로 다른 값이다."""

    format: str | None
    url: str | None
    sale_price: int | float | None


class SourceCard(BaseModel):
    """인용·카드·자료 탭이 공유하는 공개 출처. 미관측 필드는 보내지 않는다."""

    model_config = ConfigDict(extra="allow")

    id: int = Field(description="턴별 표시 번호. 본문 [n]·추천 id와 같은 번호 공간이다")
    title: str = Field(description="관측한 제목. 빈 문자열이면 프론트에서 도메인 등을 표시한다")
    url: str = Field(description="원문 링크. URL만으로 카드 분류를 추측하지 않는다")
    type: str = Field(description="기존 공개 분류 product | notice | web. 클릭 기록에도 사용한다")
    card_type: Literal["book", "document", "link"] = Field(
        description="관측 정보로 정한 카드 템플릿. product라고 반드시 book인 것은 아니다"
    )
    preview: str | None = Field(default=None, description="표시용 발췌. 전체 원문이 아니다")
    goods_no: str | int | None = Field(
        default=None, description="관측한 상품 번호. 인용 id와 다르다"
    )
    author: str | None = Field(default=None, description="관측한 저자")
    author_no: str | None = Field(default=None, description="관측한 저자 번호")
    publisher: str | None = Field(default=None, description="관측한 출판사")
    image_url: str | None = Field(default=None, description="표지·이미지 URL")
    sale_price: int | float | None = Field(
        default=None, description="관측 판매가(원). null은 확인 불가, 0은 실제 숫자 0이다"
    )
    list_price: int | float | None = Field(default=None, description="관측 정가(원)")
    rating: int | float | None = Field(default=None, description="관측 평점")
    review_count: int | None = Field(default=None, description="관측 리뷰 수")
    page_count: int | None = Field(default=None, description="관측 쪽수")
    sale_index: int | None = Field(default=None, description="관측 판매 지수")
    rank: int | None = Field(default=None, description="관측한 코너 내 순위")
    pub_date: str | None = Field(default=None, description="관측 출간일")
    kind: str | None = Field(default=None, description="관측 상품 종류")
    is_book: bool | None = Field(default=None, description="사이트 구조에서 확인한 도서 여부")
    is_ebook: bool | None = Field(default=None, description="사이트 구조에서 확인한 eBook 여부")
    checked_at: str | None = Field(default=None, description="출처를 확인한 시각(KST)")
    published_at: str | None = Field(default=None, description="웹 자료의 게시 시각")
    last_updated: str | None = Field(default=None, description="웹 자료의 갱신 시각")
    other_formats: list[SourceFormat] | None = Field(
        default=None,
        description="이 출처에서 관측한 다른 판형. []는 관측했으나 없음, 키 생략은 미관측."
        " 판형 링크 자체에 별도 인용 번호를 부여하지 않는다",
    )

    @model_serializer(mode="wrap")
    def _observed_fields_only(self, handler):
        return {
            key: value
            for key, value in handler(self).items()
            if key in self.model_fields_set or key not in type(self).model_fields
        }


class TurnRecommendation(BaseModel):
    """인용 자료와 별개인 추천 대상·이유."""

    id: int = Field(description="이 턴 sources[].id를 참조한다")
    reason: str = Field(description="이 세션에서 그 책을 추천한 이유(이전 턴에서 이월될 수 있다)")


class TurnMeta(BaseModel):
    """없거나 생성 실패해도 턴 본문·출처를 사용할 수 있는 부가 정보."""

    recommendations: list[TurnRecommendation] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    session_title: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="이 턴에서 생성한 제목. 현재 세션 제목은 세션 응답의 title을 사용한다",
    )


class TurnError(BaseModel):
    """서버가 확정한 턴 실패 사유."""

    code: str
    message: str


class TurnView(BaseModel):
    """복원된 턴 1건 — 사용자 발화와 마감된 답변."""

    turn_id: str = Field(description="ADK invocation_id — /chat/stream done.turn_id와 같은 값")
    session_id: str = Field(description="이 턴이 속한 세션 id — done.session_id와 같다")
    started_at: float | None = Field(description="턴 첫 이벤트 시각(epoch 초)")
    user_text: str = Field(description="사용자 발화 원문")
    text: str = Field(
        description="조사 내레이션을 포함한 확정 본문(마크다운). done.text와 동일하며 [n]은"
        " sources의 id에 매핑된다. 최종 답변 범위는 process.answer_start로 구분한다"
    )
    sources: list[SourceCard] = Field(
        description="이 턴이 인용한 공개 출처(등장 순서). history_saved=true면 완료 당시 값."
        " false인 구 턴만 현재 레지스트리 최신 관측이므로 과거 가격·평점을 보장하지 않는다."
        " type은 기존 분류, card_type은 카드 렌더링 분류이며 other_formats 등의 목록을 보존한다"
    )
    cited_ids: list[int] = Field(description="본문 마커와 sources를 잇는 인용 id(등장 순서)")
    meta: TurnMeta = Field(default_factory=TurnMeta, description="완료 당시 부가 정보")
    status: Literal["completed", "failed", "interrupted", "unknown"] = Field(
        default="unknown",
        description="서버가 확정한 종료 상태. 스냅샷 없는 구 턴·비정상 종료는 unknown이며"
        " completed로 추측하지 않는다",
    )
    error: TurnError | None = Field(default=None, description="실패 정보. 없거나 알 수 없으면 null")
    rbti_applied: str | None = Field(
        default=None, description="이번 턴 실제 적용한 RBTI 코드. 미적용·구 턴 복원 불가는 null"
    )
    history_saved: bool = Field(
        default=False,
        description="true면 완료 당시 공개 턴 스냅샷에서 복원했다. false면 구 이벤트"
        " 기반 복원이라 출처의 당시 값·부가 정보·정확한 종료 상태를 보장하지 않는다",
    )
    feedback: TurnFeedbackState | None = Field(
        description="내가 이 턴에 남긴 피드백(없으면 null)"
    )
    process: TurnProcessView = Field(
        description="턴 과정 요약(접힌 사고과정 헤더 재료) — 라이브 done.process와 같은 모양"
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


def _public_source_types() -> tuple[str, ...]:
    """공개 출처 어휘(`done.sources[].type`) — toolsets 레지스트리에서 파생한다.

    접히는 toolset은 공개 이름 하나(yes24→product), 안 접히는 toolset은 선언 타입 그대로(web).
    `notice`만 손으로 더한다: 공개 메타 필드가 없어 레지스트리에 선언이 없고(toolsets 주석·
    event_translate passthrough 규칙), 유일한 정의가 yes24_fetch 내부 리터럴이라 끌어올 상수가
    없다. 그 밖의 값은 여기 열거하지 않는다 — 새 toolset은 선언과 함께 자동으로 따라온다.
    """
    names: list[str] = []
    for toolset, types in TOOLSET_SOURCE_TYPES.items():
        public = TOOLSET_PUBLIC_SOURCE_TYPE.get(toolset)
        names.extend([public] if public else types)
    names.append("notice")
    return tuple(dict.fromkeys(names))


PUBLIC_SOURCE_TYPES: tuple[str, ...] = _public_source_types()


class TurnClickRequest(BaseModel):
    """`POST …/clicks` 요청 본문 — 답변 안의 링크를 눌렀다는 기록 1건.

    열쇠는 URL이다(상품·공지·웹·판형 링크 무엇이든 URL은 있다). 스킴은 http(s)만 — 저장된
    URL은 나중에 집계 화면에서 링크로 렌더될 수 있으므로 `javascript:` 류를 입구에서 끊는다.
    pydantic이 URL을 정규화한다(빈 경로에 `/` 부여, 앞뒤 공백 제거) — 그 정규형이 저장값이다.
    """

    url: Annotated[
        AnyHttpUrl, UrlConstraints(max_length=get_settings().click_url_max_chars)
    ] = Field(description="클릭한 링크(http/https). 상한은 config click_url_max_chars")
    source_id: int | None = Field(
        default=None, description="본문 [n] 마커 번호(출처 카드에서 눌렀으면). 없으면 null"
    )
    source_type: Literal[PUBLIC_SOURCE_TYPES] | None = Field(  # type: ignore[valid-type]
        default=None,
        description="누른 출처의 공개 type(done.sources[].type과 같은 어휘: "
        + " | ".join(PUBLIC_SOURCE_TYPES)
        + "). 없으면 null",
    )
    label: (
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True, max_length=get_settings().click_label_max_chars
            ),
        ]
        | None
    ) = Field(default=None, description="화면에 표시된 제목(선택)")


# ── 공통 판정 ───────────────────────────────────────────────────────────────


def _require_identified(user: AuthenticatedUser | None) -> str:
    """히스토리·피드백의 소유자 열쇠(user_no)를 뽑는다 — 없으면 여기서 끊는다.

    401 detail은 로그인월의 문구("인증이 필요합니다.")와 다르게 둔다 — 월 차단과 라우트
    판정을 응답만 보고 구분할 수 있어야 한다(테스트·운영 디버깅 공통).

    **인증 스택이 없는 구성(로컬 sqlite)에서는 러너와 같은 단일 사용자로 흘린다.** 그러지
    않으면 로컬에서 대화는 되는데(runner가 `user_id or _POC_USER_ID`로 폴백한다) 그 대화의
    목록·복원만 401이라, 프론트가 히스토리 화면을 로컬에서 만들 수 없다(2026-09-02 실측).
    같은 요청을 두 계층이 다르게 판정하던 것이라 러너 쪽에 맞춘다 — 배포에서는 인증 스택이
    항상 켜져 있어 이 분기가 돌지 않는다(구조 분기, 키워드 예외가 아니다).
    """
    if user is None:
        if not AuthService.get_instance().enabled:
            return _POC_USER_ID
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


async def _owned_turn(user_no: str, session_id: str, turn_id: str):
    """소유 세션 + 그 안에 실재하는 턴 — 턴에 무언가를 남기는 라우트(피드백·클릭)의 공통 입구.

    턴 실재 검증은 존재하지 않는 turn_id로 쓰레기 행이 쌓이지 않게 한다. 열쇠는 이벤트에
    이미 있는 invocation_id뿐이라 별도 인덱스·스캔 구조가 필요 없다.
    """
    _, session = await _owned_session(user_no, session_id)
    if not any(event.invocation_id == turn_id for event in session.events):
        raise HTTPException(status_code=404, detail="해당 턴을 찾을 수 없습니다.")
    return session


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

    공개 스냅샷이 있는 턴은 사용자 발화·시각만 읽는다. 스냅샷 없는 구 턴만
    라이브와 같은 TurnProcess로 재조립하며, 구 타이밍 메타데이터는 본문에서 제외한다.
    """
    snapshots = {
        event.invocation_id: snapshot
        for event in events
        if event.invocation_id
        and not event.partial
        and (snapshot := (event.custom_metadata or {}).get(TURN_SNAPSHOT_KEY)) is not None
    }
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
                "ended_at": event.timestamp,
                "user": [],
                "body": [],
                "tool_ran": False,
                "process": None if invocation_id in snapshots else TurnProcess(),
                "timing": None,
                "snapshot": snapshots.get(invocation_id),
            }
        metadata = event.custom_metadata or {}
        if metadata.get(TURN_SNAPSHOT_KEY) is not None:
            continue
        timing = metadata.get(PROCESS_TIMING_KEY)
        if timing is not None:
            turn["timing"] = timing
            continue
        turn["ended_at"] = event.timestamp
        chunk = _event_text(event) if event.author == "user" or turn["snapshot"] is None else ""
        if event.author == "user" and chunk:
            turn["user"].append(chunk)
        if turn["snapshot"] is not None:
            continue
        process: TurnProcess = turn["process"]
        responses = event.get_function_responses()
        if event.author != "user" and not responses:
            process.model_event(sum(map(len, turn["body"])))
        if event.author != "user" and chunk:
            # 한 이벤트에 서술 텍스트와 함수콜이 함께 실릴 수 있다(ADK 집계 형태) —
            # 텍스트가 콜보다 앞이므로 경계 판정을 먼저 하고 tool_ran을 아래에서 올린다.
            if turn["body"] and turn["tool_ran"]:
                chunk = _round_boundary_prefix(turn["body"], chunk) + chunk
            turn["tool_ran"] = False
            turn["body"].append(chunk)
            process.text_event(int((event.timestamp - turn["started_at"]) * 1000))
        for call in event.get_function_calls():
            process.tool_call(call)
        for response in responses:
            process.tool_response(response.response or {}, response=response)
        if event.get_function_calls() or responses:
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

    새 턴은 완료 스냅샷이 정본이다. 구 턴의 본문 마감만 스트림의 조립기(finalize_answer)를
    사용하며, 그 경우 현재 세션 레지스트리를 참조하므로 당시 출처 값은 보장할 수 없다.
    """
    registry = None
    turns: list[TurnView] = []
    for raw in _assemble_turns(session.events):
        snapshot = raw["snapshot"]
        if snapshot is not None:
            payload = dict(snapshot)
        else:
            if registry is None:
                registry = get_sources(session.state)
            body = "".join(raw["body"])
            _, payload = finalize_answer(body, registry, session.id)
            if not raw["body"]:
                payload["text"] = ""
            process = raw["process"].payload(
                raw_body=body,
                text=payload["text"],
                finalize_text=partial(finalize_text, sources=registry, session_id=session.id),
                elapsed_ms=int((raw["ended_at"] - raw["started_at"]) * 1000),
            )
            process.update(raw["timing"] or {})
            payload["process"] = process
        payload.update(session_id=session.id, turn_id=raw["turn_id"])
        feedback = feedback_by_turn.get(raw["turn_id"])
        turns.append(
            TurnView(
                **payload,
                started_at=raw["started_at"],
                user_text="\n".join(raw["user"]),
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
        " 매핑된다(/chat/stream done과 같은 계약). history_saved=true인 턴은 본문·출처·메타·"
        "과정·종료 상태가 완료 당시 공개 스냅샷이다. false인 구 턴은 원시 이벤트와 현재"
        " 출처 레지스트리에서 복원하므로 과거 가격·평점·메타를 보장하지 않으며 status는"
        " unknown이다. 응답은 당시 시점의 정보이므로 최신 가격이 필요하면 다시 질문한다."
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
        session = await _owned_turn(user_no, session_id, turn_id)
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

    @app.post(
        "/chat/sessions/{session_id}/turns/{turn_id}/clicks",
        tags=["history"],
        status_code=204,
        responses={
            **_SESSION_RESPONSES,
            204: {"description": "기록 완료(응답 본문 없음) — 실제로 저장됐다"},
            404: {
                "model": ErrorDetail,
                "description": "세션 또는 턴(turn_id)이 이 사용자의 것으로 존재하지 않음",
            },
            503: {
                "model": ErrorDetail,
                "description": "클릭 DB 실패 — 저장되지 않았다(재시도 대상)",
            },
        },
        summary="턴 링크 클릭 기록",
        description="턴(=/chat/stream done.turn_id) 답변 안의 링크(상품 카드·출처·공지·웹 무엇이든)"
        "를 눌렀음을 URL로 기록한다. 멱등이 아니다 — 같은 링크를 두 번 누르면 두 건이다."
        " 204는 '받았다'가 아니라 '저장됐다'다(저장 실패는 5xx로 정직하게 끊는다).",
    )
    async def post_turn_click(
        session_id: str,
        turn_id: str,
        request: TurnClickRequest,
        user: _UserDep = None,
    ) -> None:
        user_no = _require_identified(user)
        session = await _owned_turn(user_no, session_id, turn_id)
        await UserDataService.get_instance().record_click(
            user_id=user_no,
            session_id=session.id,
            turn_id=turn_id,
            url=str(request.url),
            source_id=request.source_id,
            source_type=request.source_type,
            label=request.label or None,
        )
