"""초기 질문(스타터) 회전 풀 — MySQL 풀 + 일 1회 자동 생성 + 공개·어드민 라우트.

빈 화면의 질문 칩을 프론트 하드코딩 3개에서 **서버가 관리하는 회전 풀**로 바꾼다
(설계·근거: docs/starters-design.md, DDL: scripts/starters.sql). 이 모듈이 풀의 읽기·쓰기·
생성·라우트를 전부 소유한다.

구조(결정 D1~D10의 코드 대응):
- **슬롯 = 질문 유형**이고 값은 시드 키다. 자동 슬롯은 `settings.starter_sections`
  (BROWSE_SEED_URLS의 키)와 골라주기 슬롯 `{starter_pick_from}-pick` 하나이고, 그 밖의
  슬롯(정책·범용)은 수동 풀이다 — 코드에 슬롯 열거가 없고, 칩 라벨은 `starter_labels`의
  사용자 언어 문구(없으면 시드 표의 코너 이름)다.
- **초기 질문은 콘텐츠·책·문화 중심**이다. `general` 슬롯 값은 기존 계약을 유지한다.
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
import time
import unicodedata
import uuid
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from google.genai import types
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from yes24_agent.admin import require_admin
from yes24_agent.auth import AuthenticatedUser, get_authenticated_user
from yes24_agent.config import Settings, get_genai_client, get_settings
from yes24_agent.db import MysqlBackedService
from yes24_agent.session_service import mysql_pool_kwargs
from yes24_agent.sources import KST
from yes24_agent.tools.yes24_search import get_client
from yes24_agent.usage import record_usage
from yes24_agent.yes24.client import Yes24FetchError
from yes24_agent.yes24.parsers import (
    _PUBLICATION_DATE_RE,
    ParseError,
    parse_browse_list,
    parse_category_links,
    parse_search,
)
from yes24_agent.yes24.urls import (
    BROWSE_SEED_URLS,
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
# 오늘의 화제 슬롯. 재료가 시드 코너가 아니라 **바깥 세상**(웹 그라운딩)이라 키를 따로 둔다.
_TREND_SLOT = "trend"


def pick_slot(section: str) -> str:
    return f"{section}{_PICK_SUFFIX}"


def pick_base(slot: str) -> str | None:
    """골라주기 슬롯이면 그 재료 코너, 아니면 None."""
    base = slot.removesuffix(_PICK_SUFFIX)
    return base if base != slot and base in BROWSE_SEED_URLS else None


def auto_slots(settings: Settings) -> list[str]:
    """자동 생성 대상 슬롯 — 상품 지목형(시드 키) + 골라주기 + 오늘의 화제(설정 시).

    서빙의 lazy 트리거·활성 풀 필터·어드민 생성이 **같은 목록**을 본다. 목록이 여러 벌이면
    코너를 바꿨을 때 한쪽만 고쳐 옛 슬롯이 고아로 남는다.
    """
    slots = list(settings.starter_sections)
    if settings.starter_pick_from:
        slots.append(pick_slot(settings.starter_pick_from))
    if settings.starter_trend_topics > 0:
        slots.append(_TREND_SLOT)
    return slots


def _instruction(max_chars: int) -> str:
    """생성 지시문. 길이 상한을 문구에 실어야 모델이 **제목이 긴 상품을 피해** 고른다 —
    상한은 출구가 어차피 잡지만, 모르고 쓰면 긴 제목의 상품이 통째로 폐기돼 재료가 준다."""
    return _GENERATE_INSTRUCTION + (
        f" 문장 전체는 {max_chars}자를 넘기지 않는다 — 제목이 길어 넘칠 것 같으면 그 상품은 "
        "고르지 말고 제목이 짧은 다른 상품을 고른다."
    ) + _EDITORIAL_SCOPE


# 문구 계약은 프롬프트에 두고, 개수·참조·순서는 스키마가 강제한다(enrichment 관례). 완성 문장
# 예시는 넣지 않는다 — 예시 문장은 내용까지 복사된다(빈 꼴 틀만).
_EDITORIAL_SCOPE = (
    "마지막 선택 기준이며 앞선 책 연결·문구 지시보다 우선한다. "
    "초기 질문은 책·독서·영화·드라마·음악·공연·전시·웹툰·게임 등 콘텐츠와 문화 경험을 "
    "중심으로 한다. 일반 도서의 내용·해석·감상과 Yes24 이용 질문은 허용한다. "
    "정당·정치인의 활동, 선거·국회, 사회 사건, 경제 시황 등 현안 뉴스 자체를 묻거나 "
    "관련 책·문화 이야기로 포장하지 않는다. 화제 이름보다 오늘 사건의 성격으로 판단한다. "
    "적합한 재료만 선택하며 개수를 채울 의무는 없다. 없으면 아무 항목도 선택하지 않는다. "
    "문장은 AI가 실제 정보·해석·추천으로 답할 수 있는 독립적인 질문이나 부탁이어야 한다. "
    "기대·소망만 말하거나 동의를 구하는 말, 미래 결과의 예측은 선택하지 않는다. "
)


_GENERATE_INSTRUCTION = (
    "오늘 Yes24에서 관측된 코너 목록(sections)을 재료로, 빈 화면의 초기 질문 칩에 실릴 문장을 "
    "만든다. 각 문장은 사용자가 이 AI 어시스턴트에게 그대로 눌러 보낼 완결된 질문이다 — "
    "사람이 입으로 말하듯 반말로 끝맺는 한국어 한 문장이다('~야?'·'~어?'·'~돼?'·'~알려줘'). "
    "'~는?'·'~내용은?'처럼 명사로 끊거나 '~무엇인가?' 같은 문어체로 쓰지 않는다. "
    "문장 부호는 문장 꼴을 따른다 — 묻는 꼴이면 물음표로, 청하는 꼴('~알려줘'·'~골라줘')이면 "
    "물음표 없이 끝낸다. "
    "한 문장은 그 슬롯의 rows에 실제로 있는 상품 하나를 가리킨다. goods_no에 그 상품의 번호를, "
    "title에 그 상품의 제목을 **글자 하나 바꾸지 않고** 옮겨 적고, 그 제목을 문장 안에도 "
    "그대로 넣되 『』로 감싼다 — 감싸지 않으면 제목이 문장의 서술어로 읽혀 어디까지가 책 "
    "이름인지 알 수 없다. 번호가 목록에 없거나 제목이 원문과 다르면 그 문장은 폐기된다. "
    "같은 슬롯 안에서는 서로 다른 상품, 서로 다른 각도를 다룬다. "
    "rows에 rank가 있는 상품은 그 순위를 문장에 반드시 넣는다('5위' 꼴) — 넣지 않으면 "
    "폐기된다. 출간월도 넣을 수 있고, 역시 rows에 있는 값 그대로다. "
    "코너 이름과 집계 기간은 칩 라벨이 이미 보여주므로 문장에 다시 쓰지 않는다 — 문장이 "
    "쓸 수 있는 관측 표현은 그 섹션의 anchor 문구(글자 그대로)와 순위 숫자뿐이고, 날짜·요일·"
    "주차를 따로 지어내지 않는다. 가격·평점 같은 숫자는 문장에 박지 않고 묻게 한다. "
    "문장의 꼴은 '〔관측된 제목과 그 곁의 관측값〕 … 〔묻는 것〕?'처럼 관측 재료와 질문이 한 "
    "문장에 붙는 형태다(꼴만 따르고 내용은 전부 rows에서 가져온다). "
    "마지막으로, 위의 모든 규칙보다 앞서는 기준이 하나 있다. "
    "**눌러서 나온 답이 그 책을 읽을지 정하는 데 도움이 되는가.** 그러려면 상품 페이지를 "
    "열어야 알 수 있는 것을 물어야 한다. 값·평점·쪽수·두께·배송·할인처럼 **사양 한 줄로 "
    "끝나는 것**, rows에 이미 있는 값(제목·순위·출간월), 공개되지 않는 판매 수치, 순위에 "
    "오른 이유 같은 해석, 예측, 이 서비스의 사용법 — 이 가운데 어느 하나라도 "
    "묻고 있다면 그 문장은 버리고 다시 쓴다."
)

# 골라주기 문구의 계약. 상품 하나를 지목하는 문장과 달리 **분야**를 가리키고, 답은 목록이
# 된다 — 첫 화면에서 "골라준다"는 능력을 보여주는 자리다.
_PICK_INSTRUCTION = (
    "오늘 Yes24 코너의 분야 목록(categories)을 재료로, 빈 화면의 초기 질문 칩에 실릴 문장을 "
    "만든다. 각 문장은 사용자가 그대로 눌러 보낼 완결된 질문이고, 사람이 입으로 말하듯 "
    "반말로 끝맺는다('~골라줘'·'~추천해줘'·'~있어?'). 청하는 꼴이면 물음표를 붙이지 않는다. "
    "상품 하나를 지목하지 않는다 — 목록에 있는 **분야 하나**를 골라 category에 그 이름을 "
    "그대로 적는다(목록에 없는 분야를 만들지 않는다). 다만 문장 안에서는 그 이름을 사람이 "
    "말하듯 쓴다 — '소설/시/희곡'처럼 여러 갈래가 묶인 이름은 한 갈래만 골라 쓴다. "
    "**여기서만 할 수 있는 일은 '골라주기'다.** 그러니 조건이나 상황을 하나 얹어 고르게 "
    "한다 — 어떤 사람에게 맞는지, 어떤 때 읽는지, 무엇을 처음 접하는지 같은 것이다. "
    "조건 없이 '무슨 책 있어?'처럼 넓게 묻지 않는다. "
    "코너 이름과 집계 기간은 칩 라벨이 이미 보여주므로 문장에 다시 쓰지 않는다. **시기를 "
    "가리키는 말은 anchor에 있는 것만 쓰고, anchor에 없으면 시기를 말하지 않는다** — 계절·"
    "명절·새해처럼 오늘이 언제인지 모르고 쓰면 어긋나는 말이 그렇다(상황을 얹을 때는 시기가 "
    "아니라 사람·기분·장소·목적으로 얹는다). 특정 책 제목·저자·가격·평점은 문장에 넣지 않는다 "
    "— 무엇을 고를지는 답변이 정한다. 같은 세트 안에서는 서로 다른 분야를 다룬다."
)

# 오늘의 화제 재료를 모으는 질문. 그라운딩 콜이라 구조화 출력을 못 쓴다(빌트인 검색과
# 함수 선언은 한 요청에 못 섞는다 — web_search 도구 주석) → 한 줄에 하나씩 받아 자른다.
_TREND_PROMPT = _EDITORIAL_SCOPE + (
    "오늘은 한국 시간 {today}이다. Google 검색으로 오늘 한국의 콘텐츠·문화 화제를 "
    "최대 {count}개 알려줘. 오늘 사건이나 새 보도 근거가 없는 과거 화제는 제외한다. "
    "작품·창작자·문화 행사 가운데 원작이나 관련 책으로 이어질 만한 것을 고른다. "
    "설명·번호·기호 없이 **한 줄에 하나씩 짧은 명사구만** 쓴다."
)

# 화제 문구의 계약. 지목형이 상품을, 골라주기가 분야를 가리킨다면 이쪽은 **바깥 화제**를
# 가리키고 그것을 책으로 잇는다 — 첫 화면에서 "오늘"이 보이는 자리다.
_TREND_INSTRUCTION = (
    "오늘 사람들이 이야기하는 화제(topics)를 재료로, 빈 화면의 초기 질문 칩에 실릴 문장을 "
    "만든다. 각 문장은 사용자가 그대로 눌러 보낼 완결된 질문이고, 사람이 입으로 말하듯 "
    "반말로 끝맺는다('~있어?'·'~골라줘'·'~뭐야?'). 청하는 꼴이면 물음표를 붙이지 않는다. "
    "**화제 하나를 골라 topic에 그 이름을 그대로 적고, 그 화제를 책으로 잇는 질문을 쓴다** "
    "— 원작이나 관련 책을 찾거나, 그 주제를 더 알고 싶다는 꼴이다. 목록에 없는 화제를 "
    "만들지 않는다. 화제 이름은 문장에서 알아볼 수 있게 쓰되 사람이 말하듯 줄여도 된다. "
    "각 화제에는 그것으로 Yes24를 검색했을 때 실제로 나온 책 몇 권(found)이 딸려 있다 — "
    "화제가 책과 이어진다는 증거이지 문장에 넣을 재료가 아니다. 특정 책 제목·저자·가격·"
    "평점은 문장에 넣지 않는다(무엇을 권할지는 답변이 정한다). "
    "날짜·요일을 지어내지 않는다. 같은 세트 안에서는 서로 다른 화제를 다룬다."
)

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


def observe_rows(rows: list[dict], section: str, today: dt.date) -> list[dict]:
    """코너 목록의 파싱 행 중 생성 재료가 되는 관측본을 고른다(구조 필터).

    순위가 있는 목록(has_rank)은 순위 자체가 시간 앵커(집계 기간)라 전량이 재료다. 순위가
    없는 목록(신간)은 앵커가 출간월뿐이라 **당월 출간분만** 남긴다 — 신간 코너에는 이전 달·
    이후 달 행이 섞여 있어(fixture 실측 4개 월) 걸러야 "9월 신간"이라는 문장이 참이 된다.
    """
    if BROWSE_SEED_URLS[section]["has_rank"]:
        return list(rows)
    this_month = (today.year, today.month)
    return [row for row in rows if _year_month(row.get("pub_date")) == this_month]


def _squash(value: Any) -> str:
    """문장·제목 비교의 단일 정규화 — 공백 종류·연속 공백·제로폭 문자를 없앤다.

    모델 출력과 어드민 입력이 같은 정규화를 거쳐야 "눈에 보이지 않는 차이"가 한쪽에서만
    통과하지 않는다(제로폭만으로 이루어진 문장이 빈 칩으로 서빙된 실측).
    """
    if value is None:
        return ""
    stripped = "".join(ch for ch in str(value) if unicodedata.category(ch) != "Cf")
    return " ".join(stripped.split())


def validate_items(
    raw_items: list, observed: dict[str, dict[int, dict]], *, max_chars: int
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """모델 출력의 출구 검증 — 관측본과 대조해 참조가 어긋난 문장을 버린다.

    버리는 것: 관측 밖 goods_no, **관측 제목이 문장에 그대로 들어 있지 않은 text**, 순위가
    있는 상품인데 **그 순위가 문장에 없는 text**, 빈 문장, 상한 초과, 중복. **문구를 보는
    필터가 아니라 이번 관측본과의 대조**다(인용 검증이 본문의 [n]을 이번 턴 출처와 대조하는
    것과 같은 자리). 제목을 대조하는 이유는 goods_no만 맞고 제목이 틀린 문장("그랬다고
    적어다")이 통과했기 때문이고, 순위를 대조하는 이유는 순위가 **라벨이 대신할 수 없는
    유일한 실시간 신호**여서다 — 선택 사항으로 두었더니 10건 중 7건이 순위 없이 나왔고 그
    문장들은 클릭 유인 채점에서 전부 최하였다(종합 1위 상품이 1위라는 말 없이 나갔다).

    모델이 따로 declare하는 `title` 필드는 여기서 다시 대조하지 않는다 — 그 필드의 일은
    문장을 쓰기 전에 상품을 확정시키는 것(property_ordering)이고, 문장이 맞는지는 관측 제목이
    문장 안에 있는지로 결정된다. 두 판정을 다 두면 같은 것을 두 번 재는 것이다.

    반환은 (슬롯별 생존 항목, 슬롯별 폐기 수). 생존 항목은 `{slot, goods_no, text}`이고 문장은
    공백 정규화만 한다(절단 금지 — 누르면 그대로 전송되는 문장이라 상한 초과는 잘라 살리지
    않고 버린다). 중복 판정은 슬롯을 가로질러 정규화 문장 기준이다.
    """
    kept: dict[str, list[dict]] = {slot: [] for slot in observed}
    dropped: dict[str, int] = {}
    seen_texts: set[str] = set()
    for item in raw_items:
        slot = item.get("slot")
        slot_key = slot if slot else "?"
        text = _squash(item.get("text"))
        goods_no = item.get("goods_no")
        row = (observed.get(slot) or {}).get(goods_no, {})
        observed_title = _squash(row.get("title"))
        rank = row.get("rank")
        reason = (
            "unknown_slot" if slot not in observed
            else "unobserved_goods_no" if goods_no not in observed[slot]
            else "title_not_in_text" if observed_title not in text
            else "rank_not_in_text" if rank and f"{rank}위" not in text
            else "empty_text" if not text
            else "over_max_chars" if len(text) > max_chars
            else "duplicate_text" if text in seen_texts
            else None
        )
        if reason:
            dropped[slot_key] = dropped.get(slot_key, 0) + 1
            # 폐기 사유를 남긴다 — 어느 슬롯이 왜 말라붙는지(관측 밖 참조인지 상한인지)는
            # 데이터 소스 기각 판정(설계 "판정 게이트")의 재료다.
            logger.warning(
                f"starters 폐기: slot={slot_key} goods_no={goods_no} reason={reason} "
                f"text={text[:max_chars]!r}"
            )
            continue
        seen_texts.add(text)
        kept[slot].append({"slot": slot, "goods_no": goods_no, "text": text})
    return kept, dropped


def validate_trend_items(
    raw_items: list, topics: list[dict], *, max_chars: int
) -> tuple[list[dict], int]:
    """화제 문구의 출구 검증 — 오늘 모은 화제 목록과 대조한다(분야 검증과 같은 자리).

    화제 이름을 통째로 요구하지 않는다. 사람이 말할 때는 줄여 쓰기 때문이다("크리스토퍼
    놀란 감독 영화 오디세이" → "오디세이"). 이름의 조각 중 두 글자 이상인 것이 하나라도
    문장에 있으면 그 화제를 가리킨 것으로 본다 — 관측본과의 대조이지 허용 단어 목록이 아니다.
    """
    by_name = {_squash(t["topic"]): t for t in topics}
    kept: list[dict] = []
    dropped = 0
    seen: set[str] = set()
    for item in raw_items:
        name = _squash(item.get("topic"))
        text = _squash(item.get("text"))
        words = name.replace("·", " ").split()
        parts = [w for w in words if len(w) >= 2] if name in by_name else []
        reason = (
            "unobserved_topic" if name not in by_name
            else "topic_not_in_text" if not any(part in text for part in parts)
            else "empty_text" if not text
            else "over_max_chars" if len(text) > max_chars
            else "duplicate_text" if text in seen
            else None
        )
        if reason:
            dropped += 1
            logger.warning(f"starters 폐기(화제): topic={name!r} reason={reason} text={text!r}")
            continue
        seen.add(text)
        kept.append({"topic": name, "text": text})
    return kept, dropped


def validate_pick_items(
    raw_items: list, categories: list[dict], *, max_chars: int
) -> tuple[list[dict], int]:
    """골라주기 출력의 출구 검증 — 관측된 분야 목록과 대조한다(상품 검증과 같은 자리).

    분야 이름이 문장에 **통째로** 들어 있기를 요구하지 않는다. 사이트의 분야명은
    "소설/시/희곡"처럼 슬래시로 묶인 복합명이 있어 그대로 넣으면 문장이 어색해진다 —
    구성 조각 중 하나라도 문장에 있으면 그 분야를 가리킨 것으로 본다(관측본과의 대조이지
    허용 단어 목록이 아니다).
    """
    by_name = {_squash(c["name"]): c for c in categories}
    kept: list[dict] = []
    dropped = 0
    seen: set[str] = set()
    for item in raw_items:
        name = _squash(item.get("category"))
        text = _squash(item.get("text"))
        parts = [p for p in name.split("/") if p] if name in by_name else []
        reason = (
            "unobserved_category" if name not in by_name
            else "category_not_in_text" if not any(part in text for part in parts)
            else "empty_text" if not text
            else "over_max_chars" if len(text) > max_chars
            else "duplicate_text" if text in seen
            else None
        )
        if reason:
            dropped += 1
            logger.warning(
                f"starters 폐기(골라주기): category={name!r} reason={reason} text={text!r}"
            )
            continue
        seen.add(text)
        kept.append({"category": name, "text": text})
    return kept, dropped


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


# ── 생성 파이프라인(관측 → 구조화 출력 → 출구 검증, DB 없음) ─────────────────


def _response_schema(slots: list[str], count: int) -> types.Schema:
    """구조화 출력 스키마. 슬롯은 관측된 것만 enum으로, 개수는 min/max_items로 강제한다.

    property_ordering이 계약이다 — 참조(slot·goods_no·title)를 먼저 확정하고 문장을 쓰게 해야
    문장을 먼저 짓고 번호를 끼워 맞추는 쏠림이 줄어든다(enrichment의 선행 판정 필드와 같은 이유).
    title을 따로 받는 이유는 출구가 **문장 안의 제목까지** 관측본과 대조할 수 있게 하기
    위해서다 — goods_no만 보면 참조는 맞는데 제목이 틀린 문장(오기·축약)이 통과했다.
    """
    order = ["slot", "goods_no", "title", "text"]
    item = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "slot": types.Schema(type=types.Type.STRING, enum=list(slots)),
            "goods_no": types.Schema(type=types.Type.INTEGER),
            "title": types.Schema(type=types.Type.STRING),
            "text": types.Schema(type=types.Type.STRING),
        },
        required=order,
        property_ordering=order,
    )
    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "items": types.Schema(
                type=types.Type.ARRAY, items=item, min_items=0, max_items=count
            )
        },
        required=["items"],
    )


def _anchor(seed: dict, today: dt.date) -> str:
    """문장이 쓸 수 있는 유일한 시간 표현. 코너 이름·집계 기간은 칩 라벨이 보여준다."""
    return "베스트셀러" if seed["has_rank"] else f"{today.month}월 신간"


def _payload(
    observed: dict[str, list[dict]],
    today: dt.date,
    exclude: dict[str, set[int]] | None = None,
    min_rows: int = 0,
) -> dict:
    """모델에 싣는 관측본. **목록에 보이는 것은 재료로만 주고, 물을 거리는 주지 않는다.**

    싣지 않는 것과 이유:
    - 가격·평점·판매지수: 문장에 박힐 숫자의 재료를 없앤다(문구 계약 5).
    - 저자·출판사: 실으면 모델이 그 값을 **되묻는** 문장을 쓴다("…의 저자는 누구야?").
      목록에 이미 있는 값을 묻는 질문은 상품 페이지를 열 이유가 없어 칩으로서 시시하고,
      도구 호출도 부르지 못한다(실측 10건 중 8건이 이 꼴이었다). 남기는 것은 그 상품을
      **가리키는** 데 필요한 것(제목·순위·출간월)뿐이다.
    값이 None인 키는 뺀다(칸 채우기 유혹 방지).

    관측일(today)은 싣지 않는다. 실으면 모델이 그 날짜를 시간 표현으로 베껴 "9월 8일
    베스트셀러 1위"처럼 **집계 기준과 어긋난 앵커**를 쓴다(실측) — 순위는 주간 집계고 일자
    순위가 아니다. 각 섹션이 쓸 수 있는 시간 표현은 anchor 한 줄로만 준다("이번 주"라고
    부르지 않는 이유도 같다 — 종합 탭의 집계 기간은 어제 끝난 한 주다).

    `exclude`에 든 goods_no는 rows에서 뺀다 — 최근에 이미 쓴 상품이다. 재료가 같으면 모델은
    같은 책을 다시 고른다(주간 집계 페이지를 매일 관측하니 필연). 무엇을 물을 수 있는가와
    마찬가지로 **무엇이 반복되지 않는가도 payload가 정한다**.

    단 제외가 재료를 `min_rows` 미만으로 만들면 그 슬롯은 **제외를 통째로 포기한다**. 반복을
    피하려다 슬롯을 굶기면 어제 세트가 그대로 남아 더 심하게 반복된다(제외 창이 길수록
    누적 제외가 한 페이지 행 수를 넘는다 — 실측으로 베스트셀러가 세 라운드 굶었다).
    """
    sections = {}
    for slot, rows in observed.items():
        seed = BROWSE_SEED_URLS[slot]
        anchor = _anchor(seed, today)
        skip = (exclude or {}).get(slot, set())
        fresh = [row for row in rows if int(row["goods_no"]) not in skip]
        if len(fresh) < min_rows:
            logger.info(
                f"starters 제외 포기: slot={slot} 남은행={len(fresh)} < {min_rows} "
                f"(최근 사용 {len(skip)}건) — 반복보다 굶는 쪽이 나쁘다"
            )
            fresh = list(rows)
        sections[slot] = {
            "label": seed["label"],
            "anchor": anchor,
            "rows": [
                {
                    "goods_no": int(row["goods_no"]),
                    **{
                        key: row[key]
                        for key in ("rank", "title", "pub_date")
                        if row.get(key) is not None
                    },
                }
                for row in fresh
            ],
        }
    return {"sections": sections}


async def _observe_categories(section: str, settings: Settings, client) -> list[dict]:
    """코너 페이지 내비의 분야 목록 — 시드와 **같은 트리**만 남긴다.

    내비에는 국내도서·외국도서·eBook의 동명 분야가 섞여 있어(파서 주석) 트리 접두로 걸러야
    "소설"이 다른 매장으로 새지 않는다. 접두 자신(코너 전체)은 분야가 아니라 뺀다.
    """
    html = await client.get_text(browse_url(section))
    prefix = browse_category_prefix(section)
    links = await asyncio.to_thread(
        parse_category_links, html, limit=settings.starter_pick_category_limit
    )
    return [c for c in links if c["number"].startswith(prefix) and c["number"] != prefix]


async def _observe_trends(settings: Settings, client, genai_client) -> list[dict]:
    """오늘의 화제 → **Yes24에 실제로 책이 있는 것만** 남긴다.

    바깥 세상 신호(웹 그라운딩)를 재료로 쓰지만, 접지는 여전히 Yes24가 한다 — 화제 하나로
    검색해 결과가 0건이면 그 화제는 재료에서 빠진다. "그 화제로 책 이야기를 할 수 있는가"를
    문구 규칙이 아니라 **검색 결과의 유무**로 판정하는 자리다(억지 연결을 구조로 막는다).
    """
    response = await asyncio.wait_for(
        genai_client.aio.models.generate_content(
            model=settings.web_grounding_model,
            contents=_TREND_PROMPT.format(
                count=settings.starter_trend_topics, today=_today().isoformat()
            ),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.3
            ),
        ),
        timeout=settings.starter_timeout_s,
    )
    record_usage("starter_trend", response.usage_metadata, model=settings.web_grounding_model)
    # 그라운딩 응답은 자유 텍스트다 — 줄을 잘라 앞머리 기호만 벗긴다(파싱이 새도 아래
    # 검색 단계가 걸러내므로 무해하다).
    topics = []
    for line in (response.text or "").splitlines():
        name = _squash(line).lstrip("-*•0123456789.) ").strip("*")
        if name and name not in topics:
            topics.append(name)
    topics = topics[: settings.starter_trend_topics]
    if not topics:
        return []

    async def _found(topic: str) -> list[dict]:
        try:
            html = await client.get_text(search_url(settings.yes24_base_url, topic))
            rows = await asyncio.to_thread(
                parse_search, html, base_url=settings.yes24_base_url, limit=3
            )
        except (Yes24FetchError, ParseError):
            return []
        return [row["title"] for row in rows if row.get("title")]

    found = await asyncio.gather(*(_found(topic) for topic in topics))
    return [
        {"topic": topic, "found": titles} for topic, titles in zip(topics, found) if titles
    ]


async def _generate_trends(
    settings: Settings, *, client, genai_client
) -> dict:
    """오늘의 화제 슬롯의 재료 수집 → 생성(1콜) → 검증. 골라주기와 같은 결과 모양이다."""
    try:
        topics = await _observe_trends(settings, client, genai_client)
    except Exception as exc:  # noqa: BLE001 — 바깥 신호는 없을 수 있다: 슬롯만 failed
        return _failed(f"화제 수집 실패: {type(exc).__name__}: {exc}")
    if not topics:
        return _failed("화제 0건(웹 신호 없음 또는 Yes24 검색 결과 없음)")

    count = min(settings.starter_per_slot, len(topics))
    order = ["topic", "text"]
    schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "items": types.Schema(
                type=types.Type.ARRAY,
                min_items=0,
                max_items=count,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "topic": types.Schema(
                            type=types.Type.STRING, enum=[t["topic"] for t in topics]
                        ),
                        "text": types.Schema(type=types.Type.STRING),
                    },
                    required=order,
                    property_ordering=order,
                ),
            )
        },
        required=["items"],
    )
    try:
        response = await asyncio.wait_for(
            genai_client.aio.models.generate_content(
                model=settings.starter_model,
                contents=json.dumps({"topics": topics}, ensure_ascii=False),
                config=types.GenerateContentConfig(
                    system_instruction=_TREND_INSTRUCTION
                    + f" 문장 전체는 {settings.starter_max_chars}자를 넘기지 않는다."
                    + _EDITORIAL_SCOPE,
                    temperature=1.0,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            ),
            timeout=settings.starter_timeout_s,
        )
        record_usage("starter", response.usage_metadata, model=settings.starter_model)
        raw_items = json.loads(response.text or "{}").get("items") or []
    except Exception as exc:  # noqa: BLE001 — 백그라운드 생성: 실패는 슬롯 failed로 접는다
        return _failed(f"생성 실패: {type(exc).__name__}: {exc}")

    kept, dropped = validate_trend_items(
        raw_items, topics, max_chars=settings.starter_max_chars
    )
    if not kept:
        return _failed("출구 검증에서 전부 폐기(관측 밖 화제·빈 문장·중복·상한 초과)", dropped)
    items = [
        {"slot": _TREND_SLOT, "goods_no": None, "text": item["text"], "source_url": None}
        for item in kept
    ]
    return {"status": "ok", "items": items, "dropped": dropped, "detail": ""}


async def _generate_picks(
    section: str, settings: Settings, *, today: dt.date, client, genai_client
) -> dict:
    """골라주기 한 슬롯의 관측→생성(1콜)→검증. 상품 경로와 같은 결과 모양을 돌려준다."""
    try:
        categories = await _observe_categories(section, settings, client)
    except (Yes24FetchError, ParseError) as exc:
        return _failed(f"분야 관측 실패: {exc}")
    if not categories:
        return _failed("분야 관측 0건(내비 파싱 결과 없음)")

    count = min(settings.starter_per_slot, len(categories))
    order = ["category", "text"]
    schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "items": types.Schema(
                type=types.Type.ARRAY,
                min_items=0,
                max_items=count,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "category": types.Schema(
                            type=types.Type.STRING, enum=[c["name"] for c in categories]
                        ),
                        "text": types.Schema(type=types.Type.STRING),
                    },
                    required=order,
                    property_ordering=order,
                ),
            )
        },
        required=["items"],
    )
    seed = BROWSE_SEED_URLS[section]
    payload = {
        "label": seed["label"],
        "anchor": _anchor(seed, today),
        "categories": [c["name"] for c in categories],
    }
    try:
        response = await asyncio.wait_for(
            genai_client.aio.models.generate_content(
                model=settings.starter_model,
                contents=json.dumps(payload, ensure_ascii=False),
                config=types.GenerateContentConfig(
                    system_instruction=_PICK_INSTRUCTION + (
                        f" 문장 전체는 {settings.starter_max_chars}자를 넘기지 않는다."
                    ) + _EDITORIAL_SCOPE,
                    temperature=1.0,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            ),
            timeout=settings.starter_timeout_s,
        )
        record_usage("starter", response.usage_metadata, model=settings.starter_model)
        raw_items = json.loads(response.text or "{}").get("items") or []
    except Exception as exc:  # noqa: BLE001 — 백그라운드 생성: 실패는 슬롯 failed로 접는다
        return _failed(f"생성 실패: {type(exc).__name__}: {exc}")

    kept, dropped = validate_pick_items(
        raw_items, categories, max_chars=settings.starter_max_chars
    )
    if not kept:
        return _failed("출구 검증에서 전부 폐기(관측 밖 분야·빈 문장·중복·상한 초과)", dropped)
    items = [
        {"slot": pick_slot(section), "goods_no": None, "text": item["text"],
         "source_url": browse_url(section)}
        for item in kept
    ]
    return {"status": "ok", "items": items, "dropped": dropped, "detail": ""}


async def _observe(slot: str, settings: Settings, client, today: dt.date) -> list[dict]:
    """코너 1페이지 관측 → 파싱 → 구조 필터. 실패는 예외 그대로(호출부가 슬롯 failed로 접는다)."""
    html = await client.get_text(browse_url(slot))
    # limit은 주지 않는다 — 코너 한 페이지가 곧 관측 단위이고, 파서 기본값이 그 페이지 크기다.
    rows = await asyncio.to_thread(
        parse_browse_list, html, base_url=settings.yes24_base_url, section=slot
    )
    return observe_rows(rows, slot, today)


async def build_candidates(
    slots: list[str],
    settings: Settings,
    *,
    today: dt.date,
    client=None,
    genai_client=None,
    exclude: dict[str, set[int]] | None = None,
) -> dict[str, dict]:
    """슬롯들의 관측→생성(1콜)→출구 검증. 저장은 하지 않는다(서비스·라이브 스모크가 공유).

    반환: `{slot: {status: "ok"|"failed", items: [{slot, goods_no, text, source_url}],
    dropped, detail}}`.
    관측 0건인 슬롯은 모델 콜 없이 failed(빈 성공 위장 금지, 원칙 7). 모델 콜은 관측된 슬롯
    전체에 1회이고, 실패하면 그 슬롯 전부 failed다. `exclude`(최근에 이미 쓴 goods_no)는
    payload에서만 빠진다 — 관측·검증은 페이지 전량을 그대로 본다(제외분이 재료를 다 비우면
    그 슬롯은 폐기로 접힌다).
    """
    client = client or get_client(settings)
    genai_client = genai_client or get_genai_client()
    results: dict[str, dict] = {}
    observed: dict[str, list[dict]] = {}
    # 골라주기 슬롯은 재료(분야 목록)도 스키마도 달라 **자기 콜**을 쓴다. 한 스키마에
    # 상품 칸과 분야 칸을 섞으면 안 쓰는 칸을 채우려는 편향이 생긴다.
    if _TREND_SLOT in slots:
        results[_TREND_SLOT] = await _generate_trends(
            settings, client=client, genai_client=genai_client
        )
    for slot in [s for s in slots if pick_base(s)]:
        results[slot] = await _generate_picks(
            pick_base(slot), settings, today=today, client=client, genai_client=genai_client
        )
    for slot in [s for s in slots if not pick_base(s) and s != _TREND_SLOT]:
        try:
            rows = await _observe(slot, settings, client, today)
        except (Yes24FetchError, ParseError) as exc:
            results[slot] = _failed(f"관측 실패: {exc}")
            continue
        if not rows:
            results[slot] = _failed("관측 0건(파싱 결과 없음 또는 당월 출간분 없음)")
            continue
        observed[slot] = rows
    if not observed:
        return results

    started = time.monotonic()
    per_slot = settings.starter_per_slot
    payload = _payload(observed, today, exclude, min_rows=per_slot)
    # 재료보다 많이 요구하지 않는다 — min_items가 행 수를 넘으면 모델은 같은 상품을 되풀이해
    # 채우고 그 문장들은 중복으로 폐기된다(슬롯이 통째로 비는 실제 경로였다).
    count = sum(min(per_slot, len(section["rows"])) for section in payload["sections"].values())
    try:
        response = await asyncio.wait_for(
            genai_client.aio.models.generate_content(
                model=settings.starter_model,
                contents=json.dumps(payload, ensure_ascii=False),
                config=types.GenerateContentConfig(
                    system_instruction=_instruction(settings.starter_max_chars),
                    temperature=1.0,
                    response_mime_type="application/json",
                    response_schema=_response_schema(list(observed), count),
                ),
            ),
            timeout=settings.starter_timeout_s,
        )
        record_usage("starter", response.usage_metadata, model=settings.starter_model)
        raw_items = json.loads(response.text or "{}").get("items") or []
    except Exception as exc:  # noqa: BLE001 — 백그라운드 생성: 실패는 슬롯 failed로 접는다
        for slot in observed:
            results[slot] = _failed(f"생성 실패: {type(exc).__name__}: {exc}")
        return results

    observed_sets = {slot: {int(r["goods_no"]): r for r in rows} for slot, rows in observed.items()}
    kept, dropped = validate_items(raw_items, observed_sets, max_chars=settings.starter_max_chars)
    for slot in observed:
        items = [{**item, "source_url": browse_url(slot)} for item in kept[slot]]
        if items:
            results[slot] = {
                "status": "ok", "items": items, "dropped": dropped.get(slot, 0), "detail": ""
            }
        else:
            results[slot] = _failed(
                "출구 검증에서 전부 폐기(관측 밖 goods_no·빈 문장·중복·상한 초과)",
                dropped.get(slot, 0),
            )
    logger.info(
        f"starters 생성: slots={list(observed)} raw={len(raw_items)} "
        f"kept={ {s: len(r['items']) for s, r in results.items()} } dropped={dropped} "
        f"elapsed={time.monotonic() - started:.2f}s"
    )
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
