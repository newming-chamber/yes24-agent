"""어드민 분석의 비용 추정 — usage_log 집계 행에 단가를 붙이고 합산하는 순수 함수(DB 없음).

핵심 정의(화면 각주의 근거 — 상세는 docs/admin-operations.md §7, docs는 저장소 밖):
- 금액 = 사용량(토큰) × 표준 단가(settings.llm_prices, 행의 UTC 날짜에 유효한 구간). 예상치다.
- 대화 / 대화 외 = usage_log.user_id가 있는 행 / 없는 행(user_id IS NULL — 초기 질문 자동
  생성처럼 사용자 귀속이 없는 작업). 구성요소 이름 목록으로 가르지 않는다.
- 금액을 모르는 기록(단가 미등록 모델·토큰 미측정 행)은 0으로 치지 않고 합계에서 뺀다 — 그런
  행이 있으면 각주가 그 사실만 알린다(cost_notes). 그날 행이 전부 그렇다면 그날 금액은 None.

과금 토큰은 세 갈래다(docs/admin-analytics-design-20260914.md §2.2):
    input_uncached = prompt − cached,  input_cached = cached,  output_billed = total − prompt
사고 토큰 열은 쓰지 않는다 — Gemini는 response에 사고가 없고 LiteLLM은 있어서, response+thinking은
벤더 분기 없이는 틀린다. total − prompt는 두 벤더 모두에서 출력 단가 대상과 같다.

집계 행 한 개(SQL이 돌려주는 형태)는 `rows`·`measured_rows`·세 토큰 합·
`cache_unknown_rows`를 가진다. 측정 불성립 행(prompt·total NULL 또는
total < prompt)은 SQL이 토큰 합에서 이미 뺐고 rows − measured_rows로만 남는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from yes24_agent.config import ModelPrice

TOKEN_KINDS = ("input_uncached", "input_cached", "output_billed")


def price_for(prices: Iterable[ModelPrice], model: str | None, day: date) -> ModelPrice | None:
    """그 날짜에 유효한 단가 — 모델명 정확 일치 중 effective_from <= day인 가장 늦은 구간."""
    applicable = [p for p in prices if p.model == model and p.effective_from <= day]
    return max(applicable, key=lambda p: p.effective_from, default=None)


def cost_of(tokens: Mapping[str, Any], price: ModelPrice) -> float:
    return (
        int(tokens["input_uncached"] or 0) * price.input_usd_per_mtok
        + int(tokens["input_cached"] or 0) * price.cached_input_usd_per_mtok
        + int(tokens["output_billed"] or 0) * price.output_usd_per_mtok
    ) / 1_000_000


def attach_costs(rows: Iterable[Mapping[str, Any]], prices: list[ModelPrice]) -> list[dict]:
    """집계 행마다 그 행의 날짜(`day`)·모델 단가로 `cost_usd`(단가 없으면 None)를 붙인다."""
    attached = []
    for row in rows:
        price = price_for(prices, row["model"], row["day"])
        attached.append({**row, "cost_usd": cost_of(row, price) if price else None})
    return attached


def total(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """단가가 붙은 집계 행들의 합과 커버리지 카운터.

    rows = priced_rows + unpriced_rows + unmeasured_rows(측정된 행만 단가 유무로 갈린다).
    cost_usd는 단가가 붙은 행의 합이고, 행이 있는데 단가가 붙은 묶음이 하나도 없으면 None
    ("단가 미등록"), 행이 없으면 0이다. cache_unknown_rows는 비용에 들어간 행 중 cached가 NULL이라
    할인을 못 받은 행 — 그만큼 금액이 상한이다.
    """
    out: dict[str, Any] = {
        "rows": 0,
        "priced_rows": 0,
        "unpriced_rows": 0,
        "unpriced_tokens": 0,
        "unmeasured_rows": 0,
        "cache_unknown_rows": 0,
        **dict.fromkeys(TOKEN_KINDS, 0),
        "cost_usd": 0.0,
    }
    priced_any = False
    for row in rows:
        measured = int(row["measured_rows"])
        tokens = sum(int(row[kind] or 0) for kind in TOKEN_KINDS)
        out["rows"] += int(row["rows"])
        out["unmeasured_rows"] += int(row["rows"]) - measured
        for kind in TOKEN_KINDS:
            out[kind] += int(row[kind] or 0)
        if row["cost_usd"] is None:
            out["unpriced_rows"] += measured
            out["unpriced_tokens"] += tokens
            continue
        priced_any = True
        out["priced_rows"] += measured
        out["cache_unknown_rows"] += int(row["cache_unknown_rows"])
        out["cost_usd"] += row["cost_usd"]
    if out["rows"] and not priced_any:
        out["cost_usd"] = None
    return out


def per_turn(cost_usd: float | None, turns: int) -> float | None:
    return cost_usd / turns if cost_usd is not None and turns else None


# 비용 각주 — 사용자에게 보이는 문장이라 config가 아니라 여기 둔다. 산식·제외·과대/과소 요인의
# 상세는 운영 문서(docs/admin-operations.md '비용 계산 방법')에 있고, 화면은 예상치라는 사실과
# '대화 외'의 뜻만 말한다(개발 용어 없이). 모델명은 싣지 않는다(2026-09-28 사용자 결정).
_ESTIMATE = "모델 사용량에 표준 단가를 곱한 예상치(USD)예요. 실제 청구액과 다를 수 있어요."
_BACKGROUND = "‘대화 외’는 초기 질문 자동 생성·AI 오버뷰처럼 특정 사용자의 대화가 아닌 작업의 비용이에요."
_PARTIAL = "일부 기록은 금액을 알 수 없어 합계에서 빠졌어요."


def cost_notes(coverage: Mapping[str, int]) -> list[str]:
    """화면 각주 — 예상치 안내 · '대화 외' 정의 · (있을 때만) 금액을 모르는 기록이 빠졌다는 말."""
    partial = coverage["unpriced_rows"] or coverage["unmeasured_rows"]
    return [_ESTIMATE, _BACKGROUND, *([_PARTIAL] if partial else [])]
