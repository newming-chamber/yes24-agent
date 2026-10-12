"""Yes24 제공 지식 데이터 조회 도구(toolset "knowledge") — `yes24_notice`.

묶음·낱말로 활성 묶음의 메모를 돌려준다(어떤 묶음이 있는지는 지시문 목록이 알린다). 저장소 호출은
`memo_lookup_timeout_s`
에서 끊고 unavailable로 정직하게 실패한다. 등록은 조회가 끝난 뒤 await 없이 순서대로 한다(source_id
원자·단조 — fetch_many와 같은 이유).
"""

import asyncio
import contextvars
import logging
from collections.abc import Awaitable

from google.adk.tools import ToolContext

from yes24_agent.config import get_settings
from yes24_agent.memos import _norm, catalog_lines, get_memo_service, memo_view, search_view
from yes24_agent.sources import cite_marker, register_source

logger = logging.getLogger(__name__)


async def _bounded(call: Awaitable):
    """메모 저장소 호출 상한 — DB가 응답하지 않아도 도구가 멈추지 않는다(TimeoutError)."""
    return await asyncio.wait_for(call, timeout=get_settings().memo_lookup_timeout_s)


# 목록을 못 불러올 때(그리고 목록 없이 조립하는 프롬프트 골든)의 지시문 문구.
MEMO_CATALOG_FALLBACK = "collection 없이 query만으로도 찾습니다.\n"


async def catalog_prompt() -> str:
    """지시문에 싣는 활성 묶음 목록 + '항상 참고' 묶음의 항목 전문. 저장소 장애는 턴을 막지 않고
    query 안내로 물러선다.

    항상 참고: 2026-10-12 고유 사실 테스트에서 돌려 물은 정책 질문 3/3이 예외를 놓쳤다 — 모델은
    목록의 이름만 보고는 사이트 FAQ로 가고 조회하지 않았다. 짧고 놓치면 안 되는 묶음은 조회 판단을
    거치지 않게 내용을 싣는다.
    """
    service = get_memo_service()
    try:
        active = await _bounded(service.list_collections(active_only=True))
        always = [item for item in active if item["always_include"]]
        bodies = await _bounded(asyncio.gather(*(service.list_memos(item["id"]) for item in always)))
    except Exception as exc:  # noqa: BLE001 — 메모 장애가 대화 전체로 번지지 않게
        logger.warning(f"yes24_notice catalog_prompt unavailable: {exc!r}")
        return MEMO_CATALOG_FALLBACK
    if not active:
        return "지금 볼 수 있는 Yes24 안내는 없습니다.\n"
    text = "지금 볼 수 있는 Yes24 안내:\n" + "".join(f"  - {line}\n" for line in catalog_lines(active))
    if always:
        text += _always_block(always, [body["memos"] for body in bodies])
    return text


# ── Yes24 결과 행에 붙는 관련 항목(notes) ────────────────────────────────────────
# 러너가 턴 시작에 start_knowledge_turn으로 홀더를 set한다(web prefetch와 같은 contextvar 홀더
# 관례 — ADK가 도구를 create_task 사본으로 돌려도 같은 list를 공유한다). 홀더가 None이면 이 턴은
# knowledge 구성이 아니다 → yes24 도구 결과는 종전과 바이트 동일.
_turn_entries: contextvars.ContextVar[list[asyncio.Task] | None] = contextvars.ContextVar(
    "knowledge_entries", default=None
)


def start_knowledge_turn(message: str, active: frozenset[str]) -> None:
    """이번 턴의 활성 항목 적재를 모델 사고와 병렬로 시작한다(실패·미소비 무해)."""
    del message
    if "knowledge" not in active:
        return
    task = asyncio.create_task(get_memo_service().search("", ""))
    task.add_done_callback(lambda t: t.cancelled() or t.exception())  # 미소비 예외 회수
    _turn_entries.set([task])


async def turn_entries() -> list[dict]:
    """이번 턴의 활성 항목 전부. knowledge 구성이 아니거나 저장소 장애면 []."""
    holder = _turn_entries.get()
    if not holder:
        return []
    try:
        return (await _bounded(asyncio.shield(holder[0])))["memos"] or []
    except Exception as exc:  # noqa: BLE001 — 노트는 부가물, 검색 자체를 막지 않는다
        logger.warning(f"yes24_notice turn_entries unavailable: {exc!r}")
        return []


def _related(result: dict, entries: list[dict]) -> list[dict]:
    """결과 행과 항목의 데이터 일치 — 상품번호 > 저자('누가') > 제목(내용 안) 순, 상한까지.

    2026-10-12 A/B(간접 질문 6종×5런): 모델은 "김초엽 책 추천"을 책 검색으로만 처리하고 행사·뒷이야기
    묶음을 조회하지 않았다(기준 7/30). 검색·상세 결과 행에 그 책·저자의 항목을 붙이면 조회 판단
    없이 함께 보인다(16/30, 지식 인용 5→19, 무관 질문 끼어듦 0/5). 질문의 인물 이름을 지시문에
    얹는 안(E)은 결과 행 부착과 겹쳐 추가 효과가 없어 기각했다."""
    goods = str(result.get("goods_no") or "")
    author = _norm(result.get("author") or "")
    title = _norm(result.get("title") or "")
    ranked: list[tuple[int, dict]] = []
    for memo in entries:
        about = _norm(memo["about"] or "")
        if goods and f",{goods}," in memo["goods_nos"]:
            ranked.append((0, memo))
        elif about and len(about) >= 2 and about in author:
            ranked.append((1, memo))
        elif len(title) >= 2 and title in _norm(memo["content"]):
            ranked.append((2, memo))
    ranked.sort(key=lambda pair: pair[0])
    return [memo for _, memo in ranked[: get_settings().memo_notes_per_result]]


def attach_notes(results: list[dict], entries: list[dict], tool_context) -> None:
    """Yes24 결과 행(search_result·book_detail)마다 관련 항목을 notes로 붙이고 출처로 등록한다.

    await 없이 돈다(source_id 원자·단조). 결과가 Yes24 도구를 이미 거친 뒤라 상품 사실(4a)과는
    섞이지 않는다 — notes는 그 책에 대한 사람의 말·행사·안내다."""
    if not entries:
        return
    settings = get_settings()
    attached = 0
    for result in results:
        if result.get("status") == "error" or result.get("type") not in ("search_result", "book_detail"):
            continue
        notes = [_register(memo_view(memo, settings), tool_context) for memo in _related(result, entries)]
        if notes:
            attached += len(notes)
            result["notes"] = [{k: v for k, v in note.items() if k in
                                ("source_id", "cite_as", "title", "snippet", "about", "period")}
                               for note in notes]
    if attached:
        logger.info(f"yes24_notice notes attached={attached} rows={len(results)}")


def _always_block(always: list[dict], memos: list[list[dict]]) -> str:
    """항상 참고 묶음의 항목 — 글자 예산까지 싣고, 넘친 묶음은 도구로 찾게 이름만 남긴다."""
    budget = get_settings().memo_always_max_chars
    lines = ["항상 참고(질문이 직접 묻지 않아도 해당하면 답에 반영하고, 쓴 항목은 이 도구로 찾아 "
             "인용합니다):\n"]
    for collection, items in zip(always, memos):
        lines.append(f"  [{collection['title']}]\n")
        for memo in items:
            entry = f"    · {memo['content']}" + (f" ({memo['about']})" if memo["about"] else "")
            budget -= len(entry)
            if budget < 0:
                lines.append("    · …(나머지는 도구로 찾습니다)\n")
                return "".join(lines)
            lines.append(entry + "\n")
    return "".join(lines)


def _register(item: dict, tool_context) -> dict:
    """출처 항목(memo_view) 등록 → source_id·cite_as를 앞에 붙인 응답 항목. meta는 넘기지 않는다."""
    checked_at = item.pop("checked_at")
    source_id = register_source(
        tool_context.state,
        title=item["title"],
        url=item["url"],
        source_type=item["type"],
        snippet=item["snippet"],
        checked_at=checked_at,
        invocation_id=getattr(tool_context, "invocation_id", None),
        fidelity=item["fidelity"],
    )
    return {"source_id": source_id, "cite_as": cite_marker(source_id), **item,
            "checked_at": checked_at}


async def yes24_notice(tool_context: ToolContext, collection: str = "", query: str = "") -> dict:
    """사이트에서는 볼 수 없는 Yes24 안내(작가·선정위원의 추천, 행사, 정책 예외, 응대 방법 등)를
    찾는다.

    이 자료는 고객에게 'Yes24 안내'다 — 경과 서술과 답변에서 그렇게만 가리킨다. 누가 한 말이면
    그 사람을 밝혀 쓴다(about). 항목에 나온 책을 답에서 소개하면 Yes24 도구로 그 책을
    찾아 상품 링크(출처)까지 함께 단다 — goods_urls가 있으면 그 url을 yes24_fetch로 바로 연다.
    상품 사실(가격·재고·판매상태 등)은 라이브 Yes24 도구 결과로만 말한다. 사이트 안내와 기간 한정
    항목이 다르면 기간 안에서는 이 항목이 우선한다. 항목은 작성 시점의 자료다 — 행사가 지금 진행 중인지,
    참여할 수 있는지는 라이브 Yes24 페이지로 확인해 말한다.

    Args:
        collection: (선택) 찾을 안내의 이름. 지시문 목록의 이름을 그대로 쓰거나 일부만 적는다.
        query: (선택) 좁히는 낱말들. 공백으로 나눈 낱말이 **모두** 든 항목만 온다(띄어쓰기·
            대소문자 무시, 내용·누가에서 찾고 상품번호는 정확히 같아야 한다). collection 없이
            주면 모든 안내에서 찾는다.

    Returns:
        status="ok", items=[{source_id, cite_as, title, snippet, about?, period?, goods_urls?,
        ...}], total_matches, returned, truncated. 항목의 내용은 그 항목의 cite_as로 인용한다.
        결과가 길면 truncated=True — query로 좁혀 다시 부른다.
        실패: status="error", error_type("unknown_collection"|"unavailable"), message.
    """
    settings = get_settings()
    try:
        found = await _bounded(get_memo_service().search(collection, query))
    except Exception as exc:  # noqa: BLE001 — 테이블 없음·DB 장애를 빈 성공으로 위장하지 않는다
        logger.warning(
            f"yes24_notice collection={collection!r} query={query!r} status=unavailable: {exc!r}"
        )
        return {
            "status": "error",
            "error_type": "unavailable",
            "message": "이 자료를 지금 조회할 수 없습니다.",
        }
    if found["memos"] is None:
        # 꺼진 묶음의 존재는 드러내지 않는다 — 활성 이름만 알려 준다.
        return {
            "status": "error",
            "error_type": "unknown_collection",
            "message": f"'{collection}' 안내는 없습니다. collections 중에서 고르세요.",
            "collections": [item["title"] for item in found["collections"]],
        }
    result = search_view(found["memos"], settings)
    result["items"] = [_register(item, tool_context) for item in result["items"]]
    logger.info(
        f"yes24_notice collection={collection!r} query={query!r} "
        f"matches={result['total_matches']} returned={result['returned']}"
    )
    return result
