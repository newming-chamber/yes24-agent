-- 사용자별 대화 UI 상태 — 읽음 시각과 사용자가 직접 지은 제목.
--
-- **ADK 세션 state에 두지 않는 이유**(2026-09-01 라이브 실측): ADK의 세션 테이블은
-- `update_time`에 `onupdate=func.now()`가 걸려 있어, state를 쓰는 어떤 이벤트든 세션의
-- 활동 시각을 현재로 민다. 읽음 표시를 state에 쓰면 "읽는 순간 다시 안 읽음"이 되고,
-- 이름 변경이 목록의 '최근 활동순'을 밀어 올린다. event.timestamp에 현재 update_time을
-- 그대로 실어 고정하려던 초안은 **값이 같아 SQLAlchemy가 컬럼을 dirty로 보지 않는 바람에**
-- 오히려 onupdate가 발동해 실패했다(UTC에서는 now로, KST에서는 +9시간으로 — 이유가
-- 정반대일 뿐 양쪽 다 깨진다). 그래서 경계를 나눈다: **대화 내용은 ADK가, 사용자 UI
-- 상태는 우리 DB가 소유한다**(turn_feedback과 같은 원칙).
--
-- 적용: mysql -h <RDS> -u <user> -p <db> < scripts/session_ui.sql  (turn_feedback 관례)
CREATE TABLE IF NOT EXISTS session_ui (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    created_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    user_id       VARCHAR(128) NOT NULL,
    session_id    VARCHAR(128) NOT NULL,
    -- 사용자가 지은 제목(없으면 NULL → ADK가 자동 생성한 제목을 쓴다).
    title         VARCHAR(255) NULL,
    -- 마지막으로 이 대화를 연 시각(epoch 초). 세션 last_update_time과 **같은 시간 도메인**이라
    -- 직접 비교한다 — unread = last_update_time > last_read_at.
    last_read_at  DOUBLE       NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_session_ui_owner (user_id, session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
