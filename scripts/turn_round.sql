-- turn_round: 턴의 LLM 콜(라운드)별 토큰·지연과 폴백·재시도 시도 기록 (docs/admin-lab-design-20261019.md §3).
--
-- 생산 지점은 runner의 TurnProcess.rounds 하나이고, 쓰기는 user_data.save_turn이 chat_turn과 **같은
-- 트랜잭션**에서 한다(_run_owned). 비공개 운영 기록이라 공개 스냅샷(done.process)에는 없다 — 읽기는
-- owner 어드민 추적 API(GET /admin/api/sessions/{id}/turns/{turn_id}/trace)뿐이다.
-- usage_log에 라운드 행을 넣지 않는 이유: 비용 집계가 component 이름으로 가르지 않아 이중 계산된다.
--
-- 파기: chat_turn FK CASCADE(chat_turn은 sessions FK CASCADE) — 대화 파기가 라운드도 지운다.
-- verify_schema가 이 FK의 DELETE_RULE을 기동 시 확인한다.
--
-- 적용(재적용 안전): mysql -h <RDS> -u <user> -p <db> < scripts/turn_round.sql
-- 순서: chat_turn.sql 이후 → 코드 배포(새 코드는 표가 없으면 verify_schema에서 기동 실패).
CREATE TABLE IF NOT EXISTS turn_round (
    id BIGINT NOT NULL AUTO_INCREMENT,
    app_name VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    turn_id VARCHAR(256) NOT NULL,
    -- TurnProcess.rounds 목록 순번(0부터). 한 라운드에 콜이 둘 이상일 수 있어(도구 없이 이어지는 콜)
    -- (attempt, round_index)는 유일하지 않다 — 유일 키는 순번이다.
    seq SMALLINT NOT NULL,
    -- 0 = 첫 시도, 1 = 폴백/재시도(TurnProcess.next_attempt). TINYINT가 아닌 이유: 어드민 조회
    -- 디코더(admin_data.BOOL_DECODERS)가 TINY 0/1을 bool로 읽는다.
    attempt SMALLINT NOT NULL,
    round_index SMALLINT NOT NULL,
    model VARCHAR(128) NOT NULL,
    -- 턴 시작 기준 경과(ms)
    started_ms INT NOT NULL,
    ended_ms INT NOT NULL,
    prompt_tokens INT NULL,
    response_tokens INT NULL,
    thinking_tokens INT NULL,
    cached_tokens INT NULL,
    -- 이 콜이 낸 도구 호출 이름 목록
    tool_calls JSON NULL,
    -- 시도가 벤더 오류로 끝났으면 그 상태코드(그 시도의 마지막 행에만)
    error_status SMALLINT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_turn_round (app_name, user_id, session_id, turn_id, seq),
    CONSTRAINT fk_turn_round_turn FOREIGN KEY (app_name, user_id, session_id, turn_id)
        REFERENCES chat_turn (app_name, user_id, session_id, turn_id) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
