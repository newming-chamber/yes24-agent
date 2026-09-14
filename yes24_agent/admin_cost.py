"""어드민 분석의 비용 추정 — usage_log 집계 행에 단가를 붙이고 합산하는 순수 함수(DB 없음).

과금 토큰은 세 갈래다(docs/admin-analytics-design-20260914.md §2.2):
    input_uncached = prompt − cached,  input_cached = cached,  output_billed = total − prompt
사고 토큰 열은 쓰지 않는다 — Gemini는 response에 사고가 없고 LiteLLM은 있어서, response+thinking은
벤더 분기 없이는 틀린다. total − prompt는 두 벤더 모두에서 출력 단가 대상과 같다.

집계 행 한 개(SQL이 돌려주는 형태)는 `rows`·`measured_rows`·`llm_calls`·세 토큰 합·
`cache_unknown_rows`·`ungrounded_suspect`를 가진다. 측정 불성립 행(prompt·total NULL 또는
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
    """집계 행마다 그 행의 날짜(`day`)·모델 단가로 `cost_usd`(단가 없으면 None)와
    `price_effective_from`을 붙인다."""
    attached = []
    for row in rows:
        price = price_for(prices, row["model"], row["day"])
        attached.append(
            {
                **row,
                "cost_usd": cost_of(row, price) if price else None,
                "price_effective_from": price.effective_from if price else None,
            }
        )
    return attached


def total(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """단가가 붙은 집계 행들의 합과 커버리지 카운터.

    rows = priced_rows + unpriced_rows + unmeasured_rows(측정된 행만 단가 유무로 갈린다).
    cost_usd는 단가가 붙은 행의 합이고, 행이 있는데 단가가 붙은 묶음이 하나도 없으면 None
    ("단가 미등록"), 행이 없으면 0이다. cache_unknown_rows는 비용에 들어간 행 중 cached가 NULL이라
    할인을 못 받은 행 — 그만큼 금액이 상한이다. price_effective_from은 적용된 구간 중 가장 늦은 것.
    """
    out: dict[str, Any] = {
        "rows": 0,
        "priced_rows": 0,
        "unpriced_rows": 0,
        "unpriced_tokens": 0,
        "unmeasured_rows": 0,
        "cache_unknown_rows": 0,
        "ungrounded_suspect": 0,
        "llm_calls": None,
        **dict.fromkeys(TOKEN_KINDS, 0),
        "cost_usd": 0.0,
        "price_effective_from": None,
    }
    priced_any = False
    for row in rows:
        measured = int(row["measured_rows"])
        tokens = sum(int(row[kind] or 0) for kind in TOKEN_KINDS)
        out["rows"] += int(row["rows"])
        out["unmeasured_rows"] += int(row["rows"]) - measured
        out["ungrounded_suspect"] += int(row["ungrounded_suspect"] or 0)
        if row["llm_calls"] is not None:
            out["llm_calls"] = (out["llm_calls"] or 0) + int(row["llm_calls"])
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
        out["price_effective_from"] = max(
            filter(None, (out["price_effective_from"], row["price_effective_from"]))
        )
    if out["rows"] and not priced_any:
        out["cost_usd"] = None
    return out


def per_turn(cost_usd: float | None, turns: int) -> float | None:
    return cost_usd / turns if cost_usd is not None and turns else None


def model_order(prices: Iterable[ModelPrice], observed: Iterable[str | None]) -> list[str]:
    """표시(색 배정) 순서 — 단가표 선언 순서(중복 1회) 뒤에 관측된 미등록 모델을 이름순으로.
    기간이 바뀌어도 같은 모델이 같은 자리에 온다."""
    declared = list(dict.fromkeys(price.model for price in prices))
    return declared + sorted({model for model in observed if model} - set(declared))


# 비용 각주 — 사용자에게 보이는 문장이라 config가 아니라 여기 둔다. direction: basis(산정 기준)·
# excluded(금액에서 빠짐)·over(실제보다 많게)·under(실제보다 적게). 목록 순서가 표시 순서다.
_FIXED_BASIS = (
    "유료 등급 표준 단가(USD) 기준 추정입니다. 무료 등급 키라면 실제 청구액은 0입니다.",
    "출력은 total − prompt 토큰(사고·reasoning 포함)으로, 단가는 기록 날짜(UTC)에 유효한 값으로 "
    "계산합니다. 턴당 비용은 과금 턴(usage_log에서 단가가 적용되고 토큰이 측정된 main 행) 수로 "
    "나눕니다.",
)
_GROUNDING_EXCLUDED = (
    "Google Search 그라운딩 요청료는 금액에 넣지 않았습니다. 무료 한도가 같은 API 키의 "
    "전체 사용량 기준(3.x 계열은 월 단위, 2.5 계열은 일 단위)이라 이 DB로 초과 여부를 판정할 수 "
    "없고, 요청 1건이 검색 쿼리 여러 건으로 과금될 수 있어 web_grounding 행 수(요청 수)는 과금 "
    "건수의 하한입니다."
)
_GROUNDING_OVER = (
    "web_grounding 행은 검색 결과가 입력으로 되돌아온 토큰이 출력 단가로 잡혀 실제보다 약간 "
    "많습니다."
)
_FIXED_UNDER = (
    "GPT-5.6 Luna의 캐시 쓰기 할증은 usage_log에 캐시 쓰기 토큰이 없어 반영하지 않았습니다.",
    "장문 구간 할증(Gemini 2.5 Pro 200K 초과, GPT-5.6 Luna 272K 초과)은 한 턴의 여러 콜이 합산 "
    "기록돼 콜별 프롬프트 크기를 알 수 없어 기본 구간 단가를 적용했습니다.",
)


def cost_notes(coverage: Mapping[str, int], turns: int, main_rows: int) -> list[dict[str, str]]:
    """커버리지 카운터(0보다 클 때만)와 고정 요금 조건에서 비용 각주를 만든다.

    각주 문장의 판단은 여기 한 곳뿐이다 — 프론트는 받은 순서대로 보이기만 한다."""
    notes = [("basis", text) for text in _FIXED_BASIS]
    if turns != main_rows:
        notes.append(
            (
                "basis",
                f"대화 턴 {turns:,}개와 비용 기록 {main_rows:,}건이 다릅니다. 차이는 삭제되었거나 "
                "기록이 없는 턴입니다.",
            )
        )
    if coverage["unpriced_rows"]:
        notes.append(
            (
                "excluded",
                f"단가 미등록 모델 {coverage['unpriced_rows']:,}행·"
                f"{coverage['unpriced_tokens']:,}토큰은 금액과 턴당 비용에서 빠졌습니다.",
            )
        )
    if coverage["unmeasured_rows"]:
        notes.append(
            (
                "excluded",
                f"토큰이 측정되지 않은 {coverage['unmeasured_rows']:,}행은 금액과 턴당 비용에서 "
                "빠졌습니다.",
            )
        )
    notes.append(("excluded", _GROUNDING_EXCLUDED))
    if coverage["cache_unknown_rows"]:
        notes.append(
            (
                "over",
                f"캐시 토큰이 기록되지 않은 {coverage['cache_unknown_rows']:,}행은 캐시 할인 없이 "
                "계산해 실제보다 많을 수 있습니다.",
            )
        )
    notes.append(("over", _GROUNDING_OVER))
    notes.extend(("under", text) for text in _FIXED_UNDER)
    return [{"direction": direction, "text": text} for direction, text in notes]
