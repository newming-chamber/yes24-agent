-- 어드민 분석·통계 탭의 chat_turn 집계를 인덱스만으로 끝낸다(2026-10-29). 기간 조건이 started_at
-- 범위인데 started_at 선두 인덱스가 없어 매 집계가 JSON 본문(process·sources)이 든 클러스터 행을
-- 통째로 읽었다(운영 1.6GB — 7일 집계 콜드 27초, 10-28 어드민 504). 가상 열 2개(저장 안 함)와
-- 집계가 읽는 열 전부를 담은 덮는 인덱스 1개를 더한다. 신규 DB는 chat_turn.sql에 이미 들어 있다.
--   elapsed_ms   = 뷰 chat_turns가 계산하던 식 그대로(형식이 틀린 값은 NULL — 오류로 쓰기를 막지 않는다)
--   source_count = JSON_LENGTH(sources)(통계 '링크' — sources는 NOT NULL 배열)
--
-- 재적용 안전: 열·인덱스가 있으면 SELECT 1만 실행된다(MySQL엔 ADD ... IF NOT EXISTS가 없다).
-- ① 열 추가는 메타데이터만 바꾸는 ALGORITHM=INSTANT, ② 인덱스는 온라인 ALGORITHM=INPLACE, LOCK=NONE
-- (읽기·쓰기 계속 — 빌드 중 쓰기는 온라인 로그에 쌓였다 끝에 반영)를 명시한다. 불가하면 COPY로
-- 조용히 떨어지지 않고 오류로 멈춘다. 두 ALTER 모두 시작·끝에 배타 메타데이터 잠금을 잠깐 잡는다 —
-- 긴 쿼리·트랜잭션이 테이블을 잡고 있으면 그 뒤로 대화 쓰기가 줄을 서므로 lock_wait_timeout을
-- 짧게 두고, 시간 초과면 잠시 뒤 이 파일을 다시 실행한다(이미 된 단계는 건너뛴다).
--
-- 적용: mysql -h <RDS> -u <user> -p <db> < scripts/chat_turn_admin_covering.sql
-- 순서: 이 DDL → operational_views.sql 재적용(chat_turns.elapsed_ms가 가상 열을 읽게 — 재적용 전엔
-- 뷰가 옛 식으로 계속 동작하되 느리다; verify_schema는 뷰 정의를 보지 않는다) → 코드 배포(새 코드는
-- source_count 열이 없으면 기동 시 verify_schema에서 실패한다).
SET SESSION lock_wait_timeout = 10;

SET @has_columns := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'chat_turn'
      AND column_name IN ('elapsed_ms', 'source_count')
);
-- 열은 한 ALTER로 같이 생기므로 하나만 있는 상태는 이 스크립트가 만들지 않은 손 수정이다 — 추측해
-- 메우지 않고 멈춘다. SIGNAL은 PREPARE가 안 되고(1295) 저장 프로시저는 CREATE ROUTINE 권한을 정상
-- 경로에도 요구하므로, 없는 표 이름에 메시지를 실어 오류("Table '...' doesn't exist")로 멈춘다.
SET @ddl := CASE @has_columns
    WHEN 0 THEN 'ALTER TABLE chat_turn ADD COLUMN elapsed_ms BIGINT UNSIGNED AS (JSON_VALUE(process, ''$.elapsed_ms'' RETURNING UNSIGNED)) VIRTUAL, ADD COLUMN source_count INT AS (JSON_LENGTH(sources)) VIRTUAL, ALGORITHM=INSTANT'
    WHEN 2 THEN 'SELECT 1'
    ELSE 'SELECT 1 FROM `중단: chat_turn에 elapsed_ms·source_count 중 하나만 있음 - 정의를 손으로 확인`'
END;
PREPARE chat_turn_columns_ddl FROM @ddl;
EXECUTE chat_turn_columns_ddl;
DEALLOCATE PREPARE chat_turn_columns_ddl;

SET @has_index := (
    SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'chat_turn' AND index_name = 'idx_chat_turn_admin'
);
SET @ddl := IF(
    @has_index = 0,
    'ALTER TABLE chat_turn ADD INDEX idx_chat_turn_admin (started_at, app_name, user_id, session_id, status, completed_at, rbti_applied, elapsed_ms, source_count), ALGORITHM=INPLACE, LOCK=NONE',
    'SELECT 1'
);
PREPARE chat_turn_index_ddl FROM @ddl;
EXECUTE chat_turn_index_ddl;
DEALLOCATE PREPARE chat_turn_index_ddl;
