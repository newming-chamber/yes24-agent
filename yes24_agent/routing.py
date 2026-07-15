"""모델 라우팅 — 질의이해 결과로 flash/pro 경로를 고른다.

기본은 pro(정확성 우선)이고, **한 번의 판단으로 끝나는 질의로 확신될 때만** flash로 내려
지연을 줄인다. 난도 판정 자체는 이 모듈이 하지 않는다 — `query_understanding.classify`가
값싼 모델 1회로 의미 기반 판정(multistep·confidence)을 내리고, 여기서는 그 결과를 경로로
번역하기만 한다(순수 함수, LLM·네트워크 없음).

**키워드 버킷 폐기(2026-07-14, 사용자 지시)**: 이전엔 7개 키워드 버킷(_COMPARISON·_SYNTHESIS·
_RECENCY·_REALTIME_FACTS·_EMOTIONAL·_IDENTITY_META·_SERVICE_POLICY)의 부분일치로 난도를 갈랐다.
성장형 목록이라 부류를 놓칠 때마다 단어를 덧붙여야 했고, 표면 매칭이라 의미와 어긋났다
(합성어 부분일치 오분류 → 적대 검증 R4의 파괴적 오탐). 부류 판정은 의미를 아는 계층이 맡고,
라우팅은 "애매하면 무거운 쪽"이라는 정책만 남긴다.
"""

from yes24_agent.query_understanding import QueryUnderstanding

FLASH = "flash"
PRO = "pro"


def select_route(understanding: QueryUnderstanding, *, hybrid_routing: bool) -> str:
    """질의이해 결과를 모델 경로(FLASH/PRO)로 번역한다(결정론, 부수효과 없음).

    hybrid_routing이 꺼져 있으면 항상 PRO(pro 전역). 켜져 있어도 flash로 내리는 건 **확신 있는
    단일단계 질의**뿐이다 — 다단계(multistep)이거나 분류를 신뢰할 수 없으면(confident=False:
    분류 실패·타임아웃·저확신) PRO로 남긴다. 오분류의 대가가 비대칭이기 때문이다: 쉬운 질의를
    pro로 보내면 느릴 뿐이지만, 어려운 질의를 flash로 보내면 답이 틀린다.
    """
    if not hybrid_routing:
        return PRO
    if understanding.multistep or not understanding.confident:
        return PRO
    return FLASH
