-- 관리자 계정·서버측 세션·변경 감사(설계: docs/admin-management-design-20260914.md §2).
-- 세션 DB와 같은 database에 **수동 적용**한다(starters.sql·session_ui.sql 관례, 추가 DDL뿐이라
-- 드레인 창 불필요). 재적용해도 안전하다(IF NOT EXISTS). MySQL 8.0.16+ 전제(CHECK 강제).
--
-- 적용:           mysql -h <RDS> -u <user> -p <db> < scripts/admin_management.sql
-- 첫 owner 생성:  uv run python scripts/admin_accounts.py create --username <name> --role owner
-- 순서: 이 DDL → 첫 owner → 코드 배포. 새 코드는 테이블이 없으면 로그인이 503(fail-loud)이다.
-- 롤백: 코드 되돌리기 후 (원하면) DROP TABLE admin_audit, admin_sessions, admin_users.
--       기존 users·auth_keys·starters는 바꾸지 않는다.
CREATE TABLE IF NOT EXISTS admin_users (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    username      VARCHAR(64)  NOT NULL,
    -- 자기서술 해시 문자열: scrypt$<log2 n>$<r>$<p>$<salt b64>$<dk b64>. 파라미터가 행에 있어
    -- config가 바뀌어도 기존 해시가 검증된다(재해시는 다음 비밀번호 변경 때).
    password_hash VARCHAR(255) NOT NULL,
    role          VARCHAR(16)  NOT NULL,
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    -- 로그인 실패 카운트의 기준점이기도 하다(재설정이 잠금을 푼다).
    password_changed_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    created_by    BIGINT       NULL,           -- CLI 생성은 NULL
    created_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_admin_users_username (username),
    KEY idx_admin_users_role_active (role, is_active),       -- 최후 owner 검사
    CONSTRAINT ck_admin_users_role CHECK (role IN ('viewer', 'editor', 'owner'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS admin_sessions (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    admin_user_id BIGINT       NOT NULL,
    token_hash    BINARY(32)   NOT NULL,       -- SHA-256(원문 토큰). 원문은 쿠키에만 있다
    created_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    last_seen_at  TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    expires_at    TIMESTAMP(3) NOT NULL,       -- 절대 만료(created_at + admin_session_ttl_s)
    revoked_at    TIMESTAMP(3) NULL,           -- 로그아웃·강제 폐기 시각
    ip            VARCHAR(45)  NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_admin_sessions_token (token_hash),
    KEY idx_admin_sessions_user (admin_user_id, revoked_at),  -- 사용자 전 세션 폐기
    KEY idx_admin_sessions_expires (expires_at),              -- 보존 정리
    CONSTRAINT fk_admin_sessions_user FOREIGN KEY (admin_user_id) REFERENCES admin_users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- FK 없음: actor가 사라져도 기록은 남아야 한다(actor_name 스냅샷). target_type·action에 CHECK를
-- 두지 않는다 — 새 사건 종류는 DDL이 아니라 코드만 바꾸게 한다.
CREATE TABLE IF NOT EXISTS admin_audit (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    created_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    actor_id      BIGINT       NULL,           -- 인증 전 사건(로그인 실패)·CLI는 NULL
    actor_name    VARCHAR(64)  NULL,           -- 스냅샷: 계정이 비활성돼도 기록은 남는다
    target_type   VARCHAR(16)  NOT NULL,       -- login | admin | user | auth_key | starter
    target_id     VARCHAR(128) NOT NULL,       -- login이면 시도한 계정명, 나머지는 행 id
    action        VARCHAR(32)  NOT NULL,       -- ok | failed | logout | create | update | deactivate | password_reset | password_change | generate
    `before`      JSON         NULL,           -- 바뀐 컬럼만(민감값 제외: 해시·키·raw_user_info 없음)
    `after`       JSON         NULL,
    ip            VARCHAR(45)  NULL,
    session_id    BIGINT       NULL,           -- 어느 세션에서 했는가(폐기 조사용)
    PRIMARY KEY (id),
    KEY idx_admin_audit_target (target_type, target_id, created_at),  -- 로그인 실패 카운트·대상 이력
    KEY idx_admin_audit_actor  (actor_id, created_at),
    KEY idx_admin_audit_time   (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
