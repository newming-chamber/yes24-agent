-- 초기 질문(스타터) 회전 풀 — 빈 화면의 질문 칩을 프론트 하드코딩이 아니라 서버 풀에서 뽑는다
-- (설계: docs/starters-design.md, 읽기·쓰기는 yes24_agent/starters.py 단일 경로).
--
-- 세션 DB와 같은 MySQL database에 **수동 적용**한다 — 코드에 DDL·마이그레이션이 없는
-- 관례(users·usage_log·turn_feedback·session_ui와 동일).
--
-- starters: 항목 = 문장 + 슬롯(질문 유형) + 출처(auto/manual) + 상품 참조 + 유효기간 + 고정/활성.
--   slot은 시드 키(yes24/urls.py BROWSE_SEED_URLS의 키 또는 수동 슬롯) — 코드 열거가 없다.
--   text는 **누르면 그대로 전송되는 완결 문장**이라 서버·프론트 어디서도 자르지 않는다.
--   auto 행은 goods_no(관측 상품)·source_url(관측 페이지)·run_date(생성일)를 함께 갖는다 —
--   문구 필터 대신 관측 참조로 검증한 흔적이다(원칙 4a).
-- starter_runs: 일 1회 자동 생성의 **멀티워커 잠금 + 이력**. (slot, run_date) PK를 INSERT로
--   선점한 워커만 생성한다(중복 실행 차단은 스케줄러가 아니라 이 PK가 한다).
--
-- 적용: mysql -h <RDS> -u <user> -p <db> < scripts/starters.sql
CREATE TABLE IF NOT EXISTS starters (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  slot          VARCHAR(32)  NOT NULL,          -- bestseller | new | policy (시드 키, 코드 열거 없음)
  text          VARCHAR(200) NOT NULL,          -- 누르면 그대로 전송되는 완결 문장
  source        ENUM('auto','manual') NOT NULL,
  goods_no      BIGINT       NULL,              -- auto: 관측 상품 참조
  source_url    VARCHAR(500) NULL,              -- auto: 관측 페이지
  run_date      DATE         NULL,              -- auto: 생성일(KST)
  pinned        TINYINT(1)   NOT NULL DEFAULT 0,
  active        TINYINT(1)   NOT NULL DEFAULT 1,
  valid_from    DATE         NULL,
  valid_until   DATE         NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_serve (slot, active, valid_from, valid_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS starter_runs (   -- 일 1회 생성의 멀티워커 잠금 + 이력
  slot      VARCHAR(32) NOT NULL,
  run_date  DATE        NOT NULL,
  status    ENUM('running','ok','failed') NOT NULL,
  detail    VARCHAR(500) NULL,
  started_at DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (slot, run_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 수동 슬롯은 운영자가 소유한다(자동 생성 대상이 아니다). 초기 항목은 데이터로 관리하고
-- 코드 상수로 두지 않는다(D6).
--   policy: 정책 페이지는 월 1건 갱신이라 회전 재료가 아니다.
--   general: **첫 화면이 책 이야기만으로 채워지지 않게 한다.** 이 제품은 범용 AI가 기본이고
--            책이 강점이라(CLAUDE.md 정체성), 칩이 전부 책이면 "책만 하는 봇"으로 읽힌다.
--            문구는 실사용 로그에 실제로 있던 질문 결을 따랐다(주가·영화·뉴스·글쓰기).
-- 재적용해도 중복되지 않게 문장 기준으로 넣는다(CREATE TABLE IF NOT EXISTS와 같은 멱등성).
INSERT INTO starters (slot, text, source)
SELECT * FROM (
  SELECT 'policy', '예스24 단순 변심 반품, 며칠 안에 어떤 조건이면 돼?', 'manual'
  UNION ALL SELECT 'policy', '예스24 무료배송 기준이 어떻게 돼?', 'manual'
  UNION ALL SELECT 'policy', '크레마클럽 요금제랑 무료 체험 조건이 어떻게 돼?', 'manual'
  UNION ALL SELECT 'general', '오늘 코스피 어때?', 'manual'
  UNION ALL SELECT 'general', '요즘 개봉한 영화 중에 볼만한 거 있어?', 'manual'
  UNION ALL SELECT 'general', '오늘 주요 뉴스 3개만 요약해줘', 'manual'
  UNION ALL SELECT 'general', '발표 앞두고 긴장 푸는 법 알려줘', 'manual'
  UNION ALL SELECT 'general', '이 문장 더 자연스럽게 다듬어줘', 'manual'
) AS seed(slot, text, source)
WHERE NOT EXISTS (SELECT 1 FROM starters WHERE starters.text = seed.text);
