-- giftcard_period: 크레마AI 상품권 이벤트의 지급 구간 (읽기는 yes24_agent/user_data.py·admin_data.py).
--
-- 세션 DB와 같은 MySQL database에 **배포 전 수동 적용**한다 — verify_schema가 이 테이블과
-- giftcard_issue.period_id를 확인하므로 미적용 상태로 배포하면 서버가 기동하지 않는다.
--   mysql -h <RDS> -u <user> -p <db> < scripts/giftcard_period.sql
-- 순서: ① 이 DDL → ② 진행 중인 구간 행 INSERT·백필 UPDATE(아래 예시) → ③ 코드 배포 → ④ 백필
-- UPDATE 한 번 더. ②가 없으면 배포 순간부터 대상자 확인이 전부 `closed`다(10월 이벤트 진행 중
-- 배포라면 10월 행이 먼저). ④는 ②와 ③ 사이에 옛 코드가 period_id 없이 쌓은 기록을 붙인다 —
-- WHERE period_id IS NULL이라 몇 번 돌려도 같다(멱등). 재적용 안전(CREATE IF NOT EXISTS·열/인덱스
-- 존재 확인). 신규 DB는 giftcard_issue.sql이 period_id까지 만든다 — 그래도 이 파일을 돌려야
-- giftcard_period가 생긴다.
--
-- 한 행 = 회원이 한 번 받을 수 있는 구간(KST 날짜, 양 끝 포함). "주 1회·주말 미지급"은 코드가
-- 아니라 데이터가 표현한다 — 주마다 월~금 한 행을 넣는다. Yes24 상품권번호는 **주마다 다르고**
-- eventNm은 같다. 구간이 없는 날(주말·기간 밖)은 서버가 `closed`로 답하고, 테이블이 비면 이벤트
-- 종료다(설정 스위치 없음).
-- 관리는 어드민(통계 → 이벤트 참여 → 이벤트 기간)에서 한다 — 쓰기 경로가 겹침을 막는다. 아래 SQL로
-- 직접 넣으면 **겹침은 운영 데이터 책임이다**(같은 시작일만 uq가 막는다) — 판정 코드는 검증하지
-- 않고 "오늘을 포함하는 행 중 starts_on 최신 1개"를 현재 구간으로 쓴다(결과 기록의 귀속은
-- "starts_on <= 오늘 중 최신" — giftcard.py).
-- goods_tic_no는 Yes24가 준 **원문 상품권번호(정수)**다. 암호문은 저장하지 않는다 — 서버가 대상자
-- 확인(eligible) 응답 직전에 암호화해 goods_tic_no_encrypted로 내려준다(키는 .env만, giftcard.py).
-- 원문·암호문 모두 공개 조회(GET /event/giftcard)·어드민 화면에는 나가지 않는다.
CREATE TABLE IF NOT EXISTS giftcard_period (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    event_key VARCHAR(64) NOT NULL,                -- Yes24 발급 API eventNm 그대로
    starts_on DATE NOT NULL,                       -- KST 날짜, 포함
    ends_on DATE NOT NULL,                         -- KST 날짜, 포함
    goods_tic_no BIGINT NOT NULL,                  -- 원문 상품권번호(Yes24 전달값, 암호화 전)
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    -- 조회 축: 현재·귀속 구간 = starts_on <= 오늘 중 최신(범위 스캔 + 역순 1행). UNIQUE는 같은
    -- 시작일 두 행(동시 추가 경합 포함)의 마지막 방어다 — 어드민 쓰기는 1062를 409로 돌려준다.
    UNIQUE KEY uq_giftcard_period_starts (starts_on)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- giftcard_issue에 귀속 구간 열을 더한다(기존 테이블). NULL = 구간 도입 전 기록(10월 이벤트) —
-- 아래 백필 전에는 회원 패널·구간별 표·"이번 구간 받음" 판정에서 빠진다(통계 합계에는 들어간다).
-- MySQL엔 ADD COLUMN IF NOT EXISTS가 없어 information_schema로 확인한다(usage_log_search_queries.sql
-- 관례). 열 추가는 ALGORITHM=INSTANT, 인덱스는 INPLACE·LOCK=NONE — 불가하면 조용히 COPY로 떨어지지
-- 않고 오류로 멈춘다.
SET @has_period_id := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'giftcard_issue' AND column_name = 'period_id'
);
SET @ddl := IF(
    @has_period_id = 0,
    'ALTER TABLE giftcard_issue ADD COLUMN period_id BIGINT NULL COMMENT ''giftcard_period.id(귀속 구간). NULL = 구간 도입 전 기록'', ALGORITHM=INSTANT',
    'SELECT 1'
);
PREPARE giftcard_issue_period_id_ddl FROM @ddl;
EXECUTE giftcard_issue_period_id_ddl;
DEALLOCATE PREPARE giftcard_issue_period_id_ddl;

SET @has_period_index := (
    SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'giftcard_issue'
      AND index_name = 'idx_giftcard_issue_period'
);
SET @ddl := IF(
    @has_period_index = 0,
    'ALTER TABLE giftcard_issue ADD KEY idx_giftcard_issue_period (user_id, period_id), ALGORITHM=INPLACE, LOCK=NONE',
    'SELECT 1'
);
PREPARE giftcard_issue_period_index_ddl FROM @ddl;
EXECUTE giftcard_issue_period_index_ddl;
DEALLOCATE PREPARE giftcard_issue_period_index_ddl;

-- 옛 인덱스 idx_giftcard_issue_owner (user_id, event_key)는 새 코드가 쓰지 않는다(판정이 period_id
-- 등치로 바뀌었다). 배포가 안정된 뒤 지운다 — 되돌리기 배포가 옛 코드로 돌아갈 여지를 남긴다.
--   ALTER TABLE giftcard_issue DROP KEY idx_giftcard_issue_owner, ALGORITHM=INPLACE, LOCK=NONE;

-- ── 운영 예시(실행 전 값 확인 — 이 파일을 돌려도 실행되지 않는다) ─────────────────────────
-- 10월 이벤트(구간 도입 전) 행을 넣고 그 id로 기존 기록을 백필한다(코드 배포 뒤 UPDATE 한 번 더):
--   INSERT INTO giftcard_period (event_key, starts_on, ends_on, goods_tic_no)
--   VALUES ('2026_CremaAI_Event', '2026-10-16', '2026-11-01', <10월 상품권번호>);
--   UPDATE giftcard_issue SET period_id = <위 행 id> WHERE period_id IS NULL;
-- 11월 상시 이벤트(주 1회 = 주마다 월~금 한 행, 같은 eventNm·주마다 다른 상품권번호). 보통은
-- 어드민에서 한 주씩 넣는다(추가 폼이 다음 주 월~금과 직전 이벤트명을 미리 채운다):
--   INSERT INTO giftcard_period (event_key, starts_on, ends_on, goods_tic_no) VALUES
--     ('<eventNm>', '2026-11-02', '2026-11-06', <1주차 상품권번호>),
--     ('<eventNm>', '2026-11-09', '2026-11-13', <2주차 상품권번호>),
--     ('<eventNm>', '2026-11-16', '2026-11-20', <3주차 상품권번호>),
--     ('<eventNm>', '2026-11-23', '2026-11-27', <4주차 상품권번호>);
