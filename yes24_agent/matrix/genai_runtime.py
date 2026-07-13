"""매트릭스용 google.genai 클라이언트 싱글턴(공유 팩토리).

retrieval(쿼리 정제)과 generate(16 fan-out)이 같은 클라이언트를 공유하도록 한 곳에 둔다
(순환 import 방지 겸 클라이언트 중복 생성 방지). ensure_google_api_key_env가 GOOGLE_API_KEY를
세팅하므로 genai.Client()가 인증된다. 테스트는 호출부에 스텁을 주입해 이 팩토리를 우회한다.
"""

from __future__ import annotations

from google import genai

from yes24_agent.config import ensure_google_api_key_env

_client: genai.Client | None = None


def get_genai_client() -> genai.Client:
    """공유 genai 클라이언트 싱글턴을 반환한다(최초 호출 시 생성·인증)."""
    global _client
    if _client is None:
        ensure_google_api_key_env()
        _client = genai.Client()
    return _client
