"""무출처 정책 사실 게이트 — 이번 턴 Yes24 정책 페이지 근거 없이 단정한 정책 규정을 차단한다.

배경(실측 확정, QA IDX 30): "예스24 환불정책 며칠 안에 반품돼?" 같은 질의에서 에이전트가
Yes24 내부 정책 시드(POLICY_SEED_URLS)를 yes24_fetch하지 않고 학습지식으로 반품 기한(단순변심
7일 등)을 단정한다. 프롬프트에는 이미 "정책은 Yes24 내부 페이지로 fetch"가 있으나 pro·flash
모두 이 맥락에서 도구를 건너뛰는 비결정성이 있어(라우팅만으로는 안 잡힘), product_gate와 같은
정신으로 코드 제어흐름에 물리화한다. 게다가 학습지식 단정은 사실도 틀리기 쉽다(실측: "7일 이내"로
단정했으나 Yes24 실제 규정은 "출고완료 후 10일 이내").

이 모듈은 순수 함수 계층으로 판정만 한다(product_gate와 동일 정신 — config·ADK를 import하지
않는다). 발동하면 orchestrator가 yes24_fetch를 강제하는 정책 보정 턴으로 재확인시켜, Yes24
정책 페이지 인용과 함께 다시 답하거나(채택) 규정을 지어내지 않는 안전 안내로 폴백한다.

판정:
1. **정책 규정 주장**(detect_unsourced_policy_claim): 답변이 환불·반품·교환·취소·배송 정책의
   구체 규정(기한 "N일/개월 이내"·영업일·출고완료 기준 등)을 사실로 단정하는가. 정책 주제어와
   구체성 신호(기한·기준 토큰)가 함께 있을 때만 True — 책 제목에 우연히 든 '환불'·'교환'이나
   기한 없는 일반 언급은 배제해 오탐을 막는다.
2. **Yes24 정책 접지**(has_policy_grounding): 답변을 뒷받침할 Yes24 정책 페이지 출처(yes24_fetch의
   notice)가 실제로 있는가. 상품 출처(search_result 등)·웹 출처(web)는 정책 근거가 아니다 —
   정책은 반드시 Yes24 내부 정책 페이지로만 답한다(CLAUDE.md).
"""

import re

# Yes24 '정책' 근거로 인정하는 source type. 정책 페이지는 yes24_fetch가 notice로 반환한다
# (상품 상세=book_detail, 검색=search_result, 브라우징=browse, 웹=web는 정책 근거가 아니다).
POLICY_SOURCE_TYPES = frozenset({"notice"})

# 정책 주제어: 환불·반품·교환·취소·청약철회·배송비 등 CS 규정 영역. 이것만으로는 책 제목의
# 우연한 부분일치("《환불 원정대》")일 수 있어 반드시 _POLICY_RULE(구체 규정 신호)와 함께 본다.
_POLICY_TOPIC = re.compile(r"반품|환불|교환|취소|청약\s*철회|반송|배송비|배송료")

# 구체 규정 신호: 기한("N일/개월 이내")·영업일·출고완료·수령/받은 날 기준처럼 정책을 사실로
# 단정할 때 쓰는 토큰. 정책 주제어와 동반될 때만 "무출처 정책 규정 단정"으로 본다. 기한·기준이
# 없는 일반 언급(감정 대화의 "환불받고 싶어"류 응답)은 배제한다(오탐 0 우선).
_POLICY_RULE = re.compile(
    r"\d+\s*일\s*이내|\d+\s*개월\s*이내|영업일|출고\s*완료|"
    r"수령일(?:로)?부터|받은\s*날(?:로)?부터|청약\s*철회"
)

# 무출처 정책 단정 감지 시 정책 보정 턴에 내리는 지시(2차 턴 user 메시지). yes24_fetch를 강제해
# (지시만으론 비결정적) Yes24 내부 정책 페이지를 열어 실제 규정만 인용과 함께 답하게 한다.
# 주제 중립: 특정 카테고리(반품·취소 등)를 나열하면 모델이 질문과 무관한 그 페이지로 새므로
# (실측: "배송비" 질문에 취소/교환/반품 페이지를 열어 반품 배송비만 답함), 카테고리를 열거하지
# 않고 "사용자가 물은 바로 그 주제"로 앵커한다. 이미 이번 대화에서 연 해당 페이지가 있으면 재사용.
POLICY_CORRECTION_DIRECTIVE = (
    "방금 답변은 Yes24 이용정책을 출처 없이 답했거나 질문 주제를 벗어났습니다. "
    "지금 yes24_fetch로 **사용자가 물은 바로 그 주제**의 Yes24 내부 정책·안내 페이지를 열어 "
    "(이미 이번 대화에서 그 주제의 페이지를 열었으면 그 url을 그대로 재사용), 그 페이지에 실제로 "
    "적힌 내용만 인용[n]과 함께 답하세요. 사용자가 묻지 않은 다른 카테고리(예: 배송비를 물었는데 "
    "반품·취소 규정)로 새지 말고, 페이지에 없는 기한·수치·조건은 지어내지 말며, 공감 서두 없이 "
    "곧바로 본론으로 답하세요."
)

# 정책 페이지 fetch까지 접지에 실패했을 때만 쓰는 최종 안전 안내(폴백). 규정을 지어내지 않고
# 정확한 확인 경로로 안내한다(변명·사과·내부 동작 언급 없이).
UNSOURCED_POLICY_NOTICE = (
    "정확한 환불·반품 규정은 상품과 주문 상태에 따라 달라질 수 있어요. "
    "Yes24 고객센터 또는 마이페이지 > 주문/배송 > 반품/교환 신청에서 확인해 주세요."
)


def detect_unsourced_policy_claim(text: str) -> bool:
    """답변이 정책 규정을 구체적으로 단정하는지(주제어+규정 신호) 결정론으로 판정한다.

    정책 주제어(반품·환불·교환·취소·배송비 등)와 구체 규정 신호(기한·영업일·출고완료 등)가
    함께 있을 때만 True. 접지 여부와 무관한 순수 텍스트 판정으로, 게이트 발동은 호출부가
    접지(has_policy_grounding)와 결합해 정한다.
    """
    if not text:
        return False
    return bool(_POLICY_TOPIC.search(text) and _POLICY_RULE.search(text))


def has_policy_grounding(sources: list[dict]) -> bool:
    """주어진 출처 목록에 Yes24 정책 페이지 출처(POLICY_SOURCE_TYPES)가 하나라도 있는지."""
    return any(source.get("type") in POLICY_SOURCE_TYPES for source in sources)


def evaluate_policy_answer(
    text: str,
    *,
    cited_sources: list[dict],
    observed_sources: list[dict],
) -> str | None:
    """답변의 정책 규정 주장이 근거에 어긋나는 사유를 반환한다(정상이면 None).

    - "unsourced_policy": 정책 규정을 구체적으로 단정하는데 Yes24 정책 페이지 접지가 전혀 없음.
    접지는 인용된 최종 출처 또는 이번 턴 관찰 출처 중 정책 출처가 있으면 인정한다(fetch는 했으나
    인용을 빠뜨린 경우까지 통과시켜 오탐을 막는다). 사유가 있으면 orchestrator가 재fetch로 정정한다.
    """
    grounded = has_policy_grounding(cited_sources) or has_policy_grounding(observed_sources)
    if not grounded and detect_unsourced_policy_claim(text):
        return "unsourced_policy"
    return None
