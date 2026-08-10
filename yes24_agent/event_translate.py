"""도구 이벤트 → SSE 상태 라벨·출처 dict 번역, 출처 정합.

`runner.py`에서 ADK 도구 이벤트(function_call/response)를 프론트 계약으로 옮기는
순수 번역 함수들만 추출한 모듈이다(동작 불변). 도구 호출은 진행 status 라벨로,
도구 응답은 출처 dict로 번역하고, 병렬 도구 실행 시 세션 state가 잃을 수 있는 출처를
스트림 관찰본으로 보정한다(_reconcile_sources).
"""

from yes24_agent.sources import merge_source_records
from yes24_agent.toolsets import TOOLSET_SOURCE_TYPES
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


def _status_for_call(call) -> tuple[str, str] | None:
    """도구 호출을 **모델이 만든 인자 그대로** 진행 칩으로 번역한다. 없으면 None.

    표시 문장은 짓지 않는다("Yes24에서 X 검색 중…" 류 템플릿 폐기, 2026-07-23 사용자
    방향) — 화면 텍스트는 모델 산출물(검색 각도·상세 제목·코너명)만 싣고, "무엇을 하는
    중인지"의 동사 의미는 stage 아이콘(🔎·📖·📚·🌐)이 담당한다. ChatGPT·퍼플렉시티의
    검색 칩과 같은 구조다. 사용자 노출 문구에 url 원문은 넣지 않는다.

    **None을 돌려주는 것이 폴백이다.** 진행 표시는 실제 런타임 전이를 설명할 때만
    가치가 있고, 실을 모델 인자가 없으면 조용히 지나간다(거짓 라벨 금지). 도구가 늘어도
    runner는 그대로다 — 칩이 필요하면 여기에 분기를 더한다.
    """
    name = getattr(call, "name", "") or ""
    args = call.args or {}
    if name in ("yes24_search", "web_search"):
        angles = _angles(args.get("queries"))
        if angles:
            stage = "searching_web" if name == "web_search" else "searching"
            return stage, " · ".join(angles)
        return None
    if name == "yes24_fetch":
        title = args.get("title")
        return ("reading", str(title)) if title else None
    if name == "fetch_many":
        items = args.get("items")
        titles = [
            i.get("title") for i in items if isinstance(i, dict) and i.get("title")
        ] if isinstance(items, list) else []
        return ("reading", " · ".join(titles)) if titles else None
    if name == "yes24_browse":
        label = _browse_label(args.get("section", ""))
        return ("browsing", label) if label else None
    # web_fetch는 실을 모델 인자가 url뿐이라(원문 노출 금지) 칩을 내지 않고, 모르는 도구도
    # 아무 상태도 내지 않는다(None) — "설명할 진행이 없으면 말하지 않는다"가 기본값이다.
    return None


# 도구 error_type → status(stage, detail) 매핑. **어떤 항목도 재시도를 암시하지 않는다** —
# 런너는 도구 에러에 재시도를 스케줄하지 않으므로(HTTP 재시도는 client의 max_retries에서
# 이미 소진된 뒤 에러가 올라온다) "재시도 중"류 문구는 사용자에게 헛된 기대를 준다.
# 미지 error_type은 "페이지 fetch"로 단정하지 않는 범용 문구로 폴백한다.
_ERROR_STATUS: dict[str, tuple[str, str] | None] = {
    # "fetch"는 별도 항목을 두지 않는다 — HTTP 재시도는 client의 max_retries 루프에서 이미
    # 소진된 뒤에야 error가 올라오고 런너는 아무 재시도도 스케줄하지 않으므로, "재시도 중"은
    # 거짓 라벨이었다(바로 위 주석의 "재시도를 암시하지 않는다"와도 모순). 폴백이 받는다.
    "parse": ("notice", "페이지 내용을 가져오지 못했어요"),
    "empty": ("notice", "페이지 내용을 가져오지 못했어요"),
    "not_configured": ("notice", "지금은 웹 검색을 사용할 수 없어요"),
    "invalid_section": ("notice", "요청한 코너를 찾지 못했어요"),
    # 분야명 미해석은 실패가 아니라 정상 반려다(도구가 실제 분야 목록을 동봉해 돌려주고,
    # 모델이 그중 번호로 재호출한다 — 실측상 상시 회복). 그래서 **아무 상태도 내지 않는다**:
    # 어떤 문구를 써도 사용자에겐 무엇이 없다는 건지 알 수 없는 경고로만 읽히고(⚠️ "그 이름의
    # 분야가 따로 없어요" — 2026-08-03 UX 평가), 곧바로 회복되는 내부 재호출을 실패로 오해하게
    # 만든다. 설명할 진행이 없으면 말하지 않는다 — _status_for_call과 같은 규율이다.
    "category_not_found": None,
    "category_ambiguous": None,
}
_ERROR_STATUS_FALLBACK: tuple[str, str] = ("notice", "정보를 가져오지 못했어요")


def _status_for_error(payload: dict) -> tuple[str, str] | None:
    """도구 error 응답의 error_type별 status(stage, detail)를 만든다(알릴 게 없으면 None)."""
    return _ERROR_STATUS.get(payload.get("error_type"), _ERROR_STATUS_FALLBACK)


def _status_for_result(count: int) -> tuple[str, str] | None:
    """도구 결과 도착을 **건수만으로** 알린다. 0건이면 알릴 진행이 없다(None).

    건수 표기는 상용 표준이다(2026-07-23 4사 실측: 퍼플렉시티는 소스 카운트를 라이브로
    올리고, ChatGPT "Searched 12", Claude "N results") — 대기 체감을 상쇄하는 구조 메타라
    "가짜 활동 서술" 금지 클래스가 아니다. 인자를 int로 못박아 상품 사실(제목·가격·평점)이
    이 경로로 새는 것을 시그니처로 봉인한다. payload를 통째로 받는 순간 4a 우회로가
    생기므로 넓히지 말 것. 도구별 분기도 없다 — 건수는 모든 검색·열람 도구가 같은
    이름(result_count)으로 내는 구조 신호다.
    """
    if count <= 0:
        return None
    return "found", f"{count}건 찾았어요"


# 출처 카드(sse_source)와 인용 검증에 함께 쓰이는 출처 이벤트의 **단일 정의**. 예전엔 runner와
# orchestrator가 이 dict를 각자 손으로 조립해, 한쪽에만 필드를 더하면 그 경로의 카드에는 값이
# 끝까지 안 실렸다(실측 회귀). 조립을 한 곳에 두면 계약 드리프트가 구조적으로 불가능해진다.
# 상품 결과에만 있는 필드(author·sale_price·rating·publisher·image_url)는 웹 출처에선 None이고,
# 프론트가 생략한다.
# toolset이 선언한 출처 타입별 공개 필드를 **레지스트리에서 합집합으로** 끌어온다 — 도구
# 모듈을 직수입하면 새 toolset마다 이 파일을 고쳐야 하고, 같은 목록이 두 벌이 되어 한쪽만
# 고치는 드리프트가 난다. 스칼라 형태 필터는 아래에서 동일 적용된다.
_REGISTERED_SOURCE_FIELDS = tuple(
    dict.fromkeys(
        field
        for types in TOOLSET_SOURCE_TYPES.values()
        for fields in types.values()
        for field in fields
    )
)
_PUBLIC_SOURCE_FIELDS = (
    *_REGISTERED_SOURCE_FIELDS,
    "rank",
    "is_ebook",
    "snippet",
    "published_at",
    "last_updated",
    "checked_at",
)


def project_public_source(source: dict) -> dict:
    """내부 출처를 API의 단일 public source DTO로 투영한다.

    공개 DTO 계약은 기본 필드 + **선택적 스칼라**다(qa/README 판정 절). 그래서 필드 이름이
    목록에 있어도 값이 스칼라가 아니면 싣지 않는다 — `_PUBLIC_SOURCE_FIELDS`는
    `GROUNDING_FIELDS`에서 파생되는데, 그 상류(`_ITEM_FIELDS`)는 도구 결과·접지용이라
    구조 값이 들어올 수 있다(2026-08-04 실사고: `other_formats` 리스트가 여기로 새어
    다른 상품의 가격·URL이 공개 페이로드에 실렸다). 이름 열거로 막으면 다음 구조 필드에서
    재발하므로 형태로 거른다.
    """
    meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    event = {
        "id": source.get("id", source.get("source_id")),
        "title": source.get("title", ""),
        "url": source.get("url", ""),
        "type": source.get("type", "search_result"),
    }
    for field in _PUBLIC_SOURCE_FIELDS:
        if field in source:
            value = source[field]
        elif field in meta:
            value = meta[field]
        else:
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            event[field] = value
    return event


def project_source_ref(source_event: dict) -> dict:
    """출처 이벤트에서 **스트리밍 중 마커를 렌더할 최소 정보만** 투영한다(id·url).

    제목·저자·가격·평점은 여기로 나가지 않는다(원칙 4a — 검증 전 상품 사실은 어떤 공개
    채널로도 새지 않는다). url은 마커를 하이퍼링크로 만들기 위한 것이고, 없으면 프론트가
    링크 대신 칩으로 폴백한다.
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
            by_id[sid] = merge_source_records(by_id.get(sid, {}), src)
    return [by_id[key] for key in sorted(by_id)]
