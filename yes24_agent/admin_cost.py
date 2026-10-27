"""어드민 분석의 비용 추정 — usage_log 집계 행에 단가를 붙이고 합산하는 순수 함수(DB 없음).

핵심 정의(화면 각주의 근거 — 상세는 docs/admin-operations.md §7, docs는 저장소 밖):
- 금액 = 사용량(토큰·검색 과금 단위 수 — 그라운딩 쿼리·퍼플렉시티 요청) × 단가(settings.llm_prices, 행의 UTC 날짜·처리 등급에
  유효한 구간). 예상치다.
  처리 등급(usage_log.service_tier — LiteLLM fast 등) 행이 없으면 표준 행으로 추정하고 각주가
  밝힌다.
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

# 이진 비교·정렬 — 콜레이션이 대소문자·전각을 접지 않게(파이썬 문자열 비교와 같은 순서).
_BIN = "COLLATE utf8mb4_bin"

TOKEN_KINDS = ("input_uncached", "input_cached", "output_billed")


def price_for(
    prices: Iterable[ModelPrice], model: str | None, day: date, service_tier: str | None = None
) -> ModelPrice | None:
    """그 날짜·등급에 유효한 단가 — (모델명, 등급) 정확 일치 중 effective_from <= day인 가장 늦은
    구간. 등급 행이 없으면 표준("") 행이다(돌려준 행의 service_tier로 추정 여부를 안다)."""
    prices = list(prices)

    def latest(tier: str) -> ModelPrice | None:
        applicable = [
            p for p in prices
            if p.model == model and p.service_tier == tier and p.effective_from <= day
        ]
        return max(applicable, key=lambda p: p.effective_from, default=None)

    return latest(service_tier or "") or (latest("") if service_tier else None)


def cost_of(tokens: Mapping[str, Any], price: ModelPrice) -> float:
    """토큰 비용 + 검색 과금 비용. search_queries NULL(검색 과금 없는 행·열 추가 전 기록)은 0이다."""
    return (
        int(tokens["input_uncached"] or 0) * price.input_usd_per_mtok
        + int(tokens["input_cached"] or 0) * price.cached_input_usd_per_mtok
        + int(tokens["output_billed"] or 0) * price.output_usd_per_mtok
    ) / 1_000_000 + int(tokens.get("search_queries") or 0) * price.search_usd_per_kquery / 1000


def row_cost_sql(prices: list[ModelPrice], measured: str) -> tuple[str, list[Any]]:
    """usage_log 한 행의 금액(단가 없으면 NULL) — price_for로 고른 단가에 cost_of를 곱한 SQL 식.

    CASE는 처음 맞는 WHEN이 이긴다. 등급 행을 먼저(그 등급과 정확 일치), 표준 행을 뒤에(등급
    WHEN이 안 맞은 행 — 표준 행, 또는 그 날짜·등급의 등급 단가가 없어 표준가로 대체되는 행) 두고,
    같은 판정 안에서는 늦은 구간부터 둔다. 구간 시작일 비교는 행의 UTC 날짜다(DATE(created_at) >=
    시작일 ⇔ created_at >= 시작일 0시). 정렬이 안정이라 같은 시작일이 겹치면 목록 앞 행이 이긴다
    (price_for의 max와 같다). WHEN은 prices에서 생성하므로 구간·등급을 더해도 식을 고치지 않는다.

    measured = 토큰 측정 성립 조건(측정 불성립 행은 토큰 0, 검색 과금만). 단가는 DOUBLE로 곱한다
    — DECIMAL 나눗셈은 소수 4자리씩만 늘어 1e-6 단위 금액이 잘린다. 모델·등급 비교는 이진(_BIN)
    — price_for의 ==와 같다(열 콜레이션이 대소문자를 접으면 'A'가 'a' 단가를 얻는다).
    """
    order = sorted(
        range(len(prices)),
        key=lambda i: (prices[i].service_tier == "", -prices[i].effective_from.toordinal()),
    )
    rate = "CAST(%s AS DOUBLE)"
    cost = (
        f"(CASE WHEN {measured} THEN (prompt_tokens - COALESCE(cached_tokens, 0)) * {rate} "
        f"+ COALESCE(cached_tokens, 0) * {rate} + (total_tokens - prompt_tokens) * {rate} "
        f"ELSE 0 END) / 1000000 + COALESCE(search_queries, 0) * {rate} / 1000"
    )
    whens, params = [], []
    for price in (prices[i] for i in order):
        tier = f"AND COALESCE(service_tier, '') {_BIN} = %s " if price.service_tier else ""
        whens.append(f"WHEN model {_BIN} = %s {tier}AND created_at >= %s THEN {cost}")
        params.extend([
            price.model, *([price.service_tier] if price.service_tier else []),
            price.effective_from, price.input_usd_per_mtok, price.cached_input_usd_per_mtok,
            price.output_usd_per_mtok, price.search_usd_per_kquery,
        ])
    return (f"CASE {' '.join(whens)} END" if whens else "NULL"), params


def attach_costs(rows: Iterable[Mapping[str, Any]], prices: list[ModelPrice]) -> list[dict]:
    """집계 행마다 그 행의 날짜(`day`)·모델·등급(`service_tier`, 없으면 표준) 단가로
    `cost_usd`(단가 없으면 None)를 붙인다. 등급 단가가 없어 표준가로 셈한 행은 tier_estimated."""
    attached = []
    for row in rows:
        tier = row.get("service_tier") or ""
        price = price_for(prices, row["model"], row["day"], tier)
        attached.append({
            **row,
            "cost_usd": cost_of(row, price) if price else None,
            "tier_estimated": price is not None and price.service_tier != tier,
        })
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
        "tier_estimated_rows": 0,
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
        out["tier_estimated_rows"] += measured if row.get("tier_estimated") else 0
        out["cost_usd"] += row["cost_usd"]
    if out["rows"] and not priced_any:
        out["cost_usd"] = None
    return out


def per_turn(cost_usd: float | None, turns: int) -> float | None:
    return cost_usd / turns if cost_usd is not None and turns else None


def tokens_of(sums: Mapping[str, Any]) -> dict[str, int]:
    """total()의 과금 토큰 세 갈래만(단가 유무와 무관한 측정 토큰 합)."""
    return {kind: sums[kind] for kind in TOKEN_KINDS}


def cache_hit_rate(sums: Mapping[str, Any]) -> float | None:
    """캐시 적중률 = 캐시 입력 ÷ 입력 전체(캐시 안 된 입력 + 캐시 입력). 입력이 없으면 None."""
    prompt = sums["input_uncached"] + sums["input_cached"]
    return sums["input_cached"] / prompt if prompt else None


# 비용 각주 — 사용자에게 보이는 문장이라 config가 아니라 여기 둔다. 산식·제외·과대/과소 요인의
# 상세는 운영 문서(docs/admin-operations.md '비용 계산 방법')에 있고, 화면은 예상치라는 사실과
# '대화 외'의 뜻만 말한다(개발 용어 없이). 모델명은 싣지 않는다(2026-09-28 사용자 결정).
# 할증 문구는 벤더 비의존으로 둔다 — 긴 컨텍스트 할증 경계가 벤더·모델마다 다르다(2.5-pro 200K,
# Luna 272K — config llm_prices 주석).
_ESTIMATE = (
    "모델 사용량에 그 모델·처리 등급 단가를 곱한 예상치(USD)예요. 긴 컨텍스트 구간 할증과 "
    "캐시 쓰기 단가는 반영하지 않아 실제 청구액과 다를 수 있어요. 웹 검색 무료 제공분은 빼지 "
    "않아 실제보다 조금 높을 수 있어요."
)
_BACKGROUND = (
    "‘대화 외’는 초기 질문 자동 생성·AI 오버뷰처럼 특정 사용자의 대화가 아닌 작업의 비용이에요."
)
_PARTIAL = "일부 기록은 금액을 알 수 없어 합계에서 빠졌어요."
_TIER_ESTIMATE = "처리 등급 단가가 등록되지 않은 기록은 표준 단가로 추정했어요."


def cost_notes(coverage: Mapping[str, int], *, background: bool = True) -> list[str]:
    """화면 각주 — 예상치 안내 · '대화 외' 정의 · (있을 때만) 금액을 모르는 기록이 빠졌다는 말과
    등급 단가 미등록 → 표준가 추정. 판정은 여기 한 곳이고 화면은 받은 문장을 그대로 싣는다.

    background=False는 '대화 외'가 없는 집계(모델 패널 — 답변 main 행만)용이다."""
    partial = coverage["unpriced_rows"] or coverage["unmeasured_rows"]
    return [
        _ESTIMATE,
        *([_BACKGROUND] if background else []),
        *([_PARTIAL] if partial else []),
        *([_TIER_ESTIMATE] if coverage.get("tier_estimated_rows") else []),
    ]
