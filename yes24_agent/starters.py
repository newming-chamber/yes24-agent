"""초기 질문(스타터) 회전 풀 — MySQL 풀 + 일 1회 자동 생성 + 공개·어드민 라우트.

빈 화면의 질문 칩을 프론트 하드코딩 3개에서 **서버가 관리하는 회전 풀**로 바꾼다
(설계·근거: docs/starters-design.md, DDL: scripts/starters.sql). 이 모듈이 풀의 읽기·쓰기·
생성·라우트를 전부 소유한다.

구조(결정 D1~D10의 코드 대응):
- **슬롯 = 질문 유형**이고 값은 시드 키다. 자동 슬롯은 `settings.starter_sections`
  (BROWSE_SEED_URLS의 키)와 골라주기 슬롯 `{starter_pick_from}-pick` 하나이고, 그 밖의
  슬롯(정책·범용)은 수동 풀이다 — 코드에 슬롯 열거가 없고, 칩 라벨은 `starter_labels`의
  사용자 언어 문구(없으면 시드 표의 코너 이름)다.
- **첫 화면은 책만 보여주지 않는다.** 이 제품의 정체성은 "범용 AI가 기본, 책이 강점"이므로
  수동 슬롯 `general`이 책 밖의 질문을 한 자리 맡는다(CLAUDE.md 정체성 절).
- **골라주기 슬롯**은 상품 하나가 아니라 **분야**를 가리킨다("소설 베스트 중에 처음 읽기
  좋은 거 골라줘"). 재료는 코너 내비의 분야 목록이고 검증은 관측된 분야명과의 대조다 —
  상품 지목형만 있으면 "골라준다"는 능력이 첫 화면에서 보이지 않는다.
- **생성 입력은 Yes24 관측본만**(원칙 4a). 코너 페이지를 관측해 상품 목록을 payload로 싣고,
  구조화 출력 `{items:[{slot, goods_no, title, text}]}`를 받아 **출구에서 goods_no가 그 슬롯의
  관측 집합 밖이거나 제목이 관측 원문과 다르면 폐기**한다 — 문구 필터가 아니라 참조 검증이다
  (인용 검증과 동형). 가격·평점은
  payload에 싣지 않아 문장에 숫자가 박힐 재료 자체를 없앤다.
- **일 1회, lazy**: 서빙 경로가 오늘자 `starter_runs`가 없는 자동 슬롯을 보면 백그라운드 생성
  태스크를 한 번 띄우고(프로세스 내 single-flight) 현재 풀로 즉답한다. 멀티워커 중복은
  `starter_runs(slot, run_date)` PK를 `INSERT IGNORE`로 선점한 워커만 생성하는 것으로 막는다.
  성공 시 그 슬롯의 이전 auto 항목을 비활성하고 새 항목을 **한 트랜잭션**으로 넣는다. 실패
  (관측 0건·모델 실패·전부 폐기)는 이전 것을 그대로 두고 run을 failed로 남긴다.
- **회전 = 요청마다 슬롯별 1개 무작위 → 순서 셔플 → n개**. 고정(pinned) 항목이 있는 슬롯은
  고정분 중에서만 뽑는다.
- **측정은 서빙 로그 한 줄**(set_id·ids·slots)이다. 세션 첫 user 이벤트가 서빙 문장과 같으면
  클릭이다(442c55e로 절단이 사라져 등호 대조가 성립) — 새 테이블은 없다.

실패 정책은 user_data와 같다: 저장소가 없는 구성(sqlite)·질의 실패는 503으로 정직하게 끊고
(빈 200 위장 금지), 프론트는 그것을 폴백 신호로 쓴다. 자동 생성은 백그라운드라 어떤 실패도
서빙에 얹히지 않는다(로그만).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import random
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, HTTPException, Query
from google.genai import types
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from yes24_agent.admin import require_admin
from yes24_agent.auth import AuthenticatedUser, get_authenticated_user
from yes24_agent.config import Settings, get_genai_client, get_settings
from yes24_agent.db import MysqlBackedService
from yes24_agent.session_service import SQLITE_DIALECT, db_dialect, mysql_pool_kwargs
from yes24_agent.sources import KST
from yes24_agent.tools.yes24_search import get_client
from yes24_agent.usage import record_usage
from yes24_agent.yes24.client import Yes24FetchError
from yes24_agent.yes24.parsers import (
    _PUBLICATION_DATE_RE,
    ParseError,
    extract_faq_entries,
    parse_browse_list,
    parse_category_links,
    parse_search,
)
from yes24_agent.yes24.urls import (
    BROWSE_SEED_URLS,
    POLICY_SEEDS,
    browse_category_prefix,
    browse_url,
    product_url,
    search_url,
)

logger = logging.getLogger(__name__)

# scripts/starters.sql의 slot VARCHAR(32) — 어드민 입력을 DDL 폭에서 잠근다(DB 절단 오류 대신 422).
_SLOT_MAX_CHARS = 32
# 같은 파일의 starter_runs.detail VARCHAR(500) — 마감 기록이 폭을 넘어 실패하지 않게 자른다.
_RUN_DETAIL_MAX_CHARS = 500
# 마감되지 않은 run을 죽은 것으로 보는 배수(생성 타임아웃 대비). 프로세스 강제 종료 복구용.
_STALE_RUN_FACTOR = 10
# 골라주기 슬롯의 키 접미사. 슬롯 이름을 코드에 열거하지 않고 **시드 키에서 파생**한다
# (bestseller → bestseller-pick) — 새 코너를 골라주기 대상으로 바꿔도 열거를 고칠 일이 없다.
_PICK_SUFFIX = "-pick"
# 재료가 시드 코너가 아닌 슬롯들 — 키를 파생할 곳이 없어 이름을 여기서 정한다.
_TREND_SLOT = "trend"
_GENERAL_SLOT = "general"
_POLICY_SLOT = "policy"
# 활성 풀 SELECT의 컬럼 순서 — dict 변환이 이 튜플로 하므로 SQL과 여기가 같이 움직인다.
_POOL_COLUMNS = ("id", "slot", "text", "source", "goods_no", "run_date", "pinned")
# 어드민 목록 SELECT의 컬럼 순서(DDL 전 컬럼).
_ADMIN_COLUMNS = (
    "id", "slot", "text", "source", "goods_no", "source_url", "run_date", "pinned", "active",
    "valid_from", "valid_until", "created_at", "updated_at",
)
_RUN_COLUMNS = ("slot", "run_date", "status", "detail", "started_at")


# ── 순수 함수(DB·네트워크 없음 — 테스트가 직접 잠근다) ─────────────────────────


def _year_month(pub_date: str | None) -> tuple[int, int] | None:
    """"YYYY년 MM월[ DD일]" → (연, 월). 형식이 아니면 None(파서의 출간일 정규식을 그대로 쓴다)."""
    match = _PUBLICATION_DATE_RE.search(pub_date or "")
    return (int(match.group("year")), int(match.group("month"))) if match else None


def _squash(value: Any) -> str:
    """문장·제목 비교의 단일 정규화 — 공백 종류·연속 공백·제로폭 문자를 없앤다.

    모델 출력과 어드민 입력이 같은 정규화를 거쳐야 "눈에 보이지 않는 차이"가 한쪽에서만
    통과하지 않는다(제로폭만으로 이루어진 문장이 빈 칩으로 서빙된 실측).
    """
    if value is None:
        return ""
    stripped = "".join(ch for ch in str(value) if unicodedata.category(ch) != "Cf")
    return " ".join(stripped.split())


def pick_starters(pool: list[dict], n: int, rng: random.Random | Any) -> list[dict]:
    """활성 풀에서 서빙 세트를 고른다: 슬롯별 1개(고정분이 있으면 그중에서) → 순서 셔플 → n개.

    순서를 매번 섞는 이유는 위치 효과를 클릭 측정에서 분리하기 위해서다(NN/g). rng를 주입받아
    테스트가 결정론으로 잠근다.
    """
    by_slot: dict[str, list[dict]] = {}
    for row in pool:
        by_slot.setdefault(row["slot"], []).append(row)
    picked = []
    for rows in by_slot.values():
        pinned = [row for row in rows if row.get("pinned")]
        picked.append(rng.choice(pinned or rows))
    rng.shuffle(picked)
    return picked[: max(n, 0)]


def _today() -> dt.date:
    """"오늘"의 단일 정의 — run_date·활성 조건·당월 판정이 전부 이 날짜를 쓴다.

    기준 시간대는 도구·매트릭스가 쓰는 sources.KST를 그대로 쓴다 — "오늘"의 정의가 제품 안에
    둘이면 자정 근처에서 서로 다른 날을 가리킨다.
    """
    return dt.datetime.now(KST).date()


def _label(slot: str, settings: Settings) -> str:
    """칩에 표시할 라벨 — 사용자 언어가 먼저고, 없으면 시드 표의 코너 이름으로 폴백한다."""
    seed = BROWSE_SEED_URLS.get(slot)
    return settings.starter_labels.get(slot) or (seed["label"] if seed else slot)


def _failed(detail: str, dropped: int = 0) -> dict:
    return {"status": "failed", "items": [], "dropped": dropped, "detail": detail}


# ── 슬롯 명세 ────────────────────────────────────────────────────────────────
#
# **모든 슬롯이 같은 일을 한다**: 재료 목록에서 하나를 골라, 그것을 가리키는 질문을 쓰고,
# 고른 것이 문장에 실제로 드러나는지 대조한다. 슬롯마다 다른 것은 두 가지뿐이다 —
# 재료를 **어디서** 가져오는가(observe)와 그것으로 **무엇을 묻는가**(ask).
#
# 그래서 프롬프트도 검증도 생성도 한 벌이다. 슬롯을 늘릴 때 늘어나는 것은 명세 한 줄이고,
# 재료가 새로운 종류일 때만 관측기가 하나 는다. 종전엔 슬롯마다 프롬프트·검증·생성을
# 따로 두어 슬롯 수만큼 코드가 늘었다(사례 패치).


@dataclass(frozen=True)
class Material:
    """재료 하나 — 문구가 가리킬 수 있는 대상.

    `evidence`는 "이 재료를 가리켰다"의 판정 기준이다. 바깥 리스트는 AND, 안쪽은 OR —
    상품이면 [[제목], ["5위"]](둘 다 필요), 분야·화제면 [[조각들]](하나면 충분)이다.
    이름을 통째로 요구하지 않는 이유는 사람이 줄여 말하기 때문이고, 그래도 **관측본과의
    대조**이지 허용 단어 목록이 아니다.
    """

    ref: str
    evidence: list[list[str]]
    hint: dict
    # 상품을 가리키는 재료만 채운다. **ref 문자열로 추론하지 않는다** — FAQ 참조키를 인덱스로
    # 바꾸자 "10"이 상품 번호로 읽혀 정책 칩에 없는 상품 링크가 붙었다(실측).
    goods_no: int | None = None


@dataclass(frozen=True)
class Observed:
    """관측 결과 — 재료와, 모델이 쓸 수 있는 시간 표현 한 줄."""

    materials: list[Material]
    note: str | None = None


@dataclass(frozen=True)
class SlotSpec:
    """슬롯 하나의 정의. 새 슬롯은 여기 한 줄이 늘 뿐이다."""

    key: str
    ask: str
    observe: Any  # async (SlotSpec, ObserveContext) -> Observed


@dataclass(frozen=True)
class ObserveContext:
    settings: Settings
    client: Any
    genai_client: Any
    today: dt.date
    exclude: set[int] = field(default_factory=set)
    param: str = ""
    # 같은 바깥 신호를 나눠 쓰는 슬롯들의 공동 저장소(한 번 모아 여럿이 본다).
    shared: dict = field(default_factory=dict)


# 문구 계약. 앞머리는 모든 슬롯이 공유하고 슬롯별 ask 한 줄이 가운데 들어가며, **핵심
# 기준은 맨 뒤**다 — 규칙이 쌓이면 중간에 놓인 원칙이 묻혀 문장이 사양 확인으로 무너졌다
# (2026-09-09 실측). 완성 문장 예시는 넣지 않는다: 예시는 내용까지 복사된다.
_HEAD = (
    "빈 화면의 초기 질문 칩에 실릴 문장을 만든다. 각 문장은 사용자가 이 AI 어시스턴트에게 "
    "그대로 눌러 보낼 완결된 질문이고, 사람이 입으로 말하듯 반말로 끝맺는 한국어 한 문장이다. "
    "'~는?'처럼 명사로 끊거나 문어체로 쓰지 않는다. 문장 부호는 문장 꼴을 따른다 — 묻는 "
    "꼴이면 물음표로, 청하는 꼴이면 물음표 없이 끝낸다. "
    "재료(materials) 가운데 **하나**를 골라 ref에 그 값을 그대로 적는다. 목록 밖의 것을 "
    "만들지 않는다. 고른 재료에 must가 있으면 **그 값을 하나도 빠짐없이 문장에 그대로 "
    "넣는다** — 하나라도 빠지면 그 문장은 버려진다. 재료가 어디서 왔는지(코너 이름·순위·"
    "출간월)가 거기 들어 있고, 그것 없이는 읽는 사람이 무엇을 가리키는지 모른다. must가 "
    "없는 재료는 그것을 가리킨다는 것이 문장에서 드러나기만 하면 되고, 이름은 사람이 말하듯 "
    "줄여 써도 된다. "
    "재료에 없는 날짜·요일·계절은 지어내지 않는다. 같은 세트 안에서는 서로 다른 재료를 "
    "다룬다. "
)
_TAIL = (
    " 마지막으로, 위의 모든 규칙보다 앞서는 기준이 하나 있다. "
    "**눌러서 나온 답이 사용자에게 쓸모가 있는가.** 목록만 봐도 아는 것, 사양 한 줄로 끝나는 "
    "것, 공개되지 않아 답할 수 없는 것, 예측이나 감상 요구, 이 서비스의 사용법 — 이 가운데 "
    "어느 하나라도 묻고 있다면 그 문장은 버리고 다시 쓴다."
)


def _instruction(spec: SlotSpec, max_chars: int) -> str:
    """공통 계약 + 이 슬롯이 묻는 것 + 길이. 길이를 문구에 실어야 모델이 **긴 이름의 재료를
    피해** 고른다 — 상한은 출구가 어차피 잡지만, 모르고 쓰면 통째로 폐기돼 재료가 준다."""
    return f"{_HEAD}{spec.ask} 문장 전체는 {max_chars}자를 넘기지 않는다.{_TAIL}"


def _response_schema(refs: list[str], count: int) -> types.Schema:
    """구조화 출력 스키마 — 모든 슬롯이 같은 모양이다.

    property_ordering이 계약이다: **참조(ref)를 먼저 확정하고 문장을 쓰게** 해야 문장을
    먼저 짓고 대상을 끼워 맞추는 쏠림이 줄어든다(enrichment의 선행 판정 필드와 같은 이유).
    ref를 enum으로 두어 목록 밖 대상은 애초에 만들 수 없다.
    """
    order = ["ref", "text"]
    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "items": types.Schema(
                type=types.Type.ARRAY,
                min_items=count,
                max_items=count,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "ref": types.Schema(type=types.Type.STRING, enum=refs),
                        "text": types.Schema(type=types.Type.STRING),
                    },
                    required=order,
                    property_ordering=order,
                ),
            )
        },
        required=["items"],
    )


def validate_items(
    raw_items: list, materials: list[Material], *, max_chars: int
) -> tuple[list[dict], dict[str, int]]:
    """출구 검증 — 관측본과의 대조. 슬롯이 무엇이든 판정은 하나다.

    버리는 것: 목록 밖 ref, 고른 재료가 문장에 드러나지 않는 text(evidence 미충족),
    빈 문장, 상한 초과, 중복. **문구를 보는 필터가 아니라 이번 관측본과의 대조**다
    (인용 검증이 본문의 [n]을 이번 턴 출처와 대조하는 것과 같은 자리).

    반환은 (생존 항목, 사유별 폐기 수) — 어느 슬롯이 왜 말라붙는지는 데이터 소스 기각
    판정의 재료라 사유를 세어 남긴다.
    """
    by_ref = {str(m.ref): m for m in materials}
    kept: list[dict] = []
    dropped: dict[str, int] = {}
    seen: set[str] = set()
    for item in raw_items:
        ref = str(item.get("ref") or "")
        text = _squash(item.get("text"))
        material = by_ref.get(ref)
        reason = (
            "unobserved_ref" if material is None
            else "not_in_text" if not _shows(material, text)
            else "empty_text" if not text
            else "over_max_chars" if len(text) > max_chars
            else "duplicate_text" if text in seen
            else None
        )
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            logger.warning(f"starters 폐기: ref={ref!r} reason={reason} text={text!r}")
            continue
        seen.add(text)
        kept.append({"ref": ref, "text": text, "goods_no": material.goods_no})
    return kept, dropped


def _material_payload(material: Material) -> dict:
    """모델에 싣는 재료 한 건 — **검증이 요구하는 것(must)을 함께 보여준다**.

    must는 evidence의 각 그룹 대표값이라 프롬프트와 출구가 **한 데이터에서 나온다**. 종전엔
    검증만 순위를 요구하고 프롬프트는 말해 주지 않아, 순위 없는 문장이 만들어졌다가 통째로
    폐기됐다(슬롯이 굶었다). 요구할 것이 없는 재료는 must를 싣지 않는다.
    """
    item = {"ref": material.ref, **material.hint}
    must = [group[0] for group in material.evidence if len(group) == 1 and group[0]]
    if must:
        item["must"] = must
    return item


def _shows(material: Material, text: str) -> bool:
    """고른 재료가 문장에 드러나는가 — 바깥은 AND, 안쪽은 OR."""
    return all(any(part and part in text for part in group) for group in material.evidence)


# ── 재료 관측기 ──────────────────────────────────────────────────────────────
#
# 재료의 종류만큼만 있다. 슬롯이 늘어도 재료가 같은 종류면 관측기는 늘지 않는다.


async def observe_corner_products(spec: SlotSpec, ctx: ObserveContext) -> Observed:
    """코너 한 페이지의 상품 — 그 페이지가 곧 오늘의 재료다.

    순위가 있는 목록은 순위 자체가 시간 앵커라 전량이 재료다. 순위가 없는 목록(신간)은
    앵커가 출간월뿐이라 **당월 출간분만** 남긴다 — 코너에 이전 달·이후 달 행이 섞여 있어
    걸러야 "N월 신간"이라는 문장이 참이 된다.

    `exclude`(최근에 이미 쓴 상품)는 여기서 빠지되, 그것이 재료를 `starter_per_slot`
    미만으로 만들면 **제외를 통째로 포기한다** — 반복을 피하려다 슬롯을 굶기면 어제 세트가
    그대로 남아 더 심하게 반복된다.
    """
    section = spec.key
    seed = BROWSE_SEED_URLS[section]
    html = await ctx.client.get_text(browse_url(section))
    rows = await asyncio.to_thread(
        parse_browse_list, html, base_url=ctx.settings.yes24_base_url, section=section
    )
    if not seed["has_rank"]:
        this_month = (ctx.today.year, ctx.today.month)
        rows = [r for r in rows if _year_month(r.get("pub_date")) == this_month]

    fresh = [r for r in rows if int(r["goods_no"]) not in ctx.exclude]
    if len(fresh) < ctx.settings.starter_per_slot:
        logger.info(
            f"starters 제외 포기: slot={section} 남은행={len(fresh)} "
            f"(최근 사용 {len(ctx.exclude)}건) — 반복보다 굶는 쪽이 나쁘다"
        )
        fresh = rows

    materials = []
    for row in fresh:
        rank = row.get("rank")
        title = _squash(row.get("title"))
        if not title:
            continue
        # 가격·평점·저자·출판사는 싣지 않는다. 실은 값은 **되묻는 질문**이 되고(목록만 봐도
        # 아는 것이라 도구 호출도 못 부른다), 숫자는 문장에 박힌다. 남기는 것은 그 상품을
        # 가리키는 데 필요한 것뿐이다.
        hint = {"title": title}
        evidence = [[title]]
        if rank:
            hint["rank"] = rank
            # 순위는 **라벨이 대신할 수 없는 유일한 실시간 신호**라 문장에 반드시 있어야 한다.
            evidence.append([f"{rank}위"])
        if row.get("pub_date"):
            hint["pub_date"] = row["pub_date"]
        materials.append(
            Material(
                ref=str(row["goods_no"]),
                evidence=evidence,
                hint=hint,
                goods_no=int(row["goods_no"]),
            )
        )
    note = "베스트셀러" if seed["has_rank"] else f"{ctx.today.month}월 신간"
    # note도 문장에 있어야 한다 — 칩 라벨은 코너를 말해 주지만 **눌러서 전송되는 문장**은
    # 라벨 없이 홀로 채팅에 간다. "5위인 『…』"만으로는 무슨 순위인지 알 수 없다(실측).
    for material in materials:
        material.evidence.append([note])
    return Observed(materials=materials, note=note)


async def observe_corner_categories(spec: SlotSpec, ctx: ObserveContext) -> Observed:
    """코너 내비의 분야 목록 — 시드와 **같은 트리**만 남긴다.

    내비에는 국내도서·외국도서·eBook의 동명 분야가 섞여 있어 트리 접두로 걸러야 한 매장
    안에 머문다. 접두 자신(코너 전체)은 분야가 아니라 뺀다.
    """
    section = ctx.param
    html = await ctx.client.get_text(browse_url(section))
    prefix = browse_category_prefix(section)
    links = await asyncio.to_thread(
        parse_category_links, html, limit=ctx.settings.starter_pick_category_limit
    )
    materials = []
    for link in links:
        if not link["number"].startswith(prefix) or link["number"] == prefix:
            continue
        name = _squash(link["name"])
        # 복합 분야명("소설/시/희곡")은 조각 하나만 문장에 있어도 통과한다 — 통째로 요구하면
        # 문장이 어색해진다.
        parts = [p for p in name.split("/") if p]
        materials.append(Material(ref=name, evidence=[parts], hint={"category": name}))
    return Observed(materials=materials)


async def observe_web_topics(spec: SlotSpec, ctx: ObserveContext) -> Observed:
    """오늘의 화제 — 웹에서 모아 **Yes24 검색 결과의 유무로 두 갈래**로 가른다.

    `param`이 "books"면 책이 나온 화제만, "plain"이면 안 나온 화제만 남긴다. 한 신호가 두
    슬롯을 먹이는 셈이라 웹 호출은 하루 한 번이다.

    책이 나온 것만 책 슬롯에 주는 이유: 억지 연결("파병 관련 책 있어?")을 문구 규칙이
    아니라 **검색 결과**로 막는다. 재료가 바깥에서 와도 접지는 Yes24가 한다.

    단 "결과가 있다"로는 부족하다 — Yes24 검색은 부분 일치로 무엇이든 돌려주어 배우 이름에
    무관한 책이 붙었다(실측: 인명 검색 3건 중 3건이 무관). 그래서 **결과 제목이 화제 이름을
    통째로 담고 있는지**까지 본다.

    문구 검증(evidence)이 이름 조각 하나로 만족하는 것과 달리 여기는 이름 전체를 요구한다.
    두 판정은 목적이 다르기 때문이다 — 저쪽은 "사용자가 이 화제를 알아보는가"라 관대해야
    하고(사람은 줄여 말한다), 이쪽은 "이 책이 그 화제의 책인가"라 엄격해야 한다. 조각으로
    재면 흔한 말 하나가 무관한 책을 끌어온다(실측: "누가"가 든 제목이 통과했다).
    """
    settings = ctx.settings
    pairs = ctx.shared.get("web_topics")
    if pairs is None:
        response = await asyncio.wait_for(
            ctx.genai_client.aio.models.generate_content(
                model=settings.model_name,
                contents=_TOPIC_PROMPT.format(count=settings.starter_trend_topics),
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.3
                ),
            ),
            timeout=settings.starter_timeout_s,
        )
        # 경량 모델(web_grounding_model)이 아니라 **주 대화 모델**을 쓴다 — 하루 한 번이라
        # 비용이 무의미한 반면, 경량 모델은 "오늘의 화제"로 네팔 수력발전소·숙련기술인의 날
        # 같은 것을 뽑아 첫 화면이 엉뚱해졌다(실측).
        record_usage("starter_topics", response.usage_metadata, model=settings.model_name)
        # 그라운딩 응답은 자유 텍스트다(빌트인 검색과 구조화 출력은 한 요청에 못 섞는다) —
        # 줄을 잘라 앞머리 기호만 벗긴다. 파싱이 새도 아래 검색 단계가 걸러내므로 무해하다.
        topics: list[str] = []
        for line in (response.text or "").splitlines():
            name = _squash(line).lstrip("-*•0123456789.) ").strip("*")
            if name and name not in topics:
                topics.append(name)
        topics = topics[: settings.starter_trend_topics]

        async def _titles(topic: str) -> list[str]:
            """그 화제를 **통째로 담은** 상품의 제목들. 제목이든 저자든 이름이 들어야 한다 —
            인물 화제는 그 사람이 쓴 책이나 그 사람을 다룬 책 둘 다 관련이다."""
            try:
                html = await ctx.client.get_text(search_url(settings.yes24_base_url, topic))
                rows = await asyncio.to_thread(
                    parse_search, html, base_url=settings.yes24_base_url, limit=5
                )
            except (Yes24FetchError, ParseError):
                return []
            return [
                r["title"]
                for r in rows
                if r.get("title") and topic in f"{r['title']} {r.get('author') or ''}"
            ]

        found = await asyncio.gather(*(_titles(t) for t in topics))
        pairs = ctx.shared["web_topics"] = list(zip(topics, found))
        # "검색 결과 있음"이지 "관련 책 있음"이 아니다 — 관련성은 아래에서 제목 대조로
        # 다시 가른다(둘을 같은 말로 적으면 계측이 사람 판단과 어긋난다).
        logger.info(
            f"starters 화제 수집: {len(pairs)}건 중 검색 결과 있음 "
            f"{sum(1 for _, t in pairs if t)}건"
        )

    materials = []
    for topic, titles in pairs:
        # 화제 이름은 사람이 줄여 말하므로 두 글자 이상 조각 중 하나만 있어도 통과한다.
        parts = [w for w in topic.replace("·", " ").split() if len(w) >= 2] or [topic]
        related = titles
        if bool(related) != (ctx.param == "books"):
            continue
        hint = {"topic": topic}
        if related:
            # 이 책들은 "화제가 책과 이어진다"는 증거이지 문장에 넣을 재료가 아니다.
            hint["found"] = related
        materials.append(Material(ref=topic, evidence=[parts], hint=hint))
    return Observed(materials=materials)


async def observe_faq(spec: SlotSpec, ctx: ObserveContext) -> Observed:
    """고객센터 FAQ 입구가 SSR로 싣는 **실제 질문 목록**.

    이 슬롯의 접지는 재료에서 끝난다 — 이미 "고객센터가 답하고 있는 질문"이라, 그것을
    사용자 말투로 옮기면 답이 있다는 것이 보장된다(상품 슬롯이 관측 상품으로 보장받는 것과
    같은 자리).
    """
    seed = POLICY_SEEDS[ctx.param]
    html = await ctx.client.get_text(seed["url"])
    soup = await asyncio.to_thread(BeautifulSoup, html, "lxml")
    entries = await asyncio.to_thread(extract_faq_entries, soup)
    materials = []
    for index, entry in enumerate(entries):
        question = _squash(entry.get("question"))
        if not question:
            continue
        # 대괄호 분류표([eBook]·[국내도서])를 떼고 남은 말 중 두 글자 이상이 주제어다.
        # 증거를 두지 않는다: 이 재료는 이미 "고객센터가 답하고 있는 질문"이라 접지가
        # 재료에서 끝났고, 문구는 그것을 **사용자 말투로 바꿔** 쓰는 일이다("중고도서" →
        # "중고책"). 원문 단어를 요구하면 바꿔 쓰라는 지시와 모순된다.
        # ref는 **짧은 식별자**다. 긴 자연어를 그대로 쓰면 스키마 enum이 비대해져 요청이
        # 거부된다(실측 400 INVALID_ARGUMENT). 무엇을 고르는지는 payload의 question이 말한다.
        materials.append(
            Material(ref=str(index), evidence=[], hint={"question": question})
        )
    return Observed(materials=materials)


# 오늘의 화제를 모으는 질문. 책과 이어지는지는 여기서 가리지 않는다 — 그 판정은 Yes24
# 검색이 하고, 이 프롬프트는 재료를 넓게 모으기만 한다(한 신호가 두 슬롯을 먹인다).
_TOPIC_PROMPT = (
    "오늘 한국에서 사람들이 많이 이야기하는 화제를 {count}개 알려줘. "
    "**갈래를 고르게 섞는다** — 시사·사건만으로 채우지 말고 방송·영화·공연·책·인물·유행·"
    "계절·스포츠에서 나눠 고른다(한 갈래에 쏠리면 그 갈래를 쓰는 자리만 채워지고 나머지는 "
    "빈다). 각 항목은 **검색창에 넣어 찾을 수 있는 이름 그 자체**로 쓴다 — 작품·인물·"
    "행사의 이름만 남기고 갈래·설명·수식을 붙이지 않는다(이름이 길면 검색이 빗나간다). "
    "번호·기호 없이 한 줄에 하나씩만 쓴다."
)


def build_slots(settings: Settings) -> list[SlotSpec]:
    """설정에서 오늘의 슬롯 목록을 만든다 — 자동 생성 대상의 단일 출처.

    서빙의 lazy 트리거·활성 풀 필터·어드민 생성이 **같은 목록**을 본다. 목록이 여러 벌이면
    구성을 바꿨을 때 한쪽만 고쳐 옛 슬롯이 고아로 남는다.
    """
    specs: list[SlotSpec] = []
    for section in settings.starter_sections:
        specs.append(
            SlotSpec(
                key=section,
                ask=(
                    "재료는 오늘 그 코너에 실제로 오른 상품이다. 그 책 한 권을 가리켜, "
                    "**상품 페이지를 열어야 알 수 있는 것**을 묻는다 — 어떤 내용인지, 어떤 "
                    "이야기인지, 누구에게 맞는지 같은 것이다. 제목은 『』로 감싼다."
                ),
                observe=observe_corner_products,
            )
        )
    if settings.starter_pick_from:
        specs.append(
            SlotSpec(
                key=f"{settings.starter_pick_from}{_PICK_SUFFIX}",
                ask=(
                    "재료는 그 코너의 분야 목록이다. 상품 하나를 지목하지 말고 **분야 하나를 "
                    "골라 골라 달라고** 한다 — 여기서만 할 수 있는 일이 '골라주기'이므로 "
                    "조건이나 상황을 하나 얹어 고르게 한다(어떤 사람에게 맞는지, 어떤 때 "
                    "읽는지 같은 것). 특정 책 제목·저자·가격은 문장에 넣지 않는다."
                ),
                observe=observe_corner_categories,
            )
        )
    if settings.starter_trend_topics > 0:
        specs.append(
            SlotSpec(
                key=_TREND_SLOT,
                ask=(
                    "재료는 오늘 사람들이 이야기하는 화제 가운데 **Yes24에 관련 책이 있는** "
                    "것들이다. 화제 하나를 골라 그것을 책으로 잇되, **그런 책이 있다고 "
                    "단정하지 않는다** — '있어?'·'뭐가 있어?'처럼 찾아 달라고 묻는다. "
                    "검색으로 찾은 책이 그 화제와 정말 이어지는지는 알 수 없고(같은 이름의 "
                    "다른 책일 수 있다), 단정한 문장은 답이 빈약할 때 어긋난 질문이 된다. "
                    "화제 이름은 그것이 "
                    "**무엇인지 알 수 있게 갈래를 붙여** 쓴다(방송·영화·인물처럼) — 이름만 "
                    "덩그러니 두면 읽는 사람이 무엇을 가리키는지 모른다. "
                    "특정 책 제목은 넣지 않는다."
                ),
                observe=observe_web_topics,
            )
        )
        specs.append(
            SlotSpec(
                key=_GENERAL_SLOT,
                ask=(
                    "재료는 오늘 사람들이 이야기하는 화제 가운데 **책과 이어지지 않는** "
                    "것들이다. 화제 하나를 골라 그 화제 자체를 묻는다 — 무슨 일인지, 지금 "
                    "어떤 상황인지다. 책·독서를 끌어들이지 않는다(그 일은 다른 칩이 한다). "
                    "화제 이름은 그것이 **무엇인지 알 수 있게 갈래를 붙여** 쓴다(방송·영화·"
                    "인물처럼) — 이름만 덩그러니 두면 읽는 사람이 무엇을 가리키는지 모른다. "
                    "확인되지 않은 사실을 단정하지 않는다 — 묻는 문장이지 주장이 아니다."
                ),
                observe=observe_web_topics,
            )
        )
    if settings.starter_policy_source:
        specs.append(
            SlotSpec(
                key=_POLICY_SLOT,
                ask=(
                    "재료는 Yes24 고객센터가 실제로 답하고 있는 질문이다. 항목 하나를 골라 "
                    "그것이 다루는 것을 묻되, 고객센터 말투를 그대로 옮기지 말고 사람이 "
                    "말하듯 바꿔 쓴다. **여러 사람이 겪을 법한 것**을 고른다 — 판매자 전용· "
                    "특정 기기 설정처럼 좁은 쪽은 피한다. 답을 쓰지 않고 묻기만 한다."
                ),
                observe=observe_faq,
            )
        )
    return specs


def auto_slots(settings: Settings) -> list[str]:
    return [spec.key for spec in build_slots(settings)]


def _observe_param(spec: SlotSpec, settings: Settings) -> str:
    """관측기가 슬롯마다 달리 볼 대상 — 명세 키에서 파생한다(코드에 슬롯 열거를 두지 않는다)."""
    if spec.observe is observe_corner_categories:
        return settings.starter_pick_from
    if spec.observe is observe_web_topics:
        return "books" if spec.key == _TREND_SLOT else "plain"
    if spec.observe is observe_faq:
        return settings.starter_policy_source
    return ""


async def generate_slot(
    spec: SlotSpec, ctx: ObserveContext, *, shared: dict | None = None
) -> dict:
    """한 슬롯의 관측 → 생성(1콜) → 출구 검증. 슬롯이 무엇이든 절차는 같다.

    `shared`는 같은 관측기를 쓰는 슬롯끼리 바깥 호출을 나눠 쓰기 위한 자리다(웹 화제는
    한 번 모아 두 슬롯이 나눈다). 관측 0건은 모델 콜 없이 failed다 — 빈 성공으로 위장하지
    않는다(원칙 7).
    """
    settings = ctx.settings
    try:
        observed = await spec.observe(spec, ctx)
    except Exception as exc:  # noqa: BLE001 — 관측 실패는 그 슬롯만 접는다
        return _failed(f"관측 실패: {type(exc).__name__}: {exc}")
    if not observed.materials:
        return _failed("관측 0건(재료 없음)")

    materials = observed.materials
    # 재료보다 많이 요구하지 않는다 — min_items가 재료 수를 넘으면 모델은 같은 것을
    # 되풀이해 채우고 그 문장들은 중복으로 폐기된다(슬롯이 통째로 비는 실제 경로였다).
    count = min(settings.starter_per_slot, len(materials))
    payload: dict = {"materials": [_material_payload(m) for m in materials]}
    if observed.note:
        payload["note"] = observed.note
    try:
        response = await asyncio.wait_for(
            ctx.genai_client.aio.models.generate_content(
                model=settings.starter_model,
                contents=json.dumps(payload, ensure_ascii=False),
                config=types.GenerateContentConfig(
                    system_instruction=_instruction(spec, settings.starter_max_chars),
                    temperature=1.0,
                    response_mime_type="application/json",
                    response_schema=_response_schema([m.ref for m in materials], count),
                ),
            ),
            timeout=settings.starter_timeout_s,
        )
        record_usage("starter", response.usage_metadata, model=settings.starter_model)
        raw_items = json.loads(response.text or "{}").get("items") or []
    except Exception as exc:  # noqa: BLE001 — 백그라운드 생성: 실패는 슬롯 failed로 접는다
        return _failed(f"생성 실패: {type(exc).__name__}: {exc}")

    kept, dropped = validate_items(raw_items, materials, max_chars=settings.starter_max_chars)
    total_dropped = sum(dropped.values())
    if not kept:
        return _failed(f"출구 검증에서 전부 폐기({dropped})", total_dropped)
    items = [
        {
            "slot": spec.key,
            # 상품을 가리킨 문구만 상품 번호를 남긴다 — 반복 회피와 링크가 그것을 쓴다.
            # 재료가 선언한 값을 그대로 쓴다(ref 문자열을 해석하지 않는다).
            "goods_no": item["goods_no"],
            "text": item["text"],
            "source_url": None,
        }
        for item in kept
    ]
    logger.info(
        f"starters 생성: slot={spec.key} 재료={len(materials)} 요청={count} "
        f"생존={len(items)} 폐기={dropped}"
    )
    return {"status": "ok", "items": items, "dropped": total_dropped, "detail": ""}


async def build_candidates(
    slots: list[str],
    settings: Settings,
    *,
    today: dt.date,
    client=None,
    genai_client=None,
    exclude: dict[str, set[int]] | None = None,
) -> dict[str, dict]:
    """슬롯들의 관측→생성→검증. 저장은 하지 않는다(서비스·라이브 점검이 공유).

    반환: `{slot: {status, items, dropped, detail}}`. 슬롯 하나가 실패해도 다른 슬롯은
    그대로 간다 — 실패한 슬롯은 어제 세트를 유지한다.
    """
    client = client or get_client(settings)
    genai_client = genai_client or get_genai_client()
    by_key = {spec.key: spec for spec in build_slots(settings)}
    results: dict[str, dict] = {}
    shared: dict = {}
    for key in slots:
        spec = by_key.get(key)
        if spec is None:
            results[key] = _failed(f"자동 생성 슬롯이 아닙니다: {key!r}")
            continue
        ctx = ObserveContext(
            settings=settings,
            client=client,
            genai_client=genai_client,
            today=today,
            exclude=(exclude or {}).get(key, set()),
            param=_observe_param(spec, settings),
            shared=shared,
        )
        results[key] = await generate_slot(spec, ctx)
    return results


# ── 서비스(MySQL) ────────────────────────────────────────────────────────────


class StarterService(MysqlBackedService):
    """starters·starter_runs 읽기/쓰기 + lazy 생성 트리거(프로세스 싱글턴, 풀 1개)."""

    _instance: StarterService | None = None

    def __init__(self, pool_factory=None) -> None:
        settings = get_settings()
        super().__init__(
            mysql_pool_kwargs(settings.session_db_url, maxsize=settings.starter_pool_max),
            pool_factory,
            unavailable_detail="초기 질문 저장소가 없는 구성입니다(세션 DB가 MySQL이 아님).",
            failure_detail="초기 질문 조회에 실패했습니다.",
        )
        # 프로세스 내 single-flight 생성 태스크. 끝난 태스크는 다음 서빙이 교체한다.
        self._generation: asyncio.Task | None = None
        # 생성 직렬화 — lazy 트리거와 어드민 force가 겹치면 같은 슬롯에 두 세트가 동시에
        # 활성될 수 있다(이전 auto가 0건이면 비활성 UPDATE가 서로를 못 막는다). 프로세스
        # 하나에서 생성이 겹치는 것만이라도 여기서 막는다.
        self._generation_lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> StarterService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 서빙 ─────────────────────────────────────────────────────────────

    async def active_pool(self, today: dt.date, slot: str | None = None) -> list[dict]:
        """활성 조건(active=1 + 유효기간)은 SQL이 판정한다 — 코드에서 날짜를 다시 거르지 않는다.

        자동 생성분은 **지금 생성 대상인 슬롯만** 살린다(auto_slots). 설정에서 빠진 코너의
        옛 행은 아무도 갱신하지 않는 채 계속 서빙되기 때문이다(소스를 바꾸면 옛 슬롯이 고아가
        된다). 수동 항목은 슬롯 목록과 무관하게 운영자가 소유한다.
        """
        sql = (
            f"SELECT {', '.join(_POOL_COLUMNS)} FROM starters WHERE active = 1 "
            "AND (valid_from IS NULL OR valid_from <= %s) "
            "AND (valid_until IS NULL OR valid_until >= %s)"
        )
        params: tuple = (today, today)
        sections = auto_slots(get_settings())
        placeholders = ", ".join(["%s"] * len(sections))
        sql += f" AND (source <> 'auto' OR slot IN ({placeholders}))" if sections else ""
        params += tuple(sections)
        if slot:
            sql += " AND slot = %s"
            params += (slot,)
        rows = await self._run(sql, params, fetch_all=True)
        return [dict(zip(_POOL_COLUMNS, row)) for row in rows or ()]

    async def recent_goods(
        self, slots: list[str], today: dt.date, window_days: int
    ) -> dict[str, set[int]]:
        """최근 window_days 안에 이미 문장으로 쓴 상품 번호 — 슬롯별 집합.

        비활성 항목도 센다. "지웠으니 다시 써도 된다"가 아니라 "최근에 사용자가 봤다"가
        기준이다(반복 노출을 줄이는 것이 목적).
        """
        if not slots or window_days <= 0:
            return {}
        placeholders = ", ".join(["%s"] * len(slots))
        rows = await self._run(
            f"SELECT slot, goods_no FROM starters WHERE goods_no IS NOT NULL "
            f"AND slot IN ({placeholders}) AND run_date >= %s",
            (*slots, today - dt.timedelta(days=window_days)),
            fetch_all=True,
        )
        recent: dict[str, set[int]] = {}
        for slot, goods_no in rows or ():
            recent.setdefault(slot, set()).add(int(goods_no))
        return recent

    async def slots_run_today(self, today: dt.date) -> set[str]:
        rows = await self._run(
            "SELECT slot FROM starter_runs WHERE run_date = %s", (today,), fetch_all=True
        )
        return {row[0] for row in rows or ()}

    async def serve(
        self, *, n: int, slot: str | None, today: dt.date, rng: random.Random | None = None
    ) -> dict:
        """서빙 세트 1건 — 선택 → 서빙 로그 한 줄 → (필요 시) 백그라운드 생성 기동 → 즉답."""
        settings = get_settings()
        picked = pick_starters(await self.active_pool(today, slot), n, rng or random)
        set_id = uuid.uuid4().hex
        logger.info(
            f"starters served set_id={set_id} ids={[p['id'] for p in picked]} "
            f"slots={[p['slot'] for p in picked]}"
        )
        ran = await self.slots_run_today(today)
        missing = [slot for slot in auto_slots(settings) if slot not in ran]
        if missing:
            self.ensure_generation(missing, today)
        return {"set_id": set_id, "starters": [Starter.of(p, settings) for p in picked]}

    async def refresh_loop(self) -> None:
        """오늘자 생성이 없으면 만들고 다음 주기까지 잔다 — 첫 방문자도 오늘 것을 본다.

        서빙 경로의 lazy 트리거와 **같은 판정**을 쓴다(오늘자 run이 없는 자동 슬롯). 그래서
        루프가 꺼져 있어도(interval 0) 동작이 사라지지 않고 트리거가 첫 요청으로 돌아갈
        뿐이다. 여러 워커가 함께 돌아도 실제 생성은 하루 한 번 — 선점은 DB PK가 가른다.

        **첫 동작은 한 주기 뒤**다. 기동 직후는 서빙 경로의 lazy 트리거가 담당하므로 여기서
        서두를 이유가 없고, 그래야 짧게 떴다 지는 프로세스(테스트·헬스체크)가 아무 일도
        하지 않는다. 이 루프의 몫은 "트래픽이 없는 시간대에도 오늘 것이 준비되는 것"이다.

        실패는 삼키고 다음 주기에 다시 본다. 이 루프가 서빙을 막아서는 안 된다.
        """
        interval = get_settings().starter_refresh_interval_s
        while interval > 0:
            try:
                today = _today()
                missing = [s for s in auto_slots(get_settings()) if s not in
                           await self.slots_run_today(today)]
                if missing:
                    await self.run_generation(missing, today)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 주기 작업: 다음 주기에 다시 본다
                logger.warning(f"starters 주기 갱신 실패(다음 주기 재시도): {exc}")
            await asyncio.sleep(interval)

    def ensure_generation(self, slots: list[str], today: dt.date) -> None:
        """생성 태스크를 프로세스당 하나만 띄운다(진행 중이면 무동작 — single-flight)."""
        if self._generation is not None and not self._generation.done():
            return
        self._generation = asyncio.get_running_loop().create_task(
            self._generate_guarded(slots, today)
        )

    async def _generate_guarded(self, slots: list[str], today: dt.date) -> None:
        try:
            await self.run_generation(slots, today)
        except Exception as exc:  # noqa: BLE001 — 백그라운드: 서빙에 얹히지 않는다(로그만)
            logger.warning(f"starters 자동 생성 실패(무시): {type(exc).__name__}: {exc}")

    # ── 생성·저장 ─────────────────────────────────────────────────────────

    async def _claim_run(self, slot: str, today: dt.date, force: bool) -> bool:
        """오늘자 (slot, run_date) 행을 선점한다. PK가 멀티워커 잠금이다 — INSERT IGNORE의
        rowcount 0은 다른 워커(또는 앞선 실행)가 이미 잡았다는 뜻. force는 상태를 running으로
        되돌려 재실행한다(어드민 즉시 생성)."""
        if force:
            await self._run(
                "INSERT INTO starter_runs (slot, run_date, status, detail) "
                "VALUES (%s, %s, 'running', NULL) "
                "ON DUPLICATE KEY UPDATE status = 'running', detail = NULL, "
                "started_at = CURRENT_TIMESTAMP",
                (slot, today),
            )
            return True
        rowcount, _ = await self._run(
            "INSERT IGNORE INTO starter_runs (slot, run_date, status) VALUES (%s, %s, 'running')",
            (slot, today),
        )
        if rowcount > 0:
            return True
        # 선점만 되고 마감되지 않은 행은 재선점한다 — 프로세스가 강제 종료되면(배포 교체)
        # finally 마감도, 종료 훅의 취소도 돌지 못해 running이 하루 남고 그 슬롯은 그날
        # 재시도가 없다. 상한은 생성 한 번이 걸릴 수 있는 최대치의 넉넉한 배수다.
        stale_after = int(get_settings().starter_timeout_s * _STALE_RUN_FACTOR)
        rowcount, _ = await self._run(
            "UPDATE starter_runs SET status = 'running', started_at = CURRENT_TIMESTAMP "
            "WHERE slot = %s AND run_date = %s AND status = 'running' "
            "AND started_at < CURRENT_TIMESTAMP - INTERVAL %s SECOND",
            (slot, today, stale_after),
        )
        return rowcount > 0

    async def run_generation(
        self, slots: list[str], today: dt.date, *, force: bool = False
    ) -> dict[str, dict]:
        """슬롯들의 잠금 선점 → 관측·생성·검증 → 저장.

        반환 `{slot: {status, inserted, dropped, detail}}`.

        저장은 슬롯마다 **한 트랜잭션**이다(이전 auto 비활성 + 새 항목 INSERT + run ok) —
        문장을 따로 보내면 autocommit이라 "옛것은 껐는데 새것은 없는" 빈 슬롯이 남을 수 있다.
        실패 슬롯은 이전 auto를 건드리지 않고 run만 failed로 남긴다(이전 것 유지, D4).
        """
        async with self._generation_lock:
            return await self._run_generation_locked(slots, today, force)

    async def _run_generation_locked(
        self, slots: list[str], today: dt.date, force: bool
    ) -> dict[str, dict]:
        results: dict[str, dict] = {}
        claimed: list[str] = []
        for slot in slots:
            if await self._claim_run(slot, today, force):
                claimed.append(slot)
            else:
                results[slot] = {
                    "status": "skipped", "inserted": 0, "dropped": 0,
                    "detail": "오늘자 실행이 이미 있습니다(force=1로 재실행)",
                }
        if not claimed:
            return results
        try:
            await self._generate_into(claimed, today, results)
        finally:
            # 선점한 슬롯은 **무슨 일이 있어도 마감한다**. 예외로 빠져나가면 run이 running으로
            # 남아 그날 재시도가 사라진다(어제 세트가 계속 서빙된다).
            for slot in claimed:
                if slot not in results:
                    await self._finish_run(slot, today, "failed", "생성 중 중단")
        logger.info(f"starters 생성 저장: date={today} results={results}")
        return results

    async def _generate_into(
        self, claimed: list[str], today: dt.date, results: dict[str, dict]
    ) -> None:
        settings = get_settings()
        exclude = await self.recent_goods(claimed, today, settings.starter_repeat_window_days)
        candidates = await build_candidates(
            claimed, settings, today=today, exclude=exclude
        )
        for slot in claimed:
            result = candidates.get(slot) or _failed("생성 결과 없음")
            items = result["items"]
            summary = (
                f"inserted={len(items)} dropped={result['dropped']} {result['detail']}".strip()
            )
            if items:
                await self._run_all(
                    [
                        (
                            "UPDATE starters SET active = 0 "
                            "WHERE slot = %s AND source = 'auto' AND active = 1",
                            (slot,),
                        ),
                        *(
                            (
                                "INSERT INTO starters (slot, text, source, goods_no, source_url, "
                                "run_date) VALUES (%s, %s, 'auto', %s, %s, %s)",
                                (slot, item["text"], item["goods_no"], item["source_url"], today),
                            )
                            for item in items
                        ),
                        self._finish_run_statement(slot, today, "ok", summary),
                    ]
                )
            else:
                await self._finish_run(slot, today, "failed", summary)
            results[slot] = {
                "status": result["status"],
                "inserted": len(items),
                "dropped": result["dropped"],
                "detail": result["detail"],
            }

    @staticmethod
    def _finish_run_statement(slot: str, today: dt.date, status: str, detail: str) -> tuple:
        """run 마감 문장 — detail은 컬럼 폭에서 자른다.

        자르지 않으면 예외 본문(모델 오류는 수백 자를 쉽게 넘는다)이 컬럼을 넘겨 **실패를
        기록하는 UPDATE 자체가 실패**하고, 그 슬롯이 running으로 고착된다.
        """
        return (
            "UPDATE starter_runs SET status = %s, detail = %s WHERE slot = %s AND run_date = %s",
            (status, detail[:_RUN_DETAIL_MAX_CHARS], slot, today),
        )

    async def _finish_run(self, slot: str, today: dt.date, status: str, detail: str) -> None:
        await self._run(*self._finish_run_statement(slot, today, status, detail))

    # ── 어드민 ────────────────────────────────────────────────────────────

    async def list_items(
        self, *, slot: str | None, source: str | None, active: int | None
    ) -> list[dict]:
        where: list[str] = []
        params: list = []
        for column, value in (("slot", slot), ("source", source), ("active", active)):
            if value is not None:
                where.append(f"{column} = %s")
                params.append(value)
        sql = f"SELECT {', '.join(_ADMIN_COLUMNS)} FROM starters"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY slot, pinned DESC, id DESC"
        rows = await self._run(sql, tuple(params), fetch_all=True)
        return [dict(zip(_ADMIN_COLUMNS, row)) for row in rows or ()]

    async def add_item(
        self,
        *,
        slot: str,
        text: str,
        pinned: bool,
        valid_from: dt.date | None,
        valid_until: dt.date | None,
    ) -> int:
        _, new_id = await self._run(
            "INSERT INTO starters (slot, text, source, pinned, valid_from, valid_until) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (slot, text, "manual", int(pinned), valid_from, valid_until),
        )
        return new_id

    async def update_item(self, item_id: int, fields: dict) -> bool:
        """주어진 필드만 갱신한다(불리언은 TINYINT로). 반환은 **그 항목이 있었는가**.

        rowcount로 판정하지 않는다 — MySQL은 값이 그대로면 0을 돌려주므로 "같은 값으로 다시
        저장"이 "없는 항목"과 구별되지 않는다(그러면 정상 편집이 404가 된다).
        """
        assignments = ", ".join(f"{name} = %s" for name in fields)
        values = tuple(int(v) if isinstance(v, bool) else v for v in fields.values())
        rowcount, _ = await self._run(
            f"UPDATE starters SET {assignments} WHERE id = %s", (*values, item_id)
        )
        return bool(rowcount) or await self._exists(item_id)

    async def deactivate_item(self, item_id: int) -> bool:
        rowcount, _ = await self._run("UPDATE starters SET active = 0 WHERE id = %s", (item_id,))
        return bool(rowcount) or await self._exists(item_id)

    async def _exists(self, item_id: int) -> bool:
        rows = await self._run("SELECT 1 FROM starters WHERE id = %s", (item_id,), fetch_all=True)
        return bool(rows)

    async def runs(self, *, today: dt.date, days: int) -> list[dict]:
        rows = await self._run(
            f"SELECT {', '.join(_RUN_COLUMNS)} FROM starter_runs "
            "WHERE run_date >= %s - INTERVAL %s DAY ORDER BY run_date DESC, slot",
            (today, days),
            fetch_all=True,
        )
        return [dict(zip(_RUN_COLUMNS, row)) for row in rows or ()]


def start_starter_refresh(app, settings: Settings) -> None:
    """주기 갱신 루프를 앱에 띄운다 — 기능이 꺼져 있거나 주기가 0이면 무동작.

    참조를 app.state에 잡아 GC 취소를 막고, 종료 훅이 그것을 취소한다.
    """
    # 저장소가 없는 구성(로컬 sqlite)에서는 띄우지 않는다. 설정만 보고 판단하는 이유:
    # 서비스 인스턴스를 만들어 확인하면 그 전역 싱글턴이 남아, 뒤따르는 테스트의 종료 훅이
    # 그것을 닫다 터진다(실측).
    if (
        not settings.starter_model
        or settings.starter_refresh_interval_s <= 0
        or db_dialect(settings.session_db_url) == SQLITE_DIALECT
    ):
        return

    async def _delayed_loop() -> None:
        # **첫 동작은 한 주기 뒤**다. 기동 직후는 서빙 경로의 lazy 트리거가 담당하므로 여기서
        # 서두를 이유가 없고, 그래야 짧게 떴다 지는 프로세스(테스트·헬스체크)가 서비스
        # 싱글턴조차 만들지 않는다 — 만들어 두면 그것을 물려받은 다른 종료 훅이 터진다.
        await asyncio.sleep(settings.starter_refresh_interval_s)
        await StarterService.get_instance().refresh_loop()

    app.state.starter_refresh = asyncio.create_task(_delayed_loop())


async def close_starter_service() -> None:
    """앱 종료 훅 — 서비스가 실제로 만들어졌을 때만 풀을 닫는다(close_user_data_service 대칭).

    진행 중인 생성 태스크를 먼저 취소한다. 취소하지 않고 풀만 닫으면 그 태스크는 선점해 둔
    `starter_runs` 행을 running으로 남긴 채 사라지고(배포 교체 창), 그 슬롯은 그날 재시도가
    없다.
    """
    service = StarterService._instance
    if service is None:
        return
    task = service._generation
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await service.close()


# ── 라우트 ───────────────────────────────────────────────────────────────────


class Starter(BaseModel):
    """서빙 항목. `text`는 누르면 **그대로** 보내는 문장이다 — 자르거나 고치지 않는다."""

    id: int = Field(description="풀 항목 id(클릭 대조·어드민 편집 키)")
    slot: str = Field(description="질문 유형(bestseller·new·policy …). 아이콘 매핑은 이 값으로")
    label: str = Field(description="슬롯 표시 라벨")
    text: str = Field(description="칩 문장 — 누르면 이 값을 그대로 /chat/stream message로 보낸다")
    source: str = Field(description="auto(오늘 관측 생성) | manual(운영 등록)")
    goods_no: int | None = Field(default=None, description="auto만: 문장이 가리키는 상품 번호")
    url: str | None = Field(default=None, description="auto만: 그 상품 페이지")
    run_date: str | None = Field(default=None, description="auto만: 생성일(YYYY-MM-DD)")

    @classmethod
    def of(cls, row: dict, settings: Settings) -> Starter:
        """풀 행 → 서빙 항목. 상품 참조가 없는 행(manual)의 세 필드는 None이고, 라우트의
        `response_model_exclude_none`이 응답에서 뺀다(키를 지우는 분기를 따로 두지 않는다)."""
        goods_no = row.get("goods_no")
        run_date = row.get("run_date")
        return cls(
            id=row["id"],
            slot=row["slot"],
            label=_label(row["slot"], settings),
            text=row["text"],
            source=row["source"],
            goods_no=goods_no,
            url=product_url(settings.yes24_base_url, str(goods_no)) if goods_no else None,
            run_date=run_date.isoformat() if run_date else None,
        )


class StartersResponse(BaseModel):
    set_id: str = Field(
        description="이 서빙 세트의 id(서버 로그 `starters served set_id=…`와 대조)"
    )
    starters: list[Starter] = Field(
        description="배열 길이를 가정하지 말 것 — 슬롯 수·풀 상태에 따라 n보다 적을 수 있다"
    )


_SlotText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=_SLOT_MAX_CHARS)
]


def _validated_text(value: str) -> str:
    """수동 문장의 검증 — 자동 생성분과 **같은 정규화**를 거쳐 같은 상한을 받는다.

    상한을 모듈 임포트가 아니라 여기서 읽는 이유: 임포트 시점에 읽으면 값이 동결돼 설정
    주입이 먹지 않는다. 제로폭 문자만으로 이루어진 문장은 여기서 걸린다(빈 칩 서빙 실측).
    """
    text = _squash(value)
    limit = get_settings().starter_max_chars
    if not text:
        raise ValueError("빈 문장입니다")
    if len(text) > limit:
        raise ValueError(f"{limit}자를 넘습니다(누르면 그대로 전송되는 문장이라 자르지 않는다)")
    return text


# 수동 문장도 자동분과 같은 상한을 받는다 — 칩 한 줄의 계약은 출처와 무관하다(절단 대신 422).
_StarterText = Annotated[str, AfterValidator(_validated_text)]


def _check_valid_range(item: Any) -> Any:
    """유효기간 역전은 422 — 저장되면 어느 날에도 서빙되지 않는 항목이 조용히 남는다."""
    if item.valid_from and item.valid_until and item.valid_from > item.valid_until:
        raise ValueError("valid_from이 valid_until보다 늦습니다")
    return item


class StarterCreate(BaseModel):
    slot: _SlotText
    text: _StarterText
    pinned: bool = False
    valid_from: dt.date | None = None
    valid_until: dt.date | None = None

    _range = model_validator(mode="after")(_check_valid_range)


class StarterPatch(BaseModel):
    """부분 갱신 — **실린 필드만** 바꾼다. 빈 본문은 422.

    text·pinned·active에 null을 실으면 422다. DDL이 NOT NULL인 컬럼인데, STRICT가 아닌
    MySQL은 NULL을 ''·0으로 조용히 강등해 **빈 문장이 그대로 서빙**됐다(실측). 유효기간만
    null을 받는다 — 그쪽은 "제한 없음"이라는 뜻이다.
    """

    text: _StarterText | None = None
    pinned: bool | None = None
    active: bool | None = None
    valid_from: dt.date | None = None
    valid_until: dt.date | None = None

    _range = model_validator(mode="after")(_check_valid_range)

    @model_validator(mode="after")
    def _fields_present(self) -> StarterPatch:
        if not self.model_fields_set:
            raise ValueError("바꿀 필드가 하나도 없습니다")
        nulled = [
            name
            for name in ("text", "pinned", "active")
            if name in self.model_fields_set and getattr(self, name) is None
        ]
        if nulled:
            raise ValueError(f"null을 받지 않는 필드입니다: {', '.join(nulled)}")
        return self


def register_starters(app: FastAPI, settings: Settings) -> None:
    """starter_model이 설정된 경우에만 공개·어드민 라우트를 등록한다(빈 값이면 404)."""
    if not settings.starter_model:
        return
    configured = [*settings.starter_sections, *filter(None, [settings.starter_pick_from])]
    unknown = [slot for slot in configured if slot not in BROWSE_SEED_URLS]
    if unknown:
        raise ValueError(
            f"starter_sections·starter_pick_from에 BROWSE_SEED_URLS 밖의 키가 있습니다: {unknown} "
            f"(허용: {', '.join(BROWSE_SEED_URLS)})"
        )

    @app.get(
        "/chat/starters",
        tags=["starters"],
        response_model=StartersResponse,
        response_model_exclude_none=True,
        summary="빈 화면의 초기 질문 칩",
        description=(
            "서버 회전 풀에서 초기 질문을 뽑아 준다 — 요청마다 슬롯(질문 유형)별 1개를 무작위로 "
            "고르고 순서를 섞는다. **진입 시 1회** 호출하고(폴링 금지), `text`는 절단·편집 없이 "
            "그대로 `/chat/stream`의 message로 보낸다. 저장소가 없는 구성이면 503이므로 "
            "클라이언트는 자체 폴백 문구를 갖는다."
        ),
        responses={
            503: {"description": "초기 질문 저장소가 없는 구성(로컬 sqlite) 또는 조회 실패"}
        },
    )
    async def chat_starters(
        n: Annotated[int | None, Query(ge=1, description="개수(기본 서버 설정값)")] = None,
        slot: Annotated[
            str | None, Query(max_length=_SLOT_MAX_CHARS, description="슬롯 필터(선택)")
        ] = None,
        user: Annotated[AuthenticatedUser | None, Depends(get_authenticated_user)] = None,
    ) -> dict:
        return await StarterService.get_instance().serve(
            n=n or settings.starter_count, slot=slot, today=_today()
        )

    # 어드민 API — admin_password가 비어 있으면 미등록(404, admin.py 관례). 자격 판정은
    # admin.require_admin이 소유한다(쿠키 또는 x-admin-key).
    if not settings.admin_password:
        return

    admin = {"include_in_schema": False, "dependencies": [Depends(require_admin)]}

    @app.get("/admin/starters", **admin)
    async def admin_list(
        slot: str | None = None, source: str | None = None, active: int | None = None
    ) -> dict:
        items = await StarterService.get_instance().list_items(
            slot=slot, source=source, active=active
        )
        return {"items": items}

    @app.post("/admin/starters", status_code=201, **admin)
    async def admin_add(body: StarterCreate) -> dict:
        new_id = await StarterService.get_instance().add_item(**body.model_dump())
        return {"id": new_id, **body.model_dump()}

    @app.patch("/admin/starters/{item_id}", **admin)
    async def admin_update(item_id: int, body: StarterPatch) -> dict:
        # 선언 순서로 SET 절을 만든다 — model_fields_set은 집합이라 순서가 임의다.
        fields = {
            name: getattr(body, name)
            for name in StarterPatch.model_fields
            if name in body.model_fields_set
        }
        if not await StarterService.get_instance().update_item(item_id, fields):
            raise HTTPException(status_code=404, detail=f"없는 초기 질문입니다: {item_id}")
        return {"id": item_id, "updated": sorted(fields)}

    @app.delete("/admin/starters/{item_id}", **admin)
    async def admin_delete(item_id: int) -> dict:
        if not await StarterService.get_instance().deactivate_item(item_id):
            raise HTTPException(status_code=404, detail=f"없는 초기 질문입니다: {item_id}")
        return {"id": item_id, "active": False}

    @app.post("/admin/starters/generate", **admin)
    async def admin_generate(slot: str | None = None, force: int = 0) -> dict:
        """즉시 생성(동기 — 결과를 그대로 돌려준다). force=1이면 오늘자 run이 있어도 재실행."""
        allowed = auto_slots(settings)
        if slot is not None and slot not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"자동 생성 슬롯이 아닙니다: {slot!r} (허용: {allowed})",
            )
        slots = [slot] if slot else allowed
        return await StarterService.get_instance().run_generation(
            slots, _today(), force=bool(force)
        )

    @app.get("/admin/starters/runs", **admin)
    async def admin_runs(days: int = 7) -> dict:
        runs = await StarterService.get_instance().runs(today=_today(), days=days)
        return {"runs": runs}
