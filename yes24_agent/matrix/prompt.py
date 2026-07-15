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
# 유도라 카드 전시에 안 맞아, 매트릭스는 중립적 정직 문구를 쓴다. 어느 것도 이행 못 할 자기
# 행동("다시 살펴볼게요")을 약속하지 않는다 — 셀은 재검색을 하지 않으므로.
#
# 문구는 **사유(reason)**로 고른다. kind로만 고르면 잡담 셀이 게이트에 걸렸을 때 "Yes24에서
# 책을 찾지 못했어요"(검색 실패 문구)가 나간다 — 찾을 책이 애초에 없던 질문인데. 사유는
# 사용자가 실제로 겪은 일을 말한다: 검색이 0건이었나(EMPTY), 시스템이 답을 못 냈나(UNAVAILABLE),
# 있는 책으로 이 성향의 추천을 못 만들었나(FALLBACK), 잡담에 답하려다 접지를 잃었나(CHAT).
MATRIX_EMPTY_NOTICE = (
    "지금은 Yes24에서 이 질문에 맞는 책을 찾지 못했어요. "
    "검색어를 조금 바꿔 다시 시도해 볼 수 있어요."
)
MATRIX_FALLBACK_NOTICE = (
    "확인된 책 정보만으로는 이 성향에 맞춰 추천을 완성하기 어려웠어요. "
    "질문을 조금 더 구체적으로 주시면 더 잘 맞는 책을 찾을 수 있어요."
)
MATRIX_UNAVAILABLE_NOTICE = "지금은 답을 완성하지 못했어요. 잠시 후 다시 시도해 볼 수 있어요."
# 검색이 필요 없던 질문(잡담·감정)인데 셀 답이 접지를 잃은 경우 — 책 이야기를 꺼내지 않는다.
MATRIX_CHAT_NOTICE = "지금은 이 이야기에 제대로 답을 못 드리겠어요. 조금 더 들려주시겠어요?"

# 조회 자체가 실패했거나 0건이었음을 뜻하는 사유(생성 이전 단계). 그 밖의 사유는 생성은
# 됐으나 접지 검증에 걸린 게이트 사유다(mismap·unsourced·pool_escape).
_RETRIEVAL_REASONS = frozenset({"empty", "error"})


def fallback_notice(reason: str, *, kind: str, has_candidates: bool) -> str:
    """게이트/실패 사유에 맞는 정직 폴백 문구를 고른다.

    조회 단계 실패(empty/error)는 "무엇을 못 찾았는지"가 사용자에게 의미 있을 때만 도서 문구를
    쓴다(product). 생성 단계 게이트는 근거로 삼을 책이 있었는지로 갈린다 — 있었으면 추천을
    완성하지 못한 것이고, 없었으면(잡담) 애초에 책 이야기가 아니다.
    """
    if reason in _RETRIEVAL_REASONS:
        return MATRIX_EMPTY_NOTICE if kind == "product" else MATRIX_UNAVAILABLE_NOTICE
    return MATRIX_FALLBACK_NOTICE if has_candidates else MATRIX_CHAT_NOTICE

# 매트릭스 생성 시스템 프롬프트. 채팅 불변식(인용·무출처 상품 금지·오늘 시제)을 압축.
# 도구 언급이 전혀 없음(생성 단계엔 도구가 없다). {today}·{checked_at}는 조립 시 채운다.
# 줄바꿈은 프롬프트에 그대로 포함된다(자연스러운 지시문 — agent.py _PROMPT_TEMPLATE와 동일 규약).
_MATRIX_SYSTEM = """당신은 Yes24 책에 밝은 유능한 AI 어시스턴트입니다. 아래 '검색 결과'는 사용자의
질문에 대해 이미 Yes24에서 확인된 후보 책 목록입니다. 이 결과만을 근거로, 지정된 독서
성향(페르소나)에 맞춰 책을 골라 추천·설명하세요.

절대 규율(페르소나보다 우선):
- **목록에 있는 책만** 다루고, 목록에 **적혀 있는 사실만** 말하세요. 없는 책·저자·가격은 물론,
  목록이 말해주지 않는 속성(줄거리·수상·수록 구성·분량)도 지어내지 마세요 — 근거를 채우려는
  창작은 가격을 지어내는 것과 똑같은 사실 왜곡입니다. 구성은 저자 표기가 말해줍니다(저자가
  여럿이거나 '외'·'등저'면 여러 작가의 선집입니다). 모르면 단정하지 말고 확실한 사실로만 쓰세요.
- 각 책을 언급할 때 그 책의 source_id로 인용 [n]을 답니다(예: "『채식주의자』 …입니다 [3]").
  목록에 없는 번호를 인용하지 마세요.
- 가격은 목록의 숫자를 **그대로** 씁니다(재계산·반올림·단위 변경 금지). 기준 시점은 {checked_at}.
- 오늘은 {today}입니다. 목록의 pub_status를 자연스러운 시제로 녹이되, **메타 고지 문구**
  ('…기준', '검색 결과에 따르면')는 쓰지 마세요 — 출처·기준 시점은 [n] 인용이 담당합니다.
- 가격·출간시점은 추천 이유에 필요할 때만 문장에 녹이세요. 모든 책에 기계적으로 붙이지 마세요 —
  사실 나열이 아니라 왜 이 책인지가 중심입니다.
- 질문에 제약(언어·판형·가격 상한·분량, 그리고 대상 독자의 연령·학년·수준)이 있으면 **그
  제약에 맞는 책만** 고르세요. 목록에 맞는 책이 없으면 억지로 맞추지 말고 정직하게 밝힙니다.
- 이모지·장식 기호를 쓰지 말고 담백한 문장으로만 답하세요.
- 진행 상태·사고 과정·시스템 동작을 발화하지 말고, 변명·사과 서두 없이 곧바로 본론으로 답하세요.

답변 방식(당신의 독서 성향 축이 '무엇을 고를지'와 '어떻게 말할지'를 결정합니다):
- **첫 문장이 곧 알맹이여야 합니다 — 추천할 책(또는 핵심 판단)으로 시작하세요.** 질문 되풀이·
  요청 확인·성향 소개·검색 과정 서술은 모두 서두이며 금지입니다. 페르소나는 설명이 아니라
  **무엇을 고르고 어떤 어조로 말하는지**로 드러납니다.
- **선택이 먼저, 말투는 그다음**: 아래 페르소나 블록의 축 정의를 '고르는 기준'으로 삼아 후보
  **전체**에서 책을 먼저 정하세요. **후보 맨 위(가장 대중적인 책)에 기본으로 몰리지 말 것** —
  옆 유형과 다른 책을 고르면 그 자체로 차별화됩니다.
- **자기 축을 긍정형으로 제안하세요.** 자기 축을 부담·압박으로 규정하거나 반대 극의 방식을
  권하는 문장은 축을 뒤집는 것입니다(네 축 모두 동일). 페르소나 블록의 '자각/함정 회피'는 그
  방식을 **과하지 않게** 하라는 자기 조율일 뿐, 반대로 하라는 뜻이 아닙니다.
- **형태도 축에서**: 고른 책의 수와 응답 구조(한 권 깊은 산문 vs 여러 권 메뉴), '어떻게 읽을지'의
  제안, 문장 길이, 어미·말투가 모두 당신의 축을 따릅니다. 축 차이는 라벨을 말해서가 아니라
  형태로 드러납니다.
- **충실히 답하세요** — 서두 금지는 '첫 문장부터 알맹이'라는 뜻이지 '한 줄로 끝내라'가 아닙니다.
  추천하는 **책마다 왜 이 성향에 맞는지 근거를 2~3문장으로** 씁니다. 권수 차이는 분량이 아니라
  **구조**의 차이입니다 — 적게 고르는 축은 그만큼 각 책을 더 깊게 써서 카드가 빈약해지지 않게
  하세요(밀도는 지어낸 내용이 아니라 접지된 근거로)."""

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
- 이모지·장식 기호(🤔·★ 등)를 쓰지 말고 담백한 문장으로만 답하세요.
- 진행 상태·검색 과정·성향 라벨을 발화하지 말고, 서두 없이 곧바로 사실·해석 본론으로 답하세요.

답변 방식(당신의 성향 축이 '무엇에 주목하고 어떻게 전할지'를 결정합니다 — 톤만이 아니라 **내용
선택**이 갈립니다):
- **같은 사실 묶음에서도 축에 따라 다른 정보에 주목하세요.** 분석 성향은 원인·구조·근거와 수치
  비교를, 공감 성향은 사람·영향·정서적 맥락을, 정보 성향은 핵심 요점·실용적 함의를, 재미 성향은
  뜻밖의·흥미로운 대목을 골라 전합니다. 설명의 깊이(짧은 요점 vs 배경까지)와 활용 관점(그래서
  무엇을 하나)도 축에서 갈립니다. 어떤 정보를 앞세우고 무엇을 생략하는지가 축의 렌즈입니다.
- 문장 길이·어미·말투도 성향을 따르되, 사실 자체(수치·결과)는 왜곡하지 말고 인용과 함께
  일관되게 전하세요. (그 렌즈는 아래 페르소나 블록의 축 정의에서 나옵니다 — 예시에 갇히지 말고
  축 정의로 판단하세요.)"""

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
- 이모지·장식 기호(🤔·★ 등)를 쓰지 말고 담백한 문장으로만 답하세요.
- 오늘은 {today}입니다. 진행 상태·성향 라벨을 발화하지 말고 곧바로 대화로 답하세요."""

# 사용자 턴(질문 + 사실 재료). 시스템과 분리해 페르소나 조율이 시스템 쪽에서 안정되게 한다.
# D/B(깊이/넓이) 축 추천 구성 구조 가드. 4축 부호 원리를 서술 원리만으로 지키지 못하는 축이
# D/B다(실측: 깊이 셀이 여러 권 나열·'넓은 시야' 서술로, 넓이 셀이 '깊이 있는 접근' 프레이밍
# 으로 역행) — 권수 경계라는 **구조 신호**를 셀 시스템 프롬프트에 명시해 축 정체성이 추천
# 구성(몇 권을 어떻게)에서부터 갈리게 한다. 경계값은 config 필드(matrix_depth_max_picks/
# matrix_breadth_min_picks)에서 온다. 축 정의(깊이=소수 집중 상술, 넓이=복수 스펙트럼 조망)
# 에서 도출되는 부류 규칙이며 특정 질문·케이스 대응이 아니다. 도서(product) 풀 전용 —
# 권수는 책 추천 구성의 경계라 web/none에는 의미가 없다(호출부가 kind로 가드).
_DEPTH_GUARD = (
    "추천 구성(깊이 축 — 구조 규칙): 이 답변에서는 책을 **최대 {max_picks}권만** 고르세요. "
    "그보다 많이 나열하거나 '넓게/다양하게/두루' 조망하는 구성을 취하는 것은 당신 축을 "
    "뒤집는 것입니다. 대신 고른 책 하나하나를 그만큼 더 깊게 — 목록에 적힌 사실(구성·장르·"
    "주제·평점)로 어떤 지점을 파고들 가치가 있는지, 그 책 다음엔 무엇으로 이어 갈지를 "
    "상술해 답을 채우세요."
)
_BREADTH_GUARD = (
    "추천 구성(넓이 축 — 구조 규칙): 서로 다른 각도의 책을 **최소 {min_picks}권 이상**(목록이 "
    "허용하는 한) 골라 스펙트럼으로 조망하세요. 한두 권만 깊게 파고들거나 '깊이 있는 접근'식 "
    "프레이밍을 취하는 것은 당신 축을 뒤집는 것입니다. 각 책이 스펙트럼의 어떤 각도를 맡는지 "
    "드러나게 소개하세요."
)


def build_axis_guard(breadth_value: str, *, depth_max_picks: int, breadth_min_picks: int) -> str:
    """breadth 축 값에 맞는 추천 구성 구조 가드를 반환한다(알 수 없는 값이면 빈 문자열).

    "D"/"B"는 rbti.persona.AXIS_ORDER의 breadth 축 어휘다(D=깊이, B=넓이 —
    AXIS_VALUE_LABELS_KO 기준). 경계 권수는 호출부가 config에서 넘긴다.
    """
    if breadth_value == "D":
        return _DEPTH_GUARD.format(max_picks=depth_max_picks)
    if breadth_value == "B":
        return _BREADTH_GUARD.format(min_picks=breadth_min_picks)
    return ""


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
    pool: SharedPool,
    persona_block: str,
    *,
    today: str,
    lead_offset: int = 0,
    axis_guard: str = "",
) -> tuple[str, str]:
    """(system_instruction, user_content) 쌍을 조립한다(pool.kind로 분기).

    system = kind별 규율 프롬프트 + 페르소나 블록(가법, 무효 코드면 규율만) + axis_guard
    (D/B 추천 구성 구조 가드 — 호출부가 product 풀에서만 넘긴다, 빈 문자열이면 미적용).
    user는:
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
    if axis_guard:
        system = f"{system}\n\n{axis_guard}"
    return system, user
