# Yes24 AI — 프론트엔드 SSE 연동·렌더링 가이드

2026-09-08 · 대상: 이 저장소의 `POST /chat/stream` 및 대화 히스토리.
외부 크레마 프론트의 배포가 아니라 서버와 내장 HTML의 계약이다. 매트릭스·오버뷰는 별도 API다.

이 파일 하나로 요청 방식, 이벤트 의미, 필드 타입, 화면 상태, 완료·복원 규칙을 확인할 수 있다.
아래 JSON의 상품·URL·문구·식별자는 구조 설명용 가상값이며 실제 상품 정보가 아니다.

## 먼저 확인할 핵심 계약

| 구분 | 서버가 보내는 기준 | 프론트가 할 일 |
|---|---|---|
| 본문 | `delta.text` + `content` 역할 | 글자는 즉시 표시하고 역할 전환 시 기존 블록을 이동 |
| 출처 | `sources.items` 전체 목록 | id 기준 추가·수정·삭제. 목록 자체를 이어 붙이지 않음 |
| 인용 힌트 | `status.refs`의 새/갱신 번호 | id별 누적 갱신. sources와 다른 병합 방식 |
| 추천·후속 질문 | `meta` | 출처 id로 추천 이유 결합(이유는 서버가 세션에서 이월해 실음). 본문 표시를 기다리게 하지 않음 |
| 완료 | `done` 전체 스냅샷 | 누적 상태를 최종값으로 교체하고 status로 성공/실패 판정 |
| 복원 | 히스토리의 턴 스냅샷 | 완료 화면과 같은 렌더러 사용. 원시 스트림을 다시 만들지 않음 |

**`content.phase:answer`는 답변 구간 확정이고, `done`은 턴 종료다. 두 시점은 다르다.**

## 1. 전달 파일과 정본

| 자료 | 용도 |
|---|---|
| 이 문서 | 프론트에 전달할 연동·렌더링 가이드. 요청 §2, 이벤트 §3, 출처 §5, 완료·복원 §8 |
| [sse.js](../yes24_agent/static/lib/sse.js) | POST 응답 스트림 리더. 프레임/UTF-8 분할 처리 |
| 서버 `/docs` · `/openapi.json` | 요청·인증·HTTP 오류·히스토리 응답 스키마 |
| [내장 HTML](../yes24_agent/static/index.html) | 동작하는 스트리밍·자료 탭·복원 구현 예제 |

필드·타입은 이 문서 안에서 확인한다. TypeScript 프로젝트에서는 아래 표를 기준으로 타입을 선언한다.
리더는 내장 프론트가 활성화된 서버에서도 `/static/lib/sse.js`로 제공한다. 로그인월이 적용될 수
있으므로 외부 프론트에는 파일을 복사해 프로젝트의 모듈로 포함하는 것을 권장한다.
OpenAPI에서 SSE 응답은 `text/event-stream` 문자열이며, 이벤트 union 타입이 자동 생성되지는
않는다. **REST는 OpenAPI, SSE는 이 문서의 이벤트별 필드와 처리 규칙을 함께 사용한다.**
`docs/`는 저장소의 기존 로컬 문서 정책으로 Git에서 제외되어 있어 전달 필수 자료는 `api/`와
소스 디렉터리에 두었다.

## 2. 요청과 인증

```http
POST /chat/stream
Content-Type: application/json
Accept: text/event-stream
x-api-key: <사용자별 Yes24 ServiceCookies 값>

{"message":"한강 작가 책과 관련 인터뷰를 알려줘","use_rbti":false}
```

| 요청 필드 | 타입 | 필수 / 기본값 |
|---|---|---|
| message | string | 필수, 비어 있지 않은 질문 |
| session_id | string / null | 선택. 첫 턴 생략 가능, 이후 같은 대화의 ID 전달 |
| use_rbti | boolean | 선택, 기본 false. 이 턴의 독서 성향 활용 요청 |

- 일반 서비스 인증의 `x-api-key`는 사용자별 Yes24 **ServiceCookies 값**이다. 공용 서버 비밀키가
  아니며 `Bearer `, `ServiceCookies=`, 전체 Cookie 문자열을 붙이지 않는다.
- 쿠키 문자열에서 URL 인코딩된 값을 추출했다면 한 번 디코딩해서 전달한다. 이미 디코딩된 값을
  인증 모듈에서 받았다면 그대로 사용한다. 쿠키 접근 가능 여부는 실제 서비스 도메인/속성을 확인한다.
  문서에 쿠키 값을 기록하거나 로그·소스코드에 남기지 않는다.
- 첫 턴은 `session_id` 생략 가능. 후속 턴은 `done.session_id`를 보낸다.
- 중단 후에도 세션 복원 주소가 필요하면 클라이언트에서 생성한 UUID를 첫 요청의
  `session_id`로 보내도 된다. 없으면 해당 ID로 새 세션을 만든다. 최종 ID는 `done`으로 확인한다.
- `message`는 필수이며 공백·상한 초과는 HTTP 422. 상한은 OpenAPI를 따른다.
- `use_rbti`는 적용 요청이고, 실제 적용 여부는 `done.rbti_applied` 문자열/null로 판단한다.
- 모델·도구 선택은 일반 프론트 계약이 아니다. 어드민 전용 필드를 보내지 않는다.
- 히스토리·클릭·피드백은 인증으로 확인된 사용자 소유권이 필요하다. 같은 session_id라도 다른
  사용자 대화로 이어지지 않는다. 계정 전환 시 진행 요청을 중단하고 로컬 세션/턴 상태도 분리한다.
- 헤더 인증 예제에는 `credentials:"include"`가 필요하지 않다. 데모 비밀번호 로그인 쿠키 방식은
  별도 흐름이다. 외부 프론트에서는 허용 CORS origin과 인증 모듈의 값 전달을 연동 환경에서 확인한다.
- HTTP 401/403/422/503 등 **스트림 시작 전 실패는 JSON**일 수 있다. 먼저 `response.ok`와
  `Content-Type`을 검사하고 오류 본문을 읽는다. HTTP 200만으로 생성 성공을 판정하지 않는다.

## 3. 실제 이벤트 흐름

아래는 순서를 설명하는 예시다. 도구 횟수·출처 수·라운드는 질문에 따라 달라진다.

```text
status  searching, step_id=A, state=running
status  found, step_id=A, state=completed, result_count=3, sources=[후보 URL·제목]
content phase=provisional, round=1      ← 다음 텍스트는 즉시 표시, 역할은 잠정
delta   조사 경과 텍스트, round=1
content phase=narration, round=1        ← 이 라운드를 조사 설명으로 확정
status  reading, step_id=B, state=running
status  found, step_id=B, state=completed, result_count=1, sources=[관측 URL·제목]
status  refs, refs=[{id:1,url:...}]
sources items=[출처1], final=false       ← 인용 delta 전에 카드 데이터
content phase=provisional, round=2
delta   답변 ... [1]
sources items=[출처1,출처2], final=false ← 전체 목록 교체
delta   이어지는 답변 ... [2]
reset                                  ← 인용 교정이 필요할 때만
sources items=[최종 인용 출처들], final=true
status refs / delta                    ← 정본 교정 또는 최종 집계 본문 재전송 시
content phase=answer, 정본 경계          ← 본문 배치 확정, 메타/저장 완료를 기다리지 않음
meta    추천 이유·후속 질문·세션 제목   ← 선택 이벤트
done    완전한 턴 스냅샷               ← 최종 정본
```

잡담은 도구·출처가 없어도 정상이다. 최종 `sources`는 `items:[]`일 수 있다.
`thinking` 상태나 텍스트가 도구 호출 전후에 섞일 수 있으므로 위 순서를 고정 배열로 코딩하지 않는다.

| 이벤트 | 클라이언트 처리 |
|---|---|
| `status` | 과정 UI에 반영. `step_id`로 시작/완료/실패 연결. `detail` 문구 파싱 금지 |
| `content` | 해당 round의 역할을 갱신. provisional은 즉시 표시, narration은 조사 설명, answer는 정본 경계 적용 |
| `delta` | `text`를 본문 버퍼에 누적. `round`별 블록에 배치하고 역할은 `content`를 따름 |
| `sources` | `items`로 **현재 턴의 출처 전체를 교체**. 추가·업데이트·삭제 모두 반영 |
| `reset` | 누적 본문과 본문 기반 인용/라운드 렌더 캐시를 비우고 뒤 delta로 재구성 |
| `meta` | 선택적 미리보기. 소스보다 먼저/나중에 받아도 id로 결합 |
| `error` | 실패 안내를 보이되 리더를 즉시 중단하지 않는다. 뒤의 failed done을 수용 |
| `done` | 본문·출처·메타·과정·종료 상태를 **스냅샷으로 교체**. 더 붙이지 않는다 |

### 이벤트 data 필드

모든 data는 JSON 객체이며 `ts:number`가 공통으로 붙는다. 아래 `?`는 키 생략 가능이다.

| 이벤트 | ts 이외 필드·타입 |
|---|---|
| status | stage:string, detail:string, round?:number, step_id?:string, state?:running/completed/failed, result_count?:number, sources?:[{url:string,title:string}], refs?:[{id:number,url:string}], code?:string/null |
| content | phase:provisional/narration/answer, round:number. answer일 때 answer_start:number, round_starts:number[], offset_unit:unicode_codepoint 필수 |
| delta | text:string, round?:number |
| sources | items:출처 객체 배열(§5), final:boolean |
| reset | 추가 필드 없음 |
| meta | recommendations:[{id:number,reason:string}], follow_ups:string[], session_title?:string, session_id:string |
| error | message:string. 구조화된 오류 code/message는 done.error에 제공 |
| done | §8의 전체 턴 필드. model은 어드민 응답에서만 선택적으로 제공되므로 일반 화면은 의존하지 않음 |

모든 이벤트의 `ts`는 epoch 밀리초다. SSE `id:` 기반 재개/재전송 계약은 제공하지 않는다.
JSON으로 파싱된 모르는 이벤트 이름·추가 필드는 무시한다. 아래 리더는 모든 data를 JSON으로
읽으므로 알 수 없는 이벤트라도 손상된 JSON이면 오류로 처리한다.
연결이 유지되고 서버가 마감할 수 있을 때 `done`이 한 번 온다. **done 없는 EOF는 성공이 아니다.**

### 전송 순서에서 보장되는 부분

- 새 인용 번호의 `refs/sources`는 그 번호가 완성되는 delta보다 먼저 온다.
- 라이브 텍스트는 해당 round의 provisional 뒤에 오지만, **집계·교정 delta는 round와
  provisional 없이 올 수 있다.** 이 경우에도 표시하고 뒤의 answer 경계로 배치를 확정한다.
- `reset` 뒤에는 정본 본문을 새로 누적한다. 기존 과정 스텝·질문·세션까지 지우지 않는다.
- 마감은 `(필요하면 reset →) sources(final:true) → (필요하면 refs·정본 delta →)
  content(answer) → (선택 meta →) done`이다. error가 발생한 경우도 최종 done.status를 확인한다.
- `sources.final:true`도 스트림 종료가 아니다. 메타·완료 처리를 계속 읽는다.

## 4. 소스, 메타, 진행 상태의 차이

| 데이터 | 뜻 | 화면 |
|---|---|---|
| `status.sources` | 도구가 검토한 후보. URL·제목만 있고 인용 번호 없음 | 펼친 조사 과정의 출처 칩 |
| `status.refs` | 본문 인용 번호와 URL 매핑 힌트 | 스트리밍 중 `[n]` 링크 |
| `sources.items` | 현재까지 실제 인용된 관측 자료의 전체 미리보기 | 중간 카드·자료 탭 |
| `done.sources` | 최종 인용 검증 후 자료 목록 | 본문·카드·자료 탭의 확정 출처 |
| `meta.recommendations` | `id` 출처를 **이 세션에서** 왜 추천했는지 — 이번 턴이 권한 것에 한정되지 않는다 | 해당 카드의 추천 이유 |
| `meta.follow_ups` | 이어서 물어볼 질문 | 후속 질문 버튼 |
| `process` | 조사 단계·시간·본문 라운드 경계 | 접기/펼치기 과정 UI |

후보를 모두 카드로 만들지 않는다. 검토 수(`sources_reviewed`)와 인용 자료 수는 다르다.
도서 출처가 있다고 모두 추천된 것은 아니다. 추천 이유가 없으면 이유 영역을 숨긴다.
추천 메타 생성이 실패해도 본문이 성공하면 정상이며, 최종 메타 배열은 비어 있을 수 있다.
`reason`은 "이번 턴이 권한 항목"이 아니라 **이 세션에서 그 책을 추천한 이유**다. 한 번 이유가
붙은 책은 이후 턴에서 다시 인용되면(권유가 아닌 정보 확인 턴이어도) 그 턴의 `id`로 계속 실린다.
같은 턴에 새 이유가 생기면 새 이유가 이긴다. 이 이월은 서버가 URL 기준으로 한다 — 출처 `id`는
턴마다 인용 순서로 재부여되므로(같은 책 3권이 턴1 `1,2,3` → 턴2 `3,1,2`) 클라이언트가 id로
이유를 캐싱하면 다른 책에 다른 이유가 붙는다. 클라이언트는 **각 턴의 `meta.recommendations`를
그 턴의 `id`에만 붙이고** 턴 간 이유 캐시를 만들지 않는다. 히스토리 턴 스냅샷도 같은 값을 담아
한 턴만 렌더해도 이유가 복원된다.

`status.refs`는 새 번호만 올 수 있으므로 `refsById.set(ref.id, ref.url)`처럼 **id별로 누적/갱신**한다.
sources.items는 이와 달리 전체 교체다. 완료 후 인용 링크의 최종 기준은 done.sources다.

### 진행 상태의 표시 기준

| status.stage | 의미 | 표시 정책 |
|---|---|---|
| `thinking` | 현재 사고 헤드라인 | 살아 있는 진행 라벨. 답 시작/완료 신호로 사용하지 않음 |
| `persona` | 요청한 RBTI 적용 신호 | code/detail 표시. 최종 적용 배지는 done.rbti_applied 사용 |
| `searching`, `searching_web` | Yes24 / 웹 검색 | 같은 step_id의 진행 항목 시작 |
| `reading`, `browsing`, `working` | 열람 / 둘러보기 / 기타 도구 작업 | 진행 항목 시작. detail이 비어도 step_id/state로 처리 |
| `found` | 도구 결과 수신 | 같은 step_id 갱신, result_count와 후보 sources 표시. 0건도 완료 |
| `notice` | 도구 실패 안내 | 해당 단계의 실패 표시. 턴 전체 실패나 자동 재시도 지시가 아님 |
| `refs` | 본문 인용 번호→URL 힌트 | 번호 맵만 갱신. 화면에 조사 단계로 추가하지 않음 |

도구 단계의 state는 `running/completed/failed`다. 같은 step_id의 running과 completed를
별개 작업 두 개로 만들지 않는다. step_id가 없으면 없는 ID를 만들어 다른 단계와 억지로 연결하지 않는다.
thinking/persona/refs는 done.process.steps에 저장되지 않는다. 복원 시 thinking 문구를 재현하려고
추측하지 않는다. RBTI 배지는 done.rbti_applied로 복원한다.

## 5. 카드와 자료 탭

`type`은 기존 분류(`product/notice/web`), **렌더링 기준은 `card_type`**이다.
`product`나 내부 수집 이름 `book_detail`만으로 책인지 추측하지 않는다.

| card_type | 표시 | 결측 처리 |
|---|---|---|
| `book` | 도서 표지·제목·저자·출판사·관측 가격/평점·추천 이유·판형 | 없는 행 숨김, 이미지 실패는 대체 영역 |
| `document` | 공지/FAQ 제목·발췌·출처 | 발췌 없으면 해당 영역 숨김 |
| `link` | 일반 웹 카드: 제목·도메인·발췌 | 빈 제목은 도메인, 잘못된 URL은 비활성 |

- 책 분류는 실제 사이트 도서 분류/도서 구조 데이터에 근거한다. `link`에는 책으로 확인되지
  않은 상품도 포함된다. 웹 검색에서 왔다는 뜻이 아니라 **일반 링크 템플릿**이라는 뜻이다.
- `preview`는 관측 원문의 첫 비어 있지 않은 문단이다. 고정 길이 요약/추천 이유가 아니다.
  서버의 페이로드 안전 상한을 넘으면 일부와 말줄임표만 올 수 있으므로 전문으로 취급하지 않는다.
  내부 접지 원문 `snippet`은 공개하지 않는다. 화면에서는 줄 수로 제한하고 원문 링크를 제공한다.
  Markdown 기호가 포함될 수 있으므로 안전한 렌더러를 사용한다. 카드 안의 링크/이미지는
  별도 상호작용을 만들지 않도록 처리하고 긴 코드/표가 카드 폭을 넘지 않게 한다.
- 출처 ID는 **턴 안에서만 유효**하다. 턴 간 키는 `(turn_id, source.id)`, 스트리밍 중에는
  `(로컬 턴 키, source.id)`를 쓴다. 상품 ID나 세션 전체의 고유 ID로 쓰지 않는다.
- `sources`는 전체 교체다. `source.id`로 DOM을 재사용하되 바뀐 필드를 갱신하고 목록에서
  사라진 항목은 지운다. 최종 done을 또 append하면 카드가 중복된다.
- `meta`는 `recommendations/follow_ups/session_title`만 UI 상태에 저장한다. 전송용 `ts/session_id`를
  섞으면 내용이 같은 `done.meta`도 변경으로 오인한다. 실제 내용이 같으면 카드/후속 질문 DOM을 유지한다.
- `sale_price`가 숫자면 표시하며 **0도 유효**하다. 없음/null을 0원으로 바꾸지 않는다.
- `other_formats` 없음 = 미관측, `[]` = 관측했으나 없음. 판형의 `sale_price:null`은 가격 미상이다.
  다른 판형 링크만 관측했다고 별도 인용 번호가 생기지 않는다.
- 자료 탭은 **질문별로 해당 턴 sources를 그룹화**한다. 전체/도서/문서/웹 필터와 종류별 건수,
  빈 상태를 제공한다. 데스크톱 3열·모바일 1열은 내장 HTML의 표시 정책이지 서버 계약은 아니다.
- HTML/Markdown은 안전하게 렌더링하고 링크·이미지 URL 스킴을 검증한다. 외부 링크는 새 창과
  `noopener noreferrer`를 사용한다. 자료 탭과 채팅이 각각 별도 출처 복사본을 소유하지 않게 한다.

### sources 페이로드 예시

```json
{
  "items": [
    {
      "id": 1,
      "title": "예시 도서",
      "url": "https://example.com/books/1",
      "type": "product",
      "card_type": "book",
      "author": "예시 저자",
      "publisher": "예시 출판사",
      "sale_price": 15000,
      "preview": "카드에 표시할 관측 발췌입니다.",
      "other_formats": []
    }
  ],
  "final": false,
  "ts": 1788849000000
}
```

필수 공통 필드는 `id:number`, `title:string`, `url:string`, `type:string`,
`card_type:book|document|link`다. 나머지 주요 관측 필드는 아래와 같이 선택적으로 온다.

| 선택 필드 | 타입 | 표시 |
|---|---|---|
| image_url, author, publisher, preview | string / null | 표지·저자·출판사·발췌 |
| list_price, sale_price, rating, review_count | number / null | 정가·판매가·평점·리뷰 수 |
| goods_no | string / number / null | 상품 식별 참고. 출처 id를 대체하지 않음 |
| pub_date, published_at, last_updated, checked_at | string / null | 출간/게시/갱신/확인 시각. 값이 있을 때만 표시 |
| author_no, kind | string / null | 저자 식별·사이트 분류 참고. kind로 카드 템플릿을 재판정하지 않음 |
| is_book, is_ebook | boolean / null | 관측 속성 참고. 렌더링 선택은 card_type 사용 |
| sale_index, page_count, rank | number / null | 판매지수·쪽수·순위. 관측됐을 때만 제공 |
| other_formats | 배열 / null | `{format?:string|null,url?:string|null,sale_price?:number|null}` 목록 |

키 생략과 null은 모두 표시할 값을 제공하지 않는 경우다. `0`과 `false`를 결측으로 지우지 않는다.
가격의 표시 여부는 `typeof sale_price === "number"`로 확인한다. 내부 snippet은 받지 않는다.

## 6. 본문과 조사 과정 분리

실시간 역할은 `content` 이벤트가 명시한다. **round 번호·thinking·첫 sources로 답변 시작을 추측하지 않는다.**

| phase | 의미 | 프론트 처리 |
|---|---|---|
| `provisional` | 해당 round의 첫 텍스트가 이어짐. 이후 추가 조사로 이어질 수 있음 | 작성 영역에 delta를 즉시 표시. 최종 확정까지 숨기거나 버퍼링하지 않음 |
| `narration` | 같은 round 뒤에 실제 도구 호출이 관측됨 | 이미 표시한 해당 round 텍스트를 조사 설명으로 재분류. 복제/재누적하지 않음 |
| `answer` | 최종 본문 교정이 끝나 구간이 확정됨 | 동봉된 answer_start/round_starts/offset_unit으로 조사와 답변 분리 |

provisional/narration은 `{phase,round}`, answer는 여기에 정본 경계 3필드를 더한다. 모든 이벤트에 ts가 있다.
round는 0부터 시작하지만, 텍스트 없는 도구 라운드가 있어 처음 받은 delta가 round 1 이상이어도 정상이다.
answer는 정본 delta 반영 후, meta와 저장 대기 전에 온다. **생성 성공이나 스트림 종료를 뜻하지 않는다.**
실패한 본문도 이 이벤트로 배치하며, 성공/실패는 `done.status`로 판단한다.
마지막 라운드에 텍스트가 없으면 answer_start가 본문 끝과 같아 최종 답변 구간이 비어 있을 수 있다.

AUTO 모델은 텍스트를 쓰다가 도구를 더 호출할 수 있다. 따라서 첫 토큰의 최종성을 미리 약속하지 않는다.
이를 위해 새 모델 호출·응답 버퍼링·문구 분류를 추가하지 않았다. content는 텍스트를 싣지 않고 역할만 바꾼다.
reset이 오면 앞의 잠정 구간을 무효화하고 정본 delta와 뒤의 content.answer를 적용한다.
content를 보내지 않는 구 서버/저장 이력은 아래 done.process 경계로 복원할 수 있다.

`done.text`는 **조사 경과를 포함한 전체 정본**이다. 답변만 남긴 문자열로 바꾸지 않았다.
`process.answer_start` 뒤가 최종 답변이고 앞은 조사 경과다. `round_starts`와 각 스텝의 `round`로
`r번 텍스트 → r번 도구 단계 → 다음 라운드 텍스트` 순서로 복원한다.

```js
const points = Array.from(done.text);
const narration = points.slice(0, done.process.answer_start).join("");
const answer = points.slice(done.process.answer_start).join("");
```

`offset_unit`은 `unicode_codepoint`다. JS의 `text.slice(answer_start)`를 직접 쓰면 이모지 앞뒤에서
잘못 자를 수 있다. `answer_at_ms`는 답이 시작되기까지, `elapsed_ms`는 메타 처리 후 스냅샷 저장
직전까지 시간이다. 전송/DB 저장까지 포함한 브라우저 체감 총시간과 같지 않다.

### 본문 상태를 관리할 때

1. 현재 턴에 text 버퍼, round별 텍스트 블록/역할, 출처 목록, refs 맵, 과정 단계, meta를 둔다.
2. delta는 버퍼에 한 번만 누적한다. provisional부터 읽을 수 있게 표시한다.
3. narration은 해당 round의 기존 블록을 조사 영역으로 옮긴다. 버퍼에는 다시 넣지 않는다.
4. answer는 전체 버퍼에 대한 정본 오프셋을 적용한다. 메타를 기다리지 않고 답변 영역을 확정한다.
5. reset은 text·본문 인용 캐시·round 역할/경계를 초기화한다. 이후 round 없는 delta도 수용한다.
6. done은 text/sources/process/meta/status 등 최종값으로 교체한다. 마지막 delta처럼 append하지 않는다.

round_starts는 0부터 시작하는 단조 비감소 배열이고 마지막 값은 answer_start다.
같은 오프셋이 반복되는 빈 라운드는 정상이다. 표나 Markdown 모양을 보고 이 경계를 다시 추정하지 않는다.
중단 시 정본 경계가 아직 없다면 마지막으로 받은 역할을 보존한다. 특히 narration을 받은 직후에는
조사 텍스트 뒤의 최종 답이 비어 있을 수 있다. 마지막 조사 문단을 임의로 답변으로 올리지 않는다.

## 7. 최소 스트림 소비 예제

아래는 이벤트 처리 연결 예제다. 렌더 함수는 앱에서 구현하며, 바로 아래 리더를 함께 사용하면
별도 파일 import 없이 POST SSE를 읽을 수 있다. 기존 SSE 라이브러리를 사용해도 계약은 같다.
baseUrl/message/sessionId/serviceCookies/useRbti는 앱에서 주입한다.

```js
const controller = new AbortController();
const requestedSessionId = sessionId || crypto.randomUUID();
const state = { text: "", sources: [], meta: { recommendations: [], follow_ups: [] } };
let receivedDone = false;
try {
  const response = await fetch(`${baseUrl}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream", "x-api-key": serviceCookies },
    body: JSON.stringify({ message, session_id: requestedSessionId, use_rbti: useRbti }),
    signal: controller.signal,
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  if (!response.headers.get("content-type")?.includes("text/event-stream")) {
    throw new Error("SSE 응답이 아닙니다");
  }
  await readEventStream(response, (event, data) => {
    switch (event) {
      case "delta":
        state.text += data.text;
        appendTextBlock(data.text, data.round); // round가 없어도 표시
        break;
      case "content": updateContentRole(data); break; // round별 기존 블록 역할만 갱신(§6)
      case "reset": state.text = ""; resetTextLayout(); break;
      case "sources": state.sources = data.items; break;
      case "meta":
        state.meta = {
          recommendations: data.recommendations ?? [],
          follow_ups: data.follow_ups ?? [],
          ...(data.session_title ? { session_title: data.session_title } : {}),
        };
        break;
      case "status": renderProgress(data); break;
      case "error": showError(data.message); break;
      case "done":
        Object.assign(state, data);
        replaceTurnSnapshot(data); // process 경계도 적용. 본문을 append하지 않음
        receivedDone = true;
        break;
      default: return;
    }
    scheduleRender(state); // 프레임 단위 batching, 매 토큰 전체 DOM 재생성 금지
    return !receivedDone; // done 이후 서버 EOF를 기다리지 않고 리더 종료
  });
  if (!receivedDone) showIncomplete("연결이 종료되어 답변이 완성되지 않았습니다");
} catch (error) {
  if (!receivedDone) showIncomplete(controller.signal.aborted ? "사용자가 중지했습니다" : String(error));
}
```

`appendTextBlock/updateContentRole/resetTextLayout/replaceTurnSnapshot/renderProgress/scheduleRender/`
`showError/showIncomplete`는 앱 구현 함수다. 데이터 누적과 DOM 렌더를 분리하고 동일 스냅샷을
매번 새 DOM으로 만들지 않는다. 렌더 함수의 역할은 §3~6의 처리 규칙을 따른다.
새 대화로 이동할 때는 controller.abort()와 요청 세대 확인을 함께 적용한다(§8).
실서비스에서는 위 필드 타입에 대응하는 런타임 검증도 적용한다.

### 이 문서와 함께 사용할 수 있는 스트림 리더

내장 공유 리더와 같은 로직이다. UTF-8 문자·CRLF·여러 data 줄·청크에 걸친 프레임을 처리한다.
현재 서버가 보내는 JSON SSE용이며, 손상된 JSON/미완성 프레임은 호출자의 오류 경로로 보낸다.
onEvent는 동기 콜백이다. 그 안에서 비동기 저장을 await하지 말고 별도 큐로 처리한다.

```js
function parseEvent(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  const data = dataLines.join("\n");
  if (!dataLines.length) return null;
  return { event, data: data ? JSON.parse(data) : {} };
}

async function readEventStream(response, onEvent) {
  if (!response.ok || !response.body) throw new Error("서버 응답 오류: " + response.status);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const dispatch = () => {
    let boundary;
    while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
      const block = buffer.slice(0, boundary.index).replace(/\r\n/g, "\n");
      buffer = buffer.slice(boundary.index + boundary[0].length);
      const frame = parseEvent(block);
      if (frame && onEvent(frame.event, frame.data) === false) return false;
    }
    return true;
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (!dispatch()) return;
    }
    buffer += decoder.decode();
    if (!dispatch()) return;
    if (buffer.split(/\r?\n/).some((line) => line.trim() && !line.startsWith(":"))) {
      throw new Error("완료되지 않은 SSE 프레임");
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
```

## 8. 완료·실패·중단·히스토리

### done 필드

아래 필드는 신규 done에 모두 존재한다. 선택 session_title은 meta 내부에만 있다.

| 필드 | 타입 | 의미 |
|---|---|---|
| session_id | string | 다음 턴에 보낼 세션 ID. 세션 준비 전 실패하면 빈 문자열일 수 있음 |
| turn_id | string / null | 피드백·클릭·복원의 턴 ID. 준비 전 실패하면 null일 수 있음 |
| text | string | 조사 설명을 포함한 전체 정본. 답변만 추출하려면 process.answer_start 사용 |
| sources | 출처 객체 배열 | 최종 인용 출처. 빈 배열 가능 |
| cited_ids | number[] | 최종 인용 번호 목록 |
| process | 객체, 아래 표 | 과정·시간·본문 경계 |
| meta | 객체 | recommendations 배열, follow_ups 배열, session_title?:string |
| status | completed / failed | 이 스트림의 최종 결과. interrupted/unknown은 히스토리에서 사용 |
| error | null / {code:string,message:string} | 정상은 null, 실패는 구조화된 오류 |
| rbti_applied | string / null | 실제 적용된 독서 성향 코드. 없으면 배지 숨김 |
| history_saved | boolean | 정확한 턴 스냅샷 저장 성공 여부 |
| ts | number | 이 SSE 프레임 생성 시각, epoch 밀리초. 히스토리 턴 필드가 아님 |

| process 필드 | 타입 | 의미 |
|---|---|---|
| elapsed_ms | number | 턴 시작부터 메타 처리 후 스냅샷 저장 직전까지의 밀리초 |
| answer_at_ms | number | 마지막 답 라운드의 첫 본문까지의 밀리초. 빈 답/대체 본문은 본문 마감 시각 |
| sources_reviewed | number | 검토한 고유 자료 수. 인용 카드 수와 다를 수 있음 |
| answer_start | number | 전체 text에서 답 구간 시작 코드포인트 오프셋 |
| round_starts | number[] | round별 시작 오프셋. 마지막 값은 answer_start |
| offset_unit | unicode_codepoint | JS UTF-16 인덱스가 아님 |
| steps | 과정 단계 배열 | `{round:number,stage:string,detail:string,step_id?:string,state?:running/completed/failed,result_count?:number,sources?:[{url:string,title:string}]}` |

steps에는 같은 step_id의 시작과 결과 기록이 함께 있을 수 있다. 라이브와 같은 id별 갱신 규칙으로
그린다. round별 텍스트 → 해당 round의 도구 단계 → 다음 round 텍스트 순서로 복원한다.

### done 페이로드 예시

앞의 조사 설명 `자료를 확인할게요.`는 10코드포인트다. answer_start=10 뒤의 줄바꿈부터 답 구간이다.

```json
{
  "session_id": "example-session",
  "turn_id": "example-turn",
  "text": "자료를 확인할게요.\n\n도서 정보입니다.[1]",
  "sources": [
    {"id": 1, "title": "예시 도서", "url": "https://example.com/books/1", "type": "product", "card_type": "book"}
  ],
  "cited_ids": [1],
  "process": {
    "elapsed_ms": 2400,
    "answer_at_ms": 1800,
    "sources_reviewed": 1,
    "answer_start": 10,
    "round_starts": [0, 10],
    "offset_unit": "unicode_codepoint",
    "steps": [
      {"round": 0, "stage": "searching", "detail": "예시 도서", "step_id": "step-1", "state": "running"},
      {"round": 0, "stage": "found", "detail": "1건 찾았어요", "step_id": "step-1", "state": "completed", "result_count": 1, "sources": [{"url": "https://example.com/books/1", "title": "예시 도서"}]}
    ]
  },
  "meta": {
    "recommendations": [],
    "follow_ups": ["다른 판형도 있나요?"],
    "session_title": "예시 도서 정보"
  },
  "status": "completed",
  "error": null,
  "rbti_applied": null,
  "history_saved": true,
  "ts": 1788849002400
}
```

### 종료 상태별 처리

- `done.status:completed`: 본문 정상 마감. `done.error:null`.
- `done.status:failed`: 부분 본문을 보존하고 오류 상태 표시. `error:{code,message}`.
- 사용자 중지/네트워크 단절: done이 없을 수 있다. 로컬 중지와 네트워크 오류를 UI에서 구분한다.
  서버는 둘을 확실하게 구별할 수 없어 best-effort로 `interrupted`를 저장한다.
- 프로세스 강제 종료 등으로 스냅샷을 못 남기면 복원 상태는 `unknown`일 수 있다.
- `history_saved:true`: `GET /chat/sessions/{session_id}`의 해당 턴은 완료 당시 본문·출처·메타·
  과정·종료 상태를 복원한다. 그 뒤 가격 관측이 바뀌어도 이전 턴을 최신 가격으로 덮지 않는다.
- `history_saved:false`: 정확한 스냅샷 저장 실패 또는 구 턴이다. 정확한 메타/가격/상태 복원을
  약속하지 않는다. 저장 실패만으로 이미 완성된 답변을 지우지 않는다.
- 로컬 중지 상태를 서버에 `completed`로 임의 기록하지 않는다. 자동 재전송도 하지 않는다.
  현재 계약은 요청 멱등 키가 없으므로 재시도하면 새 생성/턴이 될 수 있다.
- 새 채팅/다른 대화로 이동할 때 이전 스트림을 abort하고 **요청 세대 키**를 비교한다.
  이전 요청의 늦은 이벤트·finally가 새 대화에 본문/출처를 붙이지 못하게 한다.

### 히스토리 응답

`GET /chat/sessions/{session_id}`는 `{session_id:string,title:string|null,turns:[...]}`다.
각 턴은 ts를 제외한 위 스냅샷 필드와 다음 필드를 제공한다.

| 추가/차이 필드 | 타입 | 의미 |
|---|---|---|
| turn_id | string | 저장된 턴의 식별자 |
| user_text | string | 해당 턴의 질문. 자료 탭의 그룹 제목으로 사용 가능 |
| started_at | number / null | 질문 시각, **epoch 초**. JS Date에는 1000을 곱함 |
| feedback | null / {rating:up/down/null,comment:string/null} | 현재 피드백 |
| status | completed / failed / interrupted / unknown | 정상·실패·중단·정확한 종료 정보가 없는 구 턴 |

시간 단위를 혼동하지 않는다: `ts`는 epoch **밀리초**, `started_at`은 epoch **초**,
process의 `*_ms`는 epoch가 아닌 **소요 시간**이다.
복원에는 저장된 process 경계를 그대로 적용한다. history_saved=false인 구 턴에 정확한 당시
추천·가격·종료 상태가 모두 남아 있다고 가정하지 않는다.

## 9. 연관 REST API

| method/path | 목적 |
|---|---|
| `GET /chat/sessions` | 내 세션 목록 |
| `GET /chat/sessions/{session_id}` | 질문과 턴 스냅샷 복원(읽음 처리 포함) |
| `PUT /chat/sessions/{session_id}/turns/{turn_id}/feedback` | `{rating:"up"\|"down"\|"none",comment?}` (`none`은 철회) |
| `POST /chat/sessions/{session_id}/turns/{turn_id}/clicks` | `{url,source_id?,source_type?,label?}` |

클릭의 `source_type`은 `source.type`이지 `card_type`이 아니다. URL은 실제로 누른 판형/카드 URL이다.
성공은 204, 본문 없음. 클릭은 멱등이 아니므로 무조건 재시도하지 않는다. done 이전 클릭은
로컬 큐에 두고 최종 `session_id/turn_id`가 확정된 뒤 전송한다. 링크 열기를 저장 요청 때문에
지연시키지 않는다. 최종 turn_id가 없으면 가짜 ID로 보내지 않는다.

## 10. 전환 순서와 인수 기준

이벤트 `source` → `sources`는 이름/의미 변경이라 **구 프론트와 무조건 호환되지 않는다**.
외부 연동 배포 시 프론트가 양쪽을 받아 처리하도록 먼저 배포한 뒤 서버를 전환하고, 구 경로를
제거한다. 구 `source`만 한 건씩 모으던 구현을 단순히 이름만 바꾸면 목록 교체를 놓친다.
내장 HTML은 이번 변경과 함께 전환했다. 매트릭스의 개별 `source`는 유지한다.

- [ ] 카드가 관련 `[n]`의 첫 완성 delta 이전에 준비되는가?
- [ ] 새/갱신/삭제된 sources와 최종 done이 중복 없이 반영되는가?
- [ ] refs를 id별로 누적하고 sources는 전체 교체하는가?
- [ ] FAQ·웹·도서 혼합, 검색 0건, 출처 없는 잡담도 렌더링되는가?
- [ ] 번호는 턴별이며 추천 이유는 같은 턴의 id에만 붙는가(턴 간 이유 캐시 없이도 이전 턴 추천 이유가 다시 보이는가)?
- [ ] 이모지·여러 라운드·reset에서도 본문과 조사 과정 경계가 맞는가?
- [ ] provisional부터 글자를 보여주고 narration은 해당 round만 이동하며 answer를 성공 종료로 오인하지 않는가?
- [ ] round/provisional 없는 정본 delta도 수용하고 reset 뒤 경계를 다시 적용하는가?
- [ ] 제목/후속 질문만 갱신할 때 카드가 유지되고 추천 변경은 해당 카드에만 반영되는가?
- [ ] 새로고침 후 본문·카드·이유·종료 상태가 같고 과거 가격이 변하지 않는가?
- [ ] 중지·단절·failed done·새 대화 전환을 정상 완료로 표시하지 않는가?
- [ ] 표지/저자/가격/평점 결측, 숫자 0, 빈 판형 배열, 긴 발췌를 견디는가?
- [ ] 자료 탭 질문 그룹·필터·빈 상태·모바일 1열과 키보드 탐색이 동작하는가?
- [ ] 여러 질문과 필터 전환에서도 질문 순서가 유지되고 숨겼다 다시 보인 카드가 재사용되는가?
- [ ] 단일 세로 스크롤, 사용자가 위로 읽을 때 강제 하단 이동 방지가 유지되는가?
- [ ] 계정 전환 시 세션이 분리되고, started_at(초)·ts(밀리초)·소요 시간이 구분되는가?

이 문서는 제품 화면을 만들 수 있는 계약을 정의한다. 재개 토큰·이벤트 로그 재생·정확히 한 번
클릭 저장·강제 종료 후 완전 복원은 구현한 기능이 아니므로 보장하지 않는다.
