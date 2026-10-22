-- 프롬프트 버전(yes24_agent/prompt_versions.py · docs/admin-lab-design-20261019.md §2.2).
--  1) prompt_version: 슬롯 오버레이 버전(불변 — 편집 = parent_id를 단 새 행). 계약 본문은 DB에 없다
--     (슬롯 이름 → 텍스트 맵만 — agent.PROMPT_SLOTS). 상태 열은 두지 않는다: 활성 = model_settings.prompt_versions
--     값에 있음, 폐기 = retired_at, 나머지 = 초안(파생).
--  2) model_settings.prompt_versions: {모델 | "*": version_id} 정본 직렬화 JSON 문자열(service_tiers와 같은 규칙,
--     NULL = 오버레이 없음 = 코드 프롬프트).
--  3) chat_turn.prompt_version_id: 그 턴이 답한 프롬프트 버전(NULL = 코드 기본, 기존 행 NULL).
-- 신규 DB는 chat_turn.sql에 3)이 이미 들어 있다. 재적용 안전(IF NOT EXISTS · 열 존재 검사 후 SELECT 1).
-- ALGORITHM=INSTANT 명시 — 불가하면 조용히 COPY로 떨어지지 않고 오류로 멈춘다.
--
-- 적용: mysql -h <RDS> -u <user> -p <db> < scripts/prompt_versions.sql
--       그다음 operational_views.sql 재적용(chat_turns 뷰에 prompt_version_id).
-- 순서: model_routing.sql → 이 DDL → 뷰 → 코드 배포(새 코드는 열·표가 없으면 verify_schema에서 기동 실패).
CREATE TABLE IF NOT EXISTS prompt_version (
    id           BIGINT       NOT NULL AUTO_INCREMENT,
    name         VARCHAR(128) NOT NULL,
    -- NULL = 모든 모델 후보("*" 바인딩 가능), 값 = 그 모델 전용(그 모델 키에만 바인딩).
    model        VARCHAR(128) NULL,
    slots        JSON         NOT NULL,
    -- 저장 시점 코드 프롬프트 기본(풀 구성 조립 바이트)의 sha256 앞 16자 — 어떤 코드 기본 위의 편집인가.
    base_hash    VARCHAR(16)  NOT NULL,
    parent_id    BIGINT       NULL,
    note         VARCHAR(500) NULL,
    -- 최초 승격 시각. 값이 있으면 롤백 대상(승격 게이트 — owner 사유 메모 필수 — 면제).
    activated_at TIMESTAMP(3) NULL,
    retired_at   TIMESTAMP(3) NULL,
    created_by   BIGINT       NULL,
    created_at   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_prompt_version_name (name),
    KEY idx_prompt_version_model (model, retired_at),
    CONSTRAINT ck_prompt_version_slots CHECK (JSON_TYPE(slots) = 'OBJECT')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @has_bindings := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'model_settings' AND column_name = 'prompt_versions'
);
SET @ddl := IF(
    @has_bindings = 0,
    'ALTER TABLE model_settings ADD COLUMN prompt_versions VARCHAR(1024) NULL, ALGORITHM=INSTANT',
    'SELECT 1'
);
PREPARE model_settings_prompt_ddl FROM @ddl;
EXECUTE model_settings_prompt_ddl;
DEALLOCATE PREPARE model_settings_prompt_ddl;

SET @has_turn_version := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'chat_turn' AND column_name = 'prompt_version_id'
);
SET @ddl := IF(
    @has_turn_version = 0,
    'ALTER TABLE chat_turn ADD COLUMN prompt_version_id BIGINT NULL, ALGORITHM=INSTANT',
    'SELECT 1'
);
PREPARE chat_turn_prompt_ddl FROM @ddl;
EXECUTE chat_turn_prompt_ddl;
DEALLOCATE PREPARE chat_turn_prompt_ddl;
