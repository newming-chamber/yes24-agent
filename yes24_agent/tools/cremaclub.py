"""크레마클럽 등록 관측 — Yes24 결과 행에 클럽 등록 여부를 제자리로 싣는다.

판정은 행의 최상위 `cremaclub`(True 등록·False 미등록·키 없음 미확인)이다. 등록이면 other_formats
에도 클럽 항목(parsers.cremaclub_format — format "크레마클럽", url=클럽 상세)이 붙는다(외부
프론트가 판형 이름으로 뱃지·링크를 그린다). 작품 단위 의미다("이 책을 크레마클럽에서 볼 수
있다") — 종이책 행은 그 eBook 판이, 전자책 행은 자기 자신이 등록이면 같은 모양이다.

판정은 클럽 상세 경로(`settings.cremaclub_detail_url_template`)를 eBook goods_no로 GET한 문서다
(2026-09-28 실측: eBook 상세 배지와 123/123 일치). 등록 eBook이면 상품 상세 문서가
(parsers.is_product_detail — 클럽 문서엔 가격 전역변수가 없어 parse_product로 판정하지 않는다),
미등록이면 200 + `alert(...); history.back()` 스크립트가 온다. 그 alert는 **공통 실패 응답**이라
없는 id·종이책 id·성인인증 상품도 같은 바이트를 받는다 — 그래서 판정은 세 상태다:

- 200 + 상세 문서 → True
- 200 + **문서 요소 없이 script만 있는 응답** → False. 부정은 이 구조("클럽이 상품을 거절하고
  되돌려 보내는 스크립트")일 때만 내린다 — alert 문구는 보지 않는다. 조회 대상은 우리가 Yes24
  문서에서 **eBook으로 관측한** goods_no뿐이다(전자책 행 자신, 종이책 행의 판형 위젯이 eBook이라
  부른 링크). 없는 id·종이책 id가 섞여 거절 응답을 "클럽 아님"으로 오독하는 길을 입구에서 막는다.
  성인인증 eBook은 이 판정이 False로 떨어지는 알려진 사각이다(인증 없이는 거절 응답만 준다).
  판형 위젯·관련상품 줄은 텍스트 eBook이 없는 작품의 **오디오북**도 "eBook"이라 부르고 링크
  마크업에 둘을 가르는 구조가 없다(2026-10-01 실측 goods 8759796 → 110677754). 그 조회는
  클럽 거절(오디오북 11/11)로 False가 되는데, "이 작품은 클럽에 없다"로 사실과 같아 막지 않는다.
- 그 밖 전부(비200·204·3xx·빈 본문·점검 안내 같은 낯선 문서·예외·예산 초과) → `cremaclub` 키
  생략(미확인).
  "확인 못 함"을 "클럽 아님"으로 접지 않는다.

클럽 코너 목록의 행은 조회하지 않는다 — 목록 마크업("북클럽에 담기" 버튼)이 이미 등록을 관측해
`cremaclub: True`와 클럽 항목이 실려 온다(parsers._cremaclub_converter). `cremaclub` 키가 이미
있는 행은 다시 판정하지 않는다.

판정 단위는 goods_no다 — 오리지널 클럽 전용판처럼 goods_no가 다른 같은 작품은 이 신호로
알 수 없다. 쓰지 않는 신호: braze `cremaclubGoodsYn`(클럽 28/143이 'N'), 해시태그
"#크레마클럽에있어요"(편집 태그 — 누락·종이책 부착), eBook 상세의 클럽 배지(판정은 같지만
종이책 행마다 eBook 상세 전문을 세션 쿠키 클라이언트로 열어야 해 느리다 — 이 모듈이 대체했다).
"""

import asyncio
import logging

from bs4 import BeautifulSoup, Tag

from yes24_agent.config import Settings
from yes24_agent.yes24.client import Yes24Client, Yes24FetchError, Yes24TextCache
from yes24_agent.yes24.parsers import cremaclub_format, is_product_detail
from yes24_agent.yes24.selectors import (
    ITEM_EBOOK_LABEL,
    ITEM_FORMAT_LABEL_DECORATION,
)
from yes24_agent.yes24.urls import goods_no_from_url

logger = logging.getLogger(__name__)

# 판형 레코드(other_formats[].format)가 eBook 판을 부르는 이름 — 목록 라벨과 같은 사이트 어휘다.
_EBOOK_FORMAT = ITEM_EBOOK_LABEL.strip(ITEM_FORMAT_LABEL_DECORATION)

_club_client: Yes24Client | None = None
# 판정 값 캐시(공유 텍스트 캐시와 분리 — 용량 경쟁·키 공간이 다르다). 키는 클럽 상세 URL.
_club_cache: Yes24TextCache | None = None

# 캐시에 담는 판정 값의 표기. Yes24TextCache는 문자열을 담으므로 불리언을 한 글자로 싣는다.
_VERDICT_TEXT = {True: "1", False: "0"}


class _Unobserved(Exception):
    """판정 불가(미확인) — 캐시가 저장하지 않도록 값 대신 예외로 돌려보낸다."""


def build_club_client(settings: Settings, **overrides) -> Yes24Client:
    """클럽 조회 전용 클라이언트 — 쿠키 없음·자기 동시성·재시도 없음·예산 길이의 타임아웃.

    재시도하지 않는 이유: 곁가지 관측의 계약은 "1회 시도, 실패=미확인"이다. 재시도·백오프를
    상속하면 장애 시 행마다 수 초씩 세마포어를 붙잡고 429·503에 재요청을 퍼붓는다. 리다이렉트는
    조회 경로(get_status_text)가 따라가지 않는다 — 판정 입력이 "첫 응답"이다.
    """
    return Yes24Client.from_settings(
        settings,
        concurrency=settings.cremaclub_concurrency,
        rps=0,
        timeout_s=settings.cremaclub_budget_s,
        max_retries=0,
        persist_cookies=False,
        **overrides,
    )


def get_club_client(settings: Settings) -> Yes24Client:
    """프로세스 공유 클럽 클라이언트(최초 호출 시 생성)."""
    global _club_client
    if _club_client is None:
        _club_client = build_club_client(settings)
    return _club_client


def get_club_cache(settings: Settings) -> Yes24TextCache:
    """프로세스 공유 판정 캐시(최초 호출 시 생성). ttl 0이면 비활성(enabled=False)."""
    global _club_cache
    if _club_cache is None:
        _club_cache = Yes24TextCache(
            ttl_s=settings.cremaclub_cache_ttl_s,
            max_entries=settings.cremaclub_cache_max_entries,
        )
    return _club_cache


async def aclose_club_client() -> None:
    """클럽 클라이언트를 정리한다(서버 shutdown 훅 — yes24_search.aclose_shared_client 경유)."""
    global _club_client
    if _club_client is not None:
        await _club_client.aclose()
        _club_client = None


def _targets(records: list[dict]) -> list[tuple[dict, str]]:
    """관측 대상 (클럽 항목을 덧붙일 행, 조회할 eBook goods_no) 목록.

    전자책 행은 자기 goods_no, 종이책 행은 판형 위젯의 eBook 항목 url의 goods_no로 조회한다.
    종이책 행의 other_formats가 미관측이거나 eBook 항목이 없으면 대상이 없다. is_ebook이
    미관측인 행은 무엇의 판인지 모르므로 건드리지 않는다.
    """
    targets = []
    for record in records:
        if "cremaclub" in record:
            continue  # 목록에서 이미 관측됨(클럽 코너 행)
        formats = record.get("other_formats") or ()
        is_ebook = record.get("is_ebook")
        if is_ebook is True:
            goods_no = record.get("goods_no")
        elif is_ebook is False:
            ebook_url = next(
                (
                    fmt["url"]
                    for fmt in formats
                    if fmt.get("format") == _EBOOK_FORMAT and fmt.get("url")
                ),
                None,
            )
            goods_no = goods_no_from_url(ebook_url) if ebook_url else None
        else:
            goods_no = None
        if goods_no:
            targets.append((record, goods_no))
    return targets


def _is_rejection(html: str) -> bool:
    """문서 요소(html/head/body/title) 없이 script만 있는 응답인가 — 클럽의 거절 응답의 꼴.

    lxml은 조각을 html/body로 감싸므로 감싸지 않는 html.parser로 최상위 노드를 본다. 최상위에
    script 아닌 요소나 공백 아닌 텍스트가 하나라도 있으면 거절 응답이 아니다(점검 안내 등).
    """
    soup = BeautifulSoup(html, "html.parser")
    nodes = [node for node in soup.contents if isinstance(node, Tag) or str(node).strip()]
    return bool(nodes) and all(isinstance(n, Tag) and n.name == "script" for n in nodes)


def _verdict(status: int, html: str) -> bool | None:
    """클럽 상세 응답 → True(상세 문서)·False(거절 응답)·None(그 밖 — 미확인)."""
    if status != 200 or not html.strip():
        return None
    if is_product_detail(html):
        return True
    return False if _is_rejection(html) else None


async def _judge(client: Yes24Client, url: str) -> str:
    """클럽 상세 한 건을 조회·판정해 캐시 표기("1"/"0")로 돌려준다. 미확인은 _Unobserved."""
    try:
        status, html = await client.get_status_text(url)
    except Yes24FetchError as exc:
        logger.info(f"cremaclub url={url!r} status=unobserved reason={exc}")
        raise _Unobserved(url) from exc
    # 상세 판별(문서당 ~7ms)은 순수 계산이라 스레드로 내린다 — 한 턴에 수십 건이 몰린다.
    verdict = await asyncio.to_thread(_verdict, status, html)
    if verdict is None:
        logger.info(f"cremaclub url={url!r} status=unobserved http={status} chars={len(html)}")
        raise _Unobserved(url)
    return _VERDICT_TEXT[verdict]


async def _club_status(client: Yes24Client, cache: Yes24TextCache, url: str) -> bool | None:
    """클럽 상세 한 건의 3상태 판정 — 캐시 적중은 요청·세마포어 없이 즉시 돌아온다."""
    try:
        if cache.enabled:
            text = await cache.get_or_fetch(url, lambda: _judge(client, url))
        else:
            text = await _judge(client, url)
    except _Unobserved:
        return None
    return text == _VERDICT_TEXT[True]


async def observe_cremaclub(records: list[dict], settings: Settings) -> None:
    """결과 행(필드 dict)들에 크레마클럽 등록을 **제자리로** 싣는다(등록 전에 호출한다).

    판정된 행엔 `cremaclub`(True/False)이 실리고, 등록(True)이면 other_formats 끝에 클럽 항목도
    붙는다. 미확인은 행을 건드리지 않는다(키 없음). 같은 eBook은 한 번만 조회한다.

    배치 전체에 벽시계 예산(settings.cremaclub_budget_s, 세마포어 대기 포함)을 건다. 예산 안에
    끝난 행만 싣고 남은 조회는 취소해 미확인으로 둔다 — wait_for로 통째로 끊으면 이미 받은 판정까지
    버리게 되어 부분 결과를 살리는 asyncio.wait를 쓴다. 한 건의 예상 밖 예외도 그 행의
    "미확인"으로 접는다 — 곁가지 관측 하나가 본 결과(검색·코너·상세) 전체를 죽일 이유가 없다.
    판정은 값 캐시(get_club_cache)를 거친다 — 적중은 즉시 돌아와 예산을 쓰지 않는다. 캐시를
    거친 조회는 single-flight 공유 태스크라(shield) 이 배치가 예산·턴 중지로 취소해도 그 요청
    한 건은 끝까지 돌아 판정을 남긴다 — 요청 타임아웃이 예산과 같아 그 꼬리는 예산 길이로
    유계다. 캐시가 꺼져 있으면(ttl 0) 취소가 요청까지 바로 전파된다.
    """
    targets = _targets(records)
    if not targets:
        return
    template = settings.cremaclub_detail_url_template
    urls = {goods_no: template.format(goods_no=goods_no) for _, goods_no in targets}
    client = get_club_client(settings)
    cache = get_club_cache(settings)
    tasks = {
        goods_no: asyncio.create_task(_club_status(client, cache, url))
        for goods_no, url in urls.items()
    }
    try:
        await asyncio.wait(tasks.values(), timeout=settings.cremaclub_budget_s)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    status: dict[str, bool] = {}
    for goods_no, task in tasks.items():
        if task.cancelled():
            logger.info(f"cremaclub url={urls[goods_no]!r} status=unobserved reason=budget")
        elif task.exception() is not None:
            logger.warning(
                f"cremaclub url={urls[goods_no]!r} status=unobserved "
                f"reason={type(task.exception()).__name__}: {task.exception()}"
            )
        elif task.result() is not None:
            status[goods_no] = task.result()
    for record, goods_no in targets:
        if goods_no not in status:
            continue
        record["cremaclub"] = status[goods_no]
        if status[goods_no]:
            record["other_formats"] = [
                *(record.get("other_formats") or ()),
                cremaclub_format(urls[goods_no]),
            ]
    logger.info(f"cremaclub observed={len(status)}/{len(urls)} in_club={sum(status.values())}")
