"""RBTI 독서 페르소나 조립 — 4축을 파라미터로, 16 = 4조각 조합.

RBTI(Reading BTI) = 독서 성향 16유형. 코드 = ``[C/S][A/E][D/B][I/F]`` 4글자.
각 자리는 서로 다른 축(패턴·처리·폭·동기)이고, 축마다 2개 값이 있어 8개 축-값 조각이
존재한다. 페르소나 블록은 16코드를 키로 박지 않고(no-case-patch) 이 8조각을 데이터로
두어, 코드 4글자를 파싱해 4조각을 골라 런타임에 조립한다.

설계 원칙(가법 레이어):
- 페르소나는 **후보 선택·강조 관점**만 조율한다(미러형: 사용자 취향에 맞춤 +
  성장 스트레치 곁들임).
- 상위 실행 계약은 페르소나보다 항상 우선한다.
"""

from __future__ import annotations

# 코드 자리 순서 → 축 이름·허용값. code[0]=pattern, [1]=processing, [2]=breadth, [3]=motivation.
# (예: "CADI" → C=pattern, A=processing, D=breadth, I=motivation.)
AXIS_ORDER: tuple[tuple[str, tuple[str, str]], ...] = (
    ("pattern", ("C", "S")),
    ("processing", ("A", "E")),
    ("breadth", ("D", "B")),
    ("motivation", ("I", "F")),
)

# 8개 축-값 조각. 각 값은 {tone(강점 체화), aware(함정 자각), stretch(성장 처방)}.
# 미러형 기본: tone은 그 성향을 따르는 추천 결, aware는 스스로 경계할 함정, stretch는
# 가끔·강요 없이 곁들이는 취향 밖 한 권. 기획서 1.2 조각표가 데이터의 단일 소스다.
AXIS_FRAGMENTS: dict[str, dict[str, dict[str, str]]] = {
    "pattern": {
        "C": {
            "tone": "끝까지 읽을 가치 중심으로 엄선하고 깊게 집중",
            "aware": "완독 압박으로 무겁게 몰지 않기",
            "stretch": "가끔 '흥미 식으면 편히 하차해도 좋다' 여지",
        },
        "S": {
            "tone": "가볍게 탐색할 메뉴로 구성하고 발췌·핵심 챕터 짚기",
            "aware": "파편화·휘발 경계(각 권 맥락 한 줄)",
            "stretch": "가끔 '이건 진득하게 완독 추천' 한 권",
        },
    },
    "processing": {
        "A": {
            "tone": "논증·구조·근거로 왜 좋은지 차분·객관적으로 설명",
            "aware": "개연성 강박·건조함·비판 과잉 경계",
            "stretch": "가끔 감성·서사 강한 한 권으로 균형추",
        },
        "E": {
            "tone": "감정·위로·인물의 결로 따뜻하게, 마음에 닿는 언어로",
            "aware": "과몰입·객관성 상실·무거운 주제 집착 경계",
            "stretch": "가끔 논리·비문학 한 권으로 팩트 균형",
        },
    },
    "breadth": {
        "D": {
            "tone": "좋아하는 분야·작가를 깊이, 다음 단계까지 파고들기",
            "aware": "좁은 편식·확증편향·우물 안 경계",
            "stretch": "가끔 낯선 인접 장르 한 권 열어주기",
        },
        "B": {
            "tone": "분야를 넘나들며 트렌드·연결·큐레이션으로 폭넓게",
            "aware": "얕음·겉핥기·선택장애 경계",
            "stretch": "가끔 '한 우물' 심화서 한 권",
        },
    },
    "motivation": {
        "I": {
            "tone": "배움·실용·삶에 적용될 값 우선('무엇을 얻나')",
            "aware": "수집 집착·앎↔실천 괴리 경계",
            "stretch": "가끔 순수 재미·힐링 한 권",
        },
        "F": {
            "tone": "몰입·재미·힐링·장르적 즐거움 우선('얼마나 재밌나')",
            "aware": "현실도피·스낵컬처·깊이 부재 경계",
            "stretch": "가끔 묵직한 서사·교양 한 권 도전",
        },
    },
}

# 축값 → 한글 라벨. 축 설명("완독-분석-깊이-정보")을 코드에서 결정론적으로 파생하는 데 쓴다.
# 스프레드시트 헤더의 한글 축설명엔 원본 오타가 있어(예: CEBI를 "선택-…"로 오기 — 실제 C=완독)
# 시트 원문을 데이터로 박지 않고 코드+이 라벨맵으로 파생한다(docs/rbti-feature-plan.md 참조).
AXIS_VALUE_LABELS_KO: dict[str, dict[str, str]] = {
    "pattern": {"C": "완독", "S": "선택"},
    "processing": {"A": "분석", "E": "공감"},
    "breadth": {"D": "깊이", "B": "넓이"},
    "motivation": {"I": "정보", "F": "재미"},
}

# 카드 제목에 쓰는 16개 아키타입명. 장문 설명은 런타임에서 소비되지 않아 제거했다.
TYPE_ARCHETYPES: dict[str, str] = {
    "CADI": "지적 유희를 즐기는 완벽한 탐험가",
    "CABI": "지식을 엮어내는 체계적인 아카이버",
    "SADI": "필요한 것만 꿰뚫는 예리한 사냥꾼",
    "SABI": "세상의 흐름을 읽는 민첩한 스캐너",
    "CEDI": "타인의 삶을 이해하는 따뜻한 지성",
    "CEBI": "다름을 수용하는 넓고 유연한 포용력",
    "SEDI": "영감과 위로를 찾는 예리한 직관력",
    "SEBI": "트렌디한 영감을 수집하는 감성 큐레이터",
    "CADF": "반전과 트릭을 쫓는 치밀한 추리소설가",
    "CABF": "방대한 세계관을 즐기는 지적 모험가",
    "SADF": "취향 하나는 확실하게 파고드는 덕후",
    "SABF": "호기심 넘치는 유쾌한 이야기 탐험가",
    "CEDF": "주인공과 완벽히 동화되는 공감의 달인",
    "CEBF": "스트레스를 녹여내는 평화로운 힐러",
    "SEDF": "인생 문장을 찾아내는 낭만적인 음미자",
    "SEBF": "형식에 얽매이지 않는 자유로운 영혼",
}

# 채팅·typed 제출·매트릭스가 공유하므로 후보 선택과 강조 관점만 담는다.
_PERSONA_HEADER = (
    "## 독자 페르소나 (RBTI: {code})\n"
    "상위 실행 계약을 바꾸지 않고 후보 선택·강조 관점만 조율하세요."
)

# 유형명(아키타입 이름)은 **주입하지 않는다**. 이름을 프롬프트에 넣으면 모델이 그 단어를 본문에
# 되뇌고("…파고드는 덕후님을 위해"), 그러면 그 단어를 금지하는 규칙을 프롬프트에 또 얹어야 한다 —
# 원인을 넣어두고 결과를 막는 구조다. 본 적 없는 단어는 되뇔 수 없다. 카드 제목의 유형명은
# SSE delta.name으로 UI에 따로 전달되므로 표시에는 손실이 없다.
_PERSONA_BODY = (
    "이 사용자의 독서 성향은 {code} 유형입니다. 아래 관점으로 후보를 평가하세요.\n"
    "- 판단 관점: {tone}\n"
    "- 후보 선택: {structure}\n"
    "- 탐색·선택 범위: {breadth}\n"
    "- 우선 가치: {value}\n"
    "- 자각(함정 회피): {aware}\n"
    "- 성장 후보(선택): 맥락에 맞을 때만 다음 중 하나 — {stretch_pool}"
)


def is_valid_code(code: object) -> bool:
    """RBTI 코드가 유효한지 판정한다.

    4글자·대문자·자리별 허용값(AXIS_ORDER)만 통과한다. 길이 불일치·잘못된 값·소문자·
    비문자열은 모두 False. 대소문자를 관용하지 않는다(플러밍은 유효 코드만 저장하므로,
    저장·조립 전 경로가 일관되게 대문자 코드만 취급한다).
    """
    if not isinstance(code, str) or len(code) != len(AXIS_ORDER):
        return False
    return all(ch in allowed for ch, (_axis, allowed) in zip(code, AXIS_ORDER))


def get_archetype_name(code: object) -> str:
    """코드의 아키타입 한 줄 이름(카드 제목·정체성 힌트). 무효 코드면 ""."""
    if not is_valid_code(code):
        return ""
    return TYPE_ARCHETYPES.get(code, "")  # type: ignore[arg-type]


def axis_label(code: object) -> str:
    """코드에서 한글 축 설명을 결정론적으로 파생한다(예: "CADI" → "완독-분석-깊이-정보").

    스프레드시트 헤더의 축설명엔 원본 오타가 있어(CEBI 등) 시트 원문 대신 코드+라벨맵으로
    파생한다 — 16뷰 카드 부제 등에 코드-표시가 모순되지 않게. 무효 코드면 ""."""
    if not is_valid_code(code):
        return ""
    return "-".join(
        AXIS_VALUE_LABELS_KO[axis][ch] for ch, (axis, _allowed) in zip(code, AXIS_ORDER)  # type: ignore[index]
    )


def build_persona_block(code: object) -> str:
    """코드 4글자를 파싱해 4조각을 조립한 페르소나 블록을 반환한다.

    무효 코드면 빈 문자열이다. 유효하면 4축 의미를 후보 선택·강조 관점의
    구조 중립 블록으로 조립한다. 카드명과 축 라벨은 SSE 표시 전용이다.
    """
    if not is_valid_code(code):
        return ""

    # 자리별 조각을 축 이름으로 뽑는다(하드코딩 없이 코드 파싱).
    picked = {axis: AXIS_FRAGMENTS[axis][ch] for ch, (axis, _allowed) in zip(code, AXIS_ORDER)}

    # 본문의 각 레버를 담당 축의 tone으로 채운다(1.1 축=레버 매핑). aware·stretch는
    # 4조각을 모아 자각 목록·성장 제안 풀로 조인한다.
    aware = " · ".join(picked[axis]["aware"] for axis, _allowed in AXIS_ORDER)
    stretch_pool = "; ".join(picked[axis]["stretch"] for axis, _allowed in AXIS_ORDER)
    body = _PERSONA_BODY.format(
        code=code,
        tone=picked["processing"]["tone"],
        structure=picked["pattern"]["tone"],
        breadth=picked["breadth"]["tone"],
        value=picked["motivation"]["tone"],
        aware=aware,
        stretch_pool=stretch_pool,
    )
    return f"{_PERSONA_HEADER.format(code=code)}\n{body}"
