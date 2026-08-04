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
PRODUCT_SOURCE_TYPES = frozenset({"search_result", "book_detail", "browse"})
PRODUCT_DETAIL_SOURCE_TYPES = frozenset({"book_detail"})

# KST(UTC+9). 도구·매트릭스가 "오늘"·검색시각(checked_at)을 계산하는 단일 기준.
# 값 자체는 외부 사실이지만, 10개 파일에 재정의돼 있던 것을 여기 한 곳으로 모은다.
KST = timezone(timedelta(hours=9))


def today_kst() -> str:
    """현재 KST 날짜를 에이전트 프롬프트와 최신성 검색이 공유하는 형식으로 반환한다."""
    now = datetime.now(KST)
    return f"{now.year}년 {now.month}월 {now.day}일"


def time_kst() -> str:
    """현재 KST 시각(분 단위 절사)을 에이전트 프롬프트가 쓰는 형식으로 반환한다.

    날짜만 주입하던 동안 모델이 시각은 웹에 물어봤고, 그라운딩이 돌려준 UTC를 KST로 라벨링해
    9시간 어긋난 답이 나왔다(2026-08-03 QA: 4문항 중 3오답). 초는 답변에 무의미하고 프롬프트만
    더 자주 바꾸므로 분에서 끊는다."""
    return datetime.now(KST).strftime("%H시 %M분")


def now_checked_at() -> str:
    """도구 결과의 checked_at(KST 기준 "YYYY-MM-DD HH:MM")을 조립한다.

    7개 도구·매트릭스가 같은 문자열을 만들던 것을 단일 함수로 모은다 — 포맷이 갈라지면
    프론트가 시각을 다르게 렌더하므로 한 곳에서만 정의한다."""
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def _merge_evidence_segments(existing: object, incoming: object) -> list[dict]:
    """같은 출처를 여러 창에서 읽은 근거 구간을 ID 기준으로 합친다."""
    merged: dict[object, dict] = {}
    for segments in (existing, incoming):
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict) or segment.get("segment_id") is None:
                continue
            merged.setdefault(segment["segment_id"], segment)
    return list(merged.values())


def merge_source_records(existing: dict, incoming: dict) -> dict:
    """동일 출처 관측을 합치되 상품 상세 충실도를 보존한다.

    세션 레지스트리(멀티턴 카탈로그: source id·읽은 상세 보존)와 이번 턴 스트림 스냅샷
    양쪽에서 같은 병합 판정을 쓴다 — 범위 구분은 호출자가 어떤 컬렉션에 쌓느냐로 정해진다.
    """
    existing_is_detail = existing.get("type") in PRODUCT_DETAIL_SOURCE_TYPES
    incoming_is_product_summary = (
        incoming.get("type") in PRODUCT_SOURCE_TYPES
        and incoming.get("type") not in PRODUCT_DETAIL_SOURCE_TYPES
    )
    if existing_is_detail and incoming_is_product_summary:
        lower_fidelity, higher_fidelity = incoming, existing
    else:
        lower_fidelity, higher_fidelity = existing, incoming

    merged = dict(lower_fidelity)
    merged.update({key: value for key, value in higher_fidelity.items() if value is not None})

    lower_meta = lower_fidelity.get("meta") if isinstance(lower_fidelity.get("meta"), dict) else {}
    higher_meta = (
        higher_fidelity.get("meta") if isinstance(higher_fidelity.get("meta"), dict) else {}
    )
    merged_meta = {
        **lower_meta,
        **{key: value for key, value in higher_meta.items() if value is not None},
    }
    if higher_fidelity.get("type") in PRODUCT_DETAIL_SOURCE_TYPES:
        merged_meta.update(
            {
                key: higher_fidelity[key]
                for key in merged_meta
                if higher_fidelity.get(key) is not None
            }
        )
    if merged_meta:
        merged["meta"] = merged_meta
    if higher_fidelity.get("type") in PRODUCT_DETAIL_SOURCE_TYPES:
        merged.update(merged_meta)
        merged.update(
            {
                key: value
                for key, value in higher_fidelity.items()
                if value is not None and key != "meta"
            }
        )

    evidence_segments = _merge_evidence_segments(
        existing.get("_evidence_segments"),
        incoming.get("_evidence_segments"),
    )
    if evidence_segments:
        merged["_evidence_segments"] = evidence_segments
    return merged


def register_source(
    state: MutableMapping[str, Any],
    *,
    title: str,
    url: str,
    source_type: str,
    snippet: str | None = None,
    checked_at: str | None = None,
    meta: dict | None = None,
) -> int:
    """출처를 등록하고 source_id를 반환한다.

    동일 URL은 source_id를 유지하고 관측을 병합한다. 상품 상세는 이후 검색 목록 관측보다
    정보 충실도가 높으므로 유형·본문·canonical 메타를 유지한다.

    `None`은 이번 도구가 해당 필드를 관측하지 않았다는 뜻이므로 기존 값을 유지한다. 같은
    충실도의 새 관측과 새 상세는 기존 값을 갱신하고, 검색 요약은 저장된 상세를 낮추지 않는다.
    """
    existing = state.get(SOURCES_STATE_KEY, [])
    for index, source in enumerate(existing):
        if source["url"] == url:
            incoming = {
                "id": source["id"],
                "title": title or None,
                "url": url,
                "type": source_type,
                "snippet": snippet,
                "checked_at": checked_at,
                "meta": meta,
            }
            updated = merge_source_records(source, incoming)
            if updated != source:
                refreshed = list(existing)
                refreshed[index] = updated
                state[SOURCES_STATE_KEY] = refreshed
            return source["id"]

    new_id = max((source.get("id", 0) for source in existing), default=0) + 1
    new_source = {
        "id": new_id,
        "title": title,
        "url": url,
        "type": source_type,
        "snippet": snippet,
        "checked_at": checked_at,
        "meta": meta,
    }
    # 재할당 패턴: 기존 리스트를 변형하지 않고 새 리스트를 만들어 대입한다.
    state[SOURCES_STATE_KEY] = [*existing, new_source]
    return new_id


def get_sources(state: MutableMapping[str, Any]) -> list[dict]:
    """등록 순서대로 출처 목록을 반환한다. state는 변형하지 않는다."""
    return list(state.get(SOURCES_STATE_KEY, []))
