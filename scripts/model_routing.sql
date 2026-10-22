-- 답변 모델 기록·런타임 모델 설정(yes24_agent/model_routing.py).
--  1) chat_turn에 model(실제로 답한 모델)·fallback_from(폴백했을 때 원래 메인 모델) 열 — 기존 행은 NULL.
--  2) model_settings: owner 어드민이 쓰는 기본·폴백 모델 오버라이드(3: 모델별 처리 등급). 행은 id=1 하나뿐이고 여기서 심는다
--     — 쓰기는 그 행의 FOR UPDATE(edit_row)라 없는 행 자리에 갭 잠금이 걸리지 않는다. NULL = config 따름,
--     fallback_model '' = 폴백 끔. 값 검증(selectable_models)은 앱이 한다 — 목록은 배포마다 바뀐다.
-- 신규 DB는 chat_turn.sql에 1)이 이미 들어 있다. 재적용 안전: 열이 있으면 SELECT 1만 실행되고
-- (MySQL엔 ADD COLUMN IF NOT EXISTS가 없다), 테이블·행은 IF NOT EXISTS·INSERT IGNORE다.
-- 메타데이터만 바꾸는 ALGORITHM=INSTANT(행 재작성·테이블 잠금 없음)를 명시한다 — INSTANT가 불가한 상태면
-- INPLACE·COPY로 조용히 떨어지지 않고 오류로 멈춘다(그때 적용 방법을 다시 정한다).
--
-- 적용: mysql -h <RDS> -u <user> -p <db> < scripts/model_routing.sql
--       그다음 operational_views.sql 재적용(chat_turns 뷰에 두 열 — CREATE OR REPLACE라 멱등).
--       뷰 정의는 이 파일에 복제하지 않는다 — chat_turns·chat_turn_activity(t.*로 의존)의 정본이
--       operational_views.sql 하나라, 사본을 두면 한쪽만 고쳐지는 순간 갈라진다.
-- 순서: 이 DDL → 뷰 → 코드 배포. 새 코드는 열(표·뷰 모두)이 없으면 기동 시 verify_schema에서 실패한다.
SET @has_model := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'chat_turn' AND column_name = 'model'
);
SET @ddl := IF(
    @has_model = 0,
    'ALTER TABLE chat_turn ADD COLUMN model VARCHAR(128) NULL, ADD COLUMN fallback_from VARCHAR(128) NULL, ALGORITHM=INSTANT',
    'SELECT 1'
);
PREPARE chat_turn_model_ddl FROM @ddl;
EXECUTE chat_turn_model_ddl;
DEALLOCATE PREPARE chat_turn_model_ddl;

CREATE TABLE IF NOT EXISTS model_settings (
    id             INT          NOT NULL,
    default_model  VARCHAR(128) NULL,
    fallback_model VARCHAR(128) NULL,
    updated_at     TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    CONSTRAINT ck_model_settings_single_row CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT IGNORE INTO model_settings (id) VALUES (1);

-- 3) 처리 등급(2026-10-19 추가 — 이미 이 파일을 적용한 DB도 다시 적용하면 이 두 열만 붙는다).
--    model_settings.service_tiers: {LiteLLM 모델: 등급} 정본 직렬화 JSON 문자열(앱이 키 정렬·공백 없이
--    쓴다 — encode_service_tiers). JSON 타입이 아니라 문자열인 이유: 편집 프로토콜의 같은 값 판정·expected
--    대조가 문자열 비교라 직렬화를 앱 한 곳이 정해야 한다. NULL = 모든 모델 config 등급.
--    usage_log.service_tier: 그 행이 요청한 처리 등급(LiteLLM만, 표준·Gemini·기존 행은 NULL) — 비용 단가 축.
SET @has_tiers := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'model_settings' AND column_name = 'service_tiers'
);
SET @ddl := IF(
    @has_tiers = 0,
    'ALTER TABLE model_settings ADD COLUMN service_tiers VARCHAR(1024) NULL, ALGORITHM=INSTANT',
    'SELECT 1'
);
PREPARE model_settings_tier_ddl FROM @ddl;
EXECUTE model_settings_tier_ddl;
DEALLOCATE PREPARE model_settings_tier_ddl;

SET @has_usage_tier := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'usage_log' AND column_name = 'service_tier'
);
SET @ddl := IF(
    @has_usage_tier = 0,
    'ALTER TABLE usage_log ADD COLUMN service_tier VARCHAR(16) NULL, ALGORITHM=INSTANT',
    'SELECT 1'
);
PREPARE usage_log_tier_ddl FROM @ddl;
EXECUTE usage_log_tier_ddl;
DEALLOCATE PREPARE usage_log_tier_ddl;

-- 4) 모델별 사고 강도(2026-10-19 추가 — 다시 적용하면 이 두 열만 붙는다).
--    model_settings.reasoning_efforts: {LiteLLM 모델: 강도} 정본 직렬화 JSON 문자열(service_tiers와 같은 규칙,
--    NULL = 모든 모델 config — litellm_reasoning_efforts, 없으면 전역 litellm_reasoning_effort).
--    usage_log.reasoning_effort: 그 행이 실제로 실은 사고 강도(LiteLLM만, Gemini·미전달·기존 행은 NULL) —
--    사고 토큰·지연을 강도별로 가르는 분석 축(런타임에 모델별로 바뀌어 감사 시각 대조로는 복원이 어렵다).
SET @has_efforts := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'model_settings' AND column_name = 'reasoning_efforts'
);
SET @ddl := IF(
    @has_efforts = 0,
    'ALTER TABLE model_settings ADD COLUMN reasoning_efforts VARCHAR(1024) NULL, ALGORITHM=INSTANT',
    'SELECT 1'
);
PREPARE model_settings_effort_ddl FROM @ddl;
EXECUTE model_settings_effort_ddl;
DEALLOCATE PREPARE model_settings_effort_ddl;

SET @has_usage_effort := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'usage_log' AND column_name = 'reasoning_effort'
);
SET @ddl := IF(
    @has_usage_effort = 0,
    'ALTER TABLE usage_log ADD COLUMN reasoning_effort VARCHAR(16) NULL, ALGORITHM=INSTANT',
    'SELECT 1'
);
PREPARE usage_log_effort_ddl FROM @ddl;
EXECUTE usage_log_effort_ddl;
DEALLOCATE PREPARE usage_log_effort_ddl;
