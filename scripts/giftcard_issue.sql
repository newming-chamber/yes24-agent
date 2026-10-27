-- giftcard_issue: 크레마AI 상품권 발급 결과 기록 (쓰기는 yes24_agent/user_data.py 단일 경로).
--
-- 세션 DB와 같은 MySQL database에 **배포 전 수동 적용**한다 — verify_schema가 이 테이블을
-- 확인하므로 미적용 상태로 배포하면 서버가 기동하지 않는다. 구간 테이블은 giftcard_period.sql
-- (기존 DB의 period_id 열 추가도 그 파일이 한다).
--   mysql -h <RDS> -u <user> -p <db> < scripts/giftcard_issue.sql
--
-- 클라이언트가 Yes24 발급 API(/api/event/giftcard/issue)에서 받은 ResultCode를 전달한 운영 참고
-- 이력이다 — append-only(시도마다 한 행), 재고·잔여 수량의 근거가 아니다. 시각은 서버가 찍는다.
-- **파기 대상 아님**: 대화 세션과 무관한 회원 단위 기록이라 sessions FK가 없다.
CREATE TABLE IF NOT EXISTS giftcard_issue (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),  -- 발급 시도 시각(UTC)
    user_id VARCHAR(128) NOT NULL,     -- Yes24 userNo (turn_click과 같은 폭)
    event_key VARCHAR(64) NOT NULL,    -- 귀속 구간의 giftcard_period.event_key (기록 시점 사본)
    result_code VARCHAR(32) NOT NULL,  -- Yes24 ResultCode (API 계층 Literal 검증)
    period_id BIGINT NULL,             -- giftcard_period.id(귀속 구간). NULL = 구간 도입 전 기록
    -- 조회 축: 대상자 확인의 "이 구간에 이미 받음" 판정(회원·구간 등치). app_name이 없는 이유:
    -- 대화 세션과 무관한 회원 단위 기록이고 구간 구분은 period_id가 한다. FK가 없는 이유: 구간 행을
    -- 고쳐도 지난 기록이 막히거나 지워지지 않게(운영 참고 이력).
    KEY idx_giftcard_issue_period (user_id, period_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
