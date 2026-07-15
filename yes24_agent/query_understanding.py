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

폴백은 항상 **안전한 쪽**이다: 분류가 실패·타임아웃·저확신이면 multistep=True(→pro)이고
게이트 적용 대상(product_gate_eligible)이 된다. 이 경로에 키워드를 되살리지 않는다 — 모르면
무거운 쪽으로 가면 충분하고, 잘못 발동한 게이트의 비용은 비파괴 재검색 지연뿐이다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from google.genai import types

from yes24_agent.config import Settings, get_genai_client

logger = logging.getLogger(__name__)

# intent 부류(정규 목록). 프롬프트의 부류 정의와 1:1 대응한다.
PRODUCT = "product"  # 책·상품 자체(찾기·추천·재고·가격·구매·평점) — Yes24 검색의 강점 영역
POLICY = "policy"  # 쇼핑몰 이용 규정·혜택(주문·배송·반품·결제·회원)
RECENCY = "recency"  # 지금 시점의 사실이라 최신 확인이 필요한 질의
IDENTITY = "identity"  # 어시스턴트 자신·능력 범위를 묻는 메타 질의
CHITCHAT = "chitchat"  # 인사·감사·소감·잡담 — 정보 요구 없음
WEB = "web"  # 그 외 일반 지식·정보 질의(기본값)

INTENTS = (PRODUCT, POLICY, RECENCY, IDENTITY, CHITCHAT, WEB)

# **도구 접지가 정의상 필요한** 부류. 이 부류의 답은 도구 결과에 근거해야만 참일 수 있다 —
# 상품 사실(재고·가격·목록)·이용 규정·지금 시점의 사실은 학습 지식으로 답하면 틀린다. 그래서
# 이 턴이 인용[n] 없이 끝나면 근거가 없다는 뜻이고(미완결), 재확인이 항상 정답이다. 반대로
# 잡담·정체성·일반지식(web)은 도구 없이 답하는 것이 정상이라 이 조건에서 원천 배제된다.
GROUNDED_INTENTS = frozenset({PRODUCT, POLICY, RECENCY})

_HIGH = "high"

# 분류 프롬프트 — **부류의 정의**만 서술한다(키워드 나열 금지). 표면 문자열이 아니라 질문의
# 의미로 판정하라고 명시해, 합성어·부분일치로 부류가 갈리던 실패를 원천 차단한다.
_CLASSIFY_SYSTEM = """너는 온라인 서점(Yes24) 어시스턴트의 질의 분류기다.
사용자의 마지막 질문 하나를 읽고 아래 세 값을 JSON으로만 답한다.

intent — 질문이 **무엇에 관한 것인지** 하나만 고른다:
- product: 책·상품 자체에 관한 것. 찾기·추천·재고·가격·구매·평점·리뷰 등 쇼핑 행위가 목적이다.
- policy: 그 쇼핑몰을 이용하는 규정·혜택에 관한 것. 주문·배송·반품·교환·결제·회원 제도 등.
- recency: 지금 시점의 사실이라 최신 확인이 필요한 것. 뉴스·시세·순위·경기 결과·법으로 정해져
  바뀌는 수치 등, 학습된 지식만으로 답하면 틀리기 쉬운 질문이다.
- identity: 어시스턴트 자신에 관한 것. 정체·이름·모델·능력 범위를 묻는 질문이다.
- chitchat: 정보 요구가 없는 발화. 인사·감사·소감·맞장구·작별 등 대화를 잇는 말이다.
- web: 위 어디에도 해당하지 않는 일반 지식·정보·의견 질문.

multistep — 한 번의 검색이나 단일 판단으로 충분하면 false. 여러 대상을 견주거나(비교·선택),
흩어진 근거를 모아 구조화해야 하거나(종합·분석·설명), 감정·상황을 헤아려 취향을 종합해야 하면
true.

confidence — 부류가 명확하면 high, 애매하거나 정보가 부족하면 low.

**단어의 표면이 아니라 질문의 의미로 판단한다.** 어떤 낱말이 들어 있다는 이유만으로 부류를
정하지 마라 — 책이라는 글자가 들어 있어도 책을 찾는 질문이 아닐 수 있고, 책 이야기를 하면서도
그저 인사를 건네는 말일 수 있다."""

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "multistep": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "low"]},
    },
    "required": ["intent", "multistep", "confidence"],
}


@dataclass(frozen=True)
class QueryUnderstanding:
    """질의이해 결과. 호출부(runner)는 이 값 타입만 보고 라우팅·게이트를 결정한다."""

    standalone_query: str  # 검색·라우팅 입력으로 쓸 질의(대명사 해소 후 또는 원본)
    intent: str  # INTENTS 중 하나
    multistep: bool  # 다단계 추론이 필요한가(→ pro 라우팅)
    confident: bool  # 분류가 확실한가(실패·타임아웃·저확신이면 False)
    rewritten: bool  # standalone 재작성이 실제로 일어났는지(로깅·A/B용)

    @property
    def needs_grounding(self) -> bool:
        """이 턴의 답이 도구 결과에 접지돼야만 참일 수 있는가(인용 무결성 게이트의 intent 조건).

        도구 접지가 정의상 필요한 부류(GROUNDED_INTENTS)이거나, **분류를 신뢰할 수 없으면**
        대상이다(안전한 쪽). 오발동의 대가는 재확인 지연뿐이고, 미발동의 대가는 무접지 사실
        유출이다. 상품뿐 아니라 정책·시의성도 포함한다 — "하루키 신작 나왔어?"처럼 상품 질문이
        시의성으로 분류돼도 근거 없는 답이 새지 않는다(부류가 아니라 접지 필요성이 기준).
        """
        return self.intent in GROUNDED_INTENTS or not self.confident


def fallback(query: str) -> QueryUnderstanding:
    """분류 불가(off·실패·타임아웃·파싱 실패·스키마 위반) 시의 안전 폴백.

    multistep=True → pro 라우팅(정확성 우선), confident=False → 게이트 적용. intent 기본값은
    web이지만 product_gate_eligible이 confident=False로 이미 True라 게이트를 우회하지 않는다.
    """
    return QueryUnderstanding(
        standalone_query=query,
        intent=WEB,
        multistep=True,
        confident=False,
        rewritten=False,
    )


# 분류 결과 캐시(프로세스 메모리). 같은 문자열 질의는 같은 부류라 재호출이 낭비다 — 재시도·
# 새로고침·같은 세션 반복 질의에서 분류 지연 0. LRU로 상한(config)만 지킨다.
_CACHE: OrderedDict[str, tuple[str, bool, bool]] = OrderedDict()


def _cache_get(key: str) -> tuple[str, bool, bool] | None:
    value = _CACHE.get(key)
    if value is not None:
        _CACHE.move_to_end(key)
    return value


def _cache_put(key: str, value: tuple[str, bool, bool], max_size: int) -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > max_size:
        _CACHE.popitem(last=False)


def reset_cache() -> None:
    """분류 캐시를 비운다(테스트 격리용)."""
    _CACHE.clear()


async def _call_classifier(message: str, settings: Settings) -> dict | None:
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
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=settings.classifier_model_name,
                contents=message,
                config=config,
            ),
            timeout=settings.classifier_timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 — 분류 실패는 안전 폴백(pro+게이트)으로 흡수한다
        logger.warning("질의 분류 실패(안전 폴백: pro+게이트): %s", exc)
        return None

    raw = (response.text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("질의 분류 JSON 파싱 실패(안전 폴백): %s", exc)
        return None
    return data if isinstance(data, dict) else None


def _interpret(data: dict) -> tuple[str, bool, bool] | None:
    """분류 응답 dict를 (intent, multistep, confident)로 검증·해석한다(부적합하면 None).

    스키마 밖 값(허용 목록에 없는 intent, 비불리언 multistep)은 신뢰하지 않고 폴백으로 보낸다 —
    "애매하면 무거운 쪽" 정책을 값 검증 층에서도 지킨다.
    """
    intent = data.get("intent")
    multistep = data.get("multistep")
    if intent not in INTENTS or not isinstance(multistep, bool):
        return None
    return intent, multistep, data.get("confidence") == _HIGH


async def classify(message: str, settings: Settings) -> QueryUnderstanding:
    """질의 1건을 분류한다(캐시 → 값싼 모델 1회 → 값 검증 → 안전 폴백).

    분류기가 off이거나 빈 질의면 곧장 폴백(pro + 게이트 적용)한다. 확신 결과만 캐시에 담아 같은
    문자열의 재분류를 없앤다(저확신은 다음 기회에 다시 판정한다).
    """
    query = (message or "").strip()
    if not query or not settings.query_classifier:
        return fallback(message or "")

    cached = _cache_get(query)
    if cached is None:
        started = time.perf_counter()
        data = await _call_classifier(query, settings)
        elapsed_ms = (time.perf_counter() - started) * 1000
        interpreted = _interpret(data) if data else None
        if interpreted is None:
            return fallback(message)
        logger.info(
            "질의 분류: intent=%s multistep=%s confident=%s (%.0fms)",
            *interpreted,
            elapsed_ms,
        )
        if interpreted[2]:
            _cache_put(query, interpreted, settings.classifier_cache_size)
        cached = interpreted

    intent, multistep, confident = cached
    return QueryUnderstanding(
        standalone_query=message,
        intent=intent,
        multistep=multistep,
        confident=confident,
        rewritten=False,
    )


# ── 조건부 standalone 재작성 (멀티턴 대명사·생략 해소) ────────────────────────
# 앞 맥락에 기대는 불완전 질의를 그 자체로 완결된 검색 질의로 푼다. 리스크 관리가 핵심이라
# **게이트를 좁게**(고정밀 신호만) 두고, 재작성이 조금이라도 수상하면 원본으로 fallback한다.
# 게이트를 통과 못 하는 단일턴·명시 질의는 원본 그대로라 기존 동작과 바이트 단위 동일하다.

# 앞 턴을 되가리키는 지시대명사·생략 신호(고정밀만). "그/저/이 + 대상"류와 시점 참조어.
# 정밀도 우선 — 이 신호가 있어도 직전 턴이 없으면 재작성하지 않는다. 순수 생략("얼마야?"처럼
# 주어 없는 속성질문)은 오탐 위험이 커 의도적으로 제외한다(놓쳐도 원본 fallback=무해).
_ANAPHORA = (
    "그 책", "그책", "그 작가", "그작가", "그 저자", "그저자", "그 소설", "그 시리즈",
    "그거", "그걸", "그것", "그건", "그게", "그 상품", "그 제품",
    "저 책", "저거", "저것", "저 상품",
    "이 책", "이거", "이걸", "이것", "이 상품", "이 제품",
    "위에", "위의", "방금", "아까", "앞에서", "앞의", "걔", "얘",
)

_REWRITE_PROMPT = """다음은 사용자와 어시스턴트의 대화다. 마지막 사용자 질문을 앞 맥락의
지시대명사·생략을 풀어 **그 자체로 완결된 검색 질의**로 다시 써라.

규칙:
- 대명사("그 책", "이거", "그 작가" 등)가 가리키는 대상을 앞 맥락에서 찾아 명시로 치환한다.
- 새로운 정보·조건을 추가하지 말고, 질문의 의도·범위를 바꾸지 마라.
- 이미 그 자체로 완결된 질문이면 원문을 그대로 반환한다.
- 설명 없이 다시 쓴 질의 한 줄만 출력한다.

[대화]
{context}

[마지막 사용자 질문]
{message}

[완결된 검색 질의]"""


def needs_standalone_rewrite(message: str, has_history: bool) -> bool:
    """재작성 게이트(순수, 부수효과 없음): 직전 턴이 있고 대명사/생략 신호가 있을 때만 True.

    둘 다 충족해야 한다 — 단일턴(has_history=False)이나 명시 질의(신호 없음)는 건드리지
    않는다(오작동 0 우선). 이 게이트가 False면 호출부는 LLM을 아예 부르지 않고 원본을 쓴다.
    """
    if not has_history or not message:
        return False
    text = message.strip()
    return any(kw in text for kw in _ANAPHORA)


def _format_history(history: list[types.Content], max_turns: int) -> str:
    """세션 히스토리(최근 max_turns턴)를 재작성 프롬프트용 텍스트로 조립한다."""
    lines: list[str] = []
    for content in history[-max_turns:]:
        parts = getattr(content, "parts", None) or []
        text = " ".join(p.text for p in parts if getattr(p, "text", None)).strip()
        if not text:
            continue
        speaker = "사용자" if getattr(content, "role", "") == "user" else "어시스턴트"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def _is_safe_rewrite(original: str, rewritten: str) -> bool:
    """재작성 결과가 원본을 대체해도 안전한지 검사(수상하면 False → 원본 fallback).

    빈 문자열·원본과 동일·과도 팽창(환각/장황)을 거른다. 애매하면 안 쓴다(보수적).
    """
    candidate = rewritten.strip()
    if not candidate or candidate == original.strip():
        return False
    # 대명사 해소는 몇 단어 늘어나는 정도다. 원본 대비 과도하게 길면 새 정보를 지어냈다고 보고
    # 버린다(길이 상한은 이 규칙과 함께 두는 지역 튜닝 상수).
    return len(candidate) <= len(original.strip()) * 4 + 40


async def _call_flash_rewrite(prompt: str, settings: Settings) -> str:
    """좁은 flash 1회로 재작성 질의를 받는다(thinking=0, 타임아웃 하드). 실패 시 예외 전파.

    별도 헬퍼로 분리해 understand()의 게이트·검증·fallback 로직을 LLM 없이 단위 테스트한다.
    """
    from google import genai  # 지연 import — 재작성 비활성/미발동 경로에 부담을 주지 않는다.

    client = genai.Client()
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=settings.flash_model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(
                    thinking_budget=settings.flash_thinking_budget
                )
            ),
        ),
        timeout=settings.standalone_rewrite_timeout_s,
    )
    return response.text or ""


async def understand(
    message: str,
    history: list[types.Content],
    settings: Settings,
) -> QueryUnderstanding:
    """질의이해: 의미 분류(항상) + 조건부 standalone 재작성.

    분류(classify)는 값싼 모델 1회로 intent·multistep·confidence를 얻고, 실패·저확신이면 안전한
    폴백(pro + 게이트 적용)으로 떨어진다. 재작성은 (1) settings.standalone_rewrite가 켜져 있고
    (2) 게이트(직전 턴+대명사 신호)를 통과할 때만 수행하며, 실패·수상하면 원본으로 회귀한다.
    분류는 **원본 질의** 기준이다 — 재작성 규칙이 의도·범위 보존이라 부류가 바뀌지 않는다.
    """
    understanding = await classify(message, settings)

    if not settings.standalone_rewrite:
        return understanding
    if not needs_standalone_rewrite(message, has_history=bool(history)):
        return understanding

    context = _format_history(history, settings.standalone_rewrite_history_turns)
    if not context:  # 참조할 실질 맥락이 없으면 재작성 의미 없음
        return understanding

    prompt = _REWRITE_PROMPT.format(context=context, message=message.strip())
    try:
        candidate = await _call_flash_rewrite(prompt, settings)
    except Exception as exc:  # noqa: BLE001 — 재작성은 부가기능, 어떤 실패도 원본으로 안전 회귀
        logger.warning("standalone 재작성 실패, 원본 사용: %s", exc)
        return understanding

    if not _is_safe_rewrite(message, candidate):
        logger.info("standalone 재작성 폐기(빈결과·무변화·과팽창), 원본 사용")
        return understanding

    rewritten_query = candidate.strip()
    logger.info("standalone 재작성: %r → %r", message, rewritten_query)
    return QueryUnderstanding(
        standalone_query=rewritten_query,
        intent=understanding.intent,
        multistep=understanding.multistep,
        confident=understanding.confident,
        rewritten=True,
    )
