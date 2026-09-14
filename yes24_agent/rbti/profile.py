"""유저의 RBTI 코드 조회 — 외부 RBTI API가 정본.

**프론트가 매 요청에 싣는 값이 아니다**(2026-09-02 사용자 확인). RBTI는 사람에게 붙어 있는
속성이라 서버가 조회한다: 프론트는 `use_rbti`(이번 턴에 쓸지)만 보내고, 코드 자체는
`main.chat_stream`의 `request.rbti or await fetch_user_rbti(user_no)`가 채운다.

**조회처는 외부 API다**(2026-09-10 확정 — 그전까지 미정이라 항상 미적용이었다). 우리 DB에
복제하지 않는다: 복제하면 사용자가 유형 검사를 다시 해도 우리 쪽이 낡은 값을 계속 적용한다.
그래서 `users.rbti` 컬럼과 `AuthService.read_rbti`는 이 경로에서 더는 쓰지 않는다.

요청의 `rbti`는 **화면의 유형 선택기가 이번 턴만 덮어쓰는 공개 채널**이다(2026-09-14 공개 —
그전엔 데모 UI 전용으로 감춰 뒀는데, 통합 프론트의 선택이 서버에 닿을 길이 없었다). 실려 오면
`main.chat_stream`이 단락 평가로 이 조회를 아예 건너뛴다.
"""

from __future__ import annotations

import logging

import httpx

from yes24_agent.config import get_settings

logger = logging.getLogger(__name__)


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
    return code if isinstance(code, str) and code else None
