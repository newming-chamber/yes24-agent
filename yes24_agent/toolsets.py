"""toolset 레지스트리 — 도구·aclose 훅·페르소나 브랜딩의 선언적 단일 소스.

"도구에 따라 역할이 달라야 한다": 같은 코드베이스가 config 스위치(enabled_toolsets·
agent_persona)만으로 도구 구성이 다른 에이전트가 된다. 등록 순서·도구 구성·프롬프트
fragment 활성(agent.compose)·lifespan 정리 훅·프론트 브랜딩이 전부 여기서 파생되므로,
새 toolset·persona는 이 파일에 항목 하나를 추가하면 끝이다.

이 파일에는 URL·모델명·UA를 두지 않는다(제품 카피만) — no_hardcoding ALLOWLIST
등재가 필요 없는 상태가 목표다. import 방향: toolsets ← tools/*·config,
agent·main ← toolsets (순환 없음 — tools 쪽에서 toolsets가 필요하면 lazy import).
"""

from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace

from yes24_agent.config import get_settings
from yes24_agent.tools.fetch_many import fetch_many
from yes24_agent.tools.web_fetch import web_fetch
from yes24_agent.tools.web_search import (
    aclose_shared_client as aclose_web_search_client,
)
from yes24_agent.tools.web_search import start_web_prefetch, web_search
from yes24_agent.tools.yes24_browse import yes24_browse
from yes24_agent.tools.yes24_fetch import yes24_fetch
from yes24_agent.tools.yes24_search import (
    aclose_shared_client as aclose_yes24_client,
)
from yes24_agent.tools.yes24_search import yes24_search
from yes24_agent.yes24.parsers import GROUNDING_FIELDS


@dataclass(frozen=True)
class Branding:
    """프론트(index·login) 마커 치환용 제품 카피."""

    title: str
    greeting: str
    subtitle: str
    examples: tuple[str, ...]


@dataclass(frozen=True)
class Persona:
    """프론트 문안 묶음. home_toolset 잠금은 삭제됐다(2026-08-06 사용자: "걍 에이전트 두 개를
    같이 할 수 있게") — 정체성은 이제 고정 선택이 아니라 켜진 toolset의 강점에서 파생되므로
    (`agent.build_identity`), "도구 없는 전문성 선언 금지"는 잠금이 아니라 그 파생이 구조로
    보장한다. 남은 역할은 브랜딩 선택뿐이라 껍데기가 얇다."""

    branding: Branding


# 선언 순서 = 도구 등록 순서 정본(종전 AGENT_TOOLS 리터럴과 동일: yes24 4종 → web 2종).
# enabled_toolsets는 켜고 끄기만 하고 순서는 여기가 소유한다.
TOOLSETS: dict[str, tuple] = {
    "yes24": (yes24_search, yes24_fetch, fetch_many, yes24_browse),
    "web": (web_search, web_fetch),
}

@dataclass(frozen=True)
class Expertise:
    """정체성이 선언하는 도메인 강점. `subject`는 "…에 특히 밝습니다"의 목적어,
    `label`은 "… 특화는 강점이지"의 수식어다."""

    subject: str
    label: str


# toolset → 정체성 강점. **web은 없다** — 웹 검색은 도메인 전문성이 아니라 범용 능력이라
# 정체성에 전문성으로 선언하지 않는다(없는 전문성 선언 금지). 키는 TOOLSETS의 부분집합이며
# 그 불변식은 테스트가 잠근다(ACLOSE_HOOKS와 같은 병행 레지스트리 관례).
TOOLSET_EXPERTISE: dict[str, Expertise] = {
    "yes24": Expertise(subject="Yes24 책·상품", label="도서"),
}

# toolset이 등록하는 출처 타입 → 공개 DTO에 실을 메타 필드. 두 가지를 레지스트리로 끌어온다:
# ① 공개 필드 목록(event_translate가 도구 모듈을 직수입하지 않게) ② 소스 타입 어휘 자체
# (엔진 코어가 "book_detail이면 더 충실" 같은 도메인 지식을 갖지 않게 — T1 가드가 강제).
# 값은 그 타입이 공개 DTO에 실을 수 있는 스칼라 필드 이름이며, 형태 필터는 그대로 적용된다.
TOOLSET_SOURCE_TYPES: dict[str, dict[str, tuple[str, ...]]] = {
    "yes24": {
        "search_result": GROUNDING_FIELDS,
        "book_detail": GROUNDING_FIELDS,
        # browse만 목록 순위(rank)를 관측한다 — 코너 랭킹이 그 출처의 본질 필드다.
        "browse": (*GROUNDING_FIELDS, "rank"),
        # 상세가 함께 관측한 다른 판형(eBook·중고 등). 그 페이지를 연 것이 아니라 판형
        # 위젯이 렌더한 값이라, 관측된 것은 판매가 하나뿐이다.
        "other_format": ("sale_price",),
    },
    "web": {"web": ()},
}

# 턴 시작 선제 실행 훅. ACLOSE_HOOKS와 같은 병행 레지스트리 관례이며, **활성 toolset의 것만**
# 실행한다(끈 toolset은 선제 실행도 없다). 러너가 도구 모듈을 직수입하지 않게 하는 것이 목적:
# 코어가 tools.web_search를 import하면 web 없이는 코어가 import조차 안 된다(T1).
# 시그니처는 (message, active) 공통이다.
PREFETCH_HOOKS: dict[str, tuple] = {
    "web": (start_web_prefetch,),
}

# lifespan 종료 훅. 활성 여부와 무관하게 전부 호출한다 — 미생성 클라이언트는 no-op
# (3곳 다 `if _shared_client` 가드 실측 확인)이라 무해하고, 레지스트리 파생이라
# 새 toolset을 추가해도 main.py는 무수정이다.
ACLOSE_HOOKS: dict[str, tuple] = {
    "yes24": (aclose_yes24_client,),
    "web": (aclose_web_search_client,),
}

PERSONAS: dict[str, Persona] = {
    "yes24": Persona(
        branding=Branding(
            title="Yes24 AI 어시스턴트",
            greeting="무엇을 도와드릴까요?",
            subtitle=(
                "무엇이든 물어보세요. 책과 상품은 Yes24 실시간 근거로 더 정확하게 "
                "답해드려요."
            ),
            examples=(
                "요즘 인기 에세이 추천해줘",
                "YES24 단순 변심 반품 조건 알려줘",
                "오늘 주요 경제 뉴스 요약해줘",
                "이 문장을 더 따뜻하게 다듬어줘",
            ),
        ),
    ),
}


@dataclass(frozen=True)
class ResolvedApp:
    """config에서 한 번 해석된 앱 구성(프로세스 정적)."""

    active: frozenset[str]
    tools: tuple
    aclose_hooks: tuple
    prefetch_hooks: tuple
    persona: Persona
    persona_key: str


def resolve_app(settings) -> ResolvedApp:
    """enabled_toolsets·agent_persona를 검증하고 앱 구성으로 해석한다(fail-loud).

    미등록 toolset 이름·빈 목록·미지 persona는 전부 ValueError다 — 조용한 부분 동작보다
    시끄러운 실패가 낫다. 도구 순서는 TOOLSETS 선언 순서가 정본이며 enabled_toolsets의
    나열 순서는 무시한다.
    """
    enabled = list(settings.enabled_toolsets)
    if not enabled:
        raise ValueError("enabled_toolsets가 비어 있습니다 — 최소 한 toolset이 필요합니다.")
    unknown = [name for name in enabled if name not in TOOLSETS]
    if unknown:
        raise ValueError(
            f"미등록 toolset {unknown} — 등록된 키: {sorted(TOOLSETS)} (toolsets.TOOLSETS)"
        )
    persona_key = settings.agent_persona
    persona = PERSONAS.get(persona_key)
    if persona is None:
        raise ValueError(
            f"미지 persona {persona_key!r} — 등록된 키: {sorted(PERSONAS)} (toolsets.PERSONAS)"
        )
    active = frozenset(enabled)
    tools = tuple(
        tool for key, toolset in TOOLSETS.items() if key in active for tool in toolset
    )
    aclose_hooks = tuple(hook for hooks in ACLOSE_HOOKS.values() for hook in hooks)
    prefetch_hooks = tuple(
        hook for key, hooks in PREFETCH_HOOKS.items() if key in active for hook in hooks
    )
    return ResolvedApp(
        active=active,
        tools=tools,
        aclose_hooks=aclose_hooks,
        prefetch_hooks=prefetch_hooks,
        persona=persona,
        persona_key=persona_key,
    )


# 요청별 도구 구성 캐시. UI 토글이 요청마다 다른 조합을 보내므로 조합 수만큼(≤2^|TOOLSETS|)
# 해석 결과를 재사용한다 — ResolvedApp은 frozen dataclass라 공유해도 안전하다. 상한은
# 조합 수의 여유값이며 조정 대상 설정이 아니라 자료구조 상한이라 코드 상수로 둔다.
_RESOLVED_CACHE_MAX = 32


@lru_cache(maxsize=_RESOLVED_CACHE_MAX)
def resolve_app_for(persona_key: str, enabled: frozenset[str]) -> ResolvedApp:
    """(persona, 활성 toolset) 조합을 해석한다 — 검증은 resolve_app 한 곳을 그대로 탄다.

    요청별 구성(UI 토글)과 기동 시 전역 구성이 **같은 함수**를 지나야 한다. 두 벌로 나누면
    한쪽에만 검증이 붙는 순간 무효 조합이 조용히 부분 동작한다(fail-loud 상실).
    """
    return resolve_app(
        SimpleNamespace(enabled_toolsets=sorted(enabled), agent_persona=persona_key)
    )


def get_resolved_app() -> ResolvedApp:
    """프로세스 기본 구성(요청이 구성을 지정하지 않았을 때의 폴백)."""
    settings = get_settings()
    return resolve_app_for(settings.agent_persona, frozenset(settings.enabled_toolsets))
