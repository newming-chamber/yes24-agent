"""출간일 → 시제 상태(pub_status) 계산 공용 헬퍼.

모델이 출처의 날짜 문구를 시제 확인 없이 앵무새하는 문제를, 도구가 오늘(KST) 기준으로
계산한 시제 표현을 함께 실어 역이용해 해결한다. 세 도구(search/browse/fetch)가 공유한다.

pub_date는 Yes24 실측 기준 "YYYY년 MM월" 또는 "YYYY년 MM월 DD일" 형식이다. 연·월 단위로만
비교하며(일 단위 불필요), 파싱 불가 형식이면 None을 반환한다(억지 계산 금지).
"""

import re
from datetime import datetime, timedelta, timezone

# KST(UTC+9). "오늘" 판정 기준.
_KST = timezone(timedelta(hours=9))

# "2022년 03월" / "2022년 03월 28일" 등에서 연·월 추출.
_YEAR_MONTH_RE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월")


def pub_status(pub_date: str | None, *, now: datetime | None = None) -> str | None:
    """출간일 문자열을 오늘 기준 시제 표현으로 변환한다.

    Args:
        pub_date: "YYYY년 MM월[ DD일]" 형식 문자열. None이거나 형식이 안 맞으면 None 반환.
        now: 기준 시각(테스트 주입용). 생략 시 KST 현재.

    Returns:
        과거: "출간됨 (약 N년 M개월 전)" 또는 같은 달이면 "출간됨 (이번 달)".
        미래: "출간 예정 (약 N개월 후)". 파싱 불가 시 None.
    """
    if not pub_date:
        return None
    match = _YEAR_MONTH_RE.search(pub_date)
    if match is None:
        return None

    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None

    now = now or datetime.now(_KST)
    delta_months = (year * 12 + month) - (now.year * 12 + now.month)

    if delta_months == 0:
        return "출간됨 (이번 달)"
    if delta_months < 0:
        return f"출간됨 (약 {_format_span(-delta_months)} 전)"
    return f"출간 예정 (약 {_format_span(delta_months)} 후)"


def _format_span(months: int) -> str:
    """개월 수를 "N년 M개월"로 포맷한다(0인 단위는 생략)."""
    years, rem = divmod(months, 12)
    parts: list[str] = []
    if years:
        parts.append(f"{years}년")
    if rem:
        parts.append(f"{rem}개월")
    return " ".join(parts)
