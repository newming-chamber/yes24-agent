"""SSE 이벤트 포맷터 — `/chat/stream` 프론트 계약(status/source/delta/done/error).

이 모듈은 순수 함수 계층으로, 다른 프로젝트 모듈(config 등)을 import하지 않는다.
이벤트 계약은 `docs/spec.md` §6을 따른다.
"""

import json
import time


def format_sse(event: str, data: dict) -> str:
    """`event: {event}\\ndata: {json}\\n\\n` 형태의 SSE 프레임을 만든다.

    data에는 항상 `ts`(epoch ms)를 추가한다. 한글이 이스케이프되지 않도록
    `ensure_ascii=False`로 직렬화한다.
    """
    payload = {**data, "ts": int(time.time() * 1000)}
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


def sse_reset() -> str:
    """이미 흘려보낸 본문 버블을 비우게 하는 이벤트.

    본문을 토큰 스트리밍하는 이상, 인용 검증이 무효 마커를 지워 최종 본문이 바뀌면
    사용자가 본 것과 done.text가 어긋난다("delta 합계 == done.text", 원칙 4b).
    이미 보낸 것은 무를 수 없으므로 프론트에 비우게 하고 정본을 다시 보낸다.
    홀드 방식으로 되돌리면 이 이벤트가 필요 없어지지만, 그러면 토큰 스트리밍이 통째로
    죽는다(실측: 2,385자 답변이 12.4초 무출력 → 스트리밍 복원 후 48조각/6.0초 시작).
    """
    return format_sse("reset", {})


def sse_status(stage: str, detail: str = "") -> str:
    """진행 상태 이벤트 (예: "Yes24 검색 중…")."""
    return format_sse("status", {"stage": stage, "detail": detail})


# 16뷰 매트릭스(/chat/matrix)용 가법 kwarg `col`. col=None(기본)이면 페이로드에 키를 넣지
# 않아 /chat/stream 프레임과 **바이트 동일**하다(단일 채팅·스트리밍 팀 무영향). col 지정 시에만
# payload에 "col":k(0~15)를 실어, 매트릭스 프론트가 프레임을 열별로 라우팅한다.
def _with_col(data: dict, col: int | None) -> dict:
    """col이 주어지면 payload에 열 인덱스를 더한다(None이면 원본 그대로)."""
    return data if col is None else {**data, "col": col}


def sse_source(source: dict, col: int | None = None) -> str:
    """최종 인용 검증을 통과한 출처 이벤트를 공개 DTO로 직렬화한다.

    image_url·author·price는 있는 상품 출처에만 싣고, 웹·정책 출처에서는 생략한다.
    """
    data = {
        "id": source["id"],
        "title": source["title"],
        "url": source["url"],
        "type": source["type"],
    }
    image_url = source.get("image_url")
    if image_url:
        data["image_url"] = image_url
    author = source.get("author")
    if author:
        data["author"] = author
    price = source.get("price")
    if price is not None:
        data["price"] = price
    return format_sse("source", _with_col(data, col))


def sse_delta(text: str, col: int | None = None, extra: dict | None = None) -> str:
    """답변 본문 조각(인용 마커 포함 가능) 이벤트.

    extra는 매트릭스 열 카드 정체성(code·name·axis_label 등)을 delta에 함께 실어 프론트가
    첫 페인트에서 카드 제목·부제를 확보하게 하는 가법 필드다. col=None·extra=None(기본)이면
    페이로드가 {"text":…}뿐이라 /chat/stream delta와 바이트 동일하다.
    """
    data = {"text": text, **extra} if extra else {"text": text}
    return format_sse("delta", _with_col(data, col))


def sse_done(payload: dict, col: int | None = None) -> str:
    """최종 출처 목록·grounding_supports·session_id를 담은 종료 이벤트."""
    return format_sse("done", _with_col(payload, col))


def sse_error(message: str) -> str:
    """에러 이벤트."""
    return format_sse("error", {"message": message})
