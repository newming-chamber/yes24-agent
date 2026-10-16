"""크레마AI 상품권 이벤트 — 발급 대상자 확인 · 발급 결과 기록 (클라이언트 → 우리 서버).

발급 자체는 클라이언트가 Yes24 `POST /api/event/giftcard/issue`를 직접 호출한다(RBTI·일문백답과
같은 방식). 암호화된 상품권번호는 클라이언트 고정값이라 서버는 상품코드·암호화 키를 다루지 않는다.
그 결과 **이 대상자 확인은 보안 관문이 아니다** — 고정 암호문을 꺼내면 우리 서버를 거치지 않고도
발급 API를 부를 수 있다. 중복 차단의 정본은 Yes24 쪽 판정(`ALREADY_APPLIED`·`EXCEED`)이고, 여기
판정은 화면 안내용이다.

대상자 = 이벤트 기간(KST 날짜, 양 끝 포함)에 크레마AI에 질문한 회원. 판정 재료는 파트너 API와
같은 `question_activity` 한 곳이다(같은 판정을 두 벌 만들지 않는다). 이미 받은 회원 = 이 이벤트로
`SUCCESS`·`ALREADY_APPLIED`가 기록된 회원 — 둘 다 Yes24가 "이 회원은 받았다"고 답한 코드다.

결과 기록은 클라이언트가 전달한 운영 참고 이력이다. 시각은 서버가 찍는다(클라이언트 시각을 받지
않는다). 성공 건수로 잔여 수량을 산출할 수 없다 — 재고 정본은 Yes24에 있다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field

from yes24_agent.auth import AuthenticatedUser, get_authenticated_user
from yes24_agent.config import Settings, get_settings
from yes24_agent.history import _AUTH_RESPONSES, _require_identified
from yes24_agent.partner import utc_bound
from yes24_agent.user_data import UserDataService

# Yes24 발급 API 응답 코드(상품권 발급 API 암호화 연동 가이드 v1.3 §9). 이 밖의 값은 422다.
ResultCode = Literal[
    "SUCCESS",
    "FAIL",
    "DUPLICATE",
    "EXCEED",
    "EXPIRE",
    "ALREADY_APPLIED",
    "NOT_LOGGED_IN",
    "NOT_VERIFIED",
]

_UserDep = Annotated[AuthenticatedUser | None, Depends(get_authenticated_user)]


class EligibilityResponse(BaseModel):
    status: Literal["eligible", "not_eligible", "already_issued"] = Field(
        description="eligible=발급 요청 가능 · not_eligible=기간 내 질문 이력 없음 ·"
        " already_issued=이 이벤트로 이미 받음(SUCCESS·ALREADY_APPLIED 기록 있음)"
    )
    question_count: int = Field(description="이벤트 기간 안에 보낸 질문 수")


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_code: ResultCode = Field(description="Yes24 발급 API가 돌려준 ResultCode 그대로")


def register_giftcard(app: FastAPI, settings: Settings) -> None:
    """이벤트 키가 설정됐을 때만 상품권 라우트를 등록한다(비우면 404 — 이벤트 종료 스위치)."""
    if not settings.giftcard_event_key:
        return

    @app.post(
        "/event/giftcard/eligibility",
        tags=["event"],
        response_model=EligibilityResponse,
        responses={
            **_AUTH_RESPONSES,
            503: {"description": "조회 실패 — '대상 아님'으로 간주 금지"},
        },
        summary="상품권 발급 대상자 여부",
        description="x-api-key 회원이 이벤트 기간(KST, 양 끝 포함)에 크레마AI에 질문했는지와"
        " 이미 받았는지를 돌려준다. eligible일 때만 Yes24 발급 API를 호출한다.",
    )
    async def eligibility(user: _UserDep = None) -> EligibilityResponse:
        user_no = _require_identified(user)
        current = get_settings()
        data = UserDataService.get_instance()
        count, _, _ = await data.question_activity(
            app_name=current.app_name,
            user_id=user_no,
            since=utc_bound(current.giftcard_event_start),
            until=utc_bound(current.giftcard_event_end, days=1),
        )
        if await data.giftcard_issued(user_id=user_no, event_key=current.giftcard_event_key):
            status = "already_issued"
        else:
            status = "eligible" if count else "not_eligible"
        return EligibilityResponse(status=status, question_count=count)

    @app.post(
        "/event/giftcard/result",
        tags=["event"],
        status_code=204,
        responses={
            **_AUTH_RESPONSES,
            204: {"description": "기록 완료 — 실제로 저장됐다"},
            422: {"description": "정의되지 않은 ResultCode"},
            503: {"description": "저장 실패 — 기록되지 않았다(재시도 대상)"},
        },
        summary="상품권 발급 결과 기록",
        description="클라이언트가 Yes24 발급 API에서 받은 ResultCode를 기록한다(시각은 서버 기준)."
        " 운영 참고 이력이며 재고·잔여 수량의 근거가 아니다. 멱등이 아니다 — 시도마다 한 건.",
    )
    async def result(body: ResultRequest, user: _UserDep = None) -> None:
        await UserDataService.get_instance().record_giftcard_result(
            user_id=_require_identified(user),
            event_key=get_settings().giftcard_event_key,
            result_code=body.result_code,
        )
