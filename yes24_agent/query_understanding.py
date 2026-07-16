"""질의이해 — 값싼 모델 1회로 질의의 **의미**를 분류한다(intent·multistep·confidence).

이 질의가 **무엇에 관한 것인지**(intent)와 **한 번의 판단으로 끝나는지**(multistep)를 판정해,
runner의 모델 라우팅(flash/pro)과 인용 무결성 게이트의 발동 조건에 쓴다.

**설계 전환(2026-07-14, 사용자 지시)**: 이전 구현은 두 모듈(routing·query_understanding)에
9개의 키워드 버킷(_COMPARISON·_SYNTHESIS·_RECENCY·_REALTIME_FACTS·_EMOTIONAL·_IDENTITY_META·
_SERVICE_POLICY·_PRODUCT·_CHITCHAT)을 두고 문자열 부분일치로 부류를 갈랐다. 이는 프로젝트
원칙(no-case-patch)이 금지하는 **성장형 목록**이었고, 표면 문자열 매칭이라 의미와 어긋났다:
'책'이 '정책·산책·책상·책임'에 부분일치해 비상품 질의가 상품으로 오분류됐고(적대 검증 R4에서
파괴적 오탐의 원인), 부류를 하나 놓칠 때마다 단어를 덧붙이는 방식으로만 고칠 수 있었다.
지금은 **부류의 정의**를 프롬프트로 주고 모델이 의미로 판정한다 — 새 표현·신조어·합성어가
와도 목록을 늘릴 필요가 없다.

폴백은 항상 **안전한 쪽**이다. 저확신은 유효한 grounded intent를 보존해 pro 모델의 typed evidence
경계로 보내고, 분류 실패·타임아웃처럼 intent를 신뢰할 수 없는 경우만 세션 문맥을 가진 pro 루트로
보낸다. 별도 문자열 게이트나 generic correction 분기를 되살리지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Literal

from google.genai import types

from yes24_agent.config import Settings, get_genai_client
from yes24_agent.product_selection import (
    ConstraintOperator,
    NumericEvidenceField,
    ProductConstraint,
)
from yes24_agent.sources import (
    POLICY_SOURCE_TYPES,
    PRODUCT_DETAIL_SOURCE_TYPES,
    PRODUCT_SOURCE_TYPES,
    WEB_SOURCE_TYPES,
)

logger = logging.getLogger(__name__)

# intent 부류(정규 목록). 프롬프트의 부류 정의와 1:1 대응한다.
PRODUCT = "product"  # 책·상품 자체(찾기·추천·재고·가격·구매·평점) — Yes24 검색의 강점 영역
POLICY = "policy"  # 쇼핑몰 이용 규정·혜택(주문·배송·반품·결제·회원)
RECENCY = "recency"  # 지금 시점의 사실이라 최신 확인이 필요한 질의
IDENTITY = "identity"  # 어시스턴트 자신·능력 범위를 묻는 메타 질의
CHITCHAT = "chitchat"  # 인사·감사·소감·잡담 — 정보 요구 없음
WEB = "web"  # 검증 가능한 외부 사실·지식 질의
REASONING = "reasoning"  # 외부 사실 조회가 필요 없는 생성·변환·계산·조언

INTENTS = (PRODUCT, POLICY, RECENCY, IDENTITY, CHITCHAT, WEB, REASONING)

# **도구 접지가 정의상 필요한** 부류. 이 부류의 답은 도구 결과에 근거해야만 참일 수 있다 —
# 상품 사실(재고·가격·목록)·이용 규정·지금 시점의 사실은 학습 지식으로 답하면 틀린다. 그래서
# 이 턴이 인용[n] 없이 끝나면 근거가 없다는 뜻이고(미완결), 재확인이 항상 정답이다. 반대로
# 외부 사실·지식(web)은 검색하고, reasoning·잡담·정체성만 도구 없이 답한다.
GROUNDED_INTENTS = frozenset({PRODUCT, POLICY, RECENCY, WEB})

_HIGH = "high"
ClassificationState = Literal["confident", "ambiguous", "unavailable"]

# 분류 프롬프트 — **부류의 정의**만 서술한다(키워드 나열 금지). 표면 문자열이 아니라 질문의
# 의미로 판정하라고 명시해, 합성어·부분일치로 부류가 갈리던 실패를 원천 차단한다.
_CLASSIFY_SYSTEM = """너는 온라인 서점(Yes24) 어시스턴트의 질의 분류기다.
현재 질문과, 제공된 경우 바로 전 사용자 질문을 함께 읽고 현재 질문의 값을 JSON으로만 답한다.
현재 질문이 앞 질문의 대상·조건·결과를 가리키면 그 문맥을 이어서 intent와 하위 동작을 판정한다.
product_constraints에는 현재 질문 자체에 숫자로 명시된 조건만 넣는다.

intent — 질문이 **무엇에 관한 것인지** 하나만 고른다:
- product: 책·상품 자체에 관한 것. 새로운 상품을 찾거나 추천받는 요청뿐 아니라, 사용자가 지정한
  상품의 서지 사실·소개·목차·리뷰를 확인하거나 여러 상품의 내용을 비교하는 요청도 포함한다.
  온라인 서점 카탈로그에서 특정 창작자·분야에 속한 상품 목록을 조회하는 요청도 product다.
  사용자가 특정 온라인 서점·카탈로그 안에서 상품을 탐색하거나 추천하라고 범위를 지정한 요청은
  반드시 product이며, 새 후보를 골라 달라는 요청이면 product_selection=true다.
  질문에 쇼핑몰 이름을 쓰지 않았더라도 책·상품 후보나 목록 자체를 요청하면 product다.
- policy: 그 쇼핑몰을 이용하는 규정·혜택에 관한 것. 주문·배송·반품·교환·결제·회원 제도 등.
- recency: 지금 시점의 사실이라 최신 확인이 필요한 것. 뉴스·시세·순위·경기 결과·법으로 정해져
  바뀌는 수치 등, 학습된 지식만으로 답하면 틀리기 쉬운 질문이다.
- identity: 어시스턴트 자신에 관한 것. 정체·이름·모델·능력 범위를 묻는 질문이다.
- chitchat: 정보 요구가 없는 발화. 인사·감사·소감·맞장구·작별 등 대화를 잇는 말이다.
- web: 외부 세계에 관한 검증 가능한 사실·지식 질문 중 답의 시점 일치가 필요 없는 것. 안정적인
  상식도 포함한다.
- reasoning: 입력만으로 처리할 생성·변환·계산·아이디어·조언 질문. 외부 사실 조회가 필요 없다.

multistep — 한 번의 검색이나 단일 판단으로 충분하면 false. 여러 대상을 견주거나(비교·선택),
흩어진 근거를 모아 구조화해야 하거나(종합·분석·설명), 감정·상황을 헤아려 취향을 종합해야 하거나,
답하기 전에 **먼저 확인해야 할 전제 사실**이 있으면(어떤 조건을 만족하는 대상을 먼저 특정한 뒤
그 대상에 대해 답해야 하는 경우 — 전제 사실이 최신 확인을 요할수록 더욱) true. 사용자가 답뿐
아니라 특정한 출처 범위·근거 직접성·교차 확인까지 요구해도 true.

confidence — 부류가 명확하면 high, 애매하거나 정보가 부족하면 low.

product_selection — 답의 중심이 취향·상황·여러 조건을 판단해 새 상품 후보를 추천하거나 선택하는
것이면 true다. 저자·작품처럼 명시된 카탈로그 조건으로 검색된 제목 목록만 보여주는 요청, 사용자가
이미 대상을 지정해 사실·상세 내용을 묻는 요청, 지정한 상품들을 설명·비교하는 요청은 false다.
product가 아닌 질문도 false다.

product_detail_required — product_selection=false인 product 질문 중, 지정 상품의 사실을 확정하거나
소개·목차·리뷰 같은 본문을 읽거나 지정 상품들을 근거로 비교해야 하면 true다. 검색 결과의 제목·
저자만으로 그대로 답할 수 있는 단순 카탈로그 목록이면 false다. product_selection=true 또는
product가 아닌 질문도 false다.

requested_product_count — product 질문에서 사용자가 상품 결과 개수를 명시한 경우 그 정확한 양의
정수다. 추천·선택뿐 아니라 단순 카탈로그 목록의 개수도 보존한다. 개수를 명시하지 않았거나 product가
아니면 null이다. 범위·상한 표현을 정확한 개수로 바꾸거나 사용자가 말하지 않은 개수를 추론하지 마라.

product_constraints — product 질문에서 사용자가 숫자로 명시한 상품 조건만 구조화한다. 각 조건은
field(price, rating, page_count), operator(lt, lte, eq, gte, gt), value(숫자) 및 그 조건을
표현한 질문의 정확한 연속 문자열 constraint_text로 쓴다.
가격·평점·쪽수에 관해 사용자가 명시한 숫자 조건은 하나도 빠뜨리지 말고 각각 별도 항목으로 보존한다.
사용자가 직접 말하지 않은 숫자를 추론하거나, 숫자가 아닌 취향 표현을 임의의 수치로 바꾸지 마라.
해당 조건이 없거나 product 질문이 아니면 빈 배열이다.

source_time_required — 사용자가 답변 근거가 관측·발행·갱신·예보된 기준 시점 자체를 답에 함께
밝히라고 명시적으로 요구하면 true다. 최신 정보가 필요한 질문이라는 이유만으로 자동으로 true로
하지 말고, 출처를 확인하거나 서로 대조해 달라는 요구만 있어도 false다. 답의 대상이 현재·오늘의
사실인 것과 근거 메타데이터의 날짜·시각을 출력하라는 것은 서로 다르다. 후자를 함께 제시하라는
요구가 질문에 실제로 있을 때만 true다.

**단어의 표면이 아니라 질문의 의미로 판단한다.** 어떤 낱말이 들어 있다는 이유만으로 부류를
정하지 마라 — 책이라는 글자가 들어 있어도 책을 찾는 질문이 아닐 수 있고, 책 이야기를 하면서도
그저 인사를 건네는 말일 수 있다."""

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "multistep": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "low"]},
        "product_selection": {"type": "boolean"},
        "product_detail_required": {"type": "boolean"},
        "requested_product_count": {
            "type": "integer",
            "minimum": 1,
            "nullable": True,
        },
        "product_constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "enum": [field.value for field in NumericEvidenceField],
                    },
                    "operator": {
                        "type": "string",
                        "enum": [operator.value for operator in ConstraintOperator],
                    },
                    "value": {"type": "number"},
                    "constraint_text": {"type": "string"},
                },
                "required": ["field", "operator", "value", "constraint_text"],
            },
        },
        "source_time_required": {"type": "boolean"},
    },
    "required": [
        "intent",
        "multistep",
        "confidence",
        "product_selection",
        "product_detail_required",
        "requested_product_count",
        "product_constraints",
        "source_time_required",
    ],
}


@dataclass(frozen=True)
class QueryUnderstanding:
    """질의이해 결과. 호출부(runner)는 이 값 타입만 보고 라우팅·게이트를 결정한다."""

    standalone_query: str  # 검색·라우팅 입력으로 쓸 질의(원본)
    intent: str  # INTENTS 중 하나
    multistep: bool  # 다단계 추론이 필요한가(→ pro 라우팅)
    confident: bool
    classification_state: ClassificationState | None = None
    product_constraints: tuple[ProductConstraint, ...] = ()
    product_selection: bool = False
    product_detail_required: bool = False
    requested_product_count: int | None = None
    source_time_required: bool = False

    def __post_init__(self) -> None:
        """기존 confident 입력과 typed 원인 상태를 한 값으로 정합한다."""
        state = self.classification_state
        if state is None:
            state = "confident" if self.confident else "ambiguous"
        object.__setattr__(self, "classification_state", state)
        object.__setattr__(self, "confident", state == "confident")

    @property
    def needs_grounding(self) -> bool:
        """이 턴의 답이 도구 결과에 접지돼야만 참일 수 있는가(인용 무결성 게이트의 intent 조건).

        도구 접지가 정의상 필요한 부류(GROUNDED_INTENTS)이거나 분류를 신뢰할 수 없으면 대상이다.
        runner는 ambiguous의 유효 grounded intent는 pro typed evidence 경계로 전달한다. 특히
        ambiguous product는 하위 selection/detail 판정을 신뢰하지 않고 product typed 경계가 맡는다.
        unavailable만 세션 문맥을 가진 pro 루트로 전달한다.
        """
        return self.intent in GROUNDED_INTENTS or not self.confident


@dataclass(frozen=True)
class EvidencePolicy:
    """질의 의미가 요구하는 최소 출처 부류와 보정 첫 도구."""

    required_source_types: frozenset[str] | None
    force_tool: str | None


def evidence_policy(understanding: QueryUnderstanding) -> EvidencePolicy:
    """유효한 intent를 출처/도구 계약으로 번역하고 분류 불가만 범용 보정으로 둔다."""
    if understanding.classification_state == "unavailable":
        return EvidencePolicy(None, None)
    if understanding.intent == PRODUCT:
        required = (
            PRODUCT_DETAIL_SOURCE_TYPES
            if understanding.product_detail_required
            else PRODUCT_SOURCE_TYPES
        )
        return EvidencePolicy(required, "yes24_search")
    if understanding.intent == POLICY:
        return EvidencePolicy(POLICY_SOURCE_TYPES, "yes24_fetch")
    if understanding.intent in {WEB, RECENCY}:
        return EvidencePolicy(WEB_SOURCE_TYPES, "web_search")
    return EvidencePolicy(None, None)


def fallback(query: str) -> QueryUnderstanding:
    """분류 불가(off·실패·타임아웃·파싱 실패·스키마 위반) 시의 안전 폴백.

    classification_state="unavailable"을 runner가 근거 복구 신호로 사용한다. intent 기본값은
    web이지만 신뢰하지 않으며, multistep=True는 이 값이 단독 사용될 때도 경량 경로로 가지 않게 한다.
    """
    return QueryUnderstanding(
        standalone_query=query,
        intent=WEB,
        multistep=True,
        confident=False,
        classification_state="unavailable",
    )


async def _call_classifier(
    message: str,
    settings: Settings,
    *,
    previous_user_message: str | None = None,
) -> dict | None:
    """분류 모델을 1회 호출해 파싱된 dict를 반환한다(실패·타임아웃·파싱 불가면 None).

    별도 헬퍼로 분리해 classify()의 캐시·검증·폴백 로직을 LLM 없이 단위 테스트한다.
    """
    config = types.GenerateContentConfig(
        system_instruction=_CLASSIFY_SYSTEM,
        thinking_config=types.ThinkingConfig(thinking_budget=settings.flash_thinking_budget),
        response_mime_type="application/json",
        response_schema=_CLASSIFY_SCHEMA,
    )
    try:
        client = get_genai_client()
        contents = (
            json.dumps(
                {
                    "previous_user_message": previous_user_message,
                    "current_message": message,
                },
                ensure_ascii=False,
            )
            if previous_user_message
            else message
        )
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=settings.classifier_model_name,
                contents=contents,
                config=config,
            ),
            timeout=settings.classifier_timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 — 분류 실패는 runner의 fail-close 신호로 흡수한다
        logger.warning("질의 분류 실패: %s", exc)
        return None

    raw = (response.text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("질의 분류 JSON 파싱 실패: %s", exc)
        return None
    return data if isinstance(data, dict) else None


def _interpret(
    data: dict,
    query: str,
) -> tuple[
    str,
    bool,
    ClassificationState,
    tuple[ProductConstraint, ...],
    bool,
    bool,
    int | None,
    bool,
] | None:
    """분류 응답의 intent·multistep·confidence·숫자 조건을 검증한다.

    스키마 밖 값(허용 목록에 없는 intent, 비불리언 multistep)은 신뢰하지 않고 폴백으로 보낸다 —
    "애매하면 무거운 쪽" 정책을 값 검증 층에서도 지킨다.
    """
    intent = data.get("intent")
    multistep = data.get("multistep")
    product_selection = data.get("product_selection")
    product_detail_required = data.get("product_detail_required")
    requested_count = data.get("requested_product_count")
    source_time_required = data.get("source_time_required")
    if (
        intent not in INTENTS
        or not isinstance(multistep, bool)
        or not isinstance(product_selection, bool)
        or not isinstance(product_detail_required, bool)
        or not isinstance(source_time_required, bool)
        or (
            requested_count is not None
            and (
                isinstance(requested_count, bool)
                or not isinstance(requested_count, int)
                or requested_count < 1
            )
        )
    ):
        return None
    raw_constraints = data.get("product_constraints", [])
    if not isinstance(raw_constraints, list):
        return None
    constraints: list[ProductConstraint] = []
    seen_constraints: set[tuple[str, NumericEvidenceField, ConstraintOperator, int | float]] = (
        set()
    )
    try:
        for raw in raw_constraints:
            if not isinstance(raw, dict):
                return None
            constraint_text = raw.get("constraint_text")
            if not isinstance(constraint_text, str):
                return None
            constraint_text = constraint_text.strip()
            if not constraint_text or constraint_text not in query:
                return None
            constraint = ProductConstraint.model_validate(
                {key: raw.get(key) for key in ("field", "operator", "value")}
            )
            identity = (
                constraint_text,
                constraint.field,
                constraint.operator,
                constraint.value,
            )
            if identity in seen_constraints:
                return None
            seen_constraints.add(identity)
            constraints.append(constraint)
    except (TypeError, ValueError):
        return None
    constraints_tuple = tuple(constraints)
    if intent != PRODUCT and (
        constraints_tuple
        or product_selection
        or product_detail_required
        or requested_count is not None
    ):
        return None
    if product_selection and product_detail_required:
        return None
    state: ClassificationState = (
        "confident" if data.get("confidence") == _HIGH else "ambiguous"
    )
    return (
        intent,
        multistep,
        state,
        constraints_tuple,
        product_selection,
        product_detail_required,
        requested_count,
        source_time_required,
    )


async def classify(
    message: str,
    settings: Settings,
    previous_user_message: str | None = None,
) -> QueryUnderstanding:
    """마지막 사용자 발화를 값싼 모델 1회로 분류하고 실패하면 안전 폴백한다."""
    query = (message or "").strip()
    if not query or not settings.query_classifier:
        return fallback(message or "")

    started = time.perf_counter()
    data = (
        await _call_classifier(query, settings)
        if not previous_user_message
        else await _call_classifier(
            query,
            settings,
            previous_user_message=previous_user_message,
        )
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    interpreted = _interpret(data, query) if data else None
    if interpreted is None:
        return fallback(message)
    logger.info(
        "질의 분류: intent=%s multistep=%s state=%s constraints=%d selection=%s "
        "detail=%s count=%s source_time=%s (%.0fms)",
        interpreted[0],
        interpreted[1],
        interpreted[2],
        len(interpreted[3]),
        interpreted[4],
        interpreted[5],
        interpreted[6],
        interpreted[7],
        elapsed_ms,
    )
    (
        intent,
        multistep,
        classification_state,
        product_constraints,
        product_selection,
        product_detail_required,
        requested_product_count,
        source_time_required,
    ) = interpreted
    return QueryUnderstanding(
        standalone_query=message,
        intent=intent,
        multistep=multistep,
        confident=classification_state == "confident",
        classification_state=classification_state,
        product_constraints=product_constraints,
        product_selection=product_selection,
        product_detail_required=product_detail_required,
        requested_product_count=requested_product_count,
        source_time_required=source_time_required,
    )


async def understand(
    message: str,
    settings: Settings,
    previous_user_message: str | None = None,
) -> QueryUnderstanding:
    """질의이해: 값싼 모델 1회로 의미(intent·multistep·confidence)를 분류한다.

    분류 장애와 저확신은 각각 unavailable·ambiguous 상태로 반환한다. runner는 ambiguous의 유효
    grounded intent는 pro typed evidence 경계로 보내며 product 하위 판정은 신뢰하지 않는다.
    unavailable만 pro 루트에 원 질문 그대로 전달한다.
    """
    return await classify(message, settings, previous_user_message)
