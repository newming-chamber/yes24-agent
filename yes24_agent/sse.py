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


def sse_status(stage: str, detail: str = "", refs: list[dict] | None = None) -> str:
    """진행 상태 이벤트 (예: "Yes24 검색 중…").

    `refs`는 **마커를 렌더할 최소 정보(id·url)**를 도구 응답 시점에 미리 실어 보내는
    가법 필드다. 이게 없으면 프론트는 어떤 [n]이 실재 인용인지 몰라 스트리밍 내내
    생 대괄호로 두다가 done 직전 source 이벤트가 와서야 칩으로 승격한다(실측: 마커
    노출과 카드 도착 사이 0.42초, 서버가 id를 안 시점부터는 5.14초).

    **카드가 아니다.** 제목·가격 등 검증 대상 상품 사실은 싣지 않고, 프론트도 이걸로
    출처 카드를 만들지 않는다 — 공개 `source`와 `done.sources`가 최종 인용분만 담는다는
    원칙 4는 그대로다. refs 미지정(기본)이면 페이로드에 키를 넣지 않아 기존 프레임과
    바이트 동일하다(_with_col과 같은 규율).
    """
    data = {"stage": stage, "detail": detail}
    if refs:
        data["refs"] = refs
    return format_sse("status", data)


# 16뷰 매트릭스(/chat/matrix)용 가법 kwarg `col`. col=None(기본)이면 페이로드에 키를 넣지
# 않아 /chat/stream 프레임과 **바이트 동일**하다(단일 채팅·스트리밍 팀 무영향). col 지정 시에만
# payload에 "col":k(0~15)를 실어, 매트릭스 프론트가 프레임을 열별로 라우팅한다.
def _with_col(data: dict, col: int | None) -> dict:
    """col이 주어지면 payload에 열 인덱스를 더한다(None이면 원본 그대로)."""
    return data if col is None else {**data, "col": col}


def sse_source(source: dict, col: int | None = None) -> str:
    """최종 인용 검증을 통과한 출처 이벤트를 공개 DTO 그대로 직렬화한다.

    **필드 선별은 여기서 하지 않는다** — 모든 호출부(runner 마감 2곳·matrix 재방출)가
    이미 `project_public_source`를 거친 공개 DTO(`done.sources` 항목)를 넘기며, 무엇이
    공개 가능한지의 판정은 그 투영 계층이 소유한다. 예전엔 여기서 id·title·url·type·상품
    3필드만 다시 열거해 걸렀는데, 같은 판정의 중복 구현이라 공개 DTO에 필드가 늘 때마다
    (예: 새 출처 타입의 메타 필드) 라이브 카드만 조용히 탈락하는 드리프트를 냈다
    (2026-08-04 실측: 카드 정보줄이 새로고침 후에만 표시).
    """
    return format_sse("source", _with_col(dict(source), col))


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


def sse_meta(payload: dict) -> str:
    """턴 부가 정보(추천 이유·세션 제목) 이벤트 — `done` **직전**에 나가는 가법 채널.

    스트림은 항상 done으로 끝난다(crema-ai 계약과 동일 배치 — 2026-08-20 사용자 결정).
    done에서 스트림을 닫는 소비자도 meta를 받고, 모르는 이벤트를 무시하는 소비자에겐
    가법이라 하위 호환이다. 추천의 `id`는 done.sources·본문 마커와 **같은 공개 표시
    번호 공간**을 쓴다 — 프론트가 출처 카드에 이유를 붙일 때 재매핑이 필요 없다.
    """
    return format_sse("meta", payload)


# 범용 일시 오류 안내 문구 — 채팅(runner)·매트릭스(matrix_runner)가 같은 문구를 쓴다.
# 각자 리터럴로 두면 한쪽만 고쳐 채팅/매트릭스 안내가 갈라진다(단일 진실).
STREAM_ERROR_MESSAGE = "일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요."


def sse_error(message: str) -> str:
    """에러 이벤트."""
    return format_sse("error", {"message": message})
