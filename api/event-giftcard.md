# 크레마AI 상품권 이벤트 연동 (클라이언트 ↔ 크레마AI 서버 ↔ Yes24)

크레마AI에 질문한 회원에게 상품권(1천원)을 **지급 구간마다 1회** 준다. 11월 상시 이벤트는
**2026-11-02(월) ~ 2026-11-27(금)**, 월~금에 질문하면 **주 1회** 지급하고 주말은 지급하지 않는다
(1개월 단위 연장). RBTI·일문백답과 같은 방식으로 발급은 클라이언트가 Yes24 API를 직접 호출한다.

**진행 여부·발급 값은 전부 서버가 판단한다.** 클라이언트는 배너 노출 날짜, `eventNm`, 암호화
상품권번호를 자기 설정(env·하드코딩)에 두지 않는다 — 구간을 늘리거나 바꿔도 클라이언트 배포가
필요 없다. **암호문은 서버가 만들어 내려준다** — 클라이언트는 암호화 키(ENCKEY·VECTOR)를 갖지 않고
암호화도 하지 않는다.

## 흐름

1. 화면 진입 시 클라이언트 → 크레마AI: `GET /event/giftcard` → `active`가 `true`일 때만 배너 노출
2. 참여 시 클라이언트 → 크레마AI: `POST /event/giftcard/eligibility`
3. `status`가 `eligible`일 때만 클라이언트 → Yes24: `POST /api/event/giftcard/issue`
   — 바디 값은 2의 응답(`event_nm`·`goods_tic_no_encrypted`)을 그대로 쓴다
4. Yes24 → 클라이언트: `ResultCode` 반환 → 코드별 안내(아래 표)
5. 클라이언트 → 크레마AI: `POST /event/giftcard/result`로 받은 `ResultCode` 전달

## 환경과 인증

| 환경 | BASE_URL |
|---|---|
| QA | `https://yes24-agent.dev.griplabs.io` |
| 상용 | `https://yes24-agent.griplabs.io` |

`GET /event/giftcard`는 **인증 없음**이다(비로그인 화면도 배너를 판단한다). 나머지 두 API는 대화
API와 같은 `x-api-key: {ServiceCookies 값}` 헤더로 회원을 식별한다(본문에 회원번호를 싣지 않는다).
헤더가 없으면 401, 회원 식별이 안 된 키는 403이다.

모든 날짜는 **KST 날짜, 양 끝 포함**이다. 서버가 KST 자정 기준으로 판단하므로 클라이언트는 시각
비교를 하지 않는다.

## 1. 이벤트 진행 여부 (배너)

```
GET {BASE_URL}/event/giftcard
```

```json
{"active": true, "starts_on": "2026-11-02", "ends_on": "2026-11-06"}
```

| 필드 | 의미 |
|---|---|
| `active` | 오늘이 지급 구간 안인가 — `false`면 배너를 숨긴다(주말·기간 밖) |
| `starts_on`·`ends_on` | 현재 구간(이번 주 월~금). `active`가 `false`면 `null` |

응답은 캐시하지 않는다(`Cache-Control: no-store`) — 자정에 값이 바뀐다. 진입 시 1회 호출이면
충분하다. 503은 조회 실패다 — "이벤트 없음"으로 단정하지 말고 배너를 숨긴 채 다음 진입에 다시 묻는다.

## 2. 대상자 확인

```
POST {BASE_URL}/event/giftcard/eligibility
x-api-key: {ServiceCookies}
```

본문 없음. 응답:

```json
{
  "status": "eligible",
  "question_count": 3,
  "period_ends_on": "2026-11-06",
  "event_nm": "{eventNm}",
  "goods_tic_no_encrypted": "{서버가 암호화한 상품권번호, 표준 Base64}"
}
```

| `status` | 의미 | 안내 문구 |
|---|---|---|
| `eligible` | 이번 구간에 질문 이력 있음 → 발급 요청 진행 | — |
| `not_eligible` | 이번 구간에 질문 이력 없음 | 상품권 지급 대상이 아닙니다. 크레마AI에 질문한 뒤 다시 시도해 주세요. |
| `already_issued` | **이번 주 상품권을 이미 받음**(현재 구간에 `SUCCESS`·`ALREADY_APPLIED` 기록) | 이번 주 상품권은 이미 발급 받으셨습니다. 다음 주에 다시 참여해 주세요. |
| `closed` | 지금은 지급 구간이 아님(주말·이벤트 기간 밖) | 지금은 상품권 지급 기간이 아닙니다. 평일(월~금)에 참여해 주세요. |

- `question_count` — 현재 구간 안에 보낸 질문 수(`closed`면 0).
- `period_ends_on` — 현재 구간 종료일(`closed`면 `null`). "~11/6(금)까지" 같은 안내에 쓴다.
- `event_nm`·`goods_tic_no_encrypted` — **`eligible`일 때만** 값이 있고 그 외 상태는 `null`이다.
  암호문은 서버가 가이드 v1.3 §3~6 방식(AES-256-CBC·PKCS7·표준 Base64)으로 만든 값이다. 다음 단계
  Yes24 발급 요청 바디에 가공 없이 그대로 넣는다. **상품권번호는 주마다 바뀌므로**(eventNm은 같다)
  저장해 두고 다시 쓰지 말고 **매번 이 응답의 값**을 쓴다.

`already_issued`는 "이번 주 상품권을 이미 받음"이다 — 지난 구간은 보지 않으므로 지난주에 받은 회원도
이번 주에 질문하면 다시 `eligible`이고, 그때는 이번 주 상품권번호가 내려간다.

503은 조회 실패(또는 서버의 암호화 설정 누락으로 발급 불가)다. "대상 아님"으로 처리하지 말고
재시도를 안내한다.

## 3. Yes24 발급 요청

Yes24 「상품권 발급 API — 암호화 연동 가이드」 v1.3 §8 그대로다. 클라이언트 호출이므로
회원정보는 요청 헤더의 Cookie에 `ServiceCookies`로 실린다.

```
POST /api/event/giftcard/issue
Content-Type: application/json

{"goodsTicNoEncrypted": "{eligibility.goods_tic_no_encrypted}", "eventNm": "{eligibility.event_nm}"}
```

클라이언트 env의 암호문·`eventNm`은 폐기한다. 암호화 키는 클라이언트에 넣지 않는다(서버 .env에만 있다).

| `ResultCode` | 안내 |
|---|---|
| `SUCCESS` | 상품권이 지급되었습니다. |
| `ALREADY_APPLIED` | 이미 발급 받으셨습니다. |
| `NOT_VERIFIED` | 본인인증 페이지로 이동(가이드 §10 — PC/모바일 URL이 다르다, 모바일은 `ReturnUrl`) |
| `NOT_LOGGED_IN` | 로그인 안내 |
| `FAIL`·`DUPLICATE`·`EXCEED`·`EXPIRE` | 발급 실패 안내 |

## 4. 결과 기록

```
POST {BASE_URL}/event/giftcard/result
x-api-key: {ServiceCookies}
Content-Type: application/json

{"result_code": "SUCCESS"}
```

`result_code`는 위 8개 값만 받는다. 대소문자가 다르거나 다른 필드를 함께 보내면 422다.
성공 응답은 **204(본문 없음) = 실제로 저장됨**이고, 503이면 저장되지 않은 것이다.
시각은 서버가 기록하므로 보내지 않는다. 시도할 때마다 한 건씩 쌓인다(멱등 아님).

기록은 **이미 시작한 구간 중 가장 최근 구간**에 붙는다 — 금요일 23:59에 대상자 확인을 하고 토요일
00:00에 결과가 도착해도 그 주 구간으로 기록된다. 시작한 구간이 하나도 없으면 409(기록하지 않음)다.

기록은 클라이언트가 전달한 결과를 모은 운영 참고 이력이다. 상품권 잔여 수량은 이 기록으로
계산할 수 없다.

## 기간 운영 (크레마AI 운영자)

구간은 세션 DB의 `giftcard_period` 테이블이 정한다(DDL `scripts/giftcard_period.sql`). **한 행 =
회원이 한 번 받을 수 있는 구간**이고, 판정 코드에는 요일·기간 판정이 없다 — "주 1회·주말 미지급"은
주마다 월~금 한 행으로 표현한다. Yes24 상품권번호는 **주마다 다르고 eventNm은 같다** — 행마다 그 주
번호를 넣는다. 연장은 행 추가뿐이고 서버·클라이언트 배포가 필요 없다.

**이행 순서**(최초 1회): ① `giftcard_period.sql` 적용 → ② 진행 중인 10월 이벤트 행 INSERT와 기존 기록
백필 UPDATE(SQL 파일 하단 예시) → ③ 코드 배포 → ④ 백필 UPDATE 한 번 더. ④는 ②~③ 사이에 옛 코드가
남긴 기록을 붙인다 — `WHERE period_id IS NULL`이라 다시 돌려도 같다(멱등). ②를 빼면 배포 순간부터
대상자 확인이 전부 `closed`다.

**관리는 어드민 화면에서 한다**(통계 → '이벤트 참여' 상단 "이벤트 기간"). 운영자(editor) 이상이
추가·수정·삭제하고, 뷰어는 표만 본다. 모든 변경은 감사 기록에 남는다(상품권번호는 뒤 4자리만).

- **추가** — 한 번에 한 주(구간 하나). 이벤트명(= Yes24 `eventNm`, 최대 64자)·시작일·종료일·
  상품권번호(Yes24가 준 그 주의 **원문 정수**). 폼이 마지막 기간 다음 주 월~금과 직전 이벤트명을 미리
  채우므로 보통은 그 주 번호만 넣고 저장한다. 기존 기간과 날짜가 겹치면 409로 거부하고 겹친 기간을
  알려 준다.
- **수정** — 시작 전 기간은 모든 항목, 진행 중 기간은 종료일(오늘 이후로만 연장·단축)과 상품권번호만
  바꿀 수 있다. 종료된 기간은 바꿀 수 없다.
- **삭제** — 시작 전 기간만. 진행 중 기간을 끝내려면 종료일을 줄인다.
- 상품권번호는 화면·API 응답 어디에도 원문으로 나오지 않는다(뒤 4자리 마스킹). 암호화 키·IV 원천은
  서버 `.env`의 `GIFTCARD_ENC_KEY`·`GIFTCARD_ENC_VECTOR`이고, 비어 있으면 대상자 확인의 `eligible`이
  503이 된다.
- 날짜는 KST, 양 끝 포함. 행이 없는 날은 전부 `closed`이고, 테이블이 비면 이벤트 종료다.

SQL로 직접 넣을 때(어드민의 겹침 검사를 거치지 않는다 — 겹치게 넣지 않는다. 겹치면 서버는 오늘을
포함하는 행 중 `starts_on`이 가장 늦은 1개를 쓴다):

```sql
INSERT INTO giftcard_period (event_key, starts_on, ends_on, goods_tic_no) VALUES
  ('{eventNm}', '2026-11-02', '2026-11-06', {1주차 상품권번호}),
  ('{eventNm}', '2026-11-09', '2026-11-13', {2주차 상품권번호}),
  ('{eventNm}', '2026-11-16', '2026-11-20', {3주차 상품권번호}),
  ('{eventNm}', '2026-11-23', '2026-11-27', {4주차 상품권번호});
```

어드민 통계 '이벤트 참여'는 기간별 시도·시도 회원·받은 회원을, 회원 목록은 가장 최근 시작 기간의
참여 여부를 보여 준다.
