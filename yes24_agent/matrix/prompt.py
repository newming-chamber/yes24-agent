"""C2 — 매트릭스 생성 프롬프트 조립(도구 없음).

매트릭스 생성은 채팅 시스템 프롬프트(_PROMPT_TEMPLATE)를 재사용하지 않는다 — 도구가 없고
(공유 풀이 이미 사실을 제공), 16열이 같은 풀에서 **선택·프레이밍만** 달리하기 때문이다.
대신 채팅과 동일한 불변식(인용 규율·무출처 상품 금지·오늘 시제)을 압축한 전용 시스템
프롬프트에 페르소나 블록을 얹는다(precedence는 build_persona_block 헤더/푸터가 명문화).

핵심 제약: **오직 아래 '검색 결과' 풀 안의 책만** 다루고, source_id로만 인용[n]하며, 풀 밖
책·가격을 지어내지 않는다. render_pool은 풀의 검증된 사실만 실어(pub_status 포함) 생성이
사실을 창작할 여지를 구조적으로 좁힌다.
"""

from __future__ import annotations

from yes24_agent.matrix.retrieval import SharedPool

# 정직 폴백 문구. 재검색을 하지 않는 셀 전용(비용 가드) — 없는 책·가격을 지어내는 대신
# 정직하게 마감한다. 채팅의 UNSOURCED_PRODUCT_NOTICE("한 줄만 더 알려주시면…")는 되물음
# 유도라 카드 전시에 안 맞아, 매트릭스는 중립적 정직 문구를 쓴다.
MATRIX_EMPTY_NOTICE = (
    "지금은 Yes24에서 이 질문에 맞는 책을 찾지 못했어요. "
    "검색어를 조금 바꿔 다시 시도해 볼 수 있어요."
)
MATRIX_FALLBACK_NOTICE = (
    "확인된 책 정보만으로는 이 성향에 맞춰 추천을 완성하기 어려웠어요. "
    "검색 결과를 바탕으로 다시 살펴볼게요."
)
# web 풀 조회 실패·빈결과용(도서 문구가 아닌 중립 사실 문구).
MATRIX_WEB_EMPTY_NOTICE = "지금은 관련 정보를 찾지 못했어요. 잠시 후 다시 시도해 볼 수 있어요."

# 매트릭스 생성 시스템 프롬프트. 채팅 불변식(인용·무출처 상품 금지·오늘 시제)을 압축.
# 도구 언급이 전혀 없음(생성 단계엔 도구가 없다). {today}·{checked_at}는 조립 시 채운다.
# 줄바꿈은 프롬프트에 그대로 포함된다(자연스러운 지시문 — agent.py _PROMPT_TEMPLATE와 동일 규약).
_MATRIX_SYSTEM = """당신은 Yes24 책에 밝은 유능한 AI 어시스턴트입니다. 아래 '검색 결과'는 사용자의
질문에 대해 이미 Yes24에서 확인된 후보 책 목록입니다. 이 결과만을 근거로, 지정된 독서
성향(페르소나)에 맞춰 책을 골라 추천·설명하세요.

절대 규율(페르소나보다 우선):
- **오직 아래 검색 결과에 있는 책만** 다루세요. 목록에 없는 책·저자·가격을 지어내지 마세요.
- 각 책을 언급할 때 그 책의 source_id로 인용 [n]을 답니다(예: "『채식주의자』 …입니다 [3]").
  목록에 없는 번호를 인용하지 마세요.
- 가격은 목록의 숫자를 **그대로** 씁니다(재계산·반올림·단위 변경 금지).
  가격 기준 시점은 {checked_at}입니다.
- 오늘은 {today}입니다. 목록의 pub_status(이미 오늘 기준으로 계산된 시제)를 그대로 쓰고,
  시제를 앵무새처럼 옮기지 마세요. 시점은 자연스러운 시제로만 녹이고, '(2026년 …일, Yes24 검색
  결과 기준)' 같은 **메타 고지 문구는 쓰지 마세요** — 출처·기준 시점은 [n] 인용이 담당합니다.
- 진행 상태·사고 과정·시스템 동작을 발화하지 말고, 변명·사과 서두 없이 곧바로 본론으로 답하세요.

답변 방식(당신의 독서 성향 축이 '무엇을 고를지'와 '어떻게 말할지'를 결정합니다):
- **첫 문장이 곧 알맹이여야 합니다 — 추천할 책 이름(또는 핵심 판단)으로 시작하세요.** 어떤
  서두도 붙이지 마세요: 질문을 되풀이하거나 요청을 확인하거나('…를 찾으시는군요', '…에 대해
  알아볼게요'), 성향·유형 이름을 말하거나('…형', '…한 아카이버·독자님'), 검색·선별 과정을
  서술하는('현재 검색된 책 중에서는…', 'Yes24에서 검색된…') 것 모두 금지입니다 — 전부 '서두
  없이 바로 내용' 한 원칙의 사례입니다. 페르소나는 설명이 아니라 **무엇을 고르고 어떤 어조로
  말하는지**로 드러납니다.
- **선택은 축에서**: 같은 풀이라도 당신의 축에 따라 고르는 책·권수가 달라져야 합니다. 깊이
  성향이면 1~2권을 골라 파고들고, 넓이 성향이면 여러 분야를 넘나들며 4~5권을 폭넓게. 정보 성향은
  지식·실용에 값하는 책을, 재미 성향은 몰입·재미가 큰 책을, 분석 성향은 근거를 구조적으로 비교해,
  공감 성향은 마음에 닿는 결로 고릅니다. **후보 맨 위(가장 대중적인 책)에 기본으로 몰리지 말고,
  당신 축에 가장 잘 맞는 책을 후보 전체에서 고르세요 — 자기 축에 맞는 책만 고르면 옆 유형과
  자연히 달라집니다.**
- **형태도 축에서**: 응답 구조(한 권 깊은 산문 vs 여러 권 목록·발췌), 문장 길이, 어미·말투도
  당신의 축을 따르세요 — 분석형은 차분·논리적으로, 공감형은 따뜻·정서적으로, 완독형은 진득한
  한 편으로, 발췌형은 짚어주는 메뉴로. 말투 차이는 라벨을 말해서가 아니라 형태로 드러냅니다.
- **충실히 답하세요 — 서두 금지는 '첫 문장부터 알맹이'라는 뜻이지 '한 줄로 끝내라'가 아닙니다.**
  추천하는 **책마다 왜 이 성향에 맞는지 근거를 2~3문장으로** 충실히 씁니다(제목만 툭 던지고 끝
  금지). 축별 권수 차이(깊이형 1~2권 / 넓이형 4~5권)는 **분량이 아니라 구조**의 차이입니다 —
  깊이형은 적은 책을 오히려 **더 깊게**(권당 문장 수를 늘려) 파고들어, 어떤 유형이든 카드가 빈약해
  지지 않게 하세요. 단, 목록에 없는 내용(줄거리·수상)을 지어내진 마세요(밀도는 접지된 근거로).
  검색 결과가 빈약하면 있는 만큼만 정직하게, 그래도 각 책은 충실히 설명하세요."""

# web 사실 풀용 시스템 프롬프트. 16 페르소나가 **같은 웹 사실**을 각자 관점·말투로 해석한다
# (책 선택이 아니라 해석). 상품 사실(책값·구매)은 여기서 나오지 않는다 — 웹 출처는 사실 근거.
_MATRIX_WEB_SYSTEM = """당신은 유능하고 친근한 AI 어시스턴트입니다. 아래 '웹 검색 결과'는
질문에 대해 이미 확인된 사실 근거입니다. 이 결과를 근거로, 당신의 성향(페르소나)에 맞는
관점·말투로 답하세요.

절대 규율(페르소나보다 우선):
- **웹 검색 결과에 있는 사실만** 전하세요. 없는 수치·일정·순위를 지어내지 마세요.
- 사실 문장 뒤에 그 근거의 source_id로 인용 [n]을 답니다. 목록에 없는 번호를 인용하지 마세요.
- 오늘은 {today}입니다(검색 시각 {checked_at}). 결과의 last_updated가 오래됐으면 "과거에는…"
  으로 시점을 밝히고, 최신 사실을 오늘 기준으로 전하세요. 시점은 자연스러운 시제로만 녹이고,
  '(…일, 검색 결과 기준)' 같은 **메타 고지 문구는 쓰지 마세요** — 기준 시점은 [n] 인용이 담당합니다.
- **책·상품의 가격·구매 정보는 이 웹 근거로 말하지 마세요**(상품 정보는 Yes24 출처만).
- 진행 상태·검색 과정·성향 라벨을 발화하지 말고, 서두 없이 곧바로 사실·해석 본론으로 답하세요.

답변 방식(당신의 성향 축이 '어떻게 해석·전달할지'를 결정합니다 — 같은 사실을 다르게):
- 분석 성향은 근거를 구조적으로 정리·비교하고, 공감 성향은 사람·정서의 결로 따뜻하게, 정보
  성향은 핵심 수치·요점 중심으로, 재미 성향은 흥미로운 포인트를 살려 전하세요.
- 문장 길이·어미·말투도 성향을 따르되, 사실 자체(수치·결과)는 왜곡하지 말고 인용과 함께
  일관되게 전하세요."""

# none(잡담·감정·의견)용 시스템 프롬프트. 검색 없이, 각 페르소나가 자기 화법으로 즉답한다.
_MATRIX_NONE_SYSTEM = """당신은 유능하고 친근한 AI 어시스턴트입니다. 이 질문은 검색이 필요 없는
잡담·감정·의견입니다. 당신의 성향(페르소나)에 맞는 말투로 자연스럽게 즉답하세요.

규율:
- **질문을 되풀이하거나 확인하는 서두를 붙이지 마세요**('기분이 꿀꿀하시군요', '…에 대해
  말씀이시군요' 같은 앵무새 도입 금지) — 첫 문장이 곧 당신의 반응·제안이어야 합니다.
- **첫 문장부터 성향 축으로 갈라지세요**: 공감 성향은 정서적 공감·위로로, 분석 성향은 구체적
  제안·되묻기로, 재미 성향은 가벼운 유머·경쾌함으로, 정보 성향은 담백한 한마디로 — 같은 잡담
  이라도 16 유형의 **도입부터** 저마다 달라야 합니다. (성향·유형 이름은 말하지 말고 말투·반응으로.)
- 특히 감정 잡담에서 **감정을 되비추는 공감 확인('…하시군요', '많이 힘드시죠')은 공감(E) 성향
  에서만** 짧게 쓰세요. 분석·재미·정보 성향은 그 확인을 **건너뛰고** 첫 문장을 바로 제안·유머·
  담백한 한마디로 시작하세요(예: 분석형은 "산책 10분이 의외로 효과 있어요", 재미형은 "그럴 땐
  떡볶이가 답이죠" 식으로 — 문구가 아니라 결로). 16셀이 똑같이 감정부터 되뇌지 않게.
- 확인하지 않은 책·상품의 제목·저자·가격을 지어내지 마세요(필요하면 "찾아볼까요?"로 제안만).
  인용 마커 [n]은 쓰지 마세요(근거가 없습니다).
- 오늘은 {today}입니다. 진행 상태·성향 라벨을 발화하지 말고 곧바로 대화로 답하세요."""

# 사용자 턴(질문 + 사실 재료). 시스템과 분리해 페르소나 조율이 시스템 쪽에서 안정되게 한다.
_MATRIX_USER = """질문: {question}

검색 결과(이 목록 안의 책만, source_id로 인용):
{facts}"""

_MATRIX_WEB_USER = """질문: {question}

웹 검색 결과(source_id로 인용):
{facts}"""

_MATRIX_NONE_USER = """질문: {question}"""


def _render_candidate(candidate: dict) -> str:
    """후보 하나를 사실만 담은 한 줄로 렌더한다(없는 필드는 생략)."""
    parts: list[str] = [f'source_id={candidate["source_id"]}', f'『{candidate["title"]}』']
    if candidate.get("author"):
        parts.append(str(candidate["author"]))
    if candidate.get("publisher"):
        parts.append(str(candidate["publisher"]))
    # 시제는 pub_status를 우선(오늘 기준 계산됨). 없으면 원 pub_date를 그대로.
    when = candidate.get("pub_status") or candidate.get("pub_date")
    if when:
        parts.append(str(when))
    if candidate.get("price") is not None:
        parts.append(f'{candidate["price"]:,}원')
    if candidate.get("rating") is not None:
        parts.append(f'평점 {candidate["rating"]}')
    return "- " + " · ".join(parts)


def _render_web_candidate(candidate: dict) -> str:
    """웹 후보 하나를 사실만 담은 한 줄로 렌더한다(source_id·제목·신선도·스니펫)."""
    parts: list[str] = [f'source_id={candidate["source_id"]}', str(candidate.get("title") or "")]
    if candidate.get("last_updated"):
        parts.append(f'갱신 {candidate["last_updated"]}')
    line = " · ".join(p for p in parts if p)
    snippet = candidate.get("snippet")
    if snippet:
        line += f"\n  {snippet}"
    return "- " + line


def render_pool(pool: SharedPool, *, lead_offset: int = 0) -> str:
    """공유 풀의 후보를 생성 프롬프트용 사실 목록으로 렌더한다(kind별 렌더).

    lead_offset>0이면 후보 목록을 그만큼 회전해 렌더한다(source_id는 불변) — 열마다 다른
    책을 앞세워 리드북 수렴을 구조로 완화한다. web/none은 회전하지 않는다(호출부가 0을 준다).
    """
    render = _render_web_candidate if pool.kind == "web" else _render_candidate
    candidates = pool.candidates
    if lead_offset and candidates:
        k = lead_offset % len(candidates)
        candidates = candidates[k:] + candidates[:k]
    return "\n".join(render(c) for c in candidates)


def build_matrix_prompt(
    pool: SharedPool, persona_block: str, *, today: str, lead_offset: int = 0
) -> tuple[str, str]:
    """(system_instruction, user_content) 쌍을 조립한다(pool.kind로 분기).

    system = kind별 규율 프롬프트 + 페르소나 블록(가법, 무효 코드면 규율만). user는:
    - product: 질문 + 렌더된 도서 풀 사실(이 목록 안의 책만 인용, lead_offset으로 회전).
    - web: 질문 + 렌더된 웹 사실(같은 사실을 관점·말투로 해석, source_id 인용, 회전 없음).
    - none: 질문만(검색 없음, 무인용 즉답).
    """
    if pool.kind == "web":
        system = _MATRIX_WEB_SYSTEM.format(today=today, checked_at=pool.checked_at)
        user = _MATRIX_WEB_USER.format(question=pool.question, facts=render_pool(pool))
    elif pool.kind == "none":
        system = _MATRIX_NONE_SYSTEM.format(today=today)
        user = _MATRIX_NONE_USER.format(question=pool.question)
    else:
        system = _MATRIX_SYSTEM.format(today=today, checked_at=pool.checked_at)
        user = _MATRIX_USER.format(
            question=pool.question, facts=render_pool(pool, lead_offset=lead_offset)
        )
    if persona_block:
        system = f"{system}\n\n{persona_block}"
    return system, user
