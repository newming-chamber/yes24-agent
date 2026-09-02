"""유저 프로필의 RBTI 코드 조회 — `users.rbti` 컬럼.

**프론트가 매 요청에 싣는 값이 아니다**(2026-09-02 사용자 확인). RBTI는 사람에게 붙어 있는
속성이라 서버가 소유한다: 유형 검사 결과를 `PUT /me/rbti`로 한 번 저장하면, 그 뒤 모든
`/chat/stream` 턴이 요청에 아무것도 없어도 이 값을 자동 적용한다
(main.chat_stream의 `request.rbti or await fetch_user_rbti(user_no)`).

요청의 `rbti`는 그대로 남지만 **어드민(데모 UI)의 페르소나 선택기 전용**이라 공개 문서에서
숨긴다 — 사람의 저장된 유형을 일시적으로 덮어써 보는 용도다.
"""

from __future__ import annotations

from yes24_agent.auth import AuthService


async def fetch_user_rbti(user_no: str | None) -> str | None:
    """user_no의 저장된 RBTI 코드를 돌려준다(없으면 None).

    user_no가 None이면 익명 요청이라 조회 대상 자체가 없다. 인증 스택이 없는 구성(sqlite
    로컬)에서도 None이다 — 저장소가 없으면 저장된 유형도 없는 것이 사실이다. 반환 코드의
    유효성 검증은 호출 경로의 `is_valid_code`(runner)가 담당한다.
    """
    if not user_no:
        return None
    service = AuthService.get_instance()
    if not service.enabled:
        return None
    return await service.read_rbti(user_no)
