"""검색 각도 계획 공용 헬퍼 — yes24_search·web_search가 공유한다.

각도 계획(문자열 관용 변환→빈/비문자 제거→중복 제거→상한 cap→dropped 수집)과
각도별 실패 요약 dict, 상한 초과 안내 메시지는 두 검색 도구가 문자 단위로 같아야
한다(반환 계약이 프론트·에이전트에서 동일하게 해석된다). 같은 로직을 도구마다
복제하면 한쪽만 고치는 실수가 생기므로 여기 한 곳에만 둔다.

fetch_many는 제외 — duplicate 자리표시(None)·상이한 메시지·all_failed 병합 때문에
같은 계약이 아니다(강제 통합 시 주입 인자만 늘어난다).
"""


def plan_queries(queries, max_count: int) -> tuple[list[str], list[str]]:
    """검색 각도 리스트를 계획한다. 반환: (planned, dropped_queries).

    문자열이 아니거나 빈 각도는 버리고, 같은 각도는 한 번만(중복 검색은 트래픽·컨텍스트
    낭비), 상한(max_count)까지만 계획한다. 단일 문자열로 잘못 넘어와도 관용 처리한다.
    상한을 넘은 각도는 dropped_queries로 돌려줘 호출자가 fail-loud로 알리게 한다.
    """
    if isinstance(queries, str):
        queries = [queries]
    requested = (
        [q.strip() for q in queries if isinstance(q, str) and q.strip()]
        if isinstance(queries, list)
        else []
    )

    planned: list[str] = []
    seen_queries: set[str] = set()
    dropped_queries: list[str] = []
    for q in requested:
        if q in seen_queries:
            continue
        # 상한 초과분도 seen에 넣는다 — 같은 각도가 dropped에 중복 수록되면
        # 안내 메시지의 개수가 부풀어 거짓 알림이 된다(예: [a,b,c,c] cap2 → dropped [c,c]).
        seen_queries.add(q)
        if len(planned) >= max_count:
            dropped_queries.append(q)
            continue
        planned.append(q)
    return planned, dropped_queries


def angle_error_summary(query: str, error_type: str) -> dict:
    """searches 요약에 싣는 각도별 실패 항목(부분 실패 fail-loud)을 조립한다."""
    return {
        "query": query,
        "status": "error",
        "error_type": error_type,
        "result_count": 0,
    }


def dropped_queries_message(max_count: int, dropped_count: int) -> str:
    """상한 초과로 검색하지 않은 각도가 있음을 알리는 안내 메시지를 조립한다."""
    return (
        f"한 번에 검색할 수 있는 각도 상한({max_count}개)을 넘어 "
        f"{dropped_count}개 각도는 검색하지 않았습니다. "
        "필요하면 남은 각도로 한 번 더 호출하세요."
    )
