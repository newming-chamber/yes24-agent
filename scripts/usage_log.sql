-- usage_log: LLM 호출 토큰 사용량 히스토리 (쓰기는 yes24_agent/usage.py 단일 경로).
--
-- 세션 DB와 같은 MySQL database에 **수동 적용**한다 — 코드에 DDL·마이그레이션이 없는
-- 관례(users·rate_limit_log와 동일)를 따른다. 테이블이 없으면 INSERT가 warning으로
-- 무시될 뿐 서비스는 정상이다(부가 채널 계약).
--
-- 행 단위: component='main'은 턴당 1행(그 턴의 모든 LLM 콜 합산 + latency_ms),
-- 서브콜(enrichment·thought_translation·web_grounding·web_prefetch_hint)은 콜당 1행.
-- component 값은 호출부가 넘기는 파라미터라 새 값이 생겨도 스키마 변경이 없다.
CREATE TABLE IF NOT EXISTS usage_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    -- 기록 시각은 DB 시계·DEFAULT에 위임(naive timestamp는 DB 시계끼리만 비교 — auth 관례).
    -- 밀리초 정밀도는 rate_limit_log의 NOW(3) 관례와 정렬.
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    session_id VARCHAR(128) NULL,   -- 서브콜은 세션 문맥이 없을 수 있다(NULL 허용)
    user_id VARCHAR(64) NULL,       -- Yes24 userNo 또는 익명 단일 사용자 id
    endpoint VARCHAR(32) NULL,      -- 'chat' | 'matrix' — run_agent_stream 호출부가 지정
    component VARCHAR(32) NOT NULL, -- 'main' | 'enrichment' | 'thought_translation' | ...
    model VARCHAR(128) NULL,
    prompt_tokens INT NULL,         -- 요청(프롬프트) 토큰 — genai prompt_token_count
    response_tokens INT NULL,       -- 응답(후보) 토큰 — genai candidates_token_count
    total_tokens INT NULL,          -- 벤더 합계(사고 토큰 포함) — genai total_token_count
    latency_ms INT NULL,            -- 메인 턴만(턴 시작~마감 벽시계), 서브콜은 NULL
    -- 기간별 비용 집계·세션 추적·모델별 히스토리가 주 조회 축.
    KEY idx_usage_log_created_at (created_at),
    KEY idx_usage_log_session_id (session_id),
    KEY idx_usage_log_model (model)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
