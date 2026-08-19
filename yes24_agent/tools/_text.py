"""도구 공용 텍스트 예산 유틸 — 절단·키워드 창.

도메인 무관한 순수 함수만 둔다(Yes24/웹 어느 도구든 같은 예산 규약을 쓴다). 원래
yes24_fetch에 살았고 웹 도구들이 크로스 import하는 계층 역전이 있어 중립 모듈로
분리했다(2026-08-19 구조 감사 L2, `_planning.py`와 같은 선례). 웹 도구 단독 import는
이제 Yes24 스택을 끌지 않는다 — 단 앱 조립(toolsets)이 전 도구 모듈을 import하는 것은
별건이라, 앱 레벨 지연 로딩은 이 분리의 범위 밖이다.
"""

# 절단 표시 접미사·중간 시작 표시 접두사.
TRUNCATION_SUFFIX = "…(이하 생략)"
OMITTED_PREFIX = "(앞부분 생략)… "


def truncate(text: str, max_chars: int) -> str:
    """text를 max_chars로 절단하고 절단 표시를 붙인다. 이미 짧으면 그대로 반환."""
    if len(text) <= max_chars:
        return text
    content_budget = max_chars - len(TRUNCATION_SUFFIX)
    if content_budget <= 0:
        return TRUNCATION_SUFFIX[:max_chars]
    return text[:content_budget].rstrip() + TRUNCATION_SUFFIX


def window_around_find(
    text: str, max_chars: int, find: str | None, lead_chars: int
) -> tuple[str, bool]:
    """본문에서 반환할 창을 고른다. 반환: (창 텍스트, find 발견 여부).

    find가 없거나 본문이 상한 이내면 앞에서부터 자른다. find가 있고 상한 밖 위치에서
    발견되면 그 위치 lead_chars 앞에서 시작하는 창을 잘라 키워드 앞 맥락(소제목·조건
    문장)이 함께 담기게 한다. 못 찾으면 앞부분 창으로 폴백한다.
    """
    if not find or len(text) <= max_chars:
        return truncate(text, max_chars), bool(find) and find.lower() in text.lower()

    pos = text.lower().find(find.lower())
    if pos < 0:
        return truncate(text, max_chars), False
    if pos < max_chars:
        return truncate(text, max_chars), True

    start = max(0, pos - lead_chars)
    prefix = OMITTED_PREFIX if start > 0 else ""
    suffix = TRUNCATION_SUFFIX if start + max_chars < len(text) else ""
    content_budget = max_chars - len(prefix) - len(suffix)
    if content_budget <= 0:
        return (prefix + suffix)[:max_chars], True
    window = text[start : start + content_budget].strip()
    return f"{prefix}{window}{suffix}", True
