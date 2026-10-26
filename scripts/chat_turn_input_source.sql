-- chat_turn에 질문 유입 경로 열을 더한다(2026-10-21). 열의 의미는 chat_turn.sql 주석과
-- main.py ChatRequest.input_source(값의 정본, 422로 거른다)에 있다. 신규 DB는 chat_turn.sql에
-- 이미 들어 있다. 재적용 안전: 열이 이미 있으면 SELECT 1만 실행된다(session_ui_deleted_at.sql
-- 관례 — MySQL엔 ADD COLUMN IF NOT EXISTS가 없다). NULL 허용이라 기존 행·구 클라이언트·
-- 백필은 그대로다. 온라인 DDL — ALGORITHM=INPLACE, LOCK=NONE을 명시해 COPY로 조용히
-- 떨어지면 오류로 멈춘다. CHECK는 두지 않는다(값 추가가 앱 배포만으로 끝나게).
--
-- 적용: mysql -h <RDS> -u <user> -p <db> < scripts/chat_turn_input_source.sql
-- 순서: 이 DDL → 코드 배포. 새 코드는 열이 없으면 기동 시 verify_schema에서 실패한다.
SET @has_input_source := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'chat_turn' AND column_name = 'input_source'
);
SET @ddl := IF(
    @has_input_source = 0,
    'ALTER TABLE chat_turn ADD COLUMN input_source VARCHAR(32) NULL COMMENT ''질문 유입 경로: user_written=직접 입력, home_suggestion=홈 제안 칩, followup_suggestion=답변 하단 후속 칩, external_link=URL로 질문을 실어 연 바깥 링크, retry=다시 시도. NULL=구 클라이언트'', ALGORITHM=INPLACE, LOCK=NONE',
    'SELECT 1'
);
PREPARE chat_turn_ddl FROM @ddl;
EXECUTE chat_turn_ddl;
DEALLOCATE PREPARE chat_turn_ddl;
