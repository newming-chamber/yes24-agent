"""Yes24 코너 둘러보기 도구 — ADK FunctionTool로 노출되는 async 함수.

베스트셀러·신간·크레마클럽 인기처럼 검색어가 아니라 "코너 자체"를 열람하는 도구.
자사 실시간 목록이라 랭킹·신간 질문에는 웹 검색보다 이 도구가 가장 정확하다.

yes24_search와 마찬가지로 결과를 세션 state의 출처 레지스트리에 등록해 source_id를
부여하고, 인용에 쓸 수 있도록 반환 dict에 담는다. 실패는 예외를 밖으로 던지지 않고
구조화된 error dict로 반환한다(fail-loud).
"""

import logging

from google.adk.tools import ToolContext

from yes24_agent.config import get_settings
from yes24_agent.sources import cite_marker, now_checked_at, register_source
from yes24_agent.tools.yes24_search import get_client
from yes24_agent.yes24.client import Yes24FetchError
from yes24_agent.yes24.parsers import (
    ParseError,
    parse_browse_list,
    parse_category_links,
    product_fields,
)
from yes24_agent.yes24.urls import BROWSE_SEED_URLS, browse_category_prefix, browse_url

logger = logging.getLogger(__name__)


def _squash(name: str) -> str:
    """분야명 대조용 정규화 — 공백을 전부 없앤다.

    Yes24 내비는 복합어 분야를 띄어 적지만("경제 경영"·"사회 정치"·"IT 모바일") 사용자와
    모델은 붙여 쓴다("경제경영"). 띄어쓰기는 같은 이름의 표기 변이일 뿐이므로 양쪽을 같은
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


async def yes24_browse(
    section: str,
    tool_context: ToolContext,
    category_number: str = "",
    category_name: str = "",
) -> dict:
    """Yes24의 특정 코너(목록)를 직접 열람한다. 분야별로 좁힐 수 있다.

    검색어가 아니라 코너 전체를 랭킹·목록으로 보고 싶을 때 쓴다. 베스트셀러 순위,
    새로 나온 책, 크레마클럽 구독 인기처럼 "요즘 잘 나가는 책"류 질문에 적합하다.

    "요즘 소설"·"경제 신간"처럼 **특정 분야가 목적이면 category_name으로 한 번에**
    좁힌다(예: category_name="소설") — 도구가 코너의 분야 내비에서 이름을 해석해 그
    분야 목록까지 바로 가져온다. 이름이 정확히 하나로 해석되지 않으면 실제 분야
    목록(categories/candidates)을 돌려주니, 그중 번호를 골라 category_number로 다시
    호출한다. 분야 번호를 추측으로 만들지 말고 결과에서 본 번호만 사용한다.

    Args:
        section: 열람할 코너 코드. 허용값과 설명:
            __BROWSE_SECTIONS__.
        category_number: 분야 번호(선택, 숫자 문자열). 이전 결과의 categories에서 얻은
            번호로 코너를 그 분야로 좁힌다. category_name보다 우선한다.
        category_name: 분야 이름(선택). "소설"·"에세이"처럼 원하는 분야명을 주면 코너
            내비에서 해석해 한 호출로 그 분야 목록을 받는다. 빈 문자열이면 코너
            전체(국내도서). cremaclub은 분야 좁히기를 지원하지 않는다.

    Returns:
        성공 시 status="ok"와 section·section_label·적용된 category_number·해석된
        분야명 category_label, results 목록(각 항목에 인용용 source_id와 순위 rank
        포함), 이 페이지가 노출한 분야 목록 categories([{name, number}]), 검색 시각
        checked_at, result_count를 담은 dict. 잘못된 section은 status="error"·
        error_type="invalid_section", 잘못된 분야 번호·미지원 섹션 좁히기는
        "invalid_category", 이름 미매칭은 "category_not_found"(categories 동봉),
        다중 매칭은 "category_ambiguous"(candidates 동봉). 그 외 실패는
        error_type("fetch"|"parse"). 모든 실패 응답은 result_count=0을 함께 담는다.
    """
    settings = get_settings()

    seed = BROWSE_SEED_URLS.get(section)
    if seed is None:
        valid = ", ".join(BROWSE_SEED_URLS)
        logger.info(f"yes24_browse section={section!r} status=error error_type=invalid_section")
        return {
            "status": "error",
            "error_type": "invalid_section",
            "message": f"유효한 섹션: {valid}",
            "result_count": 0,
        }

    # 분야 번호 검증·적용. 번호는 숫자만(입구에서 fail-loud — URL 주입·추측 번호 차단),
    # 시드에 카테고리 슬롯이 없는 섹션은 browse_url이 ValueError로 알린다(조용한 무시 금지).
    if category_number and not category_number.isdigit():
        logger.info(
            f"yes24_browse section={section!r} category={category_number!r} "
            "status=error error_type=invalid_category"
        )
        return {
            "status": "error",
            "error_type": "invalid_category",
            "message": "category_number는 categories에서 본 숫자 번호여야 합니다.",
            "result_count": 0,
        }
    client = get_client(settings)

    # 분야 이름 → 번호 동적 해석(번호 미지정 시). 시드 페이지 내비가 단일 소스이며,
    # 유일 해석이 안 되면 임의로 고르지 않고 목록/후보와 함께 fail-loud한다 — 이 한
    # 도구 호출이 기존 "발견 → 재호출" LLM 2라운드를 병합한다(추천 지연의 최대 덩어리).
    category_label = ""
    category_name = category_name.strip()  # 공백뿐인 이름은 해석 대상이 아니다
    if category_name and not category_number:
        # 좁히기 미지원 섹션(cremaclub)은 fetch 전에 조기 거절 — 번호 경로와 같은
        # error_type으로 계약을 일치시킨다(이름 경로만 다른 오류를 내면 비일관).
        if not browse_category_prefix(section):
            logger.info(
                f"yes24_browse section={section!r} name={category_name!r} "
                "status=error error_type=invalid_category"
            )
            return {
                "status": "error",
                "error_type": "invalid_category",
                "message": f"'{section}' 섹션은 카테고리 좁히기를 지원하지 않습니다",
                "result_count": 0,
            }
        try:
            seed_html = await client.get_text(browse_url(section))
        except Yes24FetchError as exc:
            logger.info(f"yes24_browse section={section!r} status=error error_type=fetch")
            return {
                "status": "error",
                "error_type": "fetch",
                "message": f"Yes24 코너 조회에 실패했습니다: {exc}",
                "result_count": 0,
            }
        categories = parse_category_links(seed_html, limit=settings.browse_categories_limit)
        matches = _match_category(categories, category_name, browse_category_prefix(section))
        if not matches:
            logger.info(
                f"yes24_browse section={section!r} name={category_name!r} "
                "status=error error_type=category_not_found"
            )
            return {
                "status": "error",
                "error_type": "category_not_found",
                "message": f"'{category_name}' 분야를 찾지 못했습니다. categories에서 고르세요.",
                "categories": categories,
                "result_count": 0,
            }
        if len(matches) > 1:
            logger.info(
                f"yes24_browse section={section!r} name={category_name!r} "
                f"status=error error_type=category_ambiguous candidates={len(matches)}"
            )
            return {
                "status": "error",
                "error_type": "category_ambiguous",
                "message": f"'{category_name}'에 해당하는 분야가 여럿입니다. "
                "candidates에서 번호를 골라 category_number로 다시 호출하세요.",
                "candidates": matches,
                "result_count": 0,
            }
        category_number = matches[0]["number"]
        category_label = matches[0]["name"]

    try:
        url = browse_url(section, category_number)
    except ValueError as exc:
        logger.info(
            f"yes24_browse section={section!r} category={category_number!r} "
            "status=error error_type=invalid_category"
        )
        return {
            "status": "error",
            "error_type": "invalid_category",
            "message": str(exc),
            "result_count": 0,
        }

    try:
        html = await client.get_text(url)
    except Yes24FetchError as exc:
        logger.info(f"yes24_browse section={section!r} status=error error_type=fetch")
        return {
            "status": "error",
            "error_type": "fetch",
            "message": f"Yes24 코너 조회에 실패했습니다: {exc}",
            "result_count": 0,
        }

    try:
        parsed = parse_browse_list(
            html,
            base_url=settings.yes24_base_url,
            section=section,
            limit=settings.browse_result_limit,
        )
    except ParseError as exc:
        logger.info(f"yes24_browse section={section!r} status=error error_type=parse")
        return {
            "status": "error",
            "error_type": "parse",
            "message": f"코너 목록을 해석하지 못했습니다: {exc}",
            "result_count": 0,
        }

    checked_at = now_checked_at()

    results: list[dict] = []
    for item in parsed:
        # 검색·상세와 같은 필드 집합(product_fields) + 이 도구 고유의 rank.
        fields = product_fields(item)
        source_id = register_source(
            tool_context.state,
            title=item["title"],
            url=item["url"],
            source_type="browse",
            snippet=item.get("author"),
            checked_at=checked_at,
            meta={**fields, "rank": item.get("rank")},
            invocation_id=getattr(tool_context, "invocation_id", None),
        )
        results.append(
            {
                "source_id": source_id,
                "cite_as": cite_marker(source_id),
                "type": "browse",
                "rank": item.get("rank"),
                "title": item["title"],
                "url": item["url"],
                "checked_at": checked_at,
                **fields,
            }
        )

    # 이 페이지가 노출한 분야 내비 — 모델이 분야 번호를 발견하는 유일한 표면(추측 금지).
    categories = parse_category_links(html, limit=settings.browse_categories_limit)

    logger.info(
        f"yes24_browse section={section!r} category={category_number!r} "
        f"label={category_label!r} status=ok results={len(results)} categories={len(categories)}"
    )
    return {
        "status": "ok",
        "section": section,
        "section_label": seed["label"],
        "category_number": category_number,
        "category_label": category_label,
        "categories": categories,
        "results": results,
        "checked_at": checked_at,
        "result_count": len(results),
    }


# 도구 docstring은 모델이 보는 계약이다 — 섹션 코드·라벨을 손으로 열거하면 시드를 추가할 때
# 계약만 조용히 썩으므로(2026-08-19 감사), 정본 표(BROWSE_SEED_URLS)에서 조립해 치환한다.
# 모듈 임포트 시점에 실행되므로 toolsets가 FunctionTool을 만들기 전에 반영된다.
yes24_browse.__doc__ = yes24_browse.__doc__.replace(
    "__BROWSE_SECTIONS__",
    ", ".join(f'"{key}"({seed["label"]})' for key, seed in BROWSE_SEED_URLS.items()),
)
