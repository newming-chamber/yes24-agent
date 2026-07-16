"""현재 턴에 검증 가능한 근거가 없을 때 한 번만 재검색하는 구조 게이트."""

from dataclasses import dataclass

from yes24_agent.sources import PRODUCT_SOURCE_TYPES


def has_product_grounding(sources: list[dict]) -> bool:
    """Yes24 상품 출처가 하나라도 있는지 반환한다."""
    return any(source.get("type") in PRODUCT_SOURCE_TYPES for source in sources)


@dataclass(frozen=True)
class Gate:
    """근거 확인을 한 번 더 실행하라는 제어 신호."""

    kind: str
    reason: str
    status_detail: str
    force_tool: str | None = None
    required_source_types: frozenset[str] | None = None


def evaluate(
    *,
    cited_sources: list[dict],
    observed_tool_calls: list[dict],
    support_count: int = 0,
    needs_grounding: bool = False,
    required_source_types: frozenset[str] | None = None,
    force_tool: str | None = None,
) -> Gate | None:
    """접지가 필요한 턴에 유효 인용이 없으면 한 번의 재검색을 요청한다."""
    searched = any(isinstance(call.get("result_count"), int) for call in observed_tool_calls)
    required_missing = bool(required_source_types) and not any(
        source.get("type") in required_source_types for source in cited_sources
    )
    if (needs_grounding or searched) and (support_count == 0 or required_missing):
        return Gate(
            kind="missing",
            reason="unfulfilled" if not searched else "shallow",
            status_detail=(
                "실제로 확인해서 답해 드릴게요"
                if not searched
                else "검색 범위를 넓혀 다시 확인하고 있어요"
            ),
            force_tool=force_tool,
            required_source_types=required_source_types,
        )
    return None
