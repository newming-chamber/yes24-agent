"""도구 이벤트 → SSE 상태 라벨·출처 dict 번역, 출처 정합.

`runner.py`에서 ADK 도구 이벤트(function_call/response)를 프론트 계약으로 옮기는
순수 번역 함수들만 추출한 모듈이다(동작 불변). 도구 호출은 진행 status 라벨로,
도구 응답은 출처 dict로 번역하고, 병렬 도구 실행 시 세션 state가 잃을 수 있는 출처를
스트림 관찰본으로 보정한다(settle_sources).
"""

import logging
from collections.abc import Callable

from yes24_agent.config import get_settings
from yes24_agent.sources import REGISTRY_RECORD_FIELDS, SUMMARY_FIDELITY, merge_source_records
from yes24_agent.toolsets import TOOLSET_PUBLIC_SOURCE_TYPE, TOOLSET_SOURCE_TYPES
from yes24_agent.yes24.urls import BROWSE_SEED_URLS

logger = logging.getLogger(__name__)


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
    """도구 동작과 관측 인자를 진행 칩으로 옮긴다. 인자가 없으면 detail만 비운다."""
    name = getattr(call, "name", "") or ""
    args = call.args or {}
    if name in ("yes24_search", "web_search"):
        angles = _angles(args.get("queries"))
        stage = "searching_web" if name == "web_search" else "searching"
        return stage, " · ".join(angles)
    if name == "yes24_fetch":
        title = args.get("title")
        return "reading", str(title) if title else ""
    if name == "fetch_many":
        items = args.get("items")
        titles = [
            i.get("title") for i in items if isinstance(i, dict) and i.get("title")
        ] if isinstance(items, list) else []
        return "reading", " · ".join(titles)
    if name == "yes24_browse":
        label = _browse_label(args.get("section", ""))
        return "browsing", label or ""
    # web_fetch는 제목을 받지 않으므로 동작만 표시한다.
    return ("reading", "") if name == "web_fetch" else None


# 도구 error_type → status(stage, detail) 매핑. **어떤 항목도 재시도를 암시하지 않는다** —
# 런너는 도구 에러에 재시도를 스케줄하지 않으므로(HTTP 재시도는 client의 max_retries에서
# 이미 소진된 뒤 에러가 올라온다) "재시도 중"류 문구는 사용자에게 헛된 기대를 준다.
# 미지 error_type은 "페이지 fetch"로 단정하지 않는 범용 문구로 폴백한다. stage 이름 `notice`는
# 출처 타입 `notice`와 단어가 같지만 외부 프론트가 이미 라벨 맵에 쓰고 있어 유지한다(2026-09-08).
_ERROR_STATUS: dict[str, tuple[str, str] | None] = {
    # "fetch"는 별도 항목을 두지 않는다 — HTTP 재시도는 client의 max_retries 루프에서 이미
    # 소진된 뒤에야 error가 올라오고 런너는 아무 재시도도 스케줄하지 않으므로, "재시도 중"은
    # 거짓 라벨이었다(바로 위 주석의 "재시도를 암시하지 않는다"와도 모순). 폴백이 받는다.
    "parse": ("notice", "페이지 내용을 가져오지 못했어요"),
    "empty": ("notice", "페이지 내용을 가져오지 못했어요"),
    "not_configured": ("notice", "지금은 웹 검색을 사용할 수 없어요"),
    "invalid_section": ("notice", "요청한 코너를 찾지 못했어요"),
    # 분야명 반려는 안내 문구 없이 구조적 실패 완료만 알린다.
    "category_not_found": None,
    "category_ambiguous": None,
}
_ERROR_STATUS_FALLBACK: tuple[str, str] = ("notice", "정보를 가져오지 못했어요")


def _status_for_error(payload: dict) -> tuple[str, str] | None:
    """도구 error 응답의 error_type별 status(stage, detail)를 만든다(알릴 게 없으면 None)."""
    return _ERROR_STATUS.get(payload.get("error_type"), _ERROR_STATUS_FALLBACK)


def _status_for_response(payload: dict) -> tuple[str, str] | None:
    """도구 응답 하나의 status — 실패면 error_type별 안내, 아니면 건수(둘 다 None일 수 있다).

    라이브(runner)와 복원(history)이 같은 판정을 쓴다 — 두 분기를 각자 들면 한쪽만 고쳐진다.
    """
    if payload.get("status") == "error":
        return _status_for_error(payload)
    count = payload.get("result_count")
    return _status_for_result(count) if isinstance(count, int) else None


def _status_for_result(count: int) -> tuple[str, str] | None:
    """도구 결과 도착을 **건수만으로** 알린다. 0건도 완료다.

    건수 표기는 상용 표준이다(2026-07-23 4사 실측: 퍼플렉시티는 소스 카운트를 라이브로
    올리고, ChatGPT "Searched 12", Claude "N results") — 대기 체감을 상쇄하는 구조 메타라
    "가짜 활동 서술" 금지 클래스가 아니다. 인자를 int로 못박아 상품 사실(제목·가격·평점)이
    이 경로로 새는 것을 시그니처로 봉인한다. payload를 통째로 받는 순간 4a 우회로가
    생기므로 넓히지 말 것. 도구별 분기도 없다 — 건수는 모든 검색·열람 도구가 같은
    이름(result_count)으로 내는 구조 신호다.
    """
    return "found", f"{count}건 찾았어요"


# 출처 카드와 인용 검증에 함께 쓰이는 출처 이벤트의 **단일 정의**. 예전엔 runner와
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
    "snippet",
    "published_at",
    "last_updated",
    "checked_at",
    "preview",
)
# 내부 출처 타입 → 공개 type. toolset이 선언한 관측 깊이는 그 toolset의 공개 이름 하나로 접힌다
# (`TOOLSET_PUBLIC_SOURCE_TYPE`, 공개 계약 v2). 여기 없는 타입(web·notice·미지)은 그대로 —
# 열거 화이트리스트가 아니라 "선언된 것만 접는" 규칙이라 새 내부 타입은 선언과 함께 따라온다.
_PUBLIC_SOURCE_TYPE: dict[str, str] = {
    internal: public
    for toolset, public in TOOLSET_PUBLIC_SOURCE_TYPE.items()
    for internal in TOOLSET_SOURCE_TYPES.get(toolset, ())
}
# 공개 DTO에 **목록**으로 실리는 유일한 예외와 그 항목 스키마 — 필드명 → 항목의 공개 스칼라 키.
# 프론트가 책 카드에 "eBook 13,600원" 칩을 그리는 값이다(2026-09-08). 판형은 그 값을 관측한
# 페이지(출처)의 필드이지 별도 출처가 아니다. QA 하네스(qa/live_capture)도 이 선언을 읽는다.
PUBLIC_RECORD_LIST_FIELDS: dict[str, tuple[str, ...]] = {
    "other_formats": ("format", "url", "sale_price"),
}


def _is_public_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def project_public_source(source: dict) -> dict:
    """내부 출처를 API의 단일 public source DTO로 투영한다.

    `type`은 공개 어휘(product | notice | web)로 접는다 — 내부 관측 깊이는 `_project_observation`
    까지만 산다(레지스트리 레코드는 그쪽을 쓴다). 나머지 필드 규칙은 `_project_observation` 참조.
    """
    event = _project_observation(source)
    event["type"] = _PUBLIC_SOURCE_TYPE.get(event["type"], event["type"])
    if event["type"] == "notice":
        event["card_type"] = "document"
    elif event.get("is_book") is True or (
        "is_book" not in event
        and (event.get("kind") in ("도서", "eBook") or event.get("is_ebook") is True)
    ):
        event["card_type"] = "book"
    else:
        event["card_type"] = "link"
    snippet = event.pop("snippet", None)
    if isinstance(snippet, str):
        preview = next((part.strip() for part in snippet.split("\n\n") if part.strip()), "")
        limit = get_settings().source_preview_max_chars
        # 빈 줄 없는 원문(FAQ 등)은 "첫 문단"이 곧 전체라 snippet 상한(6000자)까지 그대로 실릴
        # 수 있다 — 화면 표시는 프론트 몫이어도 페이로드 안전 상한은 서버가 진다.
        if len(preview) > limit:
            preview = preview[:limit].rstrip() + "…"
        event["preview"] = preview
    return event


def _project_observation(source: dict) -> dict:
    """내부 출처를 공개 DTO **형태**로 투영하되 `type`은 내부 어휘 그대로 둔다.

    공개 DTO 계약은 기본 필드 + **선택적 스칼라 또는 선언된 레코드 목록**이다(qa/README
    판정 절). 필드 이름이 목록에 있어도 값이 그 형태가 아니면 싣지 않는다 —
    `_PUBLIC_SOURCE_FIELDS`는 `GROUNDING_FIELDS`에서 파생되는데, 그 상류(`_ITEM_FIELDS`)는
    도구 결과·접지용이라 구조 값이 들어올 수 있다(2026-08-04 실사고: `other_formats` 리스트가
    여기로 **의도치 않게** 새어 다른 상품의 가격·URL이 공개 페이로드에 실렸다). 사고의 본질은
    임의 구조 유출이라, 목록은 `PUBLIC_RECORD_LIST_FIELDS`가 이름과 항목 스키마를 못 박은
    것만 항목 키를 추려 통과시키고 나머지 구조 값은 형태로 거른다. 빈 목록은 그대로 싣는다 —
    상세를 열어 "판형 없음"을 관측한 것이고, 키-생략(목록 관측)과는 다른 사실이다.
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
        record_keys = PUBLIC_RECORD_LIST_FIELDS.get(field)
        if record_keys is None:
            if _is_public_scalar(value):
                event[field] = value
        elif isinstance(value, list):
            event[field] = [
                {
                    key: item[key]
                    for key in record_keys
                    if key in item and _is_public_scalar(item[key])
                }
                for item in value
                if isinstance(item, dict)
            ]
    return event


def project_registry_record(source: dict) -> dict:
    """관측을 **세션 레지스트리 레코드 스키마**로 투영한다(`REGISTRY_RECORD_FIELDS`).

    도구 응답은 관측을 평면으로 싣고(가격·쪽수가 최상위) 본문 스캐폴딩(intro·toc·links·
    weekly_reviews·status)까지 동봉하지만, state에 남을 형태는 레지스트리 레코드 한 벌뿐이다.
    원문을 그대로 영속시키면 상세 하나가 수 KB로 불어 매 턴 재로드되고, 무엇보다 형태가
    달라 다음 턴 병합이 두 축(최상위=정체성 / meta=관측값)을 구분하지 못한다.

    공개 스칼라 추림은 `_project_observation`이 이미 소유하므로 그대로 재사용한다 —
    필드 열거를 두 벌 두지 않는다(공개 `type` 접기만 빠진 투영이다 — 레지스트리엔 내부
    타입이 남아야 병합이 상세/목록을 구분한다). 그 결과 최상위에 남는 것은 정체성·본문뿐이고,
    나머지 관측값은 전부 meta로 모인다. 레지스트리 레코드를 다시 넣어도 같은 값이 나오며(멱등),
    예전 평면화가 최상위에 흘려 둔 관측값 잔재는 이 투영에서 meta로 흡수된다.

    **관측하지 않은 필드는 키 자체를 넣지 않는다** — 키-생략은 "관측 불가"의 신호이고
    (parsers.product_fields와 같은 규약), 없던 키를 None으로 만들어 내면 그대로 공개 DTO에
    실려 소비자가 "본문이 빈 출처"로 읽는다. 병합은 `.get`으로 읽어 없음과 None을 같게 본다.
    """
    public = _project_observation(source)
    meta = {key: value for key, value in public.items() if key not in REGISTRY_RECORD_FIELDS}
    record = {
        "id": public["id"],
        "title": public["title"],
        "url": public["url"],
        "type": public["type"],
        "fidelity": source.get("fidelity") or SUMMARY_FIDELITY,
    }
    observed = {
        "snippet": source.get("snippet"),
        "checked_at": public.get("checked_at"),
        "meta": meta or None,
        # 근거 구간은 공개 필드가 아니지만 병합이 명시적으로 다루는 관측 데이터라 함께 옮긴다.
        "_evidence_segments": source.get("_evidence_segments"),
    }
    record.update({key: value for key, value in observed.items() if value is not None})
    return record


def project_source_ref(source_event: dict) -> dict:
    """출처 이벤트에서 **스트리밍 중 마커를 렌더할 최소 정보만** 투영한다(id·url).

    제목·저자·가격·평점은 여기로 나가지 않는다. 원칙 4/4a가 막는 것은 **증거 표면**의 표시
    번호와 가격·평점이다 — `[n]`은 인용된 출처만 받고, 상품 사실은 검증을 거친 카드로만 나간다.
    도구가 돌려준 제목·url 자체는 과정 표면(스텝, `project_step_source`)으로 즉시 나간다.
    url은 마커를 하이퍼링크로 만들기 위한 것이고, 없으면 프론트가 링크 대신 칩으로 폴백한다.
    """
    return {"id": source_event.get("id"), "url": source_event.get("url") or ""}


def project_step_source(source: dict) -> dict | None:
    """관측 출처를 **과정 스텝에 실을 최소 정보**로 투영한다(url·title) — 없으면 None(항목 생략).

    스텝 출처의 키는 url이다: 표시 번호(id)는 인용된 출처만 받으므로 싣지 않고(인용되면
    `refs{id,url}`가 url로 잇는다), 가격·평점·저자 같은 상품 사실은 증거 표면(카드) 몫이라
    싣지 않는다. 필드는 둘뿐이고, 둘 중 하나라도 비면 항목이 아니다.
    """
    url, title = source.get("url"), source.get("title")
    return {"url": url, "title": title} if url and title else None


def _iter_source_dicts(value: object):
    """`source_id`를 가진 dict를 payload 어디에 있든 등장 순서대로 훑는다.

    판정 기준은 위치가 아니라 **번호를 갖고 있는가** 하나다. 도구마다 실리는 자리가 다르고
    (search는 results, fetch는 payload 자체, fetch_many는 다시 그 위에
    results) 자리를 열거하면 새 자리가 생길 때마다 조용히 하나씩 빠진다 — 실제로 그 형태로
    새면 그 출처를 인용한 본문이 스트리밍 내내 원시 세션 id로 흐르다 마감에서 다시 그려진다
    (표시 번호는 관측본에서만 배정된다). 번호가 없는 dict(아직 열지 않은 links 후보)는
    출처가 아니다.
    """
    if isinstance(value, dict):
        if value.get("source_id") is not None:
            yield value
        for nested in value.values():
            yield from _iter_source_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_source_dicts(item)


def _sources_from_response(payload: dict) -> list[dict]:
    """도구 응답에서 노출할 출처 dict 목록을 **원시 그대로** 뽑아낸다(id 키만 정규화).

    여기서 투영하지 않는다 — 공개 DTO로의 투영은 출구(`build_done_payload`)가 이미
    하고, 관측 즉시 투영하면 병합 판정에 필요한 정보(fidelity·meta)가 그 자리에서
    사라져 상세 관측이 뒤따르는 목록 관측에 조용히 격하된다. 대신 도구의 `source_id`를
    레지스트리 어휘인 `id`로 정규화해, 관찰본과 세션 레지스트리가 **한 vocabulary**로
    만나 같은 병합 원시함수를 탈 수 있게 한다(추출 지점이 여기 하나뿐이라 드리프트 없음).
    """
    return [
        {**{key: value for key, value in source.items() if key != "source_id"},
         "id": source["source_id"]}
        for source in _iter_source_dicts(payload)
    ]


def settle_sources(registry: list[dict], observed_sources: list[dict]) -> list[dict]:
    """세션 레지스트리에 이번 턴 스트림 관찰본을 화해해 인용 가능 집합을 만든다.

    ADK 2.3.0은 한 턴에 나온 병렬 function call을 asyncio.gather로 동시 실행하고,
    각 도구의 state_delta를 deep_merge_dicts가 **리스트 키에 대해 last-wins로 덮어쓴다**
    (flows/llm_flows/functions.py). 그래서 도구 완료 순서에 따라 세션 state["sources"]에서
    한 도구의 출처가 통째로 유실될 수 있고, postprocess가 유효한 [n] 인용을 잘라낸다.

    반면 병렬 function_response는 merge 시 parts가 모두 보존되므로, 런너가 스트림에서
    관찰해 누적한 출처(observed)는 유실되지 않는다. 둘을 합치면 **레지스트리는 판정을,
    관찰본은 완전성을** 담당한다: 레지스트리 레코드가 base가 되어 `register_source`가
    선언한 fidelity로 상세 관측이 목록 관측에 격하되는 것을 막고, 레지스트리가 잃은
    출처는 관찰본이 되살린다. 병합 판정은 `merge_source_records` 한 곳뿐이라 레지스트리
    (`register_source`)와 이번 턴 스냅샷이 서로 다른 규칙을 쓰는 드리프트가 없다.

    **양쪽 모두 레지스트리 스키마로 맞춘 뒤 합친다.** 병합이 두 축(최상위=정체성,
    meta=관측값)을 구분하려면 관측값이 meta에 있어야 하는데 도구 응답은 그것을 평면으로
    싣는다. 화해본이 그대로 복구 write의 내용이 되므로, 여기서 맞춰 두면 state에 남는
    형태가 한 벌로 유지된다(러너가 되쓰기 직전에 또 투영할 필요가 없다).
    """
    settled: dict[int, dict] = {
        source["id"]: project_registry_record(source)
        for source in registry
        if source.get("id") is not None
    }
    for source in observed_sources:
        source_id = source.get("id")
        if source_id is not None:
            settled[source_id] = merge_source_records(
                settled.get(source_id, {}), project_registry_record(source)
            )
    return [settled[key] for key in sorted(settled)]


# ── 턴 과정 누적기(done.process · 히스토리 TurnView.process) ─────────────────


def _prefix_length(text: str, narration: str) -> int | None:
    """마감된 정본 `text` 안에서 마감된 접두 `narration`이 끝나는 오프셋(접두가 정본에 없으면 None).

    `narration`은 어떤 라운드 이전의 원시 본문을 **같은 조립기**(finalize_answer)로 마감한
    것이다. 조립기는 `[n]` 마커만 치환·삭제하고 재번호는 첫 등장 순서라, 접두를 따로 마감해도
    정본의 접두와 같다 — 그래서 라운드의 시작은 접두의 길이다(테스트가 등식으로 고정한다).

    등식이 깨지는 경로는 둘이다. ① 무효 마커 삭제의 seam: 다음 라운드가 **통째로 무효인
    마커**로 시작하면 `validate_citations._seam_parts`가 그 앞(내레이션 꼬리)의 가로 공백을
    흡수해 정본의 접두가 내레이션보다 짧아진다. 그때는 꼬리 공백을 뗀 접두로 다시 맞춘다 —
    뒤쪽은 정확히 다음 라운드이고 접두는 공백만 다르다(구분자 `\n\n`은 떼지 않으므로 뒤쪽에
    남는다). 내레이션이 "공백+구두점"으로 끝나면 seam이 그 안쪽 공백까지 지워 이 재시도로도 못
    닫는다. ② 코드 스팬 문맥: 내레이션이 인라인 백틱을 연 채 도구를 부르고 다음 라운드에서
    닫히면, 정본에서는 그 구간의 마커가 리터럴로 보존되지만 접두 단독 마감은 스팬이 성립하지
    않아 재번호된다(`code_span_ranges`가 뒤 문맥에 좌우된다).
    어느 쪽이든, 그리고 본문이 최후 방어 안내로 대체된 경우에도, 접두가 정본 안에 없으면 None —
    폴백은 `_round_starts`가 한 판정으로 정한다.
    """
    for prefix in (narration, narration.rstrip(" \t")):
        if text.startswith(prefix):
            return len(prefix)
    return None


def _round_starts(
    text: str, raw_body: str, raw_starts: list[int], finalize_text: Callable[[str], str]
) -> list[int] | None:
    """라운드별 원시 시작 오프셋을 정본 `text`의 오프셋으로 사상한다 — `process.round_starts`.

    계약: `[0] == 0`, 단조 비감소, 마지막 원소가 답의 시작(`answer_start`). 라운드 r 이전의 원시
    본문(`raw_body[:raw_starts[r]]`)을 정본과 같은 조립기로 마감해 접두 길이를 잰다(접두 0은
    마감 생략). 어떤 경계가 정본 안에서 못 맞거나 앞 경계보다 뒤로 물러나면(`_prefix_length`의
    두 사각) **직전 경계로 클램프**한다 — 그 라운드의 내레이션은 빈 문자열이 되고 답은 온전하다.
    마지막 경계(답의 시작)가 못 맞으면 None — 호출부가 전부 0으로 떨어뜨린다(전부 답). 과정만
    남고 답이 사라지는 분할은 어떤 경우에도 만들지 않는다(matrix의 process_chars와 같은 원칙).
    폴백은 경고로 남겨 발생 빈도를 잴 수 있게 한다(본문은 싣지 않는다).
    """
    starts = [0]
    for round_index, raw_start in enumerate(raw_starts[1:], start=1):
        narration = finalize_text(raw_body[:raw_start]) if raw_start else ""
        offset = _prefix_length(text, narration)
        if offset is None or offset < starts[-1]:
            last = round_index == len(raw_starts) - 1
            logger.warning(
                f"round_starts 접두 불일치(round={round_index}) → "
                f"{'전부 답(0) 폴백' if last else '직전 경계로 클램프'}"
                f"(narration_len={len(narration)} text_len={len(text)})"
            )
            if last:
                return None
            offset = starts[-1]
        starts.append(offset)
    return starts


# 구 턴의 타이밍 전용 이벤트 복원 키. 신규 턴은 turn_snapshot이 공개 완성본을 보존한다.
PROCESS_TIMING_KEY = "process_timing"
PROCESS_TIMING_FIELDS = ("elapsed_ms", "answer_at_ms")


class TurnProcess:
    """턴 과정 누적기 — 라이브 스트림(runner)과 히스토리 복원(history)이 **같은 인스턴스 규칙**으로
    `process`(elapsed_ms·answer_at_ms·sources_reviewed·answer_start·round_starts·steps)를
    만든다(같은 판정 두 곳 금지).

    라운드 = LLM 콜 인덱스(0부터). 도구 응답을 처리한 뒤 **처음 도착하는 모델 이벤트**(사고·본문
    partial·function_call·final)에서 +1이다. 호출부는 이벤트 종류를 그대로 알려 주기만 한다 —
    `model_event(raw_len)`은 그 시점의 원시 본문 누적 길이를 받는데, 새 라운드가 시작되면 그
    값이 곧 그 라운드의 원시 시작 오프셋이다(라운드 첫 청크에 붙는 문단 구분자 **이전**).
    라운드 수는 그 오프셋 목록의 길이다 — `round`는 목록에서 읽는다.
    `text_event(at_ms)`는 본문 청크가 도착한 경과 시각을 받고, 라운드의 **첫 청크만** 남긴다 —
    마지막 라운드의 그 값이 `answer_at_ms`(답이 시작되기까지의 조사 시간)다.
    스텝은 도구 status(호출·결과)만이다 — thinking·refs·persona는 과정의 재료가 아니다.
    스텝 dict는 `{round,stage,detail,step_id,state}`이고 결과는 result_count/sources도 갖는다.
    """

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.source_ids: set[int] = set()
        self._tool_pending = False  # 도구 응답을 처리했고 아직 다음 모델 이벤트가 안 왔다
        self._raw_starts = [0]  # 라운드별 원시 본문 시작 오프셋(인덱스 = 라운드)
        self._answer_at_ms: int | None = None  # 마지막 라운드의 첫 본문 청크 경과 시각
        self._calls: list[dict] = []
        self._content_phase: str | None = None

    @property
    def round(self) -> int:
        """현재 LLM 라운드 인덱스(0부터) — delta·status 프레임의 `round`."""
        return len(self._raw_starts) - 1

    def model_event(self, raw_len: int) -> None:
        """모델 이벤트 도착 — 도구 응답 뒤 첫 이벤트면 새 라운드(raw_len = 그 시점 원시 길이)."""
        if self._tool_pending:
            self._tool_pending = False
            self._raw_starts.append(raw_len)
            self._content_phase = None

    def content_event(self, phase: str) -> dict | None:
        """실제 본문이 있는 라운드의 역할 전환만 한 번씩 알린다."""
        if phase == "provisional" and self._content_phase is not None:
            return None
        if phase == "narration" and self._content_phase != "provisional":
            return None
        self._content_phase = phase
        return {"phase": phase, "round": self.round}

    def text_event(self, at_ms: int) -> None:
        """본문 청크 도착(경과 ms) — 라운드의 첫 청크만 남는다(뒤 청크는 시점을 바꾸지 않는다)."""
        if self._answer_at_ms is None:
            self._answer_at_ms = at_ms

    def step(self, status: tuple[str, str] | None) -> dict | None:
        """도구 status를 현재 라운드의 스텝으로 기록하고 **그 스텝 dict**를 돌려준다(None이면 없음).

        돌려주는 것이 기록본 그 자체라, 호출부(runner)는 프레임에 실을 것을 다시 계산하지 않고
        스텝을 그대로 status 프레임으로 낸다 — 라이브 프레임과 done.process.steps·히스토리가
        한 dict에서 나온다.
        """
        if status is None:
            return None
        step = {"round": self.round, "stage": status[0], "detail": status[1]}
        self.steps.append(step)
        return step

    def tool_call(self, call) -> dict:
        status = _status_for_call(call) or ("working", "")
        step = self.step(status)
        step["step_id"] = f"step-{len(self._calls) + 1}"
        step["state"] = "running"
        self._calls.append({
            "id": getattr(call, "id", None),
            "name": getattr(call, "name", None),
            "step": step,
            "resolved": False,
        })
        return step

    def tool_response(self, payload: dict, response=None) -> tuple[dict | None, list[dict]]:
        """도구 응답 하나를 소화한다 — 다음 모델 이벤트가 새 라운드가 되고, 출처를 관측한다.

        돌려주는 것은 `(기록한 스텝, 관측 출처)`다. 실패 응답의 출처는 관측하지 않는다(runner의
        종전 동작 그대로 — 실패 payload는 번호를 갖지 않는다). 결과 스텝(`found`)에는 관측
        출처의 url·title 목록을 `sources`로 **즉시** 붙인다 — 검색은 2초 만에 끝나는데 카드는
        마감 뒤에야 오는 체감 지연과 투명성 때문(2026-09-03). 실패 스텝과 0건 결과는
        빈 sources 배열로 완료를 알린다.
        """
        self._tool_pending = True
        # 도구 응답 뒤의 텍스트는 새 라운드의 것이다 — 여기서 지우면 "대기 중인 라운드(모델
        # 이벤트 없이 마감)"도 값이 없어, payload가 라운드 시작 여부를 따로 보지 않아도 된다.
        self._answer_at_ms = None
        sources = [] if payload.get("status") == "error" else _sources_from_response(payload)
        self.source_ids.update(source["id"] for source in sources)
        response_id = getattr(response, "id", None)
        response_name = getattr(response, "name", None)
        pending = [call for call in self._calls if not call["resolved"]]
        call = next((call for call in pending if response_id and call["id"] == response_id), None)
        if call is None and not response_id:
            candidates = [call for call in pending if call["name"] == response_name]
            if candidates:
                call = candidates[0]
            elif not response_name and len(pending) == 1:
                call = pending[0]
        failed = payload.get("status") == "error"
        status = _status_for_response(payload)
        if status is None:
            status = ("notice", "") if failed else ("found", "")
        step = self.step(status)
        step["state"] = "failed" if failed else "completed"
        count = payload.get("result_count")
        step["result_count"] = count if isinstance(count, int) else len(sources)
        if call is not None:
            call["resolved"] = True
            step["step_id"] = call["step"]["step_id"]
            step["round"] = call["step"]["round"]
        step_sources = [item for item in map(project_step_source, sources) if item is not None]
        step["sources"] = step_sources
        return step, sources

    def payload(
        self, *, raw_body: str, text: str, finalize_text: Callable[[str], str], elapsed_ms: int
    ) -> dict:
        """`process` dict를 만든다 — `text`는 마감된 정본, `finalize_text`는 그 정본을 만든 조립기.

        라운드별 원시 시작 오프셋을 정본 오프셋으로 사상한 것이 `round_starts`(`_round_starts`)이고
        `answer_start`는 그 마지막 원소다(클라이언트 편의 — 항상 같다). 단일 라운드면 `[0]`.
        본문을 narration으로 알렸거나 도구 응답 뒤 다음 모델 이벤트 없이 마감되면
        (타임아웃·예외·중단된 영속 턴) 대기 중인 답은 비어 마지막 원소가 len(text)가 된다.

        `answer_at_ms`는 마지막 라운드의 첫 본문 청크 시각(`text_event`)이다. 그 라운드에 텍스트가
        없거나(도구 응답 직후 마감·타임아웃) 답이 정본 안에 없으면(최후 방어 대체·스트림 시작 전
        실패) 답은 마감 시점에 생긴 것이라 `elapsed_ms`와 같다 — 두 필드의 폴백이 한 판정이다.
        """
        waiting_for_answer = self._tool_pending or self._content_phase == "narration"
        raw_starts = self._raw_starts + ([len(raw_body)] if waiting_for_answer else [])
        round_starts = _round_starts(text, raw_body, raw_starts, finalize_text)
        answer_at_ms = (
            self._answer_at_ms if round_starts is not None and not waiting_for_answer else None
        )
        round_starts = round_starts or [0] * len(raw_starts)
        return {
            "elapsed_ms": elapsed_ms,
            "answer_at_ms": elapsed_ms if answer_at_ms is None else answer_at_ms,
            "sources_reviewed": len(self.source_ids),
            "answer_start": round_starts[-1],
            "round_starts": round_starts,
            "offset_unit": "unicode_codepoint",
            "steps": list(self.steps),
        }
