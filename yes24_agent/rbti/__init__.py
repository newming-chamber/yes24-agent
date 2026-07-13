"""RBTI 독서 페르소나 — 4축 조각 데이터 + 조립.

16유형을 케이스로 하드코딩하지 않고, 8개 축-값 조각을 데이터로 두어 코드 4글자를
파싱해 4조각을 조립한다(no-case-patch). 페르소나는 답변의 톤·추천 구성·탐색 방향만
조율하며, 인용·무출처 상품 게이트·오늘날짜 시제·정체성 순서는 약화시키지 않는다.
"""

from yes24_agent.rbti.persona import (
    AXIS_FRAGMENTS,
    AXIS_ORDER,
    AXIS_VALUE_LABELS_KO,
    TYPE_ARCHETYPES,
    axis_label,
    build_persona_block,
    get_archetype,
    get_archetype_name,
    is_valid_code,
)

__all__ = [
    "AXIS_FRAGMENTS",
    "AXIS_ORDER",
    "AXIS_VALUE_LABELS_KO",
    "TYPE_ARCHETYPES",
    "axis_label",
    "build_persona_block",
    "get_archetype",
    "get_archetype_name",
    "is_valid_code",
]
