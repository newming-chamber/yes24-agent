"""결정론 충분성 힌트(needs_followup) 계산 공용 헬퍼.

검색류 도구(yes24_search·web_search)가 "이 결과만으로 부족한가"를 LLM 없이 코드로
판정해 반환에 싣는다. runner의 충분성 게이트가 이 힌트를 읽어 재검색을 트리거한다.

판정은 순수·저비용(추가 LLM·네트워크 호출 없이 문자열 매칭만): query의 핵심 토큰이
결과 텍스트(제목·스니펫)에 하나도 등장하지 않으면 관련성이 낮다고 보고 True.
하나라도 커버되면 False. 결과가 0건이면 무조건 True.
"""

import re

# 토큰화: 한글·영숫자 연속열만 토큰으로 뽑는다(조사·기호는 경계로 무시).
_TOKEN_RE = re.compile(r"[0-9a-zA-Z가-힣]+")

# 토큰 최소 길이. 1글자 토큰(조사·관형사 "그"·"책" 등)은 관련성 신호로 약해 제외한다.
_MIN_TOKEN_LEN = 2

# 불용어/수식어: 주제가 아니라 "의도·시의성"만 나타내는 말. 결과 제목에 이 말이
# 그대로 안 실려도 관련성 판정과 무관하므로 핵심 토큰에서 뺀다(예: "요즘 인기 소설"의
# 핵심은 "소설"이지 "요즘·인기"가 아니다). 하드코딩된 매직값이 아니라 여기 한 곳에서만
# 관리한다. 실측으로 조정.
_FOLLOWUP_STOPWORDS = frozenset(
    {
        "추천",
        "최신",
        "인기",
        "요즘",
        "요새",
        "베스트",
        "베스트셀러",
    }
)


def _core_tokens(query: str) -> list[str]:
    """query에서 핵심 토큰(소문자화·불용어/1글자 제외)을 추출한다."""
    tokens = []
    for raw in _TOKEN_RE.findall(query.lower()):
        if len(raw) < _MIN_TOKEN_LEN or raw in _FOLLOWUP_STOPWORDS:
            continue
        tokens.append(raw)
    return tokens


def needs_search_followup(query: str, texts: list[str], result_count: int) -> bool:
    """검색류 결과의 재검색 필요 여부를 결정론으로 판정한다.

    Args:
        query: 사용자·에이전트가 넣은 검색어.
        texts: 결과의 관련성 근거 텍스트 목록(제목, web은 스니펫 포함).
        result_count: 반환 결과 개수.

    Returns:
        result_count == 0 이면 True. query 핵심 토큰이 하나도 texts에 안 걸리면
        True(엉뚱한 결과). 하나라도 커버되면 False. 핵심 토큰이 없으면(전부 불용어·
        1글자) 판정 근거가 없으므로 결과가 있는 한 False로 둔다.
    """
    if result_count == 0:
        return True
    tokens = _core_tokens(query)
    if not tokens:
        return False
    haystack = " ".join(texts).lower()
    return not any(token in haystack for token in tokens)
