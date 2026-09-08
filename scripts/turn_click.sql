-- turn_click: 답변 안의 링크 클릭 기록 (쓰기는 yes24_agent/user_data.py 단일 경로).
--
-- 세션 DB와 같은 MySQL database에 **수동 적용**한다 — 코드에 DDL·마이그레이션이 없는
-- 관례(turn_feedback·session_ui와 동일).
--   mysql -h <RDS> -u <user> -p <db> < scripts/turn_click.sql
--
-- 열쇠가 URL인 이유(2026-09-08): crema 시절 프론트가 보내던 book-click(goods_no·판형 열거형)을
-- 복제하지 않는다. 상품·공지·웹·판형 링크 무엇이든 URL은 있으므로 대상이 늘어도 스키마가
-- 안 바뀐다. turn_feedback과 달리 **append-only**다 — 같은 URL 재클릭은 행이 늘어난다(UNIQUE 없음).
-- 실패 정책은 turn_feedback과 같다: 저장 실패는 5xx(성공 204 = 실제 저장됨).
-- **purge 대상 아님(분석 로그)**: 대화 삭제(purge_session)는 사용자 상태(turn_feedback·session_ui)만
-- 지운다 — 클릭은 usage_log 같은 제품 분석 이벤트라 지우면 집계에 구멍이 난다(2026-09-08).
--
-- turn_id는 ADK가 턴마다 부여하는 invocation_id다(/chat/stream done.turn_id).
CREATE TABLE IF NOT EXISTS turn_click (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    user_id VARCHAR(64) NOT NULL,     -- Yes24 userNo (turn_feedback과 같은 폭)
    session_id VARCHAR(128) NOT NULL, -- ADK 세션 id
    turn_id VARCHAR(128) NOT NULL,    -- ADK invocation_id (events.invocation_id와 같은 값)
    url TEXT NOT NULL,                -- 클릭한 링크 (요청 상한은 API 계층 click_url_max_chars)
    source_id INT NULL,               -- 본문 [n] 마커 번호 (출처 카드에서 눌렀을 때만)
    source_type VARCHAR(16) NULL,     -- 공개 출처 어휘 product|notice|web (API 계층 Literal 검증)
    label VARCHAR(255) NULL,          -- 표시 제목 (API 계층 click_label_max_chars와 같은 폭)
    -- 조회 축은 소유자 스코프(사용자→세션→턴)다. URL 집계 인덱스는 그 질의가 생길 때
    -- 얹는다(선제 인덱스 금지).
    KEY idx_turn_click_owner_turn (user_id, session_id, turn_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
