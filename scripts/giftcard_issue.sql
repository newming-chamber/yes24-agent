-- giftcard_issue: 크레마AI 상품권 발급 결과 기록 (쓰기는 yes24_agent/user_data.py 단일 경로).
--
-- 세션 DB와 같은 MySQL database에 **배포 전 수동 적용**한다 — verify_schema가 이 테이블을
-- 확인하므로 미적용 상태로 배포하면 서버가 기동하지 않는다.
--   mysql -h <RDS> -u <user> -p <db> < scripts/giftcard_issue.sql
--
-- 클라이언트가 Yes24 발급 API(/api/event/giftcard/issue)에서 받은 ResultCode를 전달한 운영 참고
-- 이력이다 — append-only(시도마다 한 행), 재고·잔여 수량의 근거가 아니다. 시각은 서버가 찍는다.
-- **파기 대상 아님**: 대화 세션과 무관한 회원 단위 기록이라 sessions FK가 없다.
CREATE TABLE IF NOT EXISTS giftcard_issue (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),  -- 발급 시도 시각(UTC)
    user_id VARCHAR(128) NOT NULL,     -- Yes24 userNo (turn_click과 같은 폭)
    event_key VARCHAR(64) NOT NULL,    -- config.giftcard_event_key (이벤트 구분)
    result_code VARCHAR(32) NOT NULL,  -- Yes24 ResultCode (API 계층 Literal 검증)
    -- 조회 축: 대상자 확인의 "이미 받음" 판정(회원·이벤트 등치). app_name이 없는 이유: 대화
    -- 세션과 무관한 회원 단위 기록이고 이벤트 구분은 event_key가 한다.
    KEY idx_giftcard_issue_owner (user_id, event_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
