"""Yes24 백엔드 조회 API — 회원이 기간 안에 크레마AI에 질문했는가(이벤트 참여 판정 재료).

호출자는 회원 쿠키가 없는 Yes24 백엔드다. 그래서 회원 식별(`x-api-key`)이 아니라 설정의 공유
비밀(`partner_api_key`)을 `x-partner-key`로 받고 회원번호는 본문으로 받는다. 비밀이 비어 있으면
라우트 자체가 없다(404). 인증 없이 회원번호만 받으면 번호를 바꿔 가며 남의 이용 사실을 훑을 수 있다.

정본은 `chat_turn`이다 — 사용자에게 답을 낸 턴의 확정본이고 `user_id`가 곧 회원번호(userNo)다.
오버뷰 열람과 "이어 묻기" 시딩은 행을 남기지 않아 회원이 직접 보낸 메시지만 잡힌다.

기간은 KST 날짜이고 양 끝을 포함한다(이벤트 공지가 "10/1~10/31" 꼴이다). 하루 경계는 어드민
통계와 같은 `admin_utc_offset_hours`로 자른다 — 두 곳이 다르게 자르면 수치가 어긋난다.
회원번호를 URL이 아니라 본문에 싣는 이유: 쿼리스트링은 ALB·프록시 접근 로그에 남는다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from secrets import compare_digest

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from yes24_agent.config import Settings, get_settings
from yes24_agent.user_data import UserDataService

PARTNER_KEY_HEADER = APIKeyHeader(
    name="x-partner-key",
    auto_error=False,
    description="Yes24 백엔드 전용 공유 비밀(팀에 문의). 없거나 틀리면 401.",
)


async def require_partner(key: str | None = Security(PARTNER_KEY_HEADER)) -> None:
    """FastAPI 의존성: 공유 비밀 상수시간 비교. 로그인월은 이 판정자에 위임한다(main)."""
    configured = get_settings().partner_api_key
    if not (key and configured and compare_digest(key.encode(), configured.encode())):
        raise HTTPException(status_code=401, detail="파트너 키가 유효하지 않습니다.")


def utc_bound(day: date | None, days: int = 0) -> datetime | None:
    """KST 날짜(+days)의 자정을 tz 없는 UTC로 — chat_turn.started_at과 같은 꼴. None은 열린 끝."""
    if day is None:
        return None
    zone = timezone(timedelta(hours=get_settings().admin_utc_offset_hours))
    midnight = datetime.combine(day + timedelta(days=days), time(), zone)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


class ChatActivityRequest(BaseModel):
    # 백엔드가 회원번호를 JSON 숫자로 직렬화해도 받는다(문자열 강제 시 422 — 연동 실측).
    # 모르는 필드도 422 — 시각(start_at 등)을 실어 보내고 무시된 줄 모르는 오해를 막는다.
    model_config = ConfigDict(coerce_numbers_to_str=True, extra="forbid")

    user_no: str = Field(pattern=r"^\d{1,20}$", description="Yes24 회원번호(userNo), 숫자 1~20자리")
    start_date: date | None = Field(
        default=None, description="기간 시작일(포함, KST), YYYY-MM-DD. 없으면 처음부터"
    )
    end_date: date | None = Field(
        default=None, description="기간 종료일(포함, KST), YYYY-MM-DD. 없으면 지금까지"
    )

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _date_text_only(cls, value):
        # 숫자(유닉스 타임스탬프)가 조용히 날짜로 해석되던 것을 422로 막는다(실측). strict=True는
        # FastAPI가 본문을 파이썬 모드로 검증해 정상 문자열까지 거절하므로 쓰지 않는다.
        if value is not None and not isinstance(value, str):
            raise ValueError("날짜는 YYYY-MM-DD 문자열이어야 합니다.")
        return value

    @model_validator(mode="after")
    def _ordered(self) -> ChatActivityRequest:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date는 start_date보다 빠를 수 없습니다.")
        try:  # 0001-01-01·9999-12-31 — UTC 환산이 datetime 범위를 넘는다(500 대신 422)
            utc_bound(self.start_date), utc_bound(self.end_date, days=1)
        except OverflowError as exc:
            raise ValueError("지원하지 않는 날짜입니다.") from exc
        return self


class ChatActivityResponse(BaseModel):
    user_no: str = Field(description="요청한 회원번호(배치 호출 시 짝 맞추기용)")
    asked: bool = Field(description="기간 안에 질문이 1건 이상 있으면 true")
    question_count: int = Field(description="기간 안에 보낸 질문(턴) 수")
    first_asked_at: datetime | None = Field(description="기간 내 첫 질문 시각(KST, +09:00)")
    last_asked_at: datetime | None = Field(description="기간 내 마지막 질문 시각(KST)")


def register_partner(app: FastAPI, settings: Settings) -> None:
    """공유 비밀이 설정됐을 때만 파트너 라우트를 등록한다."""
    if not settings.partner_api_key:
        return

    @app.post(
        "/partner/chat-activity",
        tags=["partner"],
        response_model=ChatActivityResponse,
        dependencies=[Depends(require_partner)],
        responses={
            401: {"description": "파트너 키 없음·불일치"},
            503: {"description": "조회 실패 — '질문 없음'으로 간주 금지"},
        },
        summary="회원의 기간 내 크레마AI 질문 이력",
        description="회원번호와 기간(KST 날짜, 양 끝 포함, 각각 생략 가능)을 받아 그 기간에 질문한"
        " 이력이 있는지 돌려준다. 이벤트 참여 판정용 서버→서버 API다. 대화를 삭제한 회원도"
        " 질문 사실은 집계된다.",
    )
    async def chat_activity(body: ChatActivityRequest) -> ChatActivityResponse:
        current = get_settings()
        zone = timezone(timedelta(hours=current.admin_utc_offset_hours))
        count, first, last = await UserDataService.get_instance().question_activity(
            app_name=current.app_name,
            user_id=body.user_no,
            since=utc_bound(body.start_date),
            until=utc_bound(body.end_date, days=1),
        )

        def local(value: datetime | None) -> datetime | None:
            return value and value.replace(microsecond=0, tzinfo=timezone.utc).astimezone(zone)

        return ChatActivityResponse(
            user_no=body.user_no,
            asked=count > 0,
            question_count=count,
            first_asked_at=local(first),
            last_asked_at=local(last),
        )
