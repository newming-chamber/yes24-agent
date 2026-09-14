"""SSE 이벤트 포맷터 — 채팅의 진행·본문·출처 스냅샷·완료 계약.

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
**응답은 JSON이 아니라 `text/event-stream`(SSE)이다.** 이 경로는 POST이므로 스트리밍
`fetch`로 읽는다. 브라우저 기본 `EventSource`는 POST 본문을 보낼 수 없다.

각 프레임은 아래 꼴이고, 모든 `data`에 `ts`(프레임 생성 시각, epoch ms)가 붙는다.

```
event: <타입>
data: <JSON>
                 ← 빈 줄이 프레임 끝
```

| event | data | 뜻 |
|---|---|---|
| `rbti` | `{code, axes}` | 턴 속성(RBTI 적용). **턴 시작에 한 번**, 첫 모델 이벤트 전 |
| `status` | `{stage, detail, round?, ...}` | 아래 진행 계약 참고 |
| `content` | `{phase, round, answer_start?, round_starts?, offset_unit?}` | 본문 역할·경계 |
| `delta` | `{text, round?}` | 본문 조각. **이어 붙이면 본문이 된다** |
| `sources` | `{items: [PublicSource], final: boolean}` | **전체 목록 교체**, append 아님 |
| `reset` | `{}` | **이미 받은 본문을 버려라.** 인용 검증이 본문을 바꿨을 때만 온다 |
| `meta` | `{recommendations, follow_ups, session_title?, session_id}` | 부가 정보 미리보기 |
| `done` | 완성된 턴(아래 필드) | 연결이 유지되면 1회 |
| `error` | `{message}` | 사용자에게 보여줄 실패 문구 |

`done` 필드: `text/sources/cited_ids/session_id/turn_id/rbti_applied/process/meta/status/error/`
`history_saved`. `status`의 선택 필드: `step_id/state/result_count/refs/sources`.

**`rbti`** — RBTI 적용은 진행 단계가 아니라 **턴 자체의 속성**이라 `status`가 아닌 전용
이벤트다(조사 스텝 목록을 그리는 소비자가 턴 메타를 골라낼 필요가 없다). `use_rbti`가 참일
때만(기본 참) 첫 모델 이벤트 전 1회 온다. `code`는 적용된 4자 코드, 요청했지만 적용할 유형이
없으면 `code: null`·`axes: ""`다 — **`code`가 있으면 그 즉시 배지를 켠다**(`done.rbti_applied`를
기다리지 않는다; 둘은 같은 값이다). `axes`는 축 라벨(`"완독-분석-깊이-정보"`, `-`로 나눠
칩)이다. `applied` 같은 불리언은 없다 — `code`의 유무가 곧 적용 여부다.

**출처 스트리밍** — 조사 후보는 `status.sources`에 즉시 나타난다. 본문이 출처를 인용하면
그 마커의 첫 `delta` 전에 `refs`와 `sources{final:false}`를 보낸다. 이후 같은 자료의
상세 관측이 갱신되거나 새 인용이 추가되면 변경된 전체 목록을 다시 보낸다. 내용이 같으면
생략한다. 인용 검증 후 `sources{final:true}`를 한 번 보낸다(빈 목록도 포함). 본문 교정은
`reset → sources(final:true) → refs → delta` 순서여서 교정·집계 본문의 첫 마커보다 카드가
먼저 온다. 최종 `done.sources`는 이 목록과 같다. 개별 `source` 이벤트는 채팅에서
사용하지 않는다. 매트릭스의 별도 `source` 계약은 유지된다.

**PublicSource** — 공통 `id/title/url/type/card_type`에 관측된 평면 필드가 붙는다.
`id`는 턴 안의 인용 번호이며 턴 간에는 `(turn_id,id)`로 구분한다. `type`은 호환용
`product/notice/web`이고, 렌더링은 `card_type`으로 한다: `book`은 사이트의 도서 분류 또는
도서 구조 데이터를 관측한 자료, `document`는 현재 Yes24 공지·FAQ 계열, `link`는 나머지의
일반 웹 카드다. `book_detail` 같은 수집 경로는 도서 판정 근거가 아니다.
`preview`는 관측 원문의 첫 비어 있지 않은 문단이며 길이가 고정된 요약은 아니다.
접지 원문 `snippet`은 내부에 보존하고 공개하지 않는다. `other_formats`는 관측하지 않으면
생략, 관측했지만 없으면 `[]`, 가격 미상이면 항목의 `sale_price:null`이다.

**`status.stage` 열거** — 문장은 서버가 만들지 않는다. `detail`은 모델 산출물(검색 각도·상세
제목·코너명)이거나 구조 신호(건수)뿐이고, 무엇을 하는 중인지의 동사는 stage가 담당한다.
- `thinking` — 힌트(사고 요약 헤드라인). detail = 모델 사고 요약의 단계 제목. 본문이 아니며
  `done.process.steps`에도 없다.
- `searching` — 툴 호출(yes24_search). detail = 검색 각도들(` · ` 구분).
- `searching_web` — 툴 호출(web_search). detail = 검색 각도들(` · ` 구분).
- `reading` — 툴 호출(yes24_fetch·fetch_many·web_fetch). detail = 관측한 제목, 없으면 빈 문자열.
- `browsing` — 툴 호출(yes24_browse). detail = 코너명들(` · ` 구분).
- `working` — 표시용 분류가 선언되지 않은 도구의 호출. detail은 빈 문자열이다.
- `found` — 툴 완료. 명시된 건수가 있으면 detail = `"N건 찾았어요"`(0건도 보냄).
  건수가 없는 상세 성공은 detail이 비어 있고 `result_count`는 관측 출처 수다.
  `sources: [{url, title}]`가
  함께 실린다 — 찾은 출처를 **즉시**(카드보다 먼저) 보여 주기 위한 스텝 출처다. 표시 번호(`id`)와
  가격·평점은 없다(`[n]`은 인용된 출처만 받는다) — 키는 url이고, 인용되면 `refs{id,url}`가 url로
  잇는다. 실을 항목이 없으면 `[]`다. `sources_reviewed`는 각 응답을 합친 고유 출처 수다.
- `notice` — 툴 결과(실패 안내). detail = 안내 문구(재시도를 암시하지 않는다).
- `refs` — 힌트(마커 렌더). detail = `""`, `refs: [{id, url}]`가 본체. 그 번호가 처음 실리는
  delta보다 먼저 온다.

도구 호출·결과에는 `state:running/completed/failed`, 연결 가능한 경우 같은 `step_id`가
붙는다. ADK 호출 ID로 연결하며, 응답 ID가 없으면 같은 도구명의 미완료 호출 순서로
연결한다. ID가 있는 응답을 다른 ID의 호출에 추측 연결하지 않는다. `result_count`와
`sources`는 결과에만 있으며 프론트는 `detail`에서 건수를 파싱하지 않는다.

**`content`** — 모델은 본문을 쓴 뒤에도 도구를 호출할 수 있으므로 첫 토큰의 최종성은
미리 알 수 없다. 서버가 같은 `round` 블록의 역할만 알리고 텍스트는 복제하지 않는다.
- `provisional`: 해당 라운드의 첫 실제 스트리밍 `delta` 직전 1회. 빈 본문에는 보내지 않는다.
- `narration`: 그 라운드의 본문 뒤 도구 호출이 관측되면 호출 `status` 직전 1회.
- `answer`: 최종 본문 교정·집계 전송 직후, meta 생성·스냅샷 저장을 기다리기 전에 1회.
  `round/answer_start/round_starts/offset_unit`으로 정본의 최종 답 경계를 명시한다.
  범위는 뒤에 올 `done.process`와 같다. 레이아웃 확정이며 성공 선언은 아니다.
  처리 실패·세션 준비 실패에도 보내고 최종 성공 여부는 `done.status`로 판정한다.
  집계·교정 본문은 이미 정본이라 provisional 없이 `delta → content(answer)`로 올 수 있다.

**`round`(가법, delta·status 공통)** — 0부터 시작하는 LLM 라운드(콜) 인덱스. 도구 응답을
처리한 뒤 처음 도착하는 모델 이벤트에서 +1이다. 라이브 블록 역할은 `content`를 따르며
문장·문단 모양이나 최대 round 값으로 내레이션/최종 답을 추측하지 않는다.
`thinking`은 번역이 비동기라 다음 라운드의 첫 텍스트보다 늦게 나갈 수 있다(라벨의 round는
방류 시점의 현재 라운드). `reset` 뒤의 정본 재전송 delta는 여러 라운드를 합친 본문이라
round가 없다.

**`done.process`(항상 존재)** — 턴 과정 요약. 접힌 헤더 "N초 · 출처 M개 검토"의 재료다.
```json
"process": {
  "elapsed_ms": 21340,      // 턴 시작 → 메타·제목 처리 후 스냅샷 저장 직전
  "answer_at_ms": 16020,    // 턴 시작 → 최종 답(마지막 라운드)의 첫 본문 청크(접힌 헤더 "N초")
  "sources_reviewed": 5,    // 이번 턴 도구 응답으로 관측한 고유 출처 수(인용 수와 다르다)
  "answer_start": 187,      // done.text에서 최종 답(마지막 라운드)이 시작하는 문자 오프셋
  "round_starts": [0, 187], // 라운드 r 텍스트의 done.text 시작 오프셋(마지막 = answer_start)
  "offset_unit": "unicode_codepoint", // JS UTF-16 문자열 인덱스가 아니다
  "steps": [                // 이번 턴의 툴 status(thinking·refs 제외), 순서대로
    {"round": 0, "step_id": "step-1", "state": "running",
     "stage": "searching", "detail": "에세이 베스트셀러 · 요즘 인기 에세이"},
    {"round": 0, "step_id": "step-1", "state": "completed", "result_count": 2,
     "stage": "found", "detail": "2건 찾았어요",
     "sources": [{"url": "<출처 url>", "title": "책 1"}, {"url": "<출처 url>", "title": "책 2"}]}
  ]
}
```
`found` 스텝의 `sources`는 라이브 `found` status의 그것과 같은 목록(url·title뿐, 번호·가격
없음)이고, 실패 결과는 빈 목록이다. 호출 스텝엔 키가 없다.
`text[:answer_start]`가 내레이션, `text[answer_start:]`가 최종 답이다.
이 표기는 Unicode codepoint 기준이다. JS에서는 `Array.from(text).slice(start,end).join("")`
같은 공통 변환을 거쳐야 이모지 앞뒤에서 경계가 어긋나지 않는다.
단일 라운드면 0, 마지막 라운드에 텍스트가 없으면 `len(text)`, 본문이 대체됐으면 0. 라운드
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
라운드에 텍스트가 없거나 본문이 대체됐으면 본문 마감 시각이다(`answer_at_ms <= elapsed_ms`
항상). 실패 턴(`error` 뒤 `done`)에도 그 시점까지의 누적분이 실린다 — 스트림 시작 전
실패(세션 준비 실패)의 `done`에는 빈 과정(`steps: []`, `sources_reviewed: 0`,
`answer_start: 0`, `round_starts: [0]`, `answer_at_ms == elapsed_ms`)이 실린다. 히스토리 복원
(`GET /chat/sessions/{id}`)의 각 턴 `process`도 같은 모양이다 — 단 `thinking`은 영속되지
않으므로 복원 steps에는 원래 없다. `elapsed_ms`·`answer_at_ms`는 라이브가 done 직전에 확정한
그 값을 완성된 턴과 함께 영속해 히스토리도 **같은 값**을 낸다(라이브·새로고침 복원·히스토리의 헤더
"N초"가 동일). 타이밍 영속 이전의 구 턴만 영속 이벤트 timestamp로 근사한다(라운드 스트림이
끝난 시각이라 라이브보다 늦다).

**지켜지는 계약**
- 연결이 유지되고 서버가 마감할 수 있으면 `done`은 한 번 온다. 처리 실패는 `error` 뒤
  `done.status:failed`로 끝난다. 클라이언트 중단·연결 단절에는 `done` 도착을 보장하지 않는다.
- `done.status`는 `completed/failed`, `error`는 정상일 때 `null`, 실패일 때 `{code,message}`다.
  중단된 턴은 best-effort 영속하고 히스토리의 `status:interrupted`로 구분한다.
- `done.meta`는 항상 객체이며 `recommendations/follow_ups`는 항상 배열이다. 메타 생성 실패가
  본문 성공을 바꾸지 않는다. 추천 도서가 아닌 도서 출처는 recommendations에 넣지 않는다.
- `history_saved:false`면 최종 스냅샷 영속에 실패한 것이다. `done`이 없는 EOF는 완료가 아니다.
- `reset`이 없으면 **`delta` 합계 == `done.text`**. `reset`이 오면 그 뒤 `delta`만 정본이다.
- 본문의 `[n]` 마커는 **반드시** `done.sources`의 `id`에 매핑된다(무매핑 마커는 서버가 지운다).
- `done.sources`는 **인용된 출처만** 담는다(검색 후보 전체가 아니다).
- 후속 턴은 `done.session_id`를 요청에 실어 이어간다.
- `done`에 `model` 키는 **없다**. 모델명은 어드민(데모 로그인) 세션에만 실린다 — 같은 이유로
  요청의 `model`·`toolsets`도 API 키 호출에서는 무시되고 서버 기본 구성으로 고정된다.
- `done.rbti_applied`는 이 턴에 적용된 RBTI 코드다(미적용이면 `null`) — 턴 시작의 `rbti.code`와
  같은 값이며, 히스토리 복원 배지의 근거다(라이브 배지는 `rbti` 이벤트가 켠다).
- `done.turn_id`는 이 턴의 서버 식별자다(피드백 API
  `PUT /chat/sessions/{session_id}/turns/{turn_id}/feedback`, 링크 클릭 기록
  `POST /chat/sessions/{session_id}/turns/{turn_id}/clicks`, 히스토리 복원
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
네트워크 청크와 SSE 프레임 경계는 다르다. UTF-8 디코더와 프레임 버퍼가 필요하며,
내장 HTML의 `/static/lib/sse.js` 공통 parser와 저장소 `api/README.md`를 참고한다.
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

    `round`는 이 status가 속한 LLM 라운드(0부터), `extra`는 stage별 구조 데이터(found의
    `sources` = 스텝 출처 `[{url, title}]`, 도구 스텝의 `step_id`·`state`·`result_count`).
    둘 다 가법이다.
    """
    data = {"stage": stage, "detail": detail}
    if refs:
        data["refs"] = refs
    if extra:
        data.update(extra)
    return format_sse("status", _with(data, round=round))


def sse_source(source: dict, col: int | None = None) -> str:
    """매트릭스의 개별 출처 계약. 필드 투영은 project_public_source가 소유한다."""
    return format_sse("source", _with(dict(source), col=col))


def sse_sources(items: list[dict], *, final: bool) -> str:
    """채팅 출처의 전체 스냅샷. 소비자는 누적 append하지 않고 목록을 교체한다."""
    return format_sse("sources", {"items": items, "final": final})


def sse_content(payload: dict) -> str:
    """본문을 복제하지 않고 라운드 역할 또는 정본의 답 경계만 알린다."""
    return format_sse("content", payload)


def sse_rbti(code: str | None, axes: str) -> str:
    """턴 속성 이벤트 — 이 턴에 적용된 RBTI 독서 유형(턴 시작에 1회).

    진행 단계(`status`)가 아니라 **별도 이벤트**인 이유: RBTI 적용은 조사 과정의 한 스텝이
    아니라 턴 전체의 속성이다. status로 내면 진행 UI에 한 단계처럼 끼어들고, 소비자는 스텝
    스트림에서 턴 메타를 골라내야 한다(2026-09-10 분리). `done.rbti_applied`는 끝나야 알지만
    배지는 시작에 켜져야 하므로 첫 모델 이벤트 전에 나간다.

    `code`는 None이어도 **키가 실린다** — "요청했지만 적용할 유형 없음"(피그마 10-C)을 프론트가
    값으로 판정한다. `axes`는 축 라벨(`"완독-분석-깊이-정보"`, 코드 없으면 "")이다. 파생
    불리언(`applied`)은 두지 않는다 — code의 유무가 곧 적용 여부라 두 진실이 생긴다.
    """
    return format_sse("rbti", {"code": code, "axes": axes})


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
