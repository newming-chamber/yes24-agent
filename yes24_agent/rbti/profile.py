"""유저의 RBTI 코드 조회 — 외부 RBTI API가 정본.

**프론트가 매 요청에 싣는 값이 아니다**(2026-09-02 사용자 확인). RBTI는 사람에게 붙어 있는
속성이라 서버가 조회한다: 프론트는 `use_rbti`(이번 턴에 쓸지)만 보내고, 코드 자체는
`main.chat_stream`의 `request.rbti or await fetch_user_rbti(user_no)`가 채운다.

**조회처는 외부 API다**(2026-09-10 확정 — 그전까지 미정이라 항상 미적용이었다). 우리 DB에
복제하지 않는다: 복제하면 사용자가 유형 검사를 다시 해도 우리 쪽이 낡은 값을 계속 적용한다.
그래서 `users.rbti` 컬럼과 `AuthService.read_rbti`는 이 경로에서 더는 쓰지 않는다.

**대신 프로세스 메모리에 만료되는 결과로 기억한다**(2026-09-28): 앱 오픈마다 `/me/rbti`가
불려 조회가 앱 사용량에 비례하게 됐다. 유형 있음은 길게(`rbti_cache_ttl_s`), 없음은 짧게
(`rbti_negative_cache_ttl_s` — 첫 검사 직후 반영), 실패는 기억하지 않는다. 재검사 반영 지연의
상한이 곧 TTL이다. 서버마다 따로 기억하지만 ALB 고정 세션이라 한 회원은 대개 한 서버로 간다.

요청의 `rbti`는 **화면의 유형 선택기가 이번 턴만 덮어쓰는 공개 채널**이다(2026-09-14 공개 —
그전엔 데모 UI 전용으로 감춰 뒀는데, 통합 프론트의 선택이 서버에 닿을 길이 없었다). 실려 오면
`main.chat_stream`이 단락 평가로 이 조회를 아예 건너뛴다.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

import httpx

from yes24_agent.config import get_settings

logger = logging.getLogger(__name__)

# user_no → (만료 시각 monotonic, 코드 또는 None). OrderedDict 순서 = LRU(최근 사용이 뒤).
# 조회는 await 경계 밖에서 읽고 쓰므로(단일 이벤트 루프) 잠금이 필요 없다 — 같은 회원의 동시
# 첫 조회가 두 번 나갈 수는 있지만 결과가 같아 무해하다.
_CACHE: OrderedDict[str, tuple[float, str | None]] = OrderedDict()


def _remember(user_no: str, code: str | None) -> None:
    settings = get_settings()
    ttl = settings.rbti_cache_ttl_s if code else settings.rbti_negative_cache_ttl_s
    if ttl <= 0 or settings.rbti_cache_max_entries <= 0:
        return
    _CACHE[user_no] = (time.monotonic() + ttl, code)
    _CACHE.move_to_end(user_no)
    while len(_CACHE) > settings.rbti_cache_max_entries:
        _CACHE.popitem(last=False)


async def fetch_user_rbti(user_no: str | None) -> str | None:
    """user_no의 RBTI 코드를 외부 API에서 조회한다(없거나 실패하면 None).

    user_no가 None이면 익명 요청이라 조회 대상 자체가 없다. API는 유형이 없는 사용자에게도
    200 + `data.typeCode: null`로 답하므로(실측), 없음과 실패를 응답 코드로 가르지 않고
    **값의 유무**로 가른다.

    **실패는 조용히 None이다**: 조회처가 죽어도 답변 자체는 나가야 한다(성향은 답을 더 맞게
    만드는 부가 정보이지 답의 전제가 아니다). 다만 로그로는 남긴다 — 조용한 미적용이
    "유형이 없는 사용자"와 구분되지 않으면 장애를 못 본다.

    반환 코드의 유효성 검증은 호출 경로의 `is_valid_code`(runner)가 담당한다 — 이 함수는
    조회만 하고 형식 판정을 스스로 들고 있지 않다. 요청 경로(`ChatRequest._validate_rbti`)와
    **비대칭**인 것은 의도다: 사용자가 보낸 오타는 422로 되돌려 줄 상대가 있지만, 외부 API가
    준 값은 사용자 잘못이 아니라 되돌려 줄 상대가 없다(답은 성향 없이라도 나가야 한다).
    대신 그 경로의 무효값은 runner에서 조용히 미적용된다 — 실측된 응답은 유효 코드와 null뿐이라
    아직 관측된 적이 없고, 관측되면 그때 로그를 붙일 자리는 runner다.
    """
    if not user_no:
        return None
    hit = _CACHE.get(user_no)
    if hit is not None:
        if time.monotonic() < hit[0]:
            _CACHE.move_to_end(user_no)
            return hit[1]
        del _CACHE[user_no]  # 만료 — 게으른 정리
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=settings.rbti_api_timeout_s) as client:
            response = await client.get(settings.rbti_api_url, params={"userNo": user_no})
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"RBTI 조회 실패(user_no={user_no}): {exc}")
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    code = data.get("typeCode") if isinstance(data, dict) else None
    code = code if isinstance(code, str) and code else None
    _remember(user_no, code)
    return code
