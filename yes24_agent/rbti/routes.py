"""RBTI 유형 목록 라우트 — 프론트가 선택기를 그릴 재료를 준다.

`/chat/stream`의 `rbti` 필드는 "어떤 유형을 쓸지"를 받지만, 화면이 그 값을 **고르게 하려면**
16개 코드가 각각 무엇인지 알아야 한다. 그 데이터를 프론트가 손으로 베끼면 `persona.py`가
바뀔 때마다 조용히 어긋나므로(우리 데모 UI의 `static/lib/rbti.js`가 정확히 그 사본이고
동기화 가드 테스트로 겨우 붙들고 있다), 서버가 정본을 그대로 내보낸다.

라우트를 여기 두는 것은 `starters.py`·`admin.py`·`matrix`와 같은 관례다 — 도메인 모듈이
자기 라우트를 소유하고 `main.create_app`은 등록만 한다.

**상세(강점·함정·처방 각 3항목)는 싣지 않는다.** 선택기와 카드에 필요한 것은 이름·해시태그·
설명·견종까지이고, 상세는 그것을 그릴 화면이 생길 때 `TYPE_ARCHETYPES`에서 함께 열면 된다
(지금 실으면 응답이 4배가 되는데 소비자가 없다).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from yes24_agent.auth import AuthenticatedUser, get_authenticated_user
from yes24_agent.rbti.persona import (
    AXIS_ORDER,
    AXIS_UI_KO,
    TYPE_ARCHETYPES,
    axis_label,
    is_valid_code,
    matrix_codes,
)
from yes24_agent.rbti.profile import fetch_user_rbti


class AxisValue(BaseModel):
    """축이 가질 수 있는 한 값 = 코드의 한 글자."""

    code: str = Field(description="코드 한 글자(예 `C`). 이 축의 자리에 들어간다.")
    label: str = Field(description="짧은 라벨(예 `완독`). 축 설명 문자열을 이루는 조각이다.")
    title: str = Field(description="선택기 항목 제목(예 `정독·완독`).")
    desc: str = Field(description="그 값이 무엇인지 한 줄 설명(예 `한 권을 끝까지`).")


class Axis(BaseModel):
    """코드 한 자리에 대응하는 축. **배열 순서가 곧 코드 자릿수 순서다.**"""

    key: str = Field(description="축 식별자(`pattern`·`processing`·`breadth`·`motivation`).")
    label: str = Field(description="축 제목(예 `독서 패턴`).")
    short_label: str = Field(description="폭이 좁은 자리용 축 제목(예 `패턴`).")
    values: list[AxisValue] = Field(description="이 축의 두 값. 순서는 표시 순서다.")


class RbtiType(BaseModel):
    """16유형 중 하나."""

    code: str = Field(description="4글자 코드(예 `CADI`). `/chat/stream`의 `rbti`에 그대로 싣는다.")
    axis_label: str = Field(description="코드에서 파생한 축 설명(예 `완독-분석-깊이-정보`).")
    name: str = Field(description="유형 이름(예 `단서 찾는 비글`). 카드 제목용.")
    tags: list[str] = Field(description="해시태그 2개(예 `#끝장탐구`).")
    summary: str = Field(description="유형 설명 한 문단.")
    dog: str = Field(description="이름의 유래를 설명하는 견종 한 줄.")


class RbtiTypesResponse(BaseModel):
    """선택기(`axes`)와 카드(`types`)를 한 번에 그릴 수 있는 정적 데이터."""

    axes: list[Axis]
    types: list[RbtiType]


def _axes() -> list[Axis]:
    """축 정의를 AXIS_ORDER 순서로 조립한다 — 그 순서가 코드 자릿수 계약이다."""
    axes = []
    for key, codes in AXIS_ORDER:
        ui = AXIS_UI_KO[key]
        values = ui["values"]
        axes.append(
            Axis(
                key=key,
                label=ui["label"],  # type: ignore[arg-type]
                short_label=ui["short_label"],  # type: ignore[arg-type]
                values=[AxisValue(code=code, **values[code]) for code in codes],  # type: ignore[index]
            )
        )
    return axes


def _types() -> list[RbtiType]:
    """16유형을 matrix_codes() 순서로 조립한다(축 조합의 결정론적 전개)."""
    return [
        RbtiType(
            code=code,
            axis_label=axis_label(code),
            name=TYPE_ARCHETYPES[code]["name"],  # type: ignore[arg-type]
            tags=TYPE_ARCHETYPES[code]["tags"],  # type: ignore[arg-type]
            summary=TYPE_ARCHETYPES[code]["summary"],  # type: ignore[arg-type]
            dog=TYPE_ARCHETYPES[code]["dog"],  # type: ignore[arg-type]
        )
        for code in matrix_codes()
    ]


class MyRbtiResponse(BaseModel):
    """지금 이 사용자의 독서 성향 코드. 화면이 진입 시 배지·선택기 초기값을 그릴 재료다."""

    code: str | None = Field(
        default=None,
        description=(
            "이 사용자의 유형 코드(예 `SEBF`). 유형이 없거나 조회하지 못했으면 `null`이다"
            " — 둘을 가르지 않는다(둘 다 '성향 없이 답한다'와 같은 결과다)."
        ),
        examples=["SEBF"],
    )


def register_rbti(app: FastAPI) -> None:
    """`GET /rbti/types`를 등록한다.

    인증 의존성을 라우트에 **직접** 단다 — 로그인월의 x-api-key 통과 집합이 그 선언에서
    파생되므로(`main._key_checking_routes`) 손목록을 갱신할 필요가 없다. 중첩 의존성으로
    숨기면 그 파생이 못 본다.
    """

    @app.get(
        "/rbti/types",
        tags=["rbti"],
        response_model=RbtiTypesResponse,
        summary="독서 성향 16유형과 축 정의",
        description=(
            "화면에 유형 선택기를 그릴 재료다. `axes`는 코드 자릿수 순서대로의 축 4개이고"
            " (배열 순서 = 자릿수 순서), `types`는 그 조합 16개다. 고른 코드는"
            " `/chat/stream`의 `rbti`에 그대로 실으면 된다."
            " **정적 데이터라 진입 시 1회 받아 캐시한다**(폴링 금지) — 값이 바뀌는 것은 배포뿐이다."
        ),
    )
    async def rbti_types(
        user: Annotated[AuthenticatedUser | None, Depends(get_authenticated_user)] = None,
    ) -> RbtiTypesResponse:
        return RbtiTypesResponse(axes=_axes(), types=_types())

    @app.get(
        "/me/rbti",
        tags=["rbti"],
        response_model=MyRbtiResponse,
        summary="이 사용자의 독서 성향 코드",
        description=(
            "진입 시 배지·선택기 초기값을 그릴 값이다. 서버가 매 턴 쓰는 그 조회를 그대로"
            " 돌려주므로 `/chat/stream`의 `rbti` 이벤트·`done.rbti_applied`와 같은 값이다"
            " (요청에 `rbti`를 실어 덮어쓰거나 `use_rbti:false`로 끈 턴은 예외)."
            " 유형이 없거나 조회 실패면 `code: null`이며 **오류가 아니다**."
            " 저장·수정 경로는 없다 — 코드의 정본은 외부 RBTI API다."
            " 값이 사람에게 붙는 속성이라 자주 바뀌지 않으니 진입 시 1회 받는다(폴링 금지)."
        ),
    )
    async def my_rbti(
        user: Annotated[AuthenticatedUser | None, Depends(get_authenticated_user)] = None,
    ) -> MyRbtiResponse:
        """조회 결과를 채팅 경로와 **같은 판정**으로 거른다(runner.py의 `is_valid_code`).

        무효 코드를 그대로 흘리면 화면은 배지를 그리는데 답변에는 페르소나가 적용되지 않아
        둘이 어긋난다 — 이 엔드포인트의 값은 "실제로 적용될 코드"여야 한다.
        """
        code = await fetch_user_rbti(user.user_no if user else None)
        return MyRbtiResponse(code=code if is_valid_code(code) else None)
