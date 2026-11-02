"""크레마AI 상품권 이벤트 — 이벤트 노출 · 발급 대상자 확인 · 발급 결과 기록(클라이언트 → 우리 서버).

이벤트는 **구간 테이블(giftcard_period)**이 정한다. 한 행 = 회원이 한 번 받을 수 있는 구간(KST
날짜, 양 끝 포함)이고, "주 1회·주말 미지급"은 주마다 월~금 한 행으로 데이터가 표현한다 — 여기엔
요일 판정이 없다. 오늘을 포함하는 구간이 없으면(주말·기간 밖·빈 테이블) `closed`다(공개 조회는
같은 event_key의 앞뒤 구간 사이인 날을 `paused`로 따로 알린다 — 주말 안내 배너). 판정은 겹침을
검증하지 않고 starts_on 최신 1개를 쓴다(admin_data.GIFTCARD_LATEST_PERIOD) — 겹침은 어드민 쓰기
경로(아래 구간 관리)가 막고, SQL로 직접 넣은 행은 운영 데이터 책임이다.

발급 자체는 클라이언트가 Yes24 `POST /api/event/giftcard/issue`를 직접 호출한다(RBTI·일문백답과
같은 방식). 그 바디의 eventNm·암호화 상품권번호는 **eligible 응답으로만** 내려준다 — 공개 조회
(GET)에는 싣지 않는다. 구간 행은 원문 상품권번호만 갖고, 암호화는 응답 직전에 서버가 한다
(`encrypt_goods_tic_no`, 키·IV 원천은 .env의 시크릿 두 개 — 클라이언트는 키를 갖지 않는다).
그래도 **이 대상자 확인은 보안 관문이 아니다** — 받은 암호문을 다시 쓰면 우리 서버를 거치지 않고도
발급 API를 부를 수 있다. 중복 차단의 정본은 Yes24 쪽 판정(`ALREADY_APPLIED`·`EXCEED`)이고, 여기
판정은 화면 안내용이다.

대상자 = 현재 구간에 크레마AI에 질문한 회원. 판정 재료는 파트너 API와 같은 `question_activity`
한 곳이다(같은 판정을 두 벌 만들지 않는다). 이미 받은 회원 = 현재 구간으로 `SUCCESS`·
`ALREADY_APPLIED`가 기록된 회원 — 둘 다 Yes24가 "이 회원은 받았다"고 답한 코드다.

결과 기록은 클라이언트가 전달한 운영 참고 이력이다. 시각은 서버가 찍는다(클라이언트 시각을 받지
않는다). 귀속 구간은 "이미 시작한 구간 중 최신"이다 — 금요일 23:59에 대상자를 확인하고 토요일
00:00에 도착한 결과도 그 주 구간에 붙는다. 한계: 구간이 맞닿으면(일요일에 끝나는 행 → 월요일 시작
행) 자정 직전 확인 → 직후 기록이 다음 구간에 붙는 짧은 창이 있다(수용). 성공 건수로 잔여 수량을
산출할 수 없다 — 재고 정본은 Yes24에 있다.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from datetime import date
from typing import Annotated, Any, Literal

import aiomysql
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from yes24_agent.admin_auth import (
    AdminActor,
    AdminService,
    EditChanges,
    admin_enabled,
    require_editor,
)
from yes24_agent.admin_data import local_today, mask_goods_tic_no, period_status
from yes24_agent.auth import AuthenticatedUser, get_authenticated_user
from yes24_agent.config import get_settings
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

logger = logging.getLogger(__name__)


def encrypt_goods_tic_no(number: int, key: str, vector: str) -> str:
    """상품권번호 → goodsTicNoEncrypted(Yes24 「상품권 발급 API — 암호화 연동 가이드」 v1.3 §3~6).

    key = SHA-256(UTF-8(key)) 32바이트, iv = SHA-256(UTF-8(vector)) 앞 16바이트 — 문자열을 그대로
    키로 쓰지 않는다. AES-256-CBC·PKCS7, 평문 = 정수의 10진 문자열(UTF-8), 결과 = 표준 Base64.
    """
    cipher = Cipher(
        algorithms.AES(hashlib.sha256(key.encode()).digest()),
        modes.CBC(hashlib.sha256(vector.encode()).digest()[:16]),
    ).encryptor()
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(str(number).encode()) + padder.finalize()
    return base64.b64encode(cipher.update(padded) + cipher.finalize()).decode("ascii")


class EventResponse(BaseModel):
    active: bool = Field(description="오늘(KST)이 지급 구간 안인가 — 배너 노출 여부")
    paused: bool = Field(
        description="지급 구간은 아니지만 같은 이벤트가 이어지는 쉬는 날(주말)인가 — 배너를 두고"
        " 평일 응모 안내를 띄운다. active면 false"
    )
    starts_on: date | None = Field(
        description="이벤트 전체 기간 시작일(KST, 포함) — 같은 이벤트명 구간의 첫 날."
        " active·paused가 아니면 null"
    )
    ends_on: date | None = Field(
        description="이벤트 전체 기간 종료일(KST, 포함) — 같은 이벤트명 구간의 마지막 날."
        " active·paused가 아니면 null"
    )
    period_starts_on: date | None = Field(
        description="이번 주 구간 시작일(KST, 포함). active가 아니면 null"
    )
    period_ends_on: date | None = Field(
        description="이번 주 구간 종료일(KST, 포함). active가 아니면 null"
    )


class EligibilityResponse(BaseModel):
    status: Literal["eligible", "not_eligible", "already_issued", "closed"] = Field(
        description="eligible=발급 요청 가능 · not_eligible=이번 구간 질문 이력 없음 ·"
        " already_issued=이번 구간에 이미 받음(SUCCESS·ALREADY_APPLIED 기록 있음) ·"
        " closed=지금은 지급 구간이 아님(주말·기간 밖)"
    )
    question_count: int = Field(description="현재 구간 안에 보낸 질문 수(closed면 0)")
    period_ends_on: date | None = Field(description="현재 구간 종료일(KST, 포함). closed면 null")
    event_nm: str | None = Field(description="Yes24 발급 요청의 eventNm — eligible일 때만")
    goods_tic_no_encrypted: str | None = Field(
        description="Yes24 발급 요청의 goodsTicNoEncrypted — eligible일 때만"
    )


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_code: ResultCode = Field(description="Yes24 발급 API가 돌려준 ResultCode 그대로")


def _today() -> date:
    return local_today(get_settings().admin_utc_offset_hours)


# ── 공개 조회 메모 ─────────────────────────────────────────────────────────
# GET /event/giftcard는 인증 없는 배너 판정이라 호출 수가 화면 진입 수다. user_data 풀(상한이
# 작다)을 대화 쓰기와 공유하므로 요청마다 DB를 읽지 않는다 — (KST 오늘, 만료, 응답) 1칸 메모.
# 날짜가 바뀌면 만료 전이라도 다시 읽는다(자정 경계). 어드민 구간 쓰기는 **이 프로세스**의 메모만
# 비운다 — 다른 서버·컨테이너는 TTL(giftcard_event_cache_s)만큼 늦게 따라온다(허용). 조회
# 실패(503)는 담지 않는다. 만료 순간 동시 요청은 각자 한 번씩 읽는다(TTL당 한 번의 몰림, 수용).
_EVENT_MEMO: dict[str, Any] = {}


# ── 어드민 구간 관리(editor 이상 쓰기) ─────────────────────────────────────
# 판정 경로(위 라우트)는 행만 본다. 겹침 금지는 **쓰기 보조**다 — 겹친 행이 생기지 않게 여기서
# 막는다(직접 SQL로 넣은 행은 막지 못한다). 상품권번호는 주마다 다르므로 한 번에 한 구간만 넣는다.
# 상품권번호 원문은 어떤 응답·감사 기록에도 싣지 않는다(mask_goods_tic_no).

AUDIT_TARGET = "giftcard"
_BIGINT_MAX = 2**63 - 1  # giftcard_period.goods_tic_no BIGINT
_DUPLICATE_KEY = 1062  # MySQL ER_DUP_ENTRY — uq starts_on
_EventKey = Annotated[str, Field(min_length=1, max_length=64)]  # DDL 폭(= eventNm)
# 화면은 번호를 문자열로 보낸다(JS Number는 16자리부터 정밀도를 잃는다) — lax 모드가 정수로 받는다.
_GoodsTicNo = Annotated[int, Field(gt=0, le=_BIGINT_MAX)]
# 모듈 수준이어야 한다 — `from __future__ import annotations`라 라우트 안 지역 별칭은 FastAPI가
# 못 풀어 의존성이 조용히 빠진다(test_admin_auth의 전 라우트 역할 검사가 잡는다).
_Editor = Annotated[AdminActor, Depends(require_editor)]


class PeriodCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: _EventKey
    starts_on: date
    ends_on: date
    goods_tic_no: _GoodsTicNo

    @model_validator(mode="after")
    def _ordered(self):
        if self.starts_on > self.ends_on:
            raise PydanticCustomError("period_order", "시작일이 종료일보다 늦습니다.")
        return self


class PeriodChanges(EditChanges):
    # 선언 순서 = UPDATE SET 순서(model_dump는 선언 순서). 필드 이름이 곧 열 이름이다.
    event_key: _EventKey = None
    starts_on: date = None
    ends_on: date = None
    goods_tic_no: _GoodsTicNo = None


# 구간 열 = PeriodChanges 선언 순서(INSERT 열·값 순서와 삭제 감사가 이 하나를 쓴다).
_PERIOD_COLUMNS = tuple(PeriodChanges.model_fields)


class PeriodEdit(BaseModel):
    # expected(낙관적 대조)는 받지 않는다(extra=forbid → 422) — 대조 실패 응답의 current가
    # 상품권번호 원문을 싣게 된다.
    model_config = ConfigDict(extra="forbid")

    changes: PeriodChanges


def _shown(row: dict[str, Any]) -> dict[str, Any]:
    """응답·감사 기록용 — 상품권번호는 마스킹 값으로 바꾼다(그 열이 있을 때만)."""
    out = {k: v for k, v in row.items() if k != "goods_tic_no"}
    if "goods_tic_no" in row:
        out["goods_tic_no_masked"] = mask_goods_tic_no(str(row["goods_tic_no"]))
    return out


async def _reject_overlaps(cur, starts_on: date, ends_on: date, exclude_id: int = 0) -> None:
    """기존 구간과 날짜가 겹치면 409(겹친 구간을 알린다). 일반 읽기다 — 어드민 트랜잭션은 RR이라
    잠금 범위 읽기는 갭 잠금 교착을 만든다. 동시 추가의 마지막 방어는 uq starts_on(1062 → 409)이고,
    시작일이 다른 두 구간이 동시에 겹치게 들어오는 경합은 막지 못한다(운영자 수동 편집
    빈도라 수용)."""
    await cur.execute(
        "SELECT id, event_key, starts_on, ends_on FROM giftcard_period "
        "WHERE starts_on <= %s AND ends_on >= %s AND id <> %s",
        (ends_on, starts_on, exclude_id),
    )
    hits = await cur.fetchall()
    if hits:
        listed = ", ".join(
            f"#{row['id']} {row['event_key']} {row['starts_on']}~{row['ends_on']}" for row in hits
        )
        raise HTTPException(status_code=409, detail=f"기존 구간과 날짜가 겹칩니다: {listed}")


async def _write(cur, sql: str, params: tuple) -> None:
    """구간 쓰기 1문장 — 시작일 중복(uq starts_on, 1062)은 409로 돌려준다."""
    try:
        await cur.execute(sql, params)
    except aiomysql.IntegrityError as exc:
        if exc.args and exc.args[0] == _DUPLICATE_KEY:
            raise HTTPException(
                status_code=409, detail="같은 시작일의 구간이 이미 있습니다."
            ) from exc
        raise


async def _locked_period(cur, period_id: int) -> dict[str, Any]:
    await cur.execute(
        "SELECT id, event_key, starts_on, ends_on, goods_tic_no FROM giftcard_period "
        "WHERE id = %s FOR UPDATE",
        (period_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="구간을 찾을 수 없습니다.")
    return row


def _register_admin(app: FastAPI) -> None:
    """구간 추가·수정·삭제(editor 이상 — starters 관례). 변경과 감사가 한 트랜잭션이고, 커밋 뒤
    이 프로세스의 공개 조회 메모를 비운다."""

    @app.post("/admin/giftcard/periods", status_code=201, include_in_schema=False)
    async def admin_add_period(body: PeriodCreate, request: Request, actor: _Editor) -> dict:
        row = body.model_dump()
        async with AdminService.get_instance().transaction(actor, request) as tx:
            await _reject_overlaps(tx.cur, body.starts_on, body.ends_on)
            await _write(
                tx.cur,
                f"INSERT INTO giftcard_period ({', '.join(_PERIOD_COLUMNS)}) "
                f"VALUES ({', '.join(['%s'] * len(_PERIOD_COLUMNS))})",
                tuple(row[k] for k in _PERIOD_COLUMNS),
            )
            new_id = tx.cur.lastrowid
            await tx.audit(AUDIT_TARGET, new_id, "create", after=_shown(row))
        _EVENT_MEMO.clear()
        return {"id": new_id, **_shown(row)}

    @app.patch("/admin/giftcard/periods/{period_id}", include_in_schema=False)
    async def admin_update_period(
        period_id: int, body: PeriodEdit, request: Request, actor: _Editor
    ) -> dict:
        """시작 전 구간은 전 필드, 진행 중 구간은 종료일(오늘 이후)·번호만, 종료 구간은 불가.
        잠금 선조회 한 번의 행으로 상태를 판정하고 바뀐 열만 UPDATE한다."""
        changes = body.changes.model_dump(exclude_unset=True)
        today = _today()
        async with AdminService.get_instance().transaction(actor, request) as tx:
            row = await _locked_period(tx.cur, period_id)
            status = period_status(row["starts_on"], row["ends_on"], today)
            if status == "ended":
                raise HTTPException(status_code=409, detail="종료된 구간은 바꿀 수 없습니다.")
            if status == "active" and set(changes) - {"ends_on", "goods_tic_no"}:
                raise HTTPException(
                    status_code=409, detail="진행 중 구간은 종료일·상품권번호만 바꿀 수 있습니다.",
                )
            if status == "active" and changes.get("ends_on", today) < today:
                raise HTTPException(
                    status_code=409,
                    detail=f"진행 중 구간의 종료일은 오늘({today}) 이후여야 합니다.",
                )
            starts_on = changes.get("starts_on", row["starts_on"])
            ends_on = changes.get("ends_on", row["ends_on"])
            if starts_on > ends_on:
                raise HTTPException(status_code=422, detail="시작일이 종료일보다 늦습니다.")
            after = {k: v for k, v in changes.items() if row[k] != v}
            if after:
                await _reject_overlaps(tx.cur, starts_on, ends_on, exclude_id=period_id)
                assignments = ", ".join(f"`{column}` = %s" for column in after)
                await _write(
                    tx.cur,
                    f"UPDATE giftcard_period SET {assignments} WHERE id = %s",
                    (*after.values(), period_id),
                )
                before = {k: row[k] for k in after}
                await tx.audit(AUDIT_TARGET, period_id, "update", _shown(before), _shown(after))
        if after:  # 커밋 뒤에 비운다(커밋 전에 비우면 그 사이 조회가 옛 값을 다시 담는다)
            _EVENT_MEMO.clear()
        return {"id": period_id, "updated": sorted(after)}

    @app.delete("/admin/giftcard/periods/{period_id}", include_in_schema=False)
    async def admin_delete_period(period_id: int, request: Request, actor: _Editor) -> dict:
        """시작 전 구간만 지운다(시작한 구간은 409 — 종료일을 줄인다). 시작 전 구간엔 발급 기록이
        붙지 않는다(결과 귀속 = 이미 시작한 구간)."""
        async with AdminService.get_instance().transaction(actor, request) as tx:
            row = await _locked_period(tx.cur, period_id)
            if period_status(row["starts_on"], row["ends_on"], _today()) != "scheduled":
                raise HTTPException(
                    status_code=409, detail="시작한 구간은 삭제할 수 없습니다 — 종료일을 줄이세요."
                )
            await tx.cur.execute("DELETE FROM giftcard_period WHERE id = %s", (period_id,))
            await tx.audit(
                AUDIT_TARGET, period_id, "delete",
                before=_shown({k: row[k] for k in _PERIOD_COLUMNS}),
            )
        _EVENT_MEMO.clear()
        return {"id": period_id, "deleted": True}


def register_giftcard(app: FastAPI) -> None:
    """상품권 라우트를 등록한다. 켜고 끄는 스위치는 없다 — 구간 행이 없으면 `closed`가 종료다.
    어드민 구간 관리는 어드민 스택이 성립할 때만(admin_enabled — starters 관례)."""
    if admin_enabled(get_settings()):
        _register_admin(app)

    @app.get(
        "/event/giftcard",
        tags=["event"],
        response_model=EventResponse,
        responses={503: {"description": "조회 실패 — '이벤트 없음'으로 간주 금지"}},
        summary="상품권 이벤트 진행 여부(배너 노출)",
        description="오늘(KST)이 지급 구간 안인지(active), 같은 이벤트의 구간 사이 쉬는 날인지"
        "(paused)와 이벤트 전체 기간·이번 주 구간 날짜를 돌려준다. 인증 없음(비로그인"
        " 배너 판정용). 상품권번호는 싣지 않는다.",
    )
    async def event(response: Response) -> EventResponse:
        response.headers["Cache-Control"] = "no-store"  # 자정에 바뀌는 값이다
        today = _today()
        if _EVENT_MEMO.get("day") == today and _EVENT_MEMO["expires"] > time.monotonic():
            return _EVENT_MEMO["body"]
        data = UserDataService.get_instance()
        period = await data.giftcard_period(today=today, current=True)
        key = period.event_key if period else await data.giftcard_paused_key(today=today)
        starts_on, ends_on = await data.giftcard_event_span(event_key=key) if key else (None, None)
        body = EventResponse(
            active=period is not None,
            paused=period is None and key is not None,
            starts_on=starts_on,
            ends_on=ends_on,
            period_starts_on=period and period.starts_on,
            period_ends_on=period and period.ends_on,
        )
        _EVENT_MEMO.update(
            day=today, expires=time.monotonic() + get_settings().giftcard_event_cache_s, body=body
        )
        return body

    @app.post(
        "/event/giftcard/eligibility",
        tags=["event"],
        response_model=EligibilityResponse,
        responses={
            **_AUTH_RESPONSES,
            503: {"description": "조회 실패·암호화 설정 누락 — '대상 아님'으로 간주 금지"},
        },
        summary="상품권 발급 대상자 여부",
        description="x-api-key 회원이 현재 지급 구간(KST, 양 끝 포함)에 크레마AI에 질문했는지와"
        " 이번 구간에 이미 받았는지를 돌려준다. eligible일 때만 Yes24 발급 API를 호출하고, 그"
        " 바디에는 이 응답의 event_nm·goods_tic_no_encrypted를 쓴다.",
    )
    async def eligibility(user: _UserDep = None) -> EligibilityResponse:
        user_no = _require_identified(user)
        data = UserDataService.get_instance()
        period = await data.giftcard_period(today=_today(), current=True)
        if period is None:
            return EligibilityResponse(
                status="closed", question_count=0, period_ends_on=None,
                event_nm=None, goods_tic_no_encrypted=None,
            )
        count, _, _ = await data.question_activity(
            app_name=get_settings().app_name,
            user_id=user_no,
            since=utc_bound(period.starts_on),
            until=utc_bound(period.ends_on, days=1),
        )
        if await data.giftcard_issued(user_id=user_no, period_id=period.id):
            status = "already_issued"
        else:
            status = "eligible" if count else "not_eligible"
        encrypted = None
        if status == "eligible":
            current = get_settings()
            if not (current.giftcard_enc_key and current.giftcard_enc_vector):
                logger.error(
                    "상품권 암호화 설정 누락(giftcard_enc_key·giftcard_enc_vector) — "
                    f"구간 {period.id} eligible 응답 불가"
                )
                raise HTTPException(
                    status_code=503, detail="상품권 발급 설정이 준비되지 않았습니다."
                )
            encrypted = encrypt_goods_tic_no(
                period.goods_tic_no, current.giftcard_enc_key, current.giftcard_enc_vector
            )
        return EligibilityResponse(
            status=status,
            question_count=count,
            period_ends_on=period.ends_on,
            event_nm=period.event_key if encrypted is not None else None,
            goods_tic_no_encrypted=encrypted,
        )

    @app.post(
        "/event/giftcard/result",
        tags=["event"],
        status_code=204,
        responses={
            **_AUTH_RESPONSES,
            204: {"description": "기록 완료 — 실제로 저장됐다"},
            409: {"description": "시작한 지급 구간이 없다 — 기록하지 않았다"},
            422: {"description": "정의되지 않은 ResultCode"},
            503: {"description": "저장 실패 — 기록되지 않았다(재시도 대상)"},
        },
        summary="상품권 발급 결과 기록",
        description="클라이언트가 Yes24 발급 API에서 받은 ResultCode를 기록한다(시각은 서버 기준)."
        " 귀속 구간은 이미 시작한 구간 중 최신이다(구간이 끝난 직후 도착한 결과도 그 구간)."
        " 운영 참고 이력이며 재고·잔여 수량의 근거가 아니다. 멱등이 아니다 — 시도마다 한 건.",
    )
    async def result(body: ResultRequest, user: _UserDep = None) -> None:
        user_no = _require_identified(user)
        data = UserDataService.get_instance()
        period = await data.giftcard_period(today=_today(), current=False)
        if period is None:
            raise HTTPException(status_code=409, detail="시작한 상품권 지급 구간이 없습니다.")
        await data.record_giftcard_result(
            user_id=user_no,
            period_id=period.id,
            event_key=period.event_key,
            result_code=body.result_code,
        )
