"""턴 부가 정보(meta) 추출 — 추천 이유·세션 제목 (best-effort · 비파괴).

답변 본문과 별도의 경량 서브콜로 "프론트가 구조로 쓰는 부가 정보"를 뽑는다: 답변이
책·상품을 추천했다면 **추천 항목 단위**의 이유(카드용 한 줄 — 출처 나열이 아니다),
세션에 아직 제목이 없다면 대화 제목. `thought_translation.py`와 같은 경량 유틸
계열이다(전용 config 필드, 실패 시 폴백).

계약:
- **done 직전 채널**: 이 추출은 최종 본문이 다 흐른 뒤, `done` 프레임 **직전**에 불린다
  (crema-ai와 같은 배치 — 2026-08-20 사용자 결정, 스트림은 항상 done으로 끝난다).
  부가 채널 실패가 정상 답변의 마감을 오염시키면 안 되므로, 이 모듈은 **어떤 예외도
  던지지 않고** None으로 접는다(warning 로그만).
- **창작 금지의 이중 방어**: 프롬프트가 "본문에 서술된 이유만"을 지시하고, 출구 검증이
  인용 출처 집합 밖의 id·빈 이유를 코드로 걸러낸다(원칙 4의 부가 채널판 — 마커 검증이
  본문에 하는 일을 여기서는 id 대조가 한다).
- 추출 결과가 전부 비면 None — 호출부가 meta 이벤트 자체를 내지 않는다(빈 프레임 금지).
"""

import asyncio
import json
import logging
import time

from google.genai import types

from yes24_agent.config import get_genai_client, get_settings

logger = logging.getLogger(__name__)

# 세션 제목의 세션 state 키. runner가 턴 시작에 존재 여부를 보고(want_title) 추출 후 영속한다.
SESSION_TITLE_STATE_KEY = "session_title"

# 추출기는 판정하지 않는다 — 본문에 이미 서술된 것을 구조로 옮기기만 한다(창작 금지).
# 사례 열거 없이 일반 원칙만 둔다(no-case-patch). 단위는 **출처가 아니라 추천 항목**이다 —
# "인용된 출처별 요약"으로 지시하면 결과가 출처 인용 근거 나열이 된다(2026-08-20 실측:
# 분권 상·하가 같은 이유로 중복 등재, 이유가 권유가 아니라 책 소개). 권유 여부는 문구로
# 부탁하지 않고 스키마의 필수 선행 필드(is_recommendation)로 판정을 강제한다 — 배열만 두면
# 구조화 출력이 "칸을 채우는" 쪽으로 쏠려 정보 조회 턴에도 판본 설명을 담았다(같은 날 실측).
_EXTRACT_INSTRUCTION = (
    "AI 어시스턴트의 답변에서 부가 정보를 구조화한다. "
    "먼저 is_recommendation을 판정한다: 답변이 사용자에게 책·상품을 골라 권하는 내용인가. "
    "권유가 아니면(정보 확인·설명 등) recommendations는 빈 배열이다. "
    "권유라면 답변이 권한 항목만 담는다 — 인용된 출처를 전부 옮기는 것이 아니다. "
    "항목의 reason은 카드에 실릴 한 줄 카피다: 답변 본문에 서술된 '왜 이것을 권하는가'의 "
    "핵심을 서점 POP 문구처럼 20~40자로 압축한다. '~합니다' 같은 서술형 존댓말 문장이 "
    "아니라 '○○을 △△하게 그린 ◇◇' 꼴처럼 명사형으로 끝맺는 결이다(꼴만 따르고 "
    "내용은 전부 답변 본문에서 가져온다). "
    "책의 매력과 질문에 맞는 이유에 집중하고, 줄거리 나열이나 본문에 없는 이유 창작, "
    "검색·출처·시스템 동작 언급은 하지 않는다. "
    "한 항목이 여러 출처로 인용됐으면 대표 출처 id 하나만 쓴다. "
    "session_title 필드가 있으면 이 대화의 주제를 나타내는 15자 내외의 한국어 명사구 제목을 쓴다."
)


def _response_schema(want_title: bool) -> types.Schema:
    """구조화 출력 스키마. session_title은 필요할 때만 **스키마에 존재**한다.

    제목이 이미 있는 세션에서 "제목을 만들지 마라"를 프롬프트 문구로 부탁하는 대신
    필드 자체를 스키마에서 빼 생성을 구조로 차단한다(문구보다 구조 원칙).
    """
    properties: dict[str, types.Schema] = {
        # 선행 판정 필드 — 프롬프트 문구 대신 스키마가 "권유인가"의 명시 판정을 강제한다.
        # 출구(_validated_meta)가 false면 recommendations를 통째로 버린다.
        "is_recommendation": types.Schema(type=types.Type.BOOLEAN),
        "recommendations": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "id": types.Schema(type=types.Type.INTEGER),
                    "reason": types.Schema(type=types.Type.STRING),
                },
                required=["id", "reason"],
            ),
        ),
    }
    required = ["is_recommendation", "recommendations"]
    if want_title:
        properties["session_title"] = types.Schema(type=types.Type.STRING)
        required.append("session_title")
    return types.Schema(
        type=types.Type.OBJECT,
        properties=properties,
        required=required,
        # "판정이 항목보다 먼저"는 이 명시 순서가 보장한다 — dict 삽입 순서는 직렬화
        # 구현 몫이라 계약이 아니다. 판정을 먼저 쓰게 해야 항목 나열이 판정을 따른다.
        property_ordering=required,
    )


def _clip_title(title: str, max_chars: int) -> str:
    """제목을 공백 정규화 후 상한으로 절단한다(부가 표시용이라 단순 절단으로 충분)."""
    normalized = " ".join(title.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "…"


def _validated_meta(raw: dict, cited_ids: list[int], want_title: bool) -> dict | None:
    """모델 출력의 출구 검증 — 인용 집합 밖 id·빈 이유·중복을 버리고, 전부 비면 None.

    cited_ids는 **본문 인용 등장 순서**다(done.sources 순서 그대로). 추천도 그 순서로
    정렬하고 상한(enrichment_max_recommendations)을 넘는 꼬리는 버린다 — 프론트 카드
    줄과 어긋나지 않는 정렬·폭 규약(crema-ai 카드 계약 이식, 2026-08-20).
    """
    settings = get_settings()
    order = {source_id: rank for rank, source_id in enumerate(cited_ids)}
    by_id: dict[int, dict] = {}
    dropped: list[object] = []
    # 권유 답변이 아니라고 스스로 판정했으면 항목을 보지도 않는다 — 정보 조회 턴의 판본
    # 설명이 추천으로 새던 경로(2026-08-20 실측)를 출구에서 구조로 닫는다.
    items = raw.get("recommendations") or [] if raw.get("is_recommendation") else []
    for item in items:
        source_id = item.get("id") if isinstance(item, dict) else None
        reason = " ".join(str(item.get("reason") or "").split()) if isinstance(item, dict) else ""
        if source_id not in order or not reason or source_id in by_id:
            dropped.append(source_id)
            continue
        by_id[source_id] = {"id": source_id, "reason": reason}
    if dropped:
        logger.warning(f"meta 추천에서 인용 밖 id·빈 이유·중복 {len(dropped)}건 제거: {dropped}")
    recommendations = sorted(by_id.values(), key=lambda r: order[r["id"]])
    if len(recommendations) > settings.enrichment_max_recommendations:
        logger.warning(
            f"meta 추천 {len(recommendations)}건이 상한을 넘어 "
            f"{settings.enrichment_max_recommendations}건으로 자릅니다."
        )
        recommendations = recommendations[: settings.enrichment_max_recommendations]

    meta: dict = {}
    if recommendations:
        meta["recommendations"] = recommendations
    if want_title:
        title = _clip_title(str(raw.get("session_title") or ""), settings.session_title_max_chars)
        if title:
            meta["session_title"] = title
    return meta or None


async def extract_turn_meta(
    message: str,
    answer_text: str,
    cited_sources: list[dict],
    *,
    want_title: bool,
) -> dict | None:
    """최종 본문·인용 출처에서 부가 정보를 뽑는다. 실패·빈 결과는 None(예외 전파 없음).

    cited_sources는 done.sources의 공개 DTO다 — 추천 id는 본문 마커·출처 카드와 같은
    공개 표시 번호 공간을 쓴다. 호출 조건(추출할 재료가 있는가)은 호출부가 판단한다.
    """
    settings = get_settings()
    if not settings.enrichment_model:
        return None
    started = time.monotonic()
    try:
        # 부가 채널이라 상품 사실을 재주입하지 않는다 — id 대조에 필요한 최소 식별 정보만.
        payload = {
            "question": message,
            "answer": answer_text,
            "cited_sources": [
                {"id": s.get("id"), "title": s.get("title"), "type": s.get("type")}
                for s in cited_sources
            ],
        }
        response = await asyncio.wait_for(
            get_genai_client().aio.models.generate_content(
                model=settings.enrichment_model,
                contents=json.dumps(payload, ensure_ascii=False),
                config=types.GenerateContentConfig(
                    system_instruction=_EXTRACT_INSTRUCTION,
                    temperature=0,
                    max_output_tokens=settings.enrichment_max_output_tokens,
                    response_mime_type="application/json",
                    response_schema=_response_schema(want_title),
                ),
            ),
            timeout=settings.enrichment_timeout_s,
        )
        raw = json.loads(response.text or "{}")
        # done.sources 순서 = 본문 인용 등장 순서 — 출구 정렬의 기준이 된다.
        cited_ids = [s.get("id") for s in cited_sources if s.get("id") is not None]
        meta = _validated_meta(raw, cited_ids, want_title)
        # done 지연 비용의 관측 지점 — 추출이 done 직전에 실행되므로 이 시간만큼 done이 늦는다.
        logger.info(
            f"턴 meta 추출: recommendations={len((meta or {}).get('recommendations', []))} "
            f"title={bool((meta or {}).get('session_title'))} "
            f"elapsed={time.monotonic() - started:.2f}s"
        )
        return meta
    except Exception as exc:  # noqa: BLE001 — 부가 채널: 어떤 실패도 None으로 접는다(마감 무오염)
        logger.warning(f"턴 meta 추출 실패(생략): {exc}")
        return None
