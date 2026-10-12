"""Yes24 제공 지식 데이터(메모) 어드민 라우트 — 묶음 목록·메모 목록·묶음/메모 편집·엑셀 가져오기.

등록 판정·세션·역할·출처 검사·422 입력 반사 제거·409 현재값 응답은 admin_auth가 소유하고,
가져오기·검증·저장은 memos가 소유한다(CLI와 같은 함수). 여기선 둘을 잇기만 한다.

업로드 본문은 base64 JSON이다 — multipart를 열지 않아 CSRF 세 겹(Sec-Fetch-Site·JSON 전용 쓰기·
SameSite=Lax)이 그대로 유지된다. 미리보기는 저장하지 않고, 가져오기는 파일을 다시 읽는다(미리보기
결과를 믿고 저장하지 않는다). 파싱은 CPU 작업이라 스레드에서 돈다(같은 프로세스의 채팅 SSE를
멈추지 않게). 모든 쓰기는 묶음 revision을 토큰으로 받고, 변경과 감사는 같은 트랜잭션이다.
메모 쓰기 경로에 묶음 id가 드는 이유: revision이 묶음 것이라, 다른 묶음의 메모를 그 묶음의 토큰으로
고치는 일이 없게 소속을 404로 가른다.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import math
from dataclasses import asdict
from datetime import date
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic_core import PydanticCustomError
from sqlalchemy.exc import SQLAlchemyError

from yes24_agent.admin_auth import (
    AUDIT_INSERT,
    AdminActor,
    EditChanges,
    admin_enabled,
    audit_params,
    client_ip,
    require_admin,
    require_editor,
    require_owner,
)
from yes24_agent.config import Settings, get_settings
from yes24_agent.memos import (
    CollectionMeta,
    ImportMapping,
    ImportResult,
    MemoDraft,
    get_memo_service,
    read_import,
)

logger = logging.getLogger(__name__)

_Viewer = Annotated[AdminActor, Depends(require_admin)]
_Editor = Annotated[AdminActor, Depends(require_editor)]
_Owner = Annotated[AdminActor, Depends(require_owner)]


def _content_max(value: str) -> str:
    """base64 본문 상한 — 업로드 바이트 상한에서 파생(4/3배 + 패딩 여유)."""
    limit = math.ceil(get_settings().memo_import_max_bytes * 4 / 3) + 4
    if len(value) > limit:
        raise PydanticCustomError("upload_too_large", "파일이 너무 큽니다.")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _CollectionChanges(EditChanges):
    # NOT NULL 열은 `T = None`(생략 허용, null 입력은 422), 비울 수 있는 열만 `T | None`.
    title: str = None
    description: str = None
    source_url: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    active: bool = None
    always_include: bool = None


class _Memo(_Strict):
    content: str
    about: str | None = None
    link: str | None = None
    goods_nos: list[str] = []


class _MemoChanges(EditChanges):
    content: str = None
    about: str | None = None
    link: str | None = None
    goods_nos: list[str] = None


class _CollectionPatch(_Strict):
    changes: _CollectionChanges
    expected_revision: int


class _MemoPatch(_Strict):
    changes: _MemoChanges
    expected_revision: int


class _NewMemo(_Strict):
    memo: _Memo
    expected_revision: int


class _Revision(BaseModel):
    expected_revision: int


class _Upload(BaseModel):
    file_name: Annotated[str, Field(min_length=1, max_length=255)]
    content_b64: Annotated[str, AfterValidator(_content_max)]
    sheet: str | None = None
    mapping: ImportMapping | None = None


class _Import(_Upload):
    mapping: ImportMapping
    mode: Literal["append", "replace"]
    expected_revision: int
    consent: Literal[True]  # "내용이 고객 답변과 출처에 그대로 보일 수 있습니다" 확인


async def _read(body: _Upload, settings: Settings) -> ImportResult:
    try:
        data = base64.b64decode(body.content_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="파일 내용을 읽을 수 없습니다.") from None
    return await asyncio.to_thread(
        read_import, body.file_name, data, sheet=body.sheet, mapping=body.mapping,
        settings=settings,
    )


async def _stored(call):
    """저장소 호출 — 테이블 없음·DB 장애는 503(빈 성공으로 위장하지 않는다)."""
    try:
        return await call
    except SQLAlchemyError as exc:
        logger.warning(f"memos admin status=unavailable: {exc!r}")
        detail = "지식 데이터 저장소를 쓸 수 없습니다."
        raise HTTPException(status_code=503, detail=detail) from None


def register_memo_admin(app: FastAPI, settings: Settings) -> None:
    """메모 어드민 라우트 등록 — `admin_enabled`(세션 DB가 MySQL)일 때만."""
    if not admin_enabled(settings):
        return
    service = get_memo_service

    def audit(actor: AdminActor, request: Request):
        ip = client_ip(request)
        return lambda action, cid, before, after: (
            AUDIT_INSERT,
            audit_params(actor, "memo_collection", cid, action, before, after, ip=ip),
        )

    @app.get("/admin/api/memo-collections", include_in_schema=False)
    async def list_collections(actor: _Viewer) -> dict:
        """묶음 목록 + 이 서버가 조회 도구를 켰는가(usage) — 켜기만으로 반영되는지 화면이 알린다.

        tool은 서버 기본 구성(enabled_toolsets) 기준이다. 꺼진 서버에선 켠 묶음도 안 쓰인다.
        """
        usage = {"tool": "knowledge" in settings.enabled_toolsets}
        return {"collections": await _stored(service().list_collections()), "usage": usage}

    @app.get("/admin/api/memo-collections/{cid}/memos", include_in_schema=False)
    async def list_memos(actor: _Viewer, cid: int, query: str = "") -> dict:
        """묶음과 그 메모(꺼진 묶음도) — 도구와 같은 일치 함수, 메모 id 순."""
        return await _stored(service().list_memos(cid, query))

    @app.post("/admin/memo-collections", include_in_schema=False)
    async def create_collection(
        body: CollectionMeta, request: Request, response: Response, actor: _Editor
    ) -> dict:
        response.status_code = 201
        return await _stored(service().create_collection(
            body, actor.username, audit(actor, request)
        ))

    @app.patch("/admin/memo-collections/{cid}", include_in_schema=False)
    async def edit_collection(
        cid: int, body: _CollectionPatch, request: Request, actor: _Editor
    ) -> dict:
        return await _stored(service().update_collection(
            cid, body.changes.model_dump(exclude_unset=True), body.expected_revision,
            actor.username, audit(actor, request),
        ))

    @app.delete("/admin/memo-collections/{cid}", include_in_schema=False)
    async def delete_collection(cid: int, body: _Revision, request: Request, actor: _Owner) -> dict:
        """되돌릴 수 없어 owner만 한다 — editor의 안전한 동작은 끄기다."""
        await _stored(service().delete_collection(
            cid, body.expected_revision, actor.username, audit(actor, request)
        ))
        return {"id": cid, "deleted": True}

    @app.post("/admin/memo-collections/{cid}/memos", include_in_schema=False)
    async def create_memo(
        cid: int, body: _NewMemo, request: Request, response: Response, actor: _Editor
    ) -> dict:
        response.status_code = 201
        draft = MemoDraft(**body.memo.model_dump())
        return await _stored(service().create_memo(
            cid, draft, body.expected_revision, actor.username, audit(actor, request)
        ))

    @app.patch("/admin/memo-collections/{cid}/memos/{mid}", include_in_schema=False)
    async def edit_memo(cid: int, mid: int, body: _MemoPatch, request: Request,
                        actor: _Editor) -> dict:
        return await _stored(service().update_memo(
            cid, mid, body.changes.model_dump(exclude_unset=True), body.expected_revision,
            actor.username, audit(actor, request),
        ))

    @app.delete("/admin/memo-collections/{cid}/memos/{mid}", include_in_schema=False)
    async def delete_memo(cid: int, mid: int, body: _Revision, request: Request,
                          actor: _Editor) -> dict:
        return await _stored(service().delete_memo(
            cid, mid, body.expected_revision, actor.username, audit(actor, request)
        ))

    @app.post("/admin/memos/import-preview", include_in_schema=False)
    async def preview(body: _Upload, actor: _Editor) -> dict:
        """저장하지 않는다 — mapping이 없으면 시트·열·원시 샘플, 있으면 메모 초안·오류까지."""
        return asdict(await _read(body, settings))

    @app.post("/admin/memo-collections/{cid}/import", include_in_schema=False)
    async def import_memos(cid: int, body: _Import, request: Request, actor: _Editor) -> dict:
        result = await _read(body, settings)
        return await _stored(service().import_memos(
            cid, result, body.mode, body.expected_revision, actor.username, audit(actor, request)
        ))
