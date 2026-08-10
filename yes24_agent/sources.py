"""출처 레지스트리 — 도구 결과에 source_id를 부여하고 세션 state에 누적한다.

인용 환각을 구조로 차단하기 위한 핵심 모듈. 도구(yes24_search 등)가 결과를 반환할 때마다
`register_source`로 출처를 등록해 source_id를 받고, 모델은 답변에서 이 id를 `[n]` 마커로만
참조한다. postprocess 단계에서 `validate_citations`가 마커를 출처와 대조해 무효 인용을 제거한다.

ADK State 주의사항: State는 dict처럼 보이지만 변경 추적이 재할당 기반이다.
`state["sources"].append(...)` 같은 내부 변형은 델타로 기록되지 않을 수 있으므로,
반드시 새 리스트를 만들어 `state[SOURCES_STATE_KEY] = new_list`로 재할당한다.
"""

import threading
from collections.abc import MutableMapping
from datetime import datetime, timedelta, timezone
from typing import Any

# 세션 스코프 키 (temp: 접두사 금지 — 멀티턴에서 이전 턴 출처도 유지되어야 함)
SOURCES_STATE_KEY = "sources"
# 관측 충실도. **등록하는 도구가 선언하고 코어는 크기만 비교한다** — 코어가 "book_detail이면
# 더 충실"을 알면 그 순간 yes24 전용 지식이 엔진에 박힌다(T1). 척도 정의만 코어 몫이다:
# 0 = 목록·요약 관측, 1 이상 = 상세 관측(같은 URL의 요약보다 우선하고 meta를 평면으로 올린다).
SUMMARY_FIDELITY = 0
DETAIL_FIDELITY = 1

# 인보케이션 스코프 id 워터마크. id 발급은 `max(state의 기존 id)+1`이라 read-modify-write이고,
# 한 턴의 병렬 function call이 상대의 write를 보기 전에 자기 스냅샷을 읽으면 **둘 다 같은
# 번호**를 딴다. 그러면 `_reconcile_sources`가 id 기준 병합으로 서로 다른 출처를 한 레코드에
# 합쳐(서로 다른 타입의 키메라) 공개 DTO를 오염시키고, 모델도 "id n"이 둘인 결과를 받아
# 인용이 중의적이 된다. **역할 분담**: 턴 내 충돌은 이 워터마크가 막고, 턴 간 id 재사용으로
# 생기는 키메라는 merge_source_records의 정체성 가드가 막는다 — 관측된 증상(2026-08-06
# 도그푸딩: search_result 카드에 다른 타입의 메타 필드 동거)의 원인 경로는 후자로 추정되며
# 라이브 재현으로 확정된 바 없다. 이 픽스가 그 증상을 막았다고 단정하지 말 것.
# 정상 경로(ADK State write-through)에서는 스냅샷이 공유돼 충돌이 안 나지만, 그 보호는 ADK 내부
# 구현에 딸린 것이라(`State.__setitem__`에 "delta에만 쓰도록 바꾸자"는 TODO가 달려 있다) 발급
# 자체를 스냅샷과 무관하게 단조로 만든다. 같은 턴의 도구는 invocation_id를 공유하므로 번호가
# 겹칠 수 없다.
_ID_WATERMARK: dict[str, int] = {}
# 발급 구간(읽기→갱신)은 await가 없지만 ADK가 도구를 별도 스레드에서 돌릴 수 있어(sync 도구
# 경로) 락으로 원자성을 보장한다. 구간이 짧아 경합 비용은 무시할 수준이다.
_WATERMARK_LOCK = threading.Lock()
# 워터마크 보관 상한(동시 진행 인보케이션 수의 여유 상한). agent._invocation_instruction의
# lru_cache(64)와 같은 논리이며, 조정 대상 설정값이 아니라 자료구조 상한이라 코드 상수로 둔다.
# 초과 시 가장 오래 전에 시작된 인보케이션부터 버린다(dict는 삽입 순서를 보존한다).
_WATERMARK_MAX_INVOCATIONS = 64


def _next_source_id(existing: list, invocation_id: str | None) -> int:
    """다음 source_id를 발급한다. invocation_id가 있으면 그 턴 안에서 단조·유일을 보장한다."""
    state_max = max((source.get("id", 0) for source in existing), default=0)
    if invocation_id is None:
        return state_max + 1
    with _WATERMARK_LOCK:
        # 재대입은 삽입 순서를 바꾸지 않는다 — pop 후 재삽입으로 "최근 발급 = 최신"을 만든다.
        # 그래야 축출이 오래 쉰 인보케이션부터 버리고, 긴 도구 루프로 아직 진행 중인
        # 인보케이션의 워터마크를 떨어뜨려 id를 되감지 않는다.
        new_id = max(state_max, _ID_WATERMARK.pop(invocation_id, 0)) + 1
        _ID_WATERMARK[invocation_id] = new_id
        while len(_ID_WATERMARK) > _WATERMARK_MAX_INVOCATIONS:
            _ID_WATERMARK.pop(next(iter(_ID_WATERMARK)))
    return new_id


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

    **정체성 가드**: 두 레코드가 모두 url을 갖고 서로 다르면 병합하지 않고 incoming을 그대로
    돌려준다. 같은 id로 만났더라도 url이 다르면 다른 문서이며, 그때 합치면 한 레코드에 두
    문서의 필드가 동거하는 키메라가 된다(2026-08-06 실측: search_result 카드에 다른 타입의
    메타 필드 동거). 이 함수가 turn 내(_reconcile_sources)와 turn 간(runner의 known_sources 시드)
    두 노출면의 공통 원시함수라, 가드를 여기 한 곳에 두면 둘 다 덮인다. 원칙 4의 "같은 id는
    이번 턴 관측이 이김"의 구현이기도 하다. 한쪽 url이 비면 정체성을 단정할 수 없으므로
    보수적으로 기존 병합을 유지한다(빈 dict를 seed로 넘기는 누적 경로가 이 분기를 탄다).
    """
    existing_url, incoming_url = existing.get("url"), incoming.get("url")
    if existing_url and incoming_url and existing_url != incoming_url:
        return incoming
    if (existing.get("fidelity") or SUMMARY_FIDELITY) > (
        incoming.get("fidelity") or SUMMARY_FIDELITY
    ):
        lower_fidelity, higher_fidelity = incoming, existing
    else:
        lower_fidelity, higher_fidelity = existing, incoming
    winner_is_detail = (higher_fidelity.get("fidelity") or SUMMARY_FIDELITY) > SUMMARY_FIDELITY

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
    if winner_is_detail:
        merged_meta.update(
            {
                key: higher_fidelity[key]
                for key in merged_meta
                if higher_fidelity.get(key) is not None
            }
        )
    if merged_meta:
        merged["meta"] = merged_meta
    if winner_is_detail:
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
    invocation_id: str | None = None,
    fidelity: int = SUMMARY_FIDELITY,
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
                "fidelity": fidelity,
            }
            updated = merge_source_records(source, incoming)
            if updated != source:
                refreshed = list(existing)
                refreshed[index] = updated
                state[SOURCES_STATE_KEY] = refreshed
            return source["id"]

    new_id = _next_source_id(existing, invocation_id)
    new_source = {
        "id": new_id,
        "title": title,
        "url": url,
        "type": source_type,
        "snippet": snippet,
        "checked_at": checked_at,
        "meta": meta,
        "fidelity": fidelity,
    }
    # 재할당 패턴: 기존 리스트를 변형하지 않고 새 리스트를 만들어 대입한다.
    state[SOURCES_STATE_KEY] = [*existing, new_source]
    return new_id


def get_sources(state: MutableMapping[str, Any]) -> list[dict]:
    """등록 순서대로 출처 목록을 반환한다. state는 변형하지 않는다."""
    return list(state.get(SOURCES_STATE_KEY, []))
