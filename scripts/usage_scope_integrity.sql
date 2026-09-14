-- 기존 usage_log의 앱 범위 보호. 신규 테이블은 usage_log.sql에 같은 제약이 있다.
-- 적용 순서:
-- 1. 같은 DB를 쓰는 구 writer를 교체/중지하고 최종 백업을 확보한다.
-- 2. sessions와 비어 있지 않은 usage_log의 app_name이 settings.app_name 하나인지 확인한다.
-- 3. 빈 행의 원본을 보존한 뒤 트랜잭션에서 settings.app_name을 바인딩해 앱 범위만 복구한다.
--    UPDATE usage_log SET app_name=%s WHERE CHAR_LENGTH(TRIM(app_name))=0
-- 4. 변경 행 수가 사전 빈 행 수와 같고 빈 행이 0인지 확인한 뒤 커밋한다.
-- 5. 아래 제약을 적용하고 information_schema에서 ENFORCED=YES를 확인한다.
-- 사용자·세션·턴은 원본 식별자로 입증되는 경우만 별도 복구한다. 시간 근접으로 추정하지 않는다.
-- 길이를 비교하므로 BINARY가 필요 없다. 빈 문자열과 ASCII 공백만으로 된 값이 거절된다.
ALTER TABLE usage_log
    ADD CONSTRAINT ck_usage_log_app_name CHECK (CHAR_LENGTH(TRIM(app_name)) > 0);
