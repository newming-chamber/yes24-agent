# 크레마AI 상품권 이벤트 연동 (클라이언트 ↔ 크레마AI 서버 ↔ Yes24)

이벤트 기간(**2026-10-16 ~ 2026-11-01, KST, 양 끝 포함**)에 크레마AI에 질문한 회원에게 상품권
(1천원)을 지급한다. RBTI·일문백답과 같은 방식이다. 발급은 클라이언트가 Yes24 API를 직접
호출하고, 크레마AI 서버는 **대상자 확인**과 **결과 기록** 두 API만 제공한다.

## 흐름

1. 클라이언트 → 크레마AI: `POST /event/giftcard/eligibility`
2. `status`가 `eligible`일 때만 클라이언트 → Yes24: `POST /api/event/giftcard/issue`
3. Yes24 → 클라이언트: `ResultCode` 반환 → 코드별 안내(아래 표)
4. 클라이언트 → 크레마AI: `POST /event/giftcard/result`로 받은 `ResultCode` 전달

## 환경과 인증

| 환경 | BASE_URL |
|---|---|
| QA | `https://yes24-agent.dev.griplabs.io` |
| 상용 | `https://yes24-agent.griplabs.io` |

두 API 모두 대화 API와 같은 `x-api-key: {ServiceCookies 값}` 헤더로 회원을 식별한다(본문에
회원번호를 싣지 않는다). 헤더가 없으면 401, 회원 식별이 안 된 키는 403이다.

## 1. 대상자 확인

```
POST {BASE_URL}/event/giftcard/eligibility
x-api-key: {ServiceCookies}
```

본문 없음. 응답:

```json
{"status": "eligible", "question_count": 3}
```

| `status` | 의미 | 안내 문구 |
|---|---|---|
| `eligible` | 기간 내 질문 이력 있음 → 발급 요청 진행 | — |
| `not_eligible` | 기간 내 질문 이력 없음 | 상품권 지급 대상이 아닙니다. |
| `already_issued` | 이미 받음(이전에 `SUCCESS`·`ALREADY_APPLIED` 기록) | 이미 발급 받으셨습니다. |

503은 조회 실패다. "대상 아님"으로 처리하지 말고 재시도를 안내한다.

## 2. Yes24 발급 요청

Yes24 「상품권 발급 API — 암호화 연동 가이드」 v1.3 §8 그대로다. 클라이언트 호출이므로
회원정보는 요청 헤더의 Cookie에 `ServiceCookies`로 실린다.

```
POST /api/event/giftcard/issue
Content-Type: application/json

{"goodsTicNoEncrypted": "{암호화된 상품권번호 — 별도 전달}", "eventNm": "2026_CremaAI_Event"}
```

암호화된 상품권번호는 이 문서에 싣지 않는다. 별도로 전달하며 클라이언트에 고정값으로
넣는다. 암호화 키는 클라이언트에 넣지 않는다.

| `ResultCode` | 안내 |
|---|---|
| `SUCCESS` | 상품권이 지급되었습니다. |
| `ALREADY_APPLIED` | 이미 발급 받으셨습니다. |
| `NOT_VERIFIED` | 본인인증 페이지로 이동(가이드 §10 — PC/모바일 URL이 다르다, 모바일은 `ReturnUrl`) |
| `NOT_LOGGED_IN` | 로그인 안내 |
| `FAIL`·`DUPLICATE`·`EXCEED`·`EXPIRE` | 발급 실패 안내 |

## 3. 결과 기록

```
POST {BASE_URL}/event/giftcard/result
x-api-key: {ServiceCookies}
Content-Type: application/json

{"result_code": "SUCCESS"}
```

`result_code`는 위 8개 값만 받는다. 대소문자가 다르거나 다른 필드를 함께 보내면 422다.
성공 응답은 **204(본문 없음) = 실제로 저장됨**이고, 503이면 저장되지 않은 것이다.
시각은 서버가 기록하므로 보내지 않는다. 시도할 때마다 한 건씩 쌓인다(멱등 아님).

기록은 클라이언트가 전달한 결과를 모은 운영 참고 이력이다. 상품권 잔여 수량은 이 기록으로
계산할 수 없다.
