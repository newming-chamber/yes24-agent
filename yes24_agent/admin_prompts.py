"""프롬프트 버전 어드민(owner 전용) — 목록·미리보기·생성·폐기, 그리고 모델 PATCH의 승격 게이트.

docs/admin-lab-design-20261019.md §2.3·2.4. 버전은 불변이다(편집 = parent_id를 단 새 행).
상태는 열이 아니라 파생이다: 폐기 = retired_at, 활성 = model_settings.prompt_versions 값에
있음, 나머지 = 초안. 승격·롤백은 새 엔드포인트가 아니라 기존 `PATCH /admin/models`
(changes.prompt_versions)이고, 그 라우트가 같은 트랜잭션에서 `check_prompt_bindings`를 부른다.
첫 승격은 owner의 승격 사유 메모(본문 reason)가 필수이며 모델 설정 감사 행(after)에 남는다
(판정은 로컬 qa/testbed 배터리가 하고, 서버는 그 결론을 적는 사람의 사유만 요구한다).

쓰기는 전부 AdminService.transaction + 감사(target_type "prompt"), 읽기는 admin.py의 요청별
READ ONLY 스냅샷(`query` 주입 — register_admin_prompts)이다. LLM은 부르지 않는다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiomysql
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from yes24_agent.admin_auth import (
    AdminActor,
    AdminService,
    EditBody,
    EditChanges,
    EditConflict,
    require_owner,
)
from yes24_agent.admin_data import jsonable
from yes24_agent.agent import PROMPT_SLOTS, build_system_prompt, storable_overlay
from yes24_agent.config import Settings, get_settings
from yes24_agent.model_routing import SETTINGS_ROW_ID
from yes24_agent.prompt_versions import AUDIT_TARGET, code_base_hash, decode_slots, full_app

logger = logging.getLogger(__name__)

_VERSION_COLUMNS = (
    "id, name, model, slots, base_hash, parent_id, note, activated_at, retired_at, "
    "created_by, created_at"
)
_SELECT_BINDINGS = "SELECT prompt_versions FROM model_settings WHERE id = %s"
# 미리보기 슬롯 표지 — 코드 fragment에 없는 문자라 잠긴 본문과 겹치지 않는다(오버레이 값은
# 경계를 얻은 뒤에 넣는다).
_MARK = "\x00"


def decode_bindings(raw: Any) -> dict[str, int]:
    """model_settings.prompt_versions(정본 직렬화 문자열) → 맵. NULL·깨진 값은 빈 맵."""
    value = decode_slots(raw)
    return value if isinstance(value, dict) else {}


def version_status(row: dict, active: set[int]) -> str:
    """파생 상태 — 폐기 > 활성 > 초안 (활성 중 폐기는 PATCH가 막는다)."""
    if row["retired_at"] is not None:
        return "retired"
    return "active" if row["id"] in active else "draft"


def preview_segments(slots: dict[str, str]) -> list[dict[str, Any]]:
    """풀 구성 조립 프롬프트를 잠긴 영역·슬롯 영역 조각으로 — 화면은 이어 붙이면 전체 문자열이다.

    조립은 서빙과 같은 build_system_prompt 한 함수다(슬롯 값 자리에 표지를 넣어 경계를 얻은 뒤
    실제 값으로 바꾼다). 조각 단위라 클라이언트의 문자열 오프셋 단위(UTF-16 등)와 무관하다.
    """
    marks = {name: f"{_MARK}{name}{_MARK}" for name in PROMPT_SLOTS}
    parts = build_system_prompt(full_app(), overlay=marks).split(_MARK)
    segments: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if index % 2:  # 표지 사이 = 슬롯 이름
            segments.append({
                "slot": part,
                "text": slots.get(part, PROMPT_SLOTS[part]),
                "overridden": part in slots,
            })
        elif part:
            segments.append({"slot": None, "text": part, "overridden": False})
    return segments


async def fetch_prompts(cur, settings: Settings) -> dict[str, Any]:
    await cur.execute(f"SELECT {_VERSION_COLUMNS} FROM prompt_version ORDER BY id DESC")
    rows = await cur.fetchall()
    await cur.execute(_SELECT_BINDINGS, (SETTINGS_ROW_ID,))
    stored = await cur.fetchone()
    bindings = decode_bindings(stored["prompt_versions"] if stored else None)
    current = code_base_hash()
    active = set(bindings.values())
    versions = []
    for row in rows:
        item = jsonable(row, ("slots",))
        item.update(
            status=version_status(row, active),
            bound_to=sorted(key for key, value in bindings.items() if value == row["id"]),
            base_current=row["base_hash"] == current,
        )
        versions.append(item)
    return {
        "versions": versions,
        "bindings": bindings,
        "slots": [{"name": name, "default": text} for name, text in PROMPT_SLOTS.items()],
        "slot_max_chars": settings.prompt_slot_max_chars,
        "base_hash": current,
    }


async def fetch_preview(cur, version_id: int) -> dict[str, Any]:
    await cur.execute("SELECT slots FROM prompt_version WHERE id = %s", (version_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="대상을 찾을 수 없습니다.")
    slots = decode_slots(row["slots"])
    segments = preview_segments(slots if storable_overlay(slots) else {})
    return {"segments": segments, "text": "".join(segment["text"] for segment in segments)}


async def check_prompt_bindings(cur, raw: str | None, reason: str | None) -> dict[str, Any]:
    """모델 PATCH의 prompt_versions 저장값을 같은 트랜잭션에서 DB 판정한다(위반 = 422).

    1. 버전 존재·미폐기·model 일치(NULL 또는 키와 같음 — ANY_MODEL 키는 NULL만).
    2. 첫 승격(activated_at 없음)은 게이트: owner의 승격 사유 메모(reason)가 필수다. 통과하면
       activated_at을 세팅하고 감사 after에 실을 값(promoted 버전 id·promotion_reason)을 돌려준다.
       activated_at이 있으면 롤백이라 면제.
    3. base_hash가 지금 코드 기본과 다르면 거부가 아니라 경고(화면이 띄운다).
    호출부는 model_settings 행을 이미 FOR UPDATE로 잡은 상태다(edit_row) — 폐기(PATCH
    prompts)와 직렬화된다. 반환: {"audit": 감사 after에 합칠 값, "warnings": [...]} (빈 키 생략).
    """
    bindings = decode_bindings(raw)
    if not bindings:
        return {}
    ids = sorted(set(bindings.values()))
    await cur.execute(
        f"SELECT id, model, base_hash, activated_at, retired_at FROM prompt_version "
        f"WHERE id IN ({', '.join(['%s'] * len(ids))})",
        tuple(ids),
    )
    rows = {row["id"]: row for row in await cur.fetchall()}
    problems = []
    for key, version in sorted(bindings.items()):
        row = rows.get(version)
        if row is None:
            problems.append(f"{key}: 버전 {version}이 없습니다")
        elif row["retired_at"] is not None:
            problems.append(f"{key}: 버전 {version}은 폐기됐습니다")
        elif row["model"] is not None and row["model"] != key:
            problems.append(f"{key}: 버전 {version}은 {row['model']} 전용입니다")
    promoted = [
        version for version in ids
        if version in rows and rows[version]["activated_at"] is None
    ]
    if promoted and not reason:
        problems.append(
            f"버전 {', '.join(map(str, promoted))}: 첫 승격에는 승격 사유 메모(reason)가 필요합니다"
        )
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))
    if promoted:
        await cur.execute(
            f"UPDATE prompt_version SET activated_at = NOW(3) WHERE activated_at IS NULL "
            f"AND id IN ({', '.join(['%s'] * len(promoted))})",
            tuple(promoted),
        )
    current = code_base_hash()
    stale = [version for version in ids if rows[version]["base_hash"] != current]
    out: dict[str, Any] = {}
    if promoted:
        out["audit"] = {"promoted": promoted, "promotion_reason": reason}
    if stale:
        out["warnings"] = [{"kind": "base_hash_mismatch", "version_id": v} for v in stale]
    return out


# ── 요청 본문 ───────────────────────────────────────────────────────────────


class PromptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    model: str | None = None
    slots: dict[str, Any]
    parent_id: int | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("slots")
    @classmethod
    def _storable(cls, value):
        """판정은 서빙과 한 함수다(agent.storable_overlay)."""
        if storable_overlay(value):
            return value
        raise PydanticCustomError(
            "unknown_slot",
            "선언된 슬롯({slots})의 문자열만, 슬롯당 {limit}자까지 저장할 수 있습니다.",
            {"slots": ", ".join(PROMPT_SLOTS), "limit": get_settings().prompt_slot_max_chars},
        )

    @field_validator("model")
    @classmethod
    def _selectable(cls, value):
        if value is None or value in get_settings().selectable_models.values():
            return value
        raise PydanticCustomError(
            "unknown_model", "선택할 수 없는 모델입니다: {value}", {"value": value}
        )


class PromptChanges(EditChanges):
    """retired_at: true = 폐기(서버 시각), null·false = 폐기 해제. note: null = 메모 지움."""

    retired_at: bool | None = None
    note: str | None = Field(default=None, max_length=500)


class _PromptExpected(BaseModel):
    """화면이 본 값(retired_at은 폐기 여부 bool) — 검증 없이 대조만."""

    model_config = ConfigDict(extra="forbid")

    retired_at: bool | None = None
    note: str | None = None


class PromptEdit(EditBody):
    changes: PromptChanges
    expected: _PromptExpected | None = None


# ── 라우트 ─────────────────────────────────────────────────────────────────


def register_admin_prompts(app: FastAPI, settings: Settings, query) -> None:
    """owner 전용 프롬프트 버전 라우트. query는 admin.py의 READ ONLY 스냅샷 실행기(fetch, *args)."""
    owner = [Depends(require_owner)]

    @app.get("/admin/api/prompts", dependencies=owner, include_in_schema=False)
    async def admin_prompts() -> Any:
        return await query(fetch_prompts, settings)

    @app.get("/admin/api/prompts/{version_id}/preview", dependencies=owner, include_in_schema=False)
    async def admin_prompt_preview(version_id: int) -> Any:
        return await query(fetch_preview, version_id)

    @app.post("/admin/prompts", status_code=201, include_in_schema=False)
    async def admin_create_prompt(
        body: PromptCreate, request: Request, actor: AdminActor = Depends(require_owner)
    ) -> dict[str, Any]:
        """새 버전(불변). base_hash = 지금 코드 기본 해시."""
        base_hash = code_base_hash()
        async with AdminService.get_instance().transaction(actor, request) as tx:
            if body.parent_id is not None:
                await tx.cur.execute(
                    "SELECT id FROM prompt_version WHERE id = %s", (body.parent_id,)
                )
                if await tx.cur.fetchone() is None:
                    raise HTTPException(status_code=422, detail="부모 버전이 없습니다.")
            try:
                await tx.cur.execute(
                    "INSERT INTO prompt_version (name, model, slots, base_hash, parent_id, note, "
                    "created_by) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        body.name, body.model, json.dumps(body.slots, ensure_ascii=False),
                        base_hash, body.parent_id, body.note, actor.user_id,
                    ),
                )
            except aiomysql.IntegrityError:
                raise HTTPException(
                    status_code=409, detail="같은 이름의 버전이 있습니다."
                ) from None
            version_id = tx.cur.lastrowid
            await tx.audit(
                AUDIT_TARGET, version_id, "create", None,
                {**body.model_dump(), "base_hash": base_hash},
            )
        return {"id": version_id, "base_hash": base_hash}

    @app.patch("/admin/prompts/{version_id}", include_in_schema=False)
    async def admin_update_prompt(
        version_id: int, body: PromptEdit, request: Request,
        actor: AdminActor = Depends(require_owner),
    ) -> dict[str, Any]:
        """폐기·메모. 활성 바인딩 중인 버전은 폐기할 수 없다(422).

        승격(PATCH /admin/models)과 같은 model_settings 행 잠금을 먼저 잡아 직렬화한다."""
        changes = body.changes.model_dump(exclude_unset=True)
        if "retired_at" in changes:
            changes["retired_at"] = bool(changes["retired_at"])
        expected = body.expected.model_dump(exclude_unset=True) if body.expected else None
        async with AdminService.get_instance().transaction(actor, request) as tx:
            await tx.cur.execute(f"{_SELECT_BINDINGS} FOR UPDATE", (SETTINGS_ROW_ID,))
            stored = await tx.cur.fetchone()
            bindings = decode_bindings(stored["prompt_versions"] if stored else None)
            await tx.cur.execute(
                "SELECT note, retired_at FROM prompt_version WHERE id = %s FOR UPDATE",
                (version_id,),
            )
            row = await tx.cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="대상을 찾을 수 없습니다.")
            current = {"note": row["note"], "retired_at": row["retired_at"] is not None}
            if expected is not None and any(current[k] != v for k, v in expected.items()):
                raise EditConflict(current)
            after = {k: v for k, v in changes.items() if current[k] != v}
            if after.get("retired_at") and version_id in bindings.values():
                raise HTTPException(
                    status_code=422, detail="활성 바인딩 중인 버전은 폐기할 수 없습니다."
                )
            if after:
                assignments = []
                params: list[Any] = []
                if "note" in after:
                    assignments.append("note = %s")
                    params.append(after["note"])
                if "retired_at" in after:
                    assignments.append("retired_at = IF(%s, NOW(3), NULL)")
                    params.append(after["retired_at"])
                await tx.cur.execute(
                    f"UPDATE prompt_version SET {', '.join(assignments)} WHERE id = %s",
                    (*params, version_id),
                )
                await tx.audit(
                    AUDIT_TARGET, version_id, "update", {k: current[k] for k in after}, after
                )
        return {"updated": sorted(after)}
