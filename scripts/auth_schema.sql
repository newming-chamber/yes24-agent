-- 목표 스키마. 기존 users/rate_limit_log가 있는 DB에는 데이터 이관 후 이름 전환한다.
-- 자격증명은 UTF-8 원문 SHA-256 digest만 저장한다. ADK 사용자 식별자는 user_no 문자열이다.
CREATE TABLE users (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_no VARCHAR(20) NOT NULL,
    user_login_id VARCHAR(50) NULL,
    rate_limit_rpm INT NOT NULL,
    rate_limit_rpd INT NOT NULL,
    rbti VARCHAR(4) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_user_no (user_no),
    CONSTRAINT ck_users_limits CHECK (rate_limit_rpm >= 0 AND rate_limit_rpd >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE auth_keys (
    id BIGINT NOT NULL AUTO_INCREMENT,
    key_hash BINARY(32) NOT NULL,
    user_id BIGINT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    kind VARCHAR(16) NOT NULL,
    raw_user_info JSON NULL,
    user_cached_at TIMESTAMP(3) NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_auth_keys_hash (key_hash),
    KEY idx_auth_keys_user (user_id),
    CONSTRAINT fk_auth_keys_user FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT ck_auth_keys_kind CHECK (kind IN ('member', 'dev'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE rate_limit_log (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_id BIGINT NOT NULL,
    auth_key_id BIGINT NULL,
    requested_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    endpoint VARCHAR(100) NULL,
    PRIMARY KEY (id),
    KEY idx_rate_user_time (user_id, requested_at),
    KEY idx_rate_auth_key (auth_key_id),
    CONSTRAINT fk_rate_user FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT fk_rate_auth_key FOREIGN KEY (auth_key_id) REFERENCES auth_keys(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
