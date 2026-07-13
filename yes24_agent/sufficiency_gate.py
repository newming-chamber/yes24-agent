"""충분성 게이트 — 답변이 근거에 충분히 접지됐는지 코드로 판정해 재검색을 트리거한다.

`product_gate`(무출처 상품 사실·인용-제목 오매핑)·`policy_gate`(무출처 정책 규정)를 감싸고,
그 위에 **얕은 결과 재검색**을 한 겹 더 얹는 얇은 통합 계층이다. 재설계 결정 P0의 "충분성
게이트(코드)"에서 무출처 케이스에만 좁게 구현돼 있던 것을, 도구가 이미 반환하지만 아무도
읽지 않던 `needs_followup` 힌트를 소비해 "검색 결과가 질문을 충분히 못 덮은" 케이스까지
일반화한다(architecture-blueprint.md P2).

판정 우선순위(기존 무출처 게이트 동작을 그대로 보존):
  ① 무출처/오매핑(product_gate.evaluate_product_answer) — 상품 사실 환각. 발동 시 여기서 끝나고
     이후 판정은 하지 않는다(중복 재검색을 구조로 차단).
  ② 무출처 정책(policy_gate.evaluate_policy_answer) — ①이 정상일 때만. Yes24 정책 페이지 근거
     없이 환불·반품 규정을 단정하면 발동(yes24_fetch 강제 정책 보정으로 재확인).
  ③ 얕음(detect_shallow_result) — ①②가 정상일 때만. 마지막 검색성 도구 호출이 결과 0건이면 발동.

`runner`는 이 모듈의 `evaluate`만 호출하고, 반환된 `GateDecision`(무엇을·어떤 지시로 재검색할지)에
따라 기존 재검색 에스컬레이트 메커니즘을 재사용한다. 재진입은 무출처 게이트와 동일하게 딱 1회다.

이 모듈은 순수 함수 계층으로 config·ADK를 import하지 않는다(product_gate와 동일 정신).
"""

from dataclasses import dataclass

from yes24_agent.policy_gate import (
    POLICY_CORRECTION_DIRECTIVE,
    evaluate_policy_answer,
    has_policy_grounding,
)
from yes24_agent.product_gate import CORRECTION_DIRECTIVE, evaluate_product_answer

# needs_followup 힌트를 게이트에 반영할 '검색성' 도구. 이 도구들만 결과의 충분성을 논할 수
# 있다(fetch·web_fetch는 특정 URL 열람이라 '얕음' 개념이 없다). _followup.needs_search_followup를
# 반환하는 도구 집합과 일치.
_SEARCH_TOOLS = frozenset({"yes24_search", "web_search", "yes24_browse"})

# 얕은 결과 재검색 시 보정 에이전트에 내리는 지시(2차 턴 user 메시지). 무출처 정정과 달리
# '환각을 지웠다'가 아니라 '검색이 질문을 못 덮었으니 검색어를 바꿔 한 번 더 확인하라'는 것이다.
# 무출처 정정(CORRECTION_DIRECTIVE)과 같은 도구 강제 경로를 타므로 문체·계약(인용 강제, 공감
# 서두 금지, 사실↔책 치환 금지)을 맞춘다.
FOLLOWUP_DIRECTIVE = (
    "방금 검색 결과가 사용자의 질문을 충분히 다루지 못했습니다(결과가 없거나 관련성이 낮음). "
    "검색어를 바꿔(더 넓히거나 핵심어만 남겨) 지금 한 번 더 확인하고, 도구 결과에 실제로 있는 "
    "내용만 인용[n]과 함께 답하세요. 책·상품을 찾는 질문이면 yes24_search로, 사실·정보를 묻는 "
    "질문(뉴스·시세·법률 등)이면 web_search로 확인하세요(사실 질문을 책 추천으로 바꾸지 말 것). "
    "확인되지 않은 제목·저자·가격은 절대 쓰지 말고, 공감 서두 없이 곧바로 본론으로 답하세요."
)


@dataclass(frozen=True)
class GateDecision:
    """게이트 발동 결과 — runner가 어떤 재검색을 돌릴지 결정하는 값 타입.

    kind: "product"(무출처·오매핑) | "policy"(무출처 정책) | "shallow"(얕은 결과).
        재검색 후 채택 정책·보정 에이전트가 갈린다.
    reason: 로깅·메트릭용 세부 사유("mismap"|"unsourced"|"unsourced_policy"|"shallow").
    directive: 재검색 2차 턴에 보낼 user 지시 메시지.
    status_detail: 재검색 진행을 알리는 verifying status의 사용자 노출 문구.
    """

    kind: str
    reason: str
    directive: str
    status_detail: str


def detect_shallow_result(observed_tool_calls: list[dict]) -> bool:
    """마지막 검색성 도구 호출이 '얕음'(결과 0건)이라 재검색이 필요한지 판정한다.

    observed_tool_calls는 이번 턴 도구 응답에서 관찰한 힌트 목록(각 dict에 tool_name·
    result_count·needs_followup·status). 마지막 검색성 호출만 본다 — 모델이 얕은 결과 뒤
    스스로 다시 검색해 좋은 결과를 얻었으면(마지막 검색성 호출의 result_count>0) 발동하지
    않는다(자기수정 존중).

    **오탐 0을 위해 좁게 둔다**: needs_followup은 (a)결과 0건과 (b)핵심어 불일치(결과는
    있으나 제목에 질의어가 없음)를 모두 True로 잡지만, 여기서는 (a) result_count==0만
    트리거로 삼는다. (b)까지 넓히면 장르·추천성 질의("소설 추천")에서 제목에 장르어가 없는
    정상 결과가 얕음으로 오판돼 불필요한 재검색(지연↑)을 부른다. 결과 0건은 모호함 없는
    신호라 오탐이 없다. (b) 확대는 라이브 A/B로 오탐률을 관찰한 뒤 검토한다(블루프린트 §7).
    """
    search_calls = [
        c for c in observed_tool_calls if c.get("tool_name") in _SEARCH_TOOLS
    ]
    if not search_calls:
        return False
    last = search_calls[-1]
    # needs_followup=True이면서 result_count==0인 경우만(결과 0건·엉뚱해서 근거 없음).
    return bool(last.get("needs_followup")) and last.get("result_count") == 0


def _policy_decision(reason: str) -> GateDecision:
    """정책 재확인 GateDecision — yes24_fetch 강제 보정으로 Yes24 정책 페이지를 다시 연다."""
    return GateDecision(
        kind="policy",
        reason=reason,
        directive=POLICY_CORRECTION_DIRECTIVE,
        status_detail="Yes24 정책 페이지에서 정확히 확인하고 있어요",
    )


def evaluate(
    text: str,
    *,
    cited_sources: list[dict],
    observed_sources: list[dict],
    observed_tool_calls: list[dict],
) -> GateDecision | None:
    """답변의 충분성을 판정한다(정상이면 None, 발동 시 GateDecision).

    ① 무출처/오매핑을 우선 판정(evaluate_product_answer, 기존 인자·동작 그대로) — 발동 시 즉시
       반환해 이후 판정은 보지 않는다(중복 재검색 차단).
    ② ①이 정상일 때만 무출처 정책(evaluate_policy_answer)을 판정한다.
    ③ ①②가 정상일 때만 얕음(마지막 검색성 호출 결과 0건)을 판정한다.

    정책성 턴 분기: 이번 턴이 Yes24 정책 페이지(notice)를 열었다면(정책 질문의 여정), 게이트가
    발동할 때 상품 검색 보정이 아니라 **정책 fetch 보정**으로 재확인한다 — 배송비·포인트 등
    정책 질문이 상품 검색으로 탈선해 "찾으시는 책의 결을…" 되물음(UNSOURCED_PRODUCT_NOTICE)이
    나가던 실측 회귀를 막는다. 신호는 notice 출처 관측으로 보수적으로 잡는다(도서·상품 턴은
    정책 페이지를 열지 않으므로 오탐 0). 게이트 미발동(정상 접지)이면 재확인 자체가 없다.
    """
    policy_turn = has_policy_grounding(observed_sources)
    product_reason = evaluate_product_answer(
        text, cited_sources=cited_sources, observed_sources=observed_sources
    )
    if product_reason is not None:
        if policy_turn:
            return _policy_decision(f"policy_turn/{product_reason}")
        return GateDecision(
            kind="product",
            reason=product_reason,
            directive=CORRECTION_DIRECTIVE,
            status_detail="정확한 정보를 찾아 다시 확인하고 있어요",
        )
    policy_reason = evaluate_policy_answer(
        text, cited_sources=cited_sources, observed_sources=observed_sources
    )
    if policy_reason is not None:
        return _policy_decision(policy_reason)
    if detect_shallow_result(observed_tool_calls):
        if policy_turn:
            return _policy_decision("policy_turn/shallow")
        return GateDecision(
            kind="shallow",
            reason="shallow",
            directive=FOLLOWUP_DIRECTIVE,
            status_detail="검색 범위를 넓혀 다시 확인하고 있어요",
        )
    return None
