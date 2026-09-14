-- turn_feedback: 턴(=ADK invocation) 단위 사용자 피드백 (쓰기·읽기는 yes24_agent/user_data.py 단일 경로).
--
-- 세션 DB와 같은 MySQL database에 **수동 적용**한다 — 코드에 DDL·마이그레이션이 없는
-- 관례(users·rate_limit_log·usage_log와 동일)를 따른다.
--
-- 전용 테이블인 이유: 세션 state JSON에 넣으면 "싫어요 상위 턴" 같은 집계가 전 세션
-- 스캔이 된다 — 사용량 계측이 같은 이유로 events 재사용을 기각하고 usage_log로 간 판단을
-- 반복한다. 실패 정책은 usage_log와 **정반대**다: 피드백은 사용자 행동에 대한 응답이라
-- 저장 실패를 200으로 숨기지 않고 5xx로 정직하게 끊는다(auth의 fail-loud 계열).
--
-- turn_id는 ADK가 턴마다 부여하는 invocation_id다(events 테이블의 1급 컬럼과 같은 값,
-- /chat/stream done 이벤트가 클라이언트에 전달). 사용자당 턴당 최신 1행만 유지한다(upsert).
-- ADK sessions 테이블을 먼저 생성한다.
CREATE TABLE IF NOT EXISTS turn_feedback (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    -- 기록 시각은 DB 시계·DEFAULT에 위임, 밀리초 정밀도는 rate_limit_log NOW(3) 관례와 정렬.
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    app_name VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,     -- Yes24 userNo (피드백은 인증 필수라 익명 행이 없다)
    session_id VARCHAR(128) NOT NULL, -- ADK 세션 id (usage_log와 같은 폭)
    turn_id VARCHAR(256) NOT NULL,    -- ADK invocation_id (events.invocation_id와 같은 값)
    rating VARCHAR(8) NOT NULL,       -- 'up' | 'down' (철회는 행 삭제 — 상태 3종을 값 2종+부재로)
    comment TEXT NULL,                -- 선택 코멘트 (요청 본문 상한은 API 계층 request_max_chars)
    UNIQUE KEY uq_turn_feedback_user_turn (app_name, user_id, session_id, turn_id),
    KEY idx_turn_feedback_rating (rating),
    KEY idx_turn_feedback_session (session_id),
    CONSTRAINT fk_turn_feedback_session FOREIGN KEY (app_name, user_id, session_id)
        REFERENCES sessions (app_name, user_id, id) ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT ck_turn_feedback_rating CHECK (BINARY rating IN ('up', 'down'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
