# 크레마AI 질문 이력 조회 API (Yes24 백엔드 → 크레마AI)

회원번호와 기간을 보내면 그 회원이 기간 안에 크레마AI에서 질문한 이력이 있는지 돌려준다.
이벤트 참여 판정용 **서버 간 API**다. 브라우저에서 직접 부르지 않는다(공유 비밀이 노출된다).

## 환경

| 환경 | BASE_URL | 키 |
|---|---|---|
| QA | `https://yes24-agent.dev.griplabs.io` | 공용 키(상용과 같음) |
| 상용 | `https://yes24-agent.griplabs.io` | 공용 키 |

## 요청

```
POST https://yes24-agent.griplabs.io/partner/chat-activity
Content-Type: application/json
x-partner-key: {공유 비밀}

{"user_no": "12345678", "start_date": "2026-10-01", "end_date": "2026-10-31"}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `user_no` | ✅ | 회원번호(userNo). 숫자 1~20자리. 문자열·JSON 숫자 둘 다 받는다 |
| `start_date` | — | `YYYY-MM-DD`. KST 00:00:00부터 포함. **생략하면 처음부터** |
| `end_date` | — | `YYYY-MM-DD`. KST 23:59:59까지 포함. **생략하면 지금까지**. 하루짜리는 `start_date`와 같은 값 |

둘 다 생략하면 "크레마AI에 질문한 적이 한 번이라도 있는가"가 된다. 정의되지 않은 필드를 보내면 422다(예: `start_at`).

```bash
curl -X POST "https://yes24-agent.griplabs.io/partner/chat-activity" \
  -H "Content-Type: application/json" -H "x-partner-key: $PARTNER_KEY" \
  -d '{"user_no":"12345678","start_date":"2026-10-01","end_date":"2026-10-31"}'
```

## 응답 — 200

```json
{"user_no": "12345678", "asked": true, "question_count": 3,
 "first_asked_at": "2026-10-01T10:30:00+09:00", "last_asked_at": "2026-10-31T23:59:00+09:00"}
```

이력이 없으면(크레마AI를 안 써 본 회원 포함 — 404가 아니다):

```json
{"user_no": "12345678", "asked": false, "question_count": 0, "first_asked_at": null, "last_asked_at": null}
```

| 필드 | 설명 |
|---|---|
| `user_no` | 요청한 회원번호 그대로(배치 호출 시 짝 맞추기용) |
| `asked` | **참여 판정은 이 값만 보면 된다.** 기간 내 질문 1건 이상이면 `true` |
| `question_count` | 기간 내 질문 수("N회 이상" 조건이면 이 값으로 판정) |
| `first_asked_at` / `last_asked_at` | 기간 내 첫·마지막 질문 시각(KST, 초 단위). 없으면 `null` |

**질문으로 세는 것:** 회원이 크레마AI 대화창에서 직접 보낸 메시지 1건 = 1회.
대화를 나중에 삭제해도 센다. 검색결과 상단 AI 요약 열람은 세지 않는다.
답변 도중 오류·중단된 질문도 1회로 센다.

## 오류

| 상태 | 언제 | 처리 |
|---|---|---|
| 401 | `x-partner-key` 없음·불일치 | 키 확인 |
| 422 | `user_no` 누락·숫자 아닌 회원번호·날짜 형식 오류(`YYYY-MM-DD` 문자열만)·없는 날짜·`end_date < start_date`·정의되지 않은 필드 | 요청 수정 |
| 503 | 크레마AI DB 장애 | 재시도 — **미참여로 처리하지 말 것** |
| 404 | 크레마AI 서버에 키 미설정(기능 꺼짐) | 크레마AI 측 문의 |

> 판정은 **200 응답에서만** 한다. 오류를 `asked:false`로 간주하면 참여한 회원이 혜택에서 조용히 누락된다.

## 운영 메모

- 공유 비밀은 크레마AI 서버 `.env`의 `PARTNER_API_KEY`(`openssl rand -hex 32`로 생성). 비면 라우트 미등록(404).
- 요청 한도 없음. 회원 1명당 인덱스 조회 1건이다(dev DB 실측 응답 p50 10ms·최대 16ms, 로컬→RDS). 대량 배치(수만 건 이상)는 일정을 미리 공유할 것.
