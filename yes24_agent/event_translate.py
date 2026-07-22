"""도구 이벤트 → SSE 상태 라벨·출처 dict 번역, 출처 정합.

`runner.py`에서 ADK 도구 이벤트(function_call/response)를 프론트 계약으로 옮기는
순수 번역 함수들만 추출한 모듈이다(동작 불변). 도구 호출은 진행 status 라벨로,
도구 응답은 출처 dict로 번역하고, 병렬 도구 실행 시 세션 state가 잃을 수 있는 출처를
스트림 관찰본으로 보정한다(_reconcile_sources).
"""

from yes24_agent.sources import merge_turn_source_records
from yes24_agent.yes24.parsers import GROUNDING_FIELDS
from yes24_agent.yes24.urls import BROWSE_SEED_URLS


def _browse_label(section: str) -> str | None:
    """yes24_browse의 section 코드에 대한 한국어 라벨을 구한다(없으면 None).

    라벨의 단일 진실은 urls.BROWSE_SEED_URLS다(순환 import 없음). 미지 코드는 None.
    """
    entry = BROWSE_SEED_URLS.get(section)
    return entry.get("label") if entry is not None else None


def _angles(queries) -> list[str]:
    """멀티쿼리 도구의 queries 인자에서 유효한 검색 각도만 추린다(진행 문구용).

    yes24_search·web_search가 같은 리스트 계약을 쓰므로 추출도 한 곳에서 한다.
    """
    if not isinstance(queries, list):
        return []
    return [q for q in queries if isinstance(q, str) and q.strip()]


# 턴 시작 라벨. 도구 호출에서 파생되지 않는 유일한 status라 과거엔 runner와 프론트에
# 각자 흩어져 있었고(같은 문자열 3벌), 한쪽만 고치면 라벨이 두 번 뜨는 취약 구조였다.
# 사용자 노출 문구의 단일 출처는 서버다 — 프론트는 서버가 보낸 값만 렌더한다.
TURN_START_STATUS: tuple[str, str] = ("thinking", "질문을 확인하고 있어요")


def _status_for_call(call) -> tuple[str, str] | None:
    """도구 이름별 진행 status(stage, detail)를 만든다. 알릴 진행이 없으면 None.

    yes24_search는 검색, yes24_fetch는 페이지 열람, yes24_browse는 코너 둘러보기,
    web_search는 웹 검색 라벨을 쓴다. 사용자 노출 문구에 url 원문은 넣지 않는다.

    **None을 돌려주는 것이 폴백이다.** 진행 문구는 실제 런타임 전이를 설명할 때만
    가치가 있고, 아무것도 조회하지 않는 도구(reply_directly)에 "정보를 확인하는 중…"을
    붙이면 사용자에게 일어나지 않은 일을 알리게 된다. 도구가 늘어도 runner는 그대로다 —
    라벨이 필요하면 여기에 분기를 더하고, 아니면 조용히 지나간다.
    """
    name = getattr(call, "name", "") or ""
    args = call.args or {}
    if name == "yes24_search":
        angles = _angles(args.get("queries"))
        if angles:
            return "searching", f"Yes24에서 {' · '.join(angles)} 검색 중…"
        return "searching", "Yes24에서 검색 중…"
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
        angles = _angles(args.get("queries"))
        if angles:
            return "searching_web", f"웹에서 {' · '.join(angles)} 관련 정보를 찾는 중…"
        return "searching_web", "웹에서 정보를 찾는 중…"
    if name == "web_fetch":
        return "reading_web", "웹 페이지를 읽는 중…"
    # 모르는 도구는 **아무 상태도 내지 않는다**(None). 범용 폴백("정보를 확인하는 중…")은
    # reply_directly처럼 아무것도 조회하지 않는 턴에 붙어 일어나지 않는 일을 알렸다 —
    # 같은 웨이브에서 지운 거짓 라벨 "retrying"과 동일 클래스다. 도구명을 하드코딩해 예외를
    # 늘리는 대신, "설명할 진행이 없으면 말하지 않는다"를 기본값으로 둔다.
    return None


# 도구 error_type → status(stage, detail) 매핑. **어떤 항목도 재시도를 암시하지 않는다** —
# 런너는 도구 에러에 재시도를 스케줄하지 않으므로(HTTP 재시도는 client의 max_retries에서
# 이미 소진된 뒤 에러가 올라온다) "재시도 중"류 문구는 사용자에게 헛된 기대를 준다.
# 미지 error_type은 "페이지 fetch"로 단정하지 않는 범용 문구로 폴백한다.
_ERROR_STATUS: dict[str, tuple[str, str]] = {
    # "fetch"는 별도 항목을 두지 않는다 — HTTP 재시도는 client의 max_retries 루프에서 이미
    # 소진된 뒤에야 error가 올라오고 런너는 아무 재시도도 스케줄하지 않으므로, "재시도 중"은
    # 거짓 라벨이었다(바로 위 주석의 "재시도를 암시하지 않는다"와도 모순). 폴백이 받는다.
    "parse": ("notice", "페이지 내용을 가져오지 못했어요"),
    "empty": ("notice", "페이지 내용을 가져오지 못했어요"),
    "not_configured": ("notice", "지금은 웹 검색을 사용할 수 없어요"),
    "invalid_section": ("notice", "요청한 코너를 찾지 못했어요"),
}
_ERROR_STATUS_FALLBACK: tuple[str, str] = ("notice", "정보를 가져오지 못했어요")


def _status_for_error(payload: dict) -> tuple[str, str]:
    """도구 error 응답의 error_type별 status(stage, detail)를 만든다."""
    return _ERROR_STATUS.get(payload.get("error_type"), _ERROR_STATUS_FALLBACK)


def _status_for_result(count: int) -> tuple[str, str] | None:
    """도구 결과 도착을 **건수만으로** 알린다. 0건이면 알릴 진행이 없다(None).

    인자를 int로 못박아 상품 사실(제목·가격·평점)이 이 경로로 새는 것을 시그니처로 봉인한다.
    payload를 통째로 받는 순간 4a 우회로가 생기므로 넓히지 말 것. 도구별 분기도 없다 —
    건수는 모든 검색·열람 도구가 같은 이름(result_count)으로 내는 구조 신호다.
    """
    if count <= 0:
        return None
    return "found", f"{count}건 찾았어요"


# 출처 카드(sse_source)와 인용 검증에 함께 쓰이는 출처 이벤트의 **단일 정의**. 예전엔 runner와
# orchestrator가 이 dict를 각자 손으로 조립해, 한쪽에만 필드를 더하면 그 경로의 카드에는 값이
# 끝까지 안 실렸다(실측 회귀). 조립을 한 곳에 두면 계약 드리프트가 구조적으로 불가능해진다.
# 상품 결과에만 있는 필드(author·price·rating·publisher·image_url)는 웹 출처에선 None이고,
# 프론트가 생략한다.
_PUBLIC_SOURCE_FIELDS = (
    *GROUNDING_FIELDS,
    "rank",
    "is_ebook",
    "snippet",
    "published_at",
    "last_updated",
    "checked_at",
)


def project_public_source(source: dict) -> dict:
    """내부 출처를 API의 단일 public source DTO로 투영한다."""
    meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    event = {
        "id": source.get("id", source.get("source_id")),
        "title": source.get("title", ""),
        "url": source.get("url", ""),
        "type": source.get("type", "search_result"),
    }
    for field in _PUBLIC_SOURCE_FIELDS:
        if field in source:
            event[field] = source[field]
        elif field in meta:
            event[field] = meta[field]
    return event


build_source_event = project_public_source


def project_source_ref(source_event: dict) -> dict:
    """출처 이벤트에서 **스트리밍 중 마커를 렌더할 최소 정보만** 투영한다(id·url).

    _status_for_result가 건수(int)만 받아 상품 사실 누출을 시그니처로 봉인한 것과 같은
    규율이다 — 제목·저자·가격·평점은 여기로 나가지 않는다(원칙 4a). url은 마커를
    하이퍼링크로 만들기 위한 것이고, 없으면 프론트가 링크 대신 칩으로 폴백한다.
    """
    return {"id": source_event.get("id"), "url": source_event.get("url") or ""}


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


def _reconcile_sources(observed_sources: list[dict]) -> list[dict]:
    """이번 턴의 done 조립·인용 검증에 쓸 출처 스냅샷을 만든다.

    ADK 2.3.0은 한 턴에 나온 병렬 function call을 asyncio.gather로 동시 실행하고,
    각 도구의 state_delta를 deep_merge_dicts가 **리스트 키에 대해 last-wins로 덮어쓴다**
    (flows/llm_flows/functions.py). 그래서 도구 완료 순서에 따라 세션 state["sources"]에서
    한 도구의 출처가 통째로 유실될 수 있고, postprocess가 유효한 [n] 인용을 잘라낸다.

    반면 병렬 function_response는 merge 시 parts가 모두 보존되므로, 런너가 스트림에서
    관찰해 누적한 출처(observed)는 유실되지 않는다. 따라서 근거 스냅샷은 observed만 id 기준으로
    합친다. 세션 레지스트리를 섞지 않아 과거 상세·가격이 새 검색 관측을 덮지 못하게 한다.
    """
    by_id: dict[int, dict] = {}
    for src in observed_sources:
        sid = src.get("id")
        if sid is not None:
            by_id[sid] = merge_turn_source_records(by_id.get(sid, {}), src)
    return [by_id[key] for key in sorted(by_id)]
