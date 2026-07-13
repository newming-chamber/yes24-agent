"""도구 이벤트 → SSE 상태 라벨·출처 dict 번역, 출처 정합.

`runner.py`에서 ADK 도구 이벤트(function_call/response)를 프론트 계약으로 옮기는
순수 번역 함수들만 추출한 모듈이다(동작 불변). 도구 호출은 진행 status 라벨로,
도구 응답은 출처 dict로 번역하고, 병렬 도구 실행 시 세션 state가 잃을 수 있는 출처를
스트림 관찰본으로 보정한다(_reconcile_sources).
"""

from yes24_agent.yes24.urls import BROWSE_SEED_URLS


def _browse_label(section: str) -> str | None:
    """yes24_browse의 section 코드에 대한 한국어 라벨을 구한다(없으면 None).

    라벨의 단일 진실은 urls.BROWSE_SEED_URLS다(순환 import 없음). 미지 코드는 None.
    """
    entry = BROWSE_SEED_URLS.get(section)
    return entry.get("label") if entry is not None else None


def _status_for_call(call) -> tuple[str, str]:
    """도구 이름별 진행 status(stage, detail)를 만든다.

    yes24_search는 검색, yes24_fetch는 페이지 열람, yes24_browse는 코너 둘러보기,
    web_search는 웹 검색 라벨을 쓴다. 그 외 미지의 도구는 범용 라벨로 폴백해, 도구가
    늘어도 runner 수정 없이 자연스러운 상태 문구가 나오게 한다. 사용자 노출 문구에
    url 원문은 넣지 않는다.
    """
    name = getattr(call, "name", "") or ""
    args = call.args or {}
    if name == "yes24_search":
        query = args.get("query", "")
        return "searching", f"Yes24에서 '{query}' 검색 중…"
    if name == "yes24_fetch":
        title = args.get("title")
        if title:
            return "reading", f"『{title}』 상세 정보를 읽는 중…"
        return "reading", "페이지를 읽는 중…"
    if name == "fetch_many":
        items = args.get("items")
        count = len(items) if isinstance(items, list) else 0
        if count:
            return "reading", f"{count}개 상세를 함께 읽는 중…"
        return "reading", "여러 상세를 함께 읽는 중…"
    if name == "yes24_browse":
        label = _browse_label(args.get("section", ""))
        if label:
            return "browsing", f"Yes24 {label} 둘러보는 중…"
        return "browsing", "Yes24 코너를 둘러보는 중…"
    if name == "web_search":
        query = args.get("query", "")
        return "searching_web", f"웹에서 '{query}' 관련 정보를 찾는 중…"
    if name == "web_fetch":
        return "reading_web", "웹 페이지를 읽는 중…"
    return "working", "정보를 확인하는 중…"


# 도구 error_type → status(stage, detail) 매핑. "fetch"만 네트워크성이라 재시도로
# 복구될 수 있어 재시도 라벨을 쓴다. 나머지는 같은 요청을 반복해도 결과가 같으므로
# 재시도를 암시하지 않는 각 상황별 중립 문구를 쓴다(사용자에게 헛된 기대를 주지 않기 위함).
# 미지 error_type은 "페이지 fetch"로 단정하지 않는 범용 문구로 폴백한다.
_ERROR_STATUS: dict[str, tuple[str, str]] = {
    "fetch": ("retrying", "일시 오류, 재시도 중…"),
    "parse": ("notice", "페이지 내용을 가져오지 못했어요"),
    "empty": ("notice", "페이지 내용을 가져오지 못했어요"),
    "not_configured": ("notice", "지금은 웹 검색을 사용할 수 없어요"),
    "invalid_section": ("notice", "요청한 코너를 찾지 못했어요"),
}
_ERROR_STATUS_FALLBACK: tuple[str, str] = ("notice", "정보를 가져오지 못했어요")


def _status_for_error(payload: dict) -> tuple[str, str]:
    """도구 error 응답의 error_type별 status(stage, detail)를 만든다."""
    return _ERROR_STATUS.get(payload.get("error_type"), _ERROR_STATUS_FALLBACK)


def _sources_from_response(payload: dict) -> list[dict]:
    """도구 응답에서 노출할 출처 dict 목록을 방어적으로 뽑아낸다.

    yes24_search는 results 리스트를, yes24_fetch는 단일 source dict를 반환할 수
    있으므로 둘 다 허용한다(fetch 스키마는 아직 미확정). source_id를 가진 dict만
    출처로 인정한다.
    """
    results = payload.get("results")
    if isinstance(results, list):
        candidates = results
    elif payload.get("source_id") is not None:
        # results 리스트 없이 payload 자체가 하나의 출처(fetch형).
        candidates = [payload]
    else:
        candidates = []
    return [c for c in candidates if isinstance(c, dict) and c.get("source_id") is not None]


def _reconcile_sources(state_sources: list[dict], observed_sources: list[dict]) -> list[dict]:
    """done 조립·인용 검증에 쓸 최종 출처 목록을 만든다(부류 방어).

    ADK 2.3.0은 한 턴에 나온 병렬 function call을 asyncio.gather로 동시 실행하고,
    각 도구의 state_delta를 deep_merge_dicts가 **리스트 키에 대해 last-wins로 덮어쓴다**
    (flows/llm_flows/functions.py). 그래서 도구 완료 순서에 따라 세션 state["sources"]에서
    한 도구의 출처가 통째로 유실될 수 있고, postprocess가 유효한 [n] 인용을 잘라낸다.

    반면 병렬 function_response는 merge 시 parts가 모두 보존되므로, 런너가 스트림에서
    관찰해 누적한 출처(observed)는 유실되지 않는다. 두 출처를 id 기준으로 합쳐 유실 부류를
    막는다: 공통 id는 state를 우선(snippet/meta가 더 풍부), state가 잃은 id는 observed로
    채운다. id 오름차순으로 정렬해 반환한다(멀티턴 이전 턴 출처도 state에 포함돼 유지된다).
    """
    by_id: dict[int, dict] = {}
    for src in observed_sources:
        sid = src.get("id")
        if sid is not None:
            by_id.setdefault(sid, src)
    for src in state_sources:
        sid = src.get("id")
        if sid is not None:
            by_id[sid] = src
    return [by_id[key] for key in sorted(by_id)]
