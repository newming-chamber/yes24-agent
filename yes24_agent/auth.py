"""공유 패스워드 로그인월 — 토큰·검증 순수 계층.

진짜 인증 시스템이 아니라 데모 접근을 막는 단일 공유 비밀번호 게이트다(config.access_password).
쿠키에는 비밀번호 자체가 아니라 HMAC 토큰을 담아, 비밀번호 노출 없이 서버가 매 요청 재계산해
상수시간 비교로 확인한다. 세션 저장소가 필요 없다(비밀번호가 키인 결정론 토큰). config·ADK를
import하지 않는 순수 함수 계층이라 네트워크 없이 단위 테스트된다.
"""

import hmac
from hashlib import sha256
from secrets import compare_digest

# 로그인 성공 시 발급하는 쿠키 이름.
ACCESS_COOKIE = "yes24_access"
# 토큰 HMAC 메시지(비밀번호가 키). 값 자체는 비밀이 아니며 버전만 구분한다.
_TOKEN_MESSAGE = b"yes24-agent-access-v1"


def expected_token(password: str) -> str:
    """비밀번호로부터 결정론적 접근 토큰(HMAC-SHA256 hex)을 만든다.

    같은 비밀번호는 항상 같은 토큰을 낸다 → 세션 저장 없이 쿠키만으로 검증한다.
    """
    return hmac.new(password.encode("utf-8"), _TOKEN_MESSAGE, sha256).hexdigest()


def token_valid(cookie_value: str | None, password: str) -> bool:
    """쿠키 토큰이 현재 비밀번호에서 파생된 값과 일치하는지 상수시간 비교로 판정한다."""
    if not cookie_value:
        return False
    return compare_digest(cookie_value, expected_token(password))


def password_matches(candidate: str, password: str) -> bool:
    """입력 비밀번호가 설정값과 일치하는지 상수시간 비교로 판정한다(타이밍 공격 완화)."""
    return compare_digest(candidate.encode("utf-8"), password.encode("utf-8"))
