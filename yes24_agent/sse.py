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


def sse_status(stage: str, detail: str = "") -> str:
    """진행 상태 이벤트 (예: "Yes24 검색 중…")."""
    return format_sse("status", {"stage": stage, "detail": detail})


def sse_ack(text: str) -> str:
    """인터스티셜 응대(ack) 이벤트 — 도구 호출 전 공감·안내 첫 문장을 별도 채널로 즉시 흘린다.

    본문(delta)·done.text와 분리된 표시 전용 채널이다(4b 불변: done.text에 미포함). 프론트는
    답변 버블 위 은은한 응대 라인으로 띄운다. status(진행 라벨)와 별도 — "응대 → 검색 중 → 본문".
    """
    return format_sse("ack", {"text": text})


# 16뷰 매트릭스(/chat/matrix)용 가법 kwarg `col`. col=None(기본)이면 페이로드에 키를 넣지
# 않아 /chat/stream 프레임과 **바이트 동일**하다(단일 채팅·스트리밍 팀 무영향). col 지정 시에만
# payload에 "col":k(0~15)를 실어, 매트릭스 프론트가 프레임을 열별로 라우팅한다.
def _with_col(data: dict, col: int | None) -> dict:
    """col이 주어지면 payload에 열 인덱스를 더한다(None이면 원본 그대로)."""
    return data if col is None else {**data, "col": col}


def sse_source(source: dict, col: int | None = None) -> str:
    """출처 확보 즉시 노출하는 이벤트. id/title/url/type(+표지 image_url, +저자·가격)만 추출한다.

    image_url·author·price는 **있을 때만** 실어(상품 검색 결과), 없는 출처(web·notice 등)의
    프레임은 이전과 바이트 동일하게 유지한다(가법 필드, 기존 계약 불변). author·price는 카드가
    스트리밍 시점부터 저자·가격을 보이게 하려는 것 — 카드는 이 관찰 이벤트에서 그려져 done까지
    유지되므로, 여기서 빠뜨리면 비인용 카드는 끝까지 저자·가격이 없다.
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


def is_done_event(sse_str: str) -> bool:
    """스트림 펌프 종료 판정용 — `done` 이벤트인지 확인한다."""
    first_line = sse_str.split("\n", 1)[0]
    return first_line == "event: done"
