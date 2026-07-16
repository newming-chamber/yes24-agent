"""ADK 이벤트의 최종 텍스트를 손실 없이 조립한다."""


def _event_text(event) -> str:
    """이벤트 content에서 텍스트 파트만 이어붙인다."""
    if not event.content or not event.content.parts:
        return ""
    return "".join(part.text or "" for part in event.content.parts)
