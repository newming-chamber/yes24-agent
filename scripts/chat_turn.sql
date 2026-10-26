-- 공개 확정본. ADK events는 LLM 원시 이력으로 유지하고 신규 snapshot 중복 쓰기는 중단한다.
-- 기존 이벤트 스냅샷은 백필 대조가 끝나도 이 스크립트에서 삭제하지 않는다.
-- 모든 시각은 UTC DATETIME(6). 식별자는 ADK의 앱/사용자/세션 스코프와 turn_id 전체로 묶는다.
-- ADK sessions 테이블을 먼저 생성한다.
CREATE TABLE IF NOT EXISTS chat_turn (
    id BIGINT NOT NULL AUTO_INCREMENT,
    app_name VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    turn_id VARCHAR(256) NOT NULL,
    started_at DATETIME(6) NOT NULL,
    completed_at DATETIME(6) NOT NULL,
    user_text TEXT NOT NULL,
    text MEDIUMTEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    history_saved BOOLEAN NOT NULL DEFAULT TRUE,
    sources JSON NOT NULL,
    process JSON NULL,
    meta JSON NULL,
    error JSON NULL,
    rbti_applied VARCHAR(4) NULL,
    -- 실제로 답한 모델(done.model)·폴백으로 답했을 때 원래 메인 모델(아니면 NULL). 기존 DB는 model_routing.sql.
    model VARCHAR(128) NULL,
    fallback_from VARCHAR(128) NULL,
    -- 그 턴이 답한 프롬프트 버전(prompt_version.id, NULL = 코드 기본). 기존 DB는 prompt_versions.sql.
    prompt_version_id BIGINT NULL,
    -- 질문 유입 경로(클라이언트 선언, 2026-10-21). 값은 main.py ChatRequest.input_source의
    -- Literal이 정본이고 DB는 검사하지 않는다 — 구 클라이언트·백필 행은 NULL. 기존 DB는
    -- chat_turn_input_source.sql.
    input_source VARCHAR(32) NULL COMMENT '질문 유입 경로: user_written=직접 입력, home_suggestion=홈 제안 칩, followup_suggestion=답변 하단 후속 칩, external_link=URL로 질문을 실어 연 바깥 링크, retry=다시 시도. NULL=구 클라이언트',
    PRIMARY KEY (id),
    UNIQUE KEY uq_chat_turn_scope (app_name, user_id, session_id, turn_id),
    KEY idx_chat_turn_session_time (app_name, user_id, session_id, started_at, id),
    CONSTRAINT fk_chat_turn_session FOREIGN KEY (app_name, user_id, session_id)
        REFERENCES sessions (app_name, user_id, id) ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT ck_chat_turn_status CHECK (status IN ('completed','failed','interrupted','unknown')),
    CONSTRAINT ck_chat_turn_history_saved CHECK (history_saved IN (0, 1)),
    CONSTRAINT ck_chat_turn_time CHECK (completed_at >= started_at),
    CONSTRAINT ck_chat_turn_sources CHECK (JSON_TYPE(sources) = 'ARRAY')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
