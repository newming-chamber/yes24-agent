"""질의이해 — 룰 기반 intent(질의 주제) 분류 계층.

이 질의가 **무엇에 관한 것인지**(상품/도서·웹정보·시의성·정체성·잡담)를 결정론 룰로
분류한다. `routing.classify_complexity`가 판정하는 **난도**(flash/pro)와는 직교하는 축이다
— complexity는 "얼마나 깊게 답할지", intent는 "무엇에 관한 질의인지". 둘 다 LLM·네트워크
없이 도는 순수 분류기이며, 겹치는 부류(정체성·시의성)는 이 모듈이 `routing`의 키워드
버킷을 **재사용**해 중복·모순을 만들지 않는다(architecture-blueprint.md §1 "routing.py의
기존 키워드 버킷 재사용").

**범위 주의(과설계 경계)**: 블루프린트 P3 데이터 흐름에서 intent는 라우팅·게이트·프롬프트
어디에도 연결되지 않는다 — Router는 `standalone_query`(P4)를 입력으로 쓰고, 충분성 게이트는
`observed_tool_calls`/`needs_followup`을 쓴다. 블루프린트도 intent를 `rewritten`과 함께
"로깅·A/B용"으로만 명시한다(§1 인터페이스 스케치 주석). 따라서 P3의 intent는 **관측 전용**
(runner가 라우팅 로그 옆에 함께 기록해 flash/pro 분포와 질의 유형의 상관을 관찰)이고,
실제 라우팅·게이트 결정을 바꾸지 않는다. 실사용에서 특정 intent가 게이트·프롬프트에 가치를
준다는 근거가 관측되면 그때 연결을 확대한다(needs_followup이 P2 전까지 죽은 힌트였던 전철
회피 — 쓰이지 않을 분류를 미리 정교화하지 않는다).

intent 분류(`classify_intent`)는 `routing`·`sufficiency_gate`와 동일하게 config·ADK를 import하지
않는 순수 함수다. 조건부 standalone 재작성(`understand`, 아래)만 좁은 LLM을 쓰며, 그마저도
게이트를 통과한 극소수 질의에서만 호출된다(대다수 질의는 원본 그대로 통과 — 추가 지연 0).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from google.genai import types

from yes24_agent.config import Settings
from yes24_agent.routing import _IDENTITY_META, _REALTIME_FACTS, _RECENCY

logger = logging.getLogger(__name__)

# intent 부류(블루프린트 §1 인터페이스 스케치의 정규 목록).
PRODUCT = "product"  # 책·상품·쇼핑 — Yes24 실시간 검색으로 답하는 강점 영역
WEB = "web"  # 그 외 일반 정보 — web_search로 답하는 범용 질의(기본값)
CHITCHAT = "chitchat"  # 인사·감사·잡담 — 도구 없이 즉답
RECENCY = "recency"  # 시의성·실시간 사실 — 최신 사실을 도구로 확인해야 하는 질의
IDENTITY = "identity"  # 어시스턴트 자신·능력 범위를 묻는 메타 질의

# ── intent 신호 버킷 ─────────────────────────────────────────────────────────
# 정체성(IDENTITY)·시의성(RECENCY)은 routing의 난도 버킷과 같은 부류라 그대로 재사용한다
# (import). 아래 두 버킷(PRODUCT·CHITCHAT)은 routing이 인코딩하지 않는 주제 축이라 여기서만
# 정의한다 — 관측 전용이므로 정밀 taxonomy가 아니라 대표 신호만 lean하게 둔다.

# (product) 책·상품·쇼핑 신호. Yes24 도메인의 도서/상품 명사 + 재고·가격·구매 동사.
_PRODUCT = (
    "책", "도서", "소설", "시집", "에세이", "만화", "웹툰", "전자책", "ebook", "이북",
    "저자", "작가", "출판사", "베스트셀러", "베셀", "신간", "출간", "절판",
    "재고", "있어", "있나", "있을까", "품절",
    "얼마", "가격", "정가", "할인", "구매", "구입", "주문", "배송", "장바구니",
    "평점", "별점", "리뷰", "목차",
)
# (chitchat) 인사·감사·가벼운 잡담. 정보 의도(product/recency/identity)가 없을 때만 도달한다.
_CHITCHAT = (
    "안녕", "반가", "반갑", "고마워", "고마웠", "고맙", "감사", "수고",
    "잘 지내", "잘지내", "좋은 아침", "굿모닝", "잘 자", "잘자", "바이바이",
    "ㅋㅋ", "ㅎㅎ", "hello", "hi ", "thanks", "thank you",
)


def classify_intent(message: str) -> str:
    """질의 1건을 intent 부류로 분류한다(결정론, 부수효과 없음).

    가장 구체적인 부류부터 first-match로 판정한다: 정체성 → 시의성 → 상품 → 잡담 → 웹(기본).
    이 순서는 겹치는 질의("안녕, 채식주의자 있어?"는 잡담 인사가 있어도 상품 조회가
    본의도)를 정보 intent 우선으로 잡기 위한 것이다. 관측 전용이라 부류 간 경합의 정확한
    타이브레이크가 결정을 바꾸지는 않는다(runner는 이 값을 로깅만 한다).
    """
    text = message.strip() if message else ""
    if not text:
        return CHITCHAT
    lowered = text.lower()

    def _hit(keywords: tuple[str, ...]) -> bool:
        # 한글 키워드는 대소문자 무관, 라틴('hi ' 등)은 lowered에서 매칭(routing과 동일 관례).
        return any(kw in text or kw in lowered for kw in keywords)

    if _hit(_IDENTITY_META):
        return IDENTITY
    if _hit(_RECENCY) or _hit(_REALTIME_FACTS):
        return RECENCY
    if _hit(_PRODUCT):
        return PRODUCT
    if _hit(_CHITCHAT):
        return CHITCHAT
    return WEB


# ── 조건부 standalone 재작성 (멀티턴 대명사·생략 해소) ────────────────────────
# 앞 맥락에 기대는 불완전 질의를 그 자체로 완결된 검색 질의로 푼다. 리스크 관리가 핵심이라
# **게이트를 좁게**(고정밀 신호만) 두고, 재작성이 조금이라도 수상하면 원본으로 fallback한다.
# 게이트를 통과 못 하는 단일턴·명시 질의는 원본 그대로라 기존 동작과 바이트 단위 동일하다.


@dataclass(frozen=True)
class QueryUnderstanding:
    """질의이해 결과. 재작성이 안 걸리면 standalone_query는 원본과 동일하다."""

    standalone_query: str  # 검색·라우팅 입력으로 쓸 질의(대명사 해소 후 또는 원본)
    intent: str  # classify_intent 결과(관측용)
    rewritten: bool  # 재작성이 실제로 일어났는지(로깅·A/B용)


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
    # 버린다(길이 상한은 이 규칙과 함께 두는 지역 튜닝 상수, routing._LONG_QUERY_CHARS 관례).
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
    """질의이해: intent 분류(항상) + 조건부 standalone 재작성.

    재작성은 (1) settings.standalone_rewrite가 켜져 있고 (2) 게이트(직전 턴+대명사 신호)를
    통과할 때만 좁은 flash 1회로 수행한다. 그 외에는 원본을 그대로 standalone_query로 둔다
    (추가 LLM 호출 0 — 대다수 질의). 재작성이 실패·빈결과·수상하면 원본으로 fallback해
    엉뚱한 검색을 유발하지 않는다(오작동 0 우선). intent는 항상 원본 질의로 분류한다(관측용).
    """
    intent = classify_intent(message)
    default = QueryUnderstanding(standalone_query=message, intent=intent, rewritten=False)

    if not settings.standalone_rewrite:
        return default
    if not needs_standalone_rewrite(message, has_history=bool(history)):
        return default

    context = _format_history(history, settings.standalone_rewrite_history_turns)
    if not context:  # 참조할 실질 맥락이 없으면 재작성 의미 없음
        return default

    prompt = _REWRITE_PROMPT.format(context=context, message=message.strip())
    try:
        candidate = await _call_flash_rewrite(prompt, settings)
    except Exception as exc:  # noqa: BLE001 — 재작성은 부가기능, 어떤 실패도 원본으로 안전 회귀
        logger.warning("standalone 재작성 실패, 원본 사용: %s", exc)
        return default

    if not _is_safe_rewrite(message, candidate):
        logger.info("standalone 재작성 폐기(빈결과·무변화·과팽창), 원본 사용")
        return default

    rewritten_query = candidate.strip()
    logger.info("standalone 재작성: %r → %r", message, rewritten_query)
    return QueryUnderstanding(
        standalone_query=rewritten_query, intent=intent, rewritten=True
    )
