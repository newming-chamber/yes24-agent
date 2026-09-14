-- session_ui에 삭제 표식 열·인덱스를 더한다(대화 소프트 삭제 — 열의 의미는 session_ui.sql 주석).
-- 신규 DB는 session_ui.sql에 이미 들어 있다. 재적용 안전: 열이 이미 있으면 SELECT 1만 실행된다
-- (MySQL엔 ADD COLUMN IF NOT EXISTS가 없다). 온라인 DDL이라 드레인 창이 필요 없다 —
-- ALGORITHM=INPLACE, LOCK=NONE을 명시해 COPY(테이블 잠금)로 조용히 떨어지면 오류로 멈춘다.
--
-- 적용: mysql -h <RDS> -u <user> -p <db> < scripts/session_ui_deleted_at.sql
-- 순서: 이 DDL → 코드 배포. 새 코드는 열이 없으면 기동 시 verify_schema에서 실패한다.
-- 전제: 파기는 FK CASCADE에 기대므로 fk_chat_turn_session·fk_session_ui_session·
--       fk_turn_feedback_session이 적용돼 있어야 한다(migrate_database.py의 session_integrity 단계).
--       확인: SELECT constraint_name FROM information_schema.referential_constraints
--             WHERE constraint_schema = DATABASE() AND delete_rule = 'CASCADE';
SET @has_deleted_at := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'session_ui' AND column_name = 'deleted_at'
);
SET @ddl := IF(
    @has_deleted_at = 0,
    'ALTER TABLE session_ui ADD COLUMN deleted_at TIMESTAMP(3) NULL, ADD KEY idx_session_ui_deleted (deleted_at), ALGORITHM=INPLACE, LOCK=NONE',
    'SELECT 1'
);
PREPARE session_ui_ddl FROM @ddl;
EXECUTE session_ui_ddl;
DEALLOCATE PREPARE session_ui_ddl;
