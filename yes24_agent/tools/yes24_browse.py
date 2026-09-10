"""Yes24 코너 둘러보기 도구 — ADK FunctionTool로 노출되는 async 함수.

베스트셀러·신간·크레마클럽 인기처럼 검색어가 아니라 "코너 자체"를 열람하는 도구.
자사 실시간 목록이라 랭킹·신간 질문에는 웹 검색보다 이 도구가 가장 정확하다.

**다중 코너 병렬 열람**: yes24_search의 queries와 같은 꼴로 코너 여러 개(sections)를 한 번에
받아 asyncio.gather로 동시에 열람하고 한 응답으로 합친다. 코너를 갈아탈 때마다 모델 왕복이
끼어 스텝 사이 2.3~3.9초씩 비었고("베스트셀러+신간+크레마클럽" 34.1초 중 9.0초가 스텝 간
공백, Yes24 호출은 4번 합쳐 1.7초), 모델은 병렬 호출을 쓰지 않았다(16턴 중 0턴, 2026-09-09
실측). 모델에 기대는 대신 도구 구조로 닫는다. 코너 계획(관용 변환·중복 제거·상한·dropped)은
검색 도구와 공용 헬퍼(_planning)를 그대로 쓴다.

정확성 설계(레이스 0): 네트워크·파싱만 동시 실행하고, 출처 등록(register_source·id 부여)은
순차 루프로 처리한다 — yes24_search·fetch_many와 동일 규약. 같은 상품이 여러 코너에
있으면 한 번만 등록하고 어느 코너들에서 나왔는지(sections)를 합친다.

yes24_search와 마찬가지로 결과를 세션 state의 출처 레지스트리에 등록해 source_id를
부여하고, 인용에 쓸 수 있도록 반환 dict에 담는다. 실패는 예외를 밖으로 던지지 않고
구조화된 error dict로 반환한다(fail-loud). 부분 실패는 성공 결과와 함께 코너별 상태를
browses로 노출한다 — 빈 성공으로 위장하지 않는다.
"""

import asyncio
import logging

from google.adk.tools import ToolContext

from yes24_agent.config import Settings, get_settings
from yes24_agent.sources import cite_marker, now_checked_at, register_source
from yes24_agent.tool_progress import ToolProgressGroup, parsed_progress
from yes24_agent.tools._planning import dropped_queries_message, plan_queries
from yes24_agent.tools.yes24_search import get_client
from yes24_agent.yes24.client import Yes24Client, Yes24FetchError
from yes24_agent.yes24.parsers import (
    ParseError,
    parse_browse_list,
    parse_category_links,
    product_fields,
)
from yes24_agent.yes24.urls import BROWSE_SEED_URLS, browse_category_prefix, browse_url

logger = logging.getLogger(__name__)


def _parse_browse_page(html: str, section: str, settings) -> tuple[list[dict], list[dict]]:
    """코너 페이지 HTML → (상품 목록, 분야 내비). 순수 계산이라 워커 스레드에서 돈다.

    분야 내비는 모델이 분야 번호를 발견하는 유일한 표면(추측 금지)이라 목록과 함께 관측한다.
    """
    parsed = parse_browse_list(
        html,
        base_url=settings.yes24_base_url,
        section=section,
        limit=settings.browse_result_limit,
    )
    categories = parse_category_links(html, limit=settings.browse_categories_limit)
    return parsed, categories


def _squash(name: str) -> str:
    """분야명 대조용 정규화 — 공백을 전부 없앤다.

    Yes24 내비는 복합어 분야를 띄어 적지만("경제 경영"·"사회 정치"·"IT 모바일") 사용자와
    모델은 붙여 쓴다. 띄어쓰기는 같은 이름의 표기 변이일 뿐이므로 양쪽을 같은
    형태로 눕혀 비교한다 — 별칭 사전이 아니라 표기 정규화라, 분야가 늘어도 갱신할 목록이
    없다(2026-08-03: "경제경영" 미해석으로 베스트셀러 폴백, 재현 2/2).
    """
    return "".join(name.split())


def _match_category(categories: list[dict], name: str, tree_prefix: str) -> list[dict]:
    """분야 이름을 페이지 내비 항목과 대조해 후보를 고른다(동적 해석 — 이름 목록 미보유).

    내비에는 국내도서(001)·eBook(017) 등 여러 트리의 동명 분야가 섞여 있으므로 시드
    트리(tree_prefix)로 먼저 한정하고, 정확 일치 → 접두 일치("소설"→"소설/시/희곡") →
    순방향 포함("과학"→"자연과학") 순으로 좁힌다. 대조는 공백을 눕힌 형태로 한다(_squash).
    역방향 포함(분야명⊂입력)은 두지 않는다 — "시사경제"를 "경제 경영"으로 넓혀 잇는 순간
    도구가 의미 선택을 하게 되어 하네스의 선을 넘는다(그런 입력은 미매칭으로 categories와
    함께 모델에 반납). 같은 단계에서 2개+면 그대로 돌려 호출자가 fail-loud하게 한다
    (임의 선택 금지).
    """
    wanted = _squash(name)
    pool = [
        c for c in categories if not tree_prefix or c["number"].startswith(tree_prefix)
    ]
    for tier in (
        [c for c in pool if _squash(c["name"]) == wanted],
        [c for c in pool if _squash(c["name"]).startswith(wanted)],
        [c for c in pool if wanted in _squash(c["name"])],
    ):
        if tier:
            return tier
    return []


def _error(section: str, error_type: str, message: str, **extra) -> dict:
    """코너 하나의 실패 결과. 모든 실패는 result_count=0을 함께 담는다(단일 코너 계약 유지)."""
    logger.info(f"yes24_browse section={section!r} status=error error_type={error_type}")
    return {
        "section": section,
        "status": "error",
        "error_type": error_type,
        "message": message,
        "result_count": 0,
        **extra,
    }


async def _browse_one(
    section: str,
    category_number: str,
    category_name: str,
    client: Yes24Client,
    settings: Settings,
) -> dict:
    """코너 하나를 열람해 **파싱 결과만** 돌려준다(등록 없음).

    출처 등록은 여기서 하지 않는다 — 여러 코너를 gather로 동시 실행할 때 등록을 병렬로
    돌리면 source_id 부여에 레이스가 생기므로, 네트워크·파싱(순수 계산)만 여기서 하고
    등록은 호출부의 순차 루프에서 처리한다(_search_one과 같은 분업). 예상된 오류(잘못된
    코너·분야, 조회·파싱)만 구조화된 error dict로 반환하고, 예상 밖 예외는 그대로 올려보낸다.

    반환: {"section", "status": "ok", "section_label", "category_number", "category_label",
           "parsed": [item...], "categories": [{name, number}...]}
        또는 {"section", "status": "error", "error_type", "message", "result_count": 0,
           (+ "categories" | "candidates")}.
    """
    seed = BROWSE_SEED_URLS.get(section)
    if seed is None:
        return _error(section, "invalid_section", f"유효한 섹션: {', '.join(BROWSE_SEED_URLS)}")

    # 분야 번호 검증·적용. 번호는 숫자만(입구에서 fail-loud — URL 주입·추측 번호 차단),
    # 시드에 카테고리 슬롯이 없는 섹션은 browse_url이 ValueError로 알린다(조용한 무시 금지).
    if category_number and not category_number.isdigit():
        return _error(
            section,
            "invalid_category",
            "category_number는 categories에서 본 숫자 번호여야 합니다.",
        )

    # 분야 이름 → 번호 동적 해석(번호 미지정 시). 이 코너의 시드 페이지 내비가 단일 소스이며,
    # 유일 해석이 안 되면 임의로 고르지 않고 목록/후보와 함께 fail-loud한다 — 이 한
    # 도구 호출이 기존 "발견 → 재호출" LLM 2라운드를 병합한다(추천 지연의 최대 덩어리).
    category_label = ""
    if category_name and not category_number:
        # 좁히기 미지원 섹션(cremaclub)은 fetch 전에 조기 거절 — 번호 경로와 같은
        # error_type으로 계약을 일치시킨다(이름 경로만 다른 오류를 내면 비일관).
        if not browse_category_prefix(section):
            return _error(
                section,
                "invalid_category",
                f"'{section}' 섹션은 카테고리 좁히기를 지원하지 않습니다",
            )
        try:
            seed_html = await client.get_text(browse_url(section))
        except Yes24FetchError as exc:
            return _error(section, "fetch", f"Yes24 코너 조회에 실패했습니다: {exc}")
        categories = parse_category_links(seed_html, limit=settings.browse_categories_limit)
        matches = _match_category(categories, category_name, browse_category_prefix(section))
        if not matches:
            return _error(
                section,
                "category_not_found",
                f"'{category_name}' 분야를 찾지 못했습니다. categories에서 고르세요.",
                categories=categories,
            )
        if len(matches) > 1:
            return _error(
                section,
                "category_ambiguous",
                f"'{category_name}'에 해당하는 분야가 여럿입니다. "
                "candidates에서 번호를 골라 category_number로 다시 호출하세요.",
                candidates=matches,
            )
        category_number = matches[0]["number"]
        category_label = matches[0]["name"]

    try:
        url = browse_url(section, category_number)
    except ValueError as exc:
        return _error(section, "invalid_category", str(exc))

    try:
        html = await client.get_text(url)
    except Yes24FetchError as exc:
        return _error(section, "fetch", f"Yes24 코너 조회에 실패했습니다: {exc}")

    try:
        # 목록·분야 내비 파싱은 순수 계산이라 한 워커 스레드에서 묶어 처리한다(H17 오프로드).
        parsed, categories = await asyncio.to_thread(_parse_browse_page, html, section, settings)
    except ParseError as exc:
        return _error(section, "parse", f"코너 목록을 해석하지 못했습니다: {exc}")

    logger.info(
        f"yes24_browse section={section!r} category={category_number!r} "
        f"label={category_label!r} status=ok results={len(parsed)} categories={len(categories)}"
    )
    return {
        "section": section,
        "status": "ok",
        "section_label": seed["label"],
        "category_number": category_number,
        "category_label": category_label,
        "parsed": parsed,
        "categories": categories,
    }


async def yes24_browse(
    sections: list[str],
    tool_context: ToolContext,
    category_number: str = "",
    category_name: str = "",
) -> dict:
    """Yes24의 코너(목록)들을 직접 열람한다. 분야별로 좁힐 수 있다.

    질문이 코너 여럿에 걸치면(예: 베스트셀러와 신간을 함께) **그 코너들을 한 번에 sections에
    함께 담는다** — 코너들은 동시에 열람되므로 나눠 호출할 때보다 훨씬 빠르다. 한 코너만
    필요하면 원소 하나만 전달한다.

    특정 분야가 목적이면 category_name으로 한 번에 좁힌다 — 도구가 코너의 분야 내비에서
    이름을 해석해 그 분야 목록까지 바로 가져온다. 분야 번호를 추측으로 만들지 말고
    결과에서 본 번호만 사용한다.

    Args:
        sections: 열람할 코너 코드 리스트. 허용값과 설명:
            __BROWSE_SECTIONS__.
        category_number: 분야 번호(선택, 숫자 문자열, 모든 코너에 공통 적용). 이전 결과의
            categories에서 얻은 번호로 코너를 그 분야로 좁힌다. category_name보다 우선한다.
        category_name: 분야 이름(선택, 모든 코너에 공통 적용). "소설"·"에세이"처럼 원하는
            분야명을 주면 코너 내비에서 해석해 한 호출로 그 분야 목록을 받는다. 빈 문자열이면
            코너 전체(국내도서). cremaclub은 분야 좁히기를 지원하지 않는다.

    Returns:
        코너 중 하나라도 열람에 성공하면 status="ok"와 results 목록(모든 코너의 결과를 상품
        기준으로 병합·중복제거, 각 항목에 인용용 source_id, 어느 코너들에서 나왔는지 sections,
        그 코너 안의 순위 rank 포함), 코너별 성공/실패/결과 수·적용된 category_number·해석된
        분야명 category_label을 담은 browses 요약, 페이지들이 노출한 분야 목록
        categories([{name, number}]), 검색 시각 checked_at, result_count를 담은 dict. 상한을
        넘겨 열람하지 않은 코너가 있으면 dropped_count·dropped_sections로 명시한다. 모든 코너가
        실패했을 때만 status="error"와 error_type, message, result_count=0을 담은 dict —
        잘못된 코너 코드는 error_type="invalid_section", 잘못된 분야 번호·미지원 섹션
        좁히기는 "invalid_category", 이름 미매칭은 "category_not_found"(categories 동봉),
        다중 매칭은 "category_ambiguous"(candidates 동봉), 그 외 실패는 "fetch"|"parse".
    """
    settings = get_settings()

    # 코너 계획(관용 변환·중복 제거·상한 cap)은 검색 도구와 공용 헬퍼를 쓴다.
    planned, dropped_sections = plan_queries(sections, settings.yes24_browse_max_sections)
    if not planned:
        # 유효한 코너가 하나도 없다 — 빈 성공으로 위장하지 않고 명시적 실패(invalid_section).
        logger.info("yes24_browse status=error error_type=invalid_section")
        return {
            "status": "error",
            "error_type": "invalid_section",
            "message": f"유효한 섹션: {', '.join(BROWSE_SEED_URLS)}",
            "result_count": 0,
        }

    client = get_client(settings)
    # category_number/category_name은 **모든 코너에 같은 분야**를 적용한다 — 실제 사용례가
    # "소설 베스트셀러랑 신간"이고, 코너마다 다른 분야를 요구하는 사용례는 관측된 적 없다.
    # 좁히기 미지원 코너(cremaclub)가 섞이면 그 코너만 invalid_category로 browses에 남고
    # 나머지는 정상 열람된다(조용히 코너 전체로 대체하지 않는다).
    category_name = category_name.strip()  # 공백뿐인 이름은 해석 대상이 아니다
    # 네트워크·파싱만 동시 실행한다(코너별 병렬). 등록은 아래 순차 루프에서 — 레이스 0.
    progress = ToolProgressGroup(
        tool_context,
        stage="browsing",
        details=[BROWSE_SEED_URLS.get(section, {}).get("label", section) for section in planned],
    )
    browsed = await progress.gather(
        [
            (index, _browse_one(s, category_number, category_name, client, settings))
            for index, s in enumerate(planned)
        ],
        summarize=parsed_progress,
    )

    checked_at = now_checked_at()

    # 1) 상품 병합(순수): 같은 상품이 여러 코너에 있으면 한 행으로 합친다. 동일성은 goods_no가
    #    정본(없으면 url). rank는 코너별 순위인데 순위가 있는 코너(bestseller=종이책·
    #    cremaclub=eBook)는 상품 집합이 겹치지 않으므로, 한 상품은 순위를 최대 하나만 갖는다 —
    #    먼저 관측된 non-None 순위를 행의 rank로 두면 손실 없이 합쳐지고, 어느 코너의 순위인지는
    #    sections와 browses의 has_rank 성격으로 읽힌다. 등록 전에 합치는 이유는 레지스트리
    #    meta의 rank와 행의 rank가 한 값이 되게 하기 위해서다(재등록으로 덮어쓰지 않는다).
    rows: dict[str, dict] = {}
    browses: list[dict] = []  # 코너별 성공/실패/결과 수 요약(부분 실패 fail-loud)
    categories: list[dict] = []  # 페이지들이 노출한 분야 내비의 합집합(번호 기준, 관측 순)
    seen_numbers: set[str] = set()
    for outcome in browsed:
        # 분야 내비는 코너별로 반복 싣지 않는다 — 좁히기 가능한 코너들은 같은 국내도서(001)
        # 트리를 렌더하므로 합집합이 곧 그 트리이고, category_not_found의 선택지도 여기로 모인다.
        for c in outcome.pop("categories", []):
            if c["number"] not in seen_numbers:
                seen_numbers.add(c["number"])
                categories.append(c)
        if outcome["status"] == "error":
            browses.append(outcome)
            continue
        parsed = outcome.pop("parsed")
        browses.append({**outcome, "result_count": len(parsed)})
        for item in parsed:
            fields = product_fields(item)
            key = fields.get("goods_no") or item["url"]
            row = rows.get(key)
            if row is None:
                rows[key] = {**item, "fields": fields, "sections": [outcome["section"]]}
                continue
            row["sections"].append(outcome["section"])
            if row.get("rank") is None:
                row["rank"] = item.get("rank")

    ok_count = sum(1 for b in browses if b["status"] == "ok")
    # 2) 등록(순차): 병합된 행마다 한 번만 등록한다 — source_id 유일·단조.
    results: list[dict] = []
    for row in rows.values():
        # 검색·상세와 같은 필드 집합(product_fields) + 이 도구 고유의 rank.
        fields = row["fields"]
        source_id = register_source(
            tool_context.state,
            title=row["title"],
            url=row["url"],
            source_type="browse",
            snippet=row.get("author"),
            checked_at=checked_at,
            meta={**fields, "rank": row.get("rank")},
            invocation_id=getattr(tool_context, "invocation_id", None),
        )
        results.append(
            {
                "source_id": source_id,
                "cite_as": cite_marker(source_id),
                "type": "browse",
                "rank": row.get("rank"),
                "sections": row["sections"],
                "title": row["title"],
                "url": row["url"],
                "checked_at": checked_at,
                **fields,
            }
        )

    logger.info(
        f"yes24_browse sections={len(planned)} sections_ok={ok_count} "
        f"results={len(results)} dropped={len(dropped_sections)}"
    )
    if ok_count == 0:
        response = {**browses[0], "browses": browses, "categories": categories}
    else:
        response = {
            "status": "ok",
            "sections": planned,
            "browses": browses,
            "categories": categories,
            "results": results,
            "checked_at": checked_at,
            "result_count": len(results),
        }
    if dropped_sections:
        # 가법 필드: 드롭이 없으면 반환 형태는 단일/다중 코너 모두 이 키가 없다.
        response["dropped_count"] = len(dropped_sections)
        response["dropped_sections"] = dropped_sections
        dropped_message = dropped_queries_message(
            settings.yes24_browse_max_sections, len(dropped_sections), unit="코너", action="열람"
        )
        response["message"] = " ".join(filter(None, (response.get("message"), dropped_message)))
    return response


# 도구 docstring은 모델이 보는 계약이다 — 섹션 코드·라벨을 손으로 열거하면 시드를 추가할 때
# 계약만 조용히 썩으므로(2026-08-19 감사), 정본 표(BROWSE_SEED_URLS)에서 조립해 치환한다.
# 모듈 임포트 시점에 실행되므로 toolsets가 FunctionTool을 만들기 전에 반영된다.
yes24_browse.__doc__ = yes24_browse.__doc__.replace(
    "__BROWSE_SECTIONS__",
    ", ".join(f'"{key}"({seed["label"]})' for key, seed in BROWSE_SEED_URLS.items()),
)
