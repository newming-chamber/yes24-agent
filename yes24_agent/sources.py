"""출처 레지스트리 — 도구 결과에 source_id를 부여하고 세션 state에 누적한다.

인용 환각을 구조로 차단하기 위한 핵심 모듈. 도구(yes24_search 등)가 결과를 반환할 때마다
`register_source`로 출처를 등록해 source_id를 받고, 모델은 답변에서 이 id를 `[n]` 마커로만
참조한다. postprocess 단계에서 `validate_citations`가 마커를 출처와 대조해 무효 인용을 제거한다.

ADK State 주의사항: State는 dict처럼 보이지만 변경 추적이 재할당 기반이다.
`state["sources"].append(...)` 같은 내부 변형은 델타로 기록되지 않을 수 있으므로,
반드시 새 리스트를 만들어 `state[SOURCES_STATE_KEY] = new_list`로 재할당한다.
"""

from collections.abc import MutableMapping
from datetime import datetime, timedelta, timezone
from typing import Any

# 세션 스코프 키 (temp: 접두사 금지 — 멀티턴에서 이전 턴 출처도 유지되어야 함)
SOURCES_STATE_KEY = "sources"

# KST(UTC+9). 도구·매트릭스가 "오늘"·검색시각(checked_at)을 계산하는 단일 기준.
# 값 자체는 외부 사실이지만, 10개 파일에 재정의돼 있던 것을 여기 한 곳으로 모은다.
KST = timezone(timedelta(hours=9))


def now_checked_at() -> str:
    """도구 결과의 checked_at(KST 기준 "YYYY-MM-DD HH:MM")을 조립한다.

    7개 도구·매트릭스가 같은 문자열을 만들던 것을 단일 함수로 모은다 — 포맷이 갈라지면
    프론트가 시각을 다르게 렌더하므로 한 곳에서만 정의한다."""
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def register_source(
    state: MutableMapping[str, Any],
    *,
    title: str,
    url: str,
    source_type: str,
    snippet: str | None = None,
    meta: dict | None = None,
) -> int:
    """출처를 등록하고 source_id를 반환한다.

    동일 url이 이미 등록돼 있으면 새로 추가하지 않고 기존 source_id를 반환한다.
    """
    existing = state.get(SOURCES_STATE_KEY, [])
    for source in existing:
        if source["url"] == url:
            return source["id"]

    new_id = len(existing) + 1
    new_source = {
        "id": new_id,
        "title": title,
        "url": url,
        "type": source_type,
        "snippet": snippet,
        "meta": meta,
    }
    # 재할당 패턴: 기존 리스트를 변형하지 않고 새 리스트를 만들어 대입한다.
    state[SOURCES_STATE_KEY] = [*existing, new_source]
    return new_id


def get_sources(state: MutableMapping[str, Any]) -> list[dict]:
    """등록 순서대로 출처 목록을 반환한다. state는 변형하지 않는다."""
    return list(state.get(SOURCES_STATE_KEY, []))
