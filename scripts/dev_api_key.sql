-- 프론트 로컬 개발용 API 키 — **dev 환경 전용**.
--
-- 왜 필요한가: `x-api-key`는 Yes24 service_cookie이고, 서버가 그 값으로 회원 API를 조회해
-- userNo를 얻는다. 조회에 실패하는 키는 403으로 끊긴다(2026-09-02 — 임의 문자열이 통해
-- LLM 비용이 새던 것을 막았다). 그래서 프론트 개발자가 자기 Yes24 쿠키를 꺼내 오지 않는 한
-- 로컬에서 API를 부를 수 없다.
--
-- 이 행은 `user_no`를 **미리 채워** 두므로 `_register`(신규 키 조회) 경로를 타지 않는다.
-- `user_cached_at`을 먼 미래로 두는 이유: 만료되면 매 요청이 Yes24 회원 API를 헛되이
-- 호출한다(응답은 실패하고 저장된 user_no로 계속 동작하지만, 요청마다 왕복이 붙는다).
-- 값이 2038년인 이유는 컬럼이 TIMESTAMP라서다 — 그 이후 값은 조용히 '0000-00-00'이 되어
-- 오히려 항상 만료 상태가 된다(2026-09-02 실측하고 고친 값).
--
-- user_no는 실 회원과 겹치지 않게 9자리 대역을 쓴다. 대화·피드백은 이 user_no 밑에만
-- 쌓이므로 실사용자 데이터와 섞이지 않는다.
--
-- 적용: mysql -h <RDS> -u <user> -p <db> < scripts/dev_api_key.sql
-- 폐기: DELETE FROM users WHERE api_key = 'dev-frontend-local';
INSERT INTO users (api_key, user_no, user_login_id, rate_limit_rpm, rate_limit_rpd, user_cached_at)
VALUES ('dev-frontend-local', 990000001, 'dev_frontend', 120, 5000, '2038-01-01 00:00:00')
ON DUPLICATE KEY UPDATE
    user_no = VALUES(user_no),
    user_login_id = VALUES(user_login_id),
    rate_limit_rpm = VALUES(rate_limit_rpm),
    rate_limit_rpd = VALUES(rate_limit_rpd),
    user_cached_at = VALUES(user_cached_at),
    is_active = 1;
