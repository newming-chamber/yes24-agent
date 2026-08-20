"""유저 프로필의 RBTI 코드 조회 — **껍데기**(데이터 소스 미정).

요청에 rbti가 없을 때 유저 프로필에서 저장된 RBTI를 가져와 페르소나를 자동 적용하는
자리다. 데이터 소스(Yes24 회원 API 확장 필드? 자체 users 테이블 컬럼?)가 아직 정해지지
않아 지금은 항상 None을 반환한다 — 배선(main.chat_stream의 폴백)은 살아 있으므로 소스가
정해지면 이 함수 본문만 채우면 된다.
"""

from __future__ import annotations


async def fetch_user_rbti(user_no: str | None) -> str | None:
    """user_no의 저장된 RBTI 코드를 돌려준다(없으면 None). 현재는 항상 None.

    user_no가 None이면 익명 요청이라 조회 대상 자체가 없다. 반환 코드의 유효성 검증은
    호출 경로의 `is_valid_code`(runner)가 담당하므로 여기서 형식을 보증하지 않는다.
    async인 이유: 장래 구현이 DB·HTTP 조회라 호출부 배선을 미리 비동기로 고정해 둔다.
    """
    return None
