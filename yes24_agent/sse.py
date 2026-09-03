"""SSE 이벤트 포맷터 — `/chat/stream` 프론트 계약(status/source/delta/done/error).

이 모듈은 순수 함수 계층으로, 다른 프로젝트 모듈(config 등)을 import하지 않는다.
이벤트 계약은 `docs/spec.md` §6을 따른다.
"""

import json
import time

# 프론트 개발자용 **SSE 응답 계약**(OpenAPI 설명의 단일 소유자). 라우트가 이 상수를 실어
# `/docs`에 그대로 노출한다 — 종전엔 OpenAPI가 스트리밍 라우트를 전부 `application/json`으로
# 기술해, 문서대로 `res.json()`을 쓰면 그냥 멈췄다(2026-09-01 지적). 계약을 라우트마다 손으로
# 적으면 사본이 갈라지므로, **이벤트를 만드는 이 모듈**이 설명도 소유한다.
SSE_EVENT_CONTRACT = """
**응답은 JSON이 아니라 `text/event-stream`(SSE)이다.** `EventSource`나 스트리밍 fetch로 읽는다.

각 프레임은 아래 꼴이고, 모든 `data`에 `ts`(프레임 생성 시각, epoch ms)가 붙는다.

```
event: <타입>
data: <JSON>
                 ← 빈 줄이 프레임 끝
```

| event | data | 뜻 |
|---|---|---|
| `status` | `{stage, detail, round?, refs?, code?, sources?}` | 진행 상태(stage 열거는 아래) |
| `delta` | `{text, round?}` | 본문 조각. **이어 붙이면 본문이 된다** |
| `source` | `{source}` | 인용된 출처 1건(제목·url·가격·평점 등) |
| `reset` | `{}` | **이미 받은 본문을 버려라.** 인용 검증이 본문을 바꿨을 때만 온다 |
| `meta` | `{recommendations?, session_title?}` | `done` **직전**의 부가 정보(선택적) |
| `done` | `{text, sources, cited_ids, session_id, turn_id, rbti_applied, process}` | 종료·1회 |
| `error` | `{message}` | 사용자에게 보여줄 실패 문구 |

**`status.stage` 열거** — 문장은 서버가 만들지 않는다. `detail`은 모델 산출물(검색 각도·상세
제목·코너명)이거나 구조 신호(건수)뿐이고, 무엇을 하는 중인지의 동사는 stage가 담당한다.
- `thinking` — 힌트(사고 요약 헤드라인). detail = 모델 사고 요약의 단계 제목. 본문이 아니며
  `done.process.steps`에도 없다.
- `persona` — 턴 시작 신호. **`use_rbti: true`일 때만** 첫 모델 이벤트 전 1회, `round: 0`.
  detail = 적용된 RBTI 축 라벨(`"완독-분석-깊이-정보"`, `-`로 나눠 칩), `code`가 함께 실린다 —
  적용됐으면 코드, 요청했지만 코드가 없으면 `code: null`·`detail: ""`.
- `searching` — 툴 호출(yes24_search). detail = 검색 각도들(` · ` 구분).
- `searching_web` — 툴 호출(web_search). detail = 검색 각도들(` · ` 구분).
- `reading` — 툴 호출(yes24_fetch·fetch_many). detail = 여는 상세의 제목(들).
- `browsing` — 툴 호출(yes24_browse). detail = 코너명.
- `found` — 툴 결과. detail = `"N건 찾았어요"`(0건이면 프레임 없음). `sources: [{url, title}]`가
  함께 실린다 — 찾은 출처를 **즉시**(카드보다 먼저) 보여 주기 위한 스텝 출처다. 표시 번호(`id`)와
  가격·평점은 없다(`[n]`은 인용된 출처만 받는다) — 키는 url이고, 인용되면 `refs{id,url}`가 url로
  잇는다. 실을 항목이 없으면 키가 없다. 목록은 그 응답이 **관측시킨 출처 전체**라
  `done.process.sources_reviewed`와 같은 집합이고, detail의 N건과 길이가 다를 수 있다(상세
  열람은 한 권이 종이책·eBook 판형 레코드를 함께 돌려준다 — "2건 찾았어요" 아래 4개).
- `notice` — 툴 결과(실패 안내). detail = 안내 문구(재시도를 암시하지 않는다).
- `refs` — 힌트(마커 렌더). detail = `""`, `refs: [{id, url}]`가 본체. 그 번호가 처음 실리는
  delta보다 먼저 온다.

**`round`(가법, delta·status 공통)** — 0부터 시작하는 LLM 라운드(콜) 인덱스. 도구 응답을
처리한 뒤 처음 도착하는 모델 이벤트에서 +1이다. 렌더 규칙: **round r의 텍스트 뒤에 툴
status가 오면 그 텍스트는 조사 경과(내레이션)이고, 마지막 라운드의 텍스트가 최종 답이다.**
라이브 중에는 현재 라운드가 마지막인지 알 수 없으므로 (a) `done.process.answer_start`로
확정 분할하거나 (b) 문단 휴리스틱으로 미리 답 스타일을 시작한다 — 서버는 (a)를 보장한다.
`thinking`은 번역이 비동기라 다음 라운드의 첫 텍스트보다 늦게 나갈 수 있다(라벨의 round는
방류 시점의 현재 라운드). `reset` 뒤의 정본 재전송 delta는 여러 라운드를 합친 본문이라
round가 없다.

**`done.process`(항상 존재)** — 턴 과정 요약. 접힌 헤더 "N초 · 출처 M개 검토"의 재료다.
```json
"process": {
  "elapsed_ms": 21340,      // 턴 시작 → done 직전(서버 계측)
  "answer_at_ms": 16020,    // 턴 시작 → 최종 답(마지막 라운드)의 첫 본문 청크(접힌 헤더 "N초")
  "sources_reviewed": 5,    // 이번 턴 도구 응답으로 관측한 고유 출처 수(인용 수와 다르다)
  "answer_start": 187,      // done.text에서 최종 답(마지막 라운드)이 시작하는 문자 오프셋
  "round_starts": [0, 187], // 라운드 r 텍스트의 done.text 시작 오프셋(마지막 = answer_start)
  "steps": [                // 이번 턴의 툴 status(thinking·refs·persona 제외), 순서대로
    {"round": 0, "stage": "searching", "detail": "에세이 베스트셀러 · 요즘 인기 에세이"},
    {"round": 0, "stage": "found", "detail": "2건 찾았어요",
     "sources": [{"url": "<출처 url>", "title": "책 1"}, {"url": "<출처 url>", "title": "책 2"}]}
  ]
}
```
`found` 스텝의 `sources`는 라이브 `found` status의 그것과 같은 목록(url·title뿐, 번호·가격
없음)이고, 다른 스텝엔 키가 없다.
`text[:answer_start]`가 내레이션, `text[answer_start:]`가 최종 답이다. 단일 라운드면 0,
마지막 라운드에 텍스트가 없으면 `len(text)`, 본문이 최후 방어 안내로 대체됐으면 0. 라운드
사이의 문단 구분자는 답 쪽에 붙는다. **`round_starts`는 그 분할을 라운드별로 편 것** —
`round_starts[0] == 0`, 단조 비감소, `round_starts[-1] == answer_start`(항상), 라운드 r의
텍스트 = `text[round_starts[r]:round_starts[r+1]]`(마지막은 끝까지 = 최종 답). **히스토리
복원의 순서 재현: r번 텍스트 → r번 스텝(`step.round == r`) → r+1번 텍스트 → …** — 라이브의
`delta.round` 없이도 이 배열과 `steps`만으로 같은 순서를 그린다. 단일 라운드면 `[0]`, 도구
응답 뒤 모델 이벤트 없이 마감된 대기 라운드는 마지막 원소가 `len(text)`(빈 답). 어떤 중간
경계가 정본 안에서 못 맞으면 직전 경계로 클램프(그 라운드 내레이션은 빈 문자열), 마지막
경계가 못 맞으면 전부 0(전부 답 — `answer_start`와 한 판정).
`answer_at_ms`는 헤더 "N초"의 N이다 — 전체 소요가 아니라 **답이 시작되기까지의 조사
시간**(단일 라운드면 벤더 사고 구간 뒤 첫 청크). 마지막
라운드에 텍스트가 없거나 본문이 대체됐으면 `elapsed_ms`와 같다(`answer_at_ms <= elapsed_ms`
항상). 실패 턴(`error` 뒤 `done`)에도 그 시점까지의 누적분이 실린다 — 스트림 시작 전
실패(세션 준비 실패)의 `done`에는 빈 과정(`steps: []`, `sources_reviewed: 0`,
`answer_start: 0`, `round_starts: [0]`, `answer_at_ms == elapsed_ms`)이 실린다. 히스토리 복원
(`GET /chat/sessions/{id}`)의 각 턴 `process`도 같은 모양이다 — 단 `thinking`은 영속되지
않으므로 복원 steps에는 원래 없다. `elapsed_ms`·`answer_at_ms`는 라이브가 done 직전에 확정한
그 값을 턴에 영속해 히스토리도 **같은 값**을 낸다(라이브·새로고침 복원·히스토리의 헤더
"N초"가 동일). 타이밍 영속 이전의 구 턴만 영속 이벤트 timestamp로 근사한다(라운드 스트림이
끝난 시각이라 라이브보다 늦다).

**지켜지는 계약**
- `done`은 **정확히 한 번** 온다. 실패해도 `error` 뒤에 `done`이 온다.
- `reset`이 없으면 **`delta` 합계 == `done.text`**. `reset`이 오면 그 뒤 `delta`만 정본이다.
- 본문의 `[n]` 마커는 **반드시** `done.sources`의 `id`에 매핑된다(무매핑 마커는 서버가 지운다).
- `done.sources`는 **인용된 출처만** 담는다(검색 후보 전체가 아니다).
- 후속 턴은 `done.session_id`를 요청에 실어 이어간다.
- `done`에 `model` 키는 **없다**. 모델명은 어드민(데모 로그인) 세션에만 실린다 — 같은 이유로
  요청의 `model`·`toolsets`도 API 키 호출에서는 무시되고 서버 기본 구성으로 고정된다.
- `done.rbti_applied`는 이 턴에 적용된 RBTI 코드다(미적용이면 `null`) — truthiness가 곧
  "✦ RBTI 데이터가 활용됨" 배지 여부이고, 값은 어떤 독서 유형이 적용됐는지다.
- `done.turn_id`는 이 턴의 서버 식별자다(피드백 API
  `PUT /chat/sessions/{session_id}/turns/{turn_id}/feedback`와 히스토리 복원
  `GET /chat/sessions/{session_id}`의 턴 id가 같은 값을 쓴다). 스트림 시작 전에 실패한
  턴은 `null`일 수 있다.

**최소 예시**
```bash
curl -N -X POST "$BASE_URL/chat/stream" \\
  -H 'Content-Type: application/json' \\
  -d '{"message":"채식주의자 가격 알려줘"}'
```
```js
const res = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
                             body: JSON.stringify({message})});
const reader = res.body.getReader(); // res.json() 아님
```
"""


# 오버뷰(/overview·/overview/warm) 전용 계약. **챗 계약표를 그대로 붙이면 거짓말이 된다** —
# 오버뷰는 status·source·meta를 한 번도 내지 않고 done의 모양도 다르다(2026-09-02 문서 계약
# 검증 W4 실측). 공통 규칙(delta 누적·reset 의미·done 1회·[n] 매핑)은 되풀이하지 않고
# 위 계약을 가리킨다.
OVERVIEW_EVENT_CONTRACT = """
**이벤트** (챗보다 좁다 — `status`·`source`·`meta`는 **오지 않는다**)

| event | data | 뜻 |
|---|---|---|
| `delta` | `{text}` | 본문 조각. 이어 붙이면 본문이 된다 |
| `reset` | `{}` | 이미 받은 본문을 버려라(인용 검증이 본문을 바꿨을 때만) |
| `done` | `{overview: {text, sources, cited_ids}}` 또는 `{degraded: "..."}` | 종료·1회 |
| `error` | `{message}` | 실패 문구 |

**지켜지는 계약**
- `done`은 **정확히 한 번** 온다. 본문·출처는 챗처럼 최상위가 아니라 **`done.overview` 안**에 있다.
- 낼 것이 없으면 `done.degraded`만 온다(`no_results`·`irrelevant`·`timeout` 등)—
  `overview` 키가 없다. 이때 프론트는 패널을 **조용히 접는다**(에러 배너 금지).
- `reset`이 없으면 `delta` 합계 == `done.overview.text`.
- 본문의 `[n]` 마커는 `done.overview.sources`의 `id`에 매핑된다(챗과 같은 규칙).
- 출처 카드는 `source` 이벤트가 아니라 **`done`이 올 때 한 번에** 그린다.
"""


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


# 가법 kwarg의 공통 규율: **None이면 키를 넣지 않는다** — 그래서 지정하지 않은 경로의 프레임은
# 바이트 동일하다. 16뷰 매트릭스(/chat/matrix)의 `col`(열 인덱스 0~15)과 사고과정 UI의 `round`
# (LLM 라운드 인덱스)가 같은 규율을 쓴다. 매트릭스·오버뷰는 round를 넘기지 않는다.
def _with(data: dict, **optional) -> dict:
    """값이 None이 아닌 가법 키만 payload에 더한다(전부 None이면 원본 그대로)."""
    return {**data, **{key: value for key, value in optional.items() if value is not None}}


def sse_status(
    stage: str,
    detail: str = "",
    refs: list[dict] | None = None,
    round: int | None = None,
    extra: dict | None = None,
) -> str:
    """진행 상태 이벤트 (예: "Yes24 검색 중…").

    `refs`는 **마커를 렌더할 최소 정보(id·url)**를 도구 응답 시점에 미리 실어 보내는
    가법 필드다. 이게 없으면 프론트는 어떤 [n]이 실재 인용인지 몰라 스트리밍 내내
    생 대괄호로 두다가 done 직전 source 이벤트가 와서야 칩으로 승격한다(실측: 마커
    노출과 카드 도착 사이 0.42초, 서버가 id를 안 시점부터는 5.14초).

    **카드가 아니다.** 제목·가격 등 검증 대상 상품 사실은 싣지 않고, 프론트도 이걸로
    출처 카드를 만들지 않는다 — 공개 `source`와 `done.sources`가 최종 인용분만 담는다는
    원칙 4는 그대로다. refs 미지정(기본)이면 페이로드에 키를 넣지 않아 기존 프레임과
    바이트 동일하다(_with와 같은 규율).

    `round`는 이 status가 속한 LLM 라운드(0부터), `extra`는 stage별 구조 데이터(persona의
    `code` — 값이 None이어도 **키는 실린다**: "요청했지만 코드 없음"을 프론트가 값으로
    판정한다 — 와 found의 `sources` = 스텝 출처 `[{url, title}]`). 둘 다 가법이다.
    """
    data = {"stage": stage, "detail": detail}
    if refs:
        data["refs"] = refs
    if extra:
        data.update(extra)
    return format_sse("status", _with(data, round=round))


def sse_source(source: dict, col: int | None = None) -> str:
    """최종 인용 검증을 통과한 출처 이벤트를 공개 DTO 그대로 직렬화한다.

    **필드 선별은 여기서 하지 않는다** — 모든 호출부(runner 마감 2곳·matrix 재방출)가
    이미 `project_public_source`를 거친 공개 DTO(`done.sources` 항목)를 넘기며, 무엇이
    공개 가능한지의 판정은 그 투영 계층이 소유한다. 예전엔 여기서 id·title·url·type·상품
    3필드만 다시 열거해 걸렀는데, 같은 판정의 중복 구현이라 공개 DTO에 필드가 늘 때마다
    (예: 새 출처 타입의 메타 필드) 라이브 카드만 조용히 탈락하는 드리프트를 냈다
    (2026-08-04 실측: 카드 정보줄이 새로고침 후에만 표시).
    """
    return format_sse("source", _with(dict(source), col=col))


def sse_delta(
    text: str, col: int | None = None, extra: dict | None = None, round: int | None = None
) -> str:
    """답변 본문 조각(인용 마커 포함 가능) 이벤트.

    extra는 매트릭스 열 카드 정체성(code·name·axis_label 등)을 delta에 함께 실어 프론트가
    첫 페인트에서 카드 제목·부제를 확보하게 하는 가법 필드다. round는 이 조각이 속한 LLM
    라운드(0부터)다 — 라이브 partial에만 실리고, reset 뒤 정본 재전송처럼 여러 라운드를
    합친 조각에는 없다(`done.process.answer_start`가 그 분할을 소유한다). col=None·
    extra=None·round=None(기본)이면 페이로드가 {"text":…}뿐이라 /chat/stream delta와
    바이트 동일하다.
    """
    data = {"text": text, **extra} if extra else {"text": text}
    return format_sse("delta", _with(data, col=col, round=round))


def sse_done(payload: dict, col: int | None = None) -> str:
    """최종 본문·출처 목록·session_id·turn_id를 담은 종료 이벤트(정확히 1회)."""
    return format_sse("done", _with(payload, col=col))


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
