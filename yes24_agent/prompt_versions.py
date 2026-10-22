"""프롬프트 버전 — DB 슬롯 오버레이의 서빙 계층(docs/admin-lab-design-20261019.md §2).

계약 경계는 코드에 있다: fragment 중 슬롯 이름이 있는 것만 편집 가능하고(agent.PROMPT_SLOTS),
DB의 버전은 슬롯 이름 → 텍스트 맵뿐이라 계약 본문은 DB에 존재할 수 없다. 버전은 **불변**(편집 =
새 행)이라 id → 슬롯 맵을 프로세스 영구 캐시(agent.register_overlay)에 두고, 에이전트 캐시 키도
id 하나다.

서빙 정책(model_routing.plan_turn_models가 부른다): 읽기 실패·검증 실패는 WARNING 후 오버레이 없이
진행한다 — 요청을 실패시키지 않는다(ModelSettingsStore와 같은 반대 정책, db.py 주석).
쓰기(생성·폐기·승격)는 어드민 트랜잭션이 한다(admin_prompts).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from yes24_agent.agent import build_system_prompt, has_overlay, register_overlay, storable_overlay
from yes24_agent.config import get_settings
from yes24_agent.db import LazyAiomysqlPool
from yes24_agent.session_service import mysql_pool_kwargs
from yes24_agent.toolsets import TOOLSETS, resolve_app_for

logger = logging.getLogger(__name__)

# 감사 기록(admin_audit.target_type) — 프롬프트 버전 생성·폐기·메모.
AUDIT_TARGET = "prompt"
_BASE_HASH_CHARS = 16  # prompt_version.base_hash VARCHAR(16)
_SELECT_SLOTS = "SELECT slots FROM prompt_version WHERE id = %s"


def decode_slots(raw: Any) -> Any:
    """prompt_version.slots 열 값(드라이버가 JSON을 문자열로 준다) → 파이썬 값(깨진 JSON은 None)."""
    if isinstance(raw, (str, bytes)):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def full_app():
    """풀 구성(설정 페르소나 + 등록된 전 toolset) — base_hash·미리보기의 구성 축(골든과 같다)."""
    return resolve_app_for(get_settings().agent_persona, frozenset(TOOLSETS))


def code_base_hash() -> str:
    """지금 코드의 프롬프트 기본(오버레이 없는 풀 구성 조립 바이트) 해시.

    버전 저장 시 base_hash로 박아 두고, 승격 때 다르면 "코드 기본이 바뀐 뒤 만든 버전이 아님" 경고를
    낸다.
    """
    digest = hashlib.sha256(build_system_prompt(full_app()).encode("utf-8")).hexdigest()
    return digest[:_BASE_HASH_CHARS]


class PromptVersionStore:
    """prompt_version 읽기 전용 풀(maxsize 1). db_url 기본은 세션 DB다."""

    _instance: PromptVersionStore | None = None

    def __init__(self, db_url: str | None = None, pool_factory=None) -> None:
        url = get_settings().session_db_url if db_url is None else db_url
        kwargs = mysql_pool_kwargs(url, maxsize=1)
        self._db = LazyAiomysqlPool(kwargs, pool_factory) if kwargs is not None else None

    @classmethod
    def get_instance(cls) -> PromptVersionStore:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def enabled(self) -> bool:
        return self._db is not None

    async def read_slots(self, version_id: int) -> Any:
        """버전의 슬롯 맵(행이 없으면 None). 드라이버 예외는 그대로 올린다(정책은 ensure_loaded)."""

        async def select(conn):
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_SLOTS, (version_id,))
                return await cur.fetchone()

        row = await self._db.run(select)
        return decode_slots(row[0]) if row else None

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()


async def close_prompt_version_store() -> None:
    """앱 종료 훅 — 만들어진 적 없으면 no-op."""
    if PromptVersionStore._instance is not None:
        await PromptVersionStore._instance.close()


async def ensure_loaded(version_id: int, store: PromptVersionStore | None = None) -> bool:
    """버전 슬롯을 프로세스 캐시에 올린다(이미 있으면 즉시 True). 실패는 WARNING + False.

    검증은 저장과 같은 단일 판정(agent.storable_overlay)이다 — 저장 뒤 슬롯 선언이 바뀐 배포에서
    낡은 행이 선언 밖 키를 들고 오면 여기서 거른다.
    """
    if has_overlay(version_id):
        return True
    store = store or PromptVersionStore.get_instance()
    if not store.enabled:
        logger.warning(f"프롬프트 버전 {version_id}: 버전 저장소가 없는 구성 → 오버레이 없이 진행")
        return False
    try:
        slots = await store.read_slots(version_id)
    except Exception as exc:  # noqa: BLE001 — 버전 읽기 실패가 답변을 막지 않는다
        logger.warning(
            f"프롬프트 버전 {version_id} 읽기 실패 → 오버레이 없이 진행: {type(exc).__name__}"
        )
        return False
    if slots is None or not storable_overlay(slots):
        logger.warning(f"프롬프트 버전 {version_id}: 없거나 선언 밖 슬롯 → 오버레이 없이 진행")
        return False
    register_overlay(version_id, slots)
    return True
