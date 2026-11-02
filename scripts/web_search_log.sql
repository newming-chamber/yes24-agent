-- web_search_log: 웹 검색 백엔드 요청 원문 기록 (쓰기는 yes24_agent/usage.py record_web_search 단일 경로).
--
-- 행 단위: 백엔드 요청 1건(= 질의 1개)마다 1행 — 퍼플렉시티 search_raw·SearchApi searchapi_raw는
-- 요청당, 그라운딩은 서브콜 1회(재시도 포함, 프리페치 포함)당. 성공·실패 모두 남긴다(실패는
-- results NULL). 요청을 보내지 않은 단락(키 미설정·쿼터 쿨다운·도메인 형식 오류)은 행이 없다.
-- 목적은 검색 결과 수집·재사용이다 — usage_log에 넣지 않는 이유: 어드민 비용·통계가 매번 훑는
-- 표라 행당 수십 KB JSON을 섞지 않는다. 과금 측정은 계속 usage_log(web_* component)가 정본이다.
--
-- turn_id·session_id·user_id·endpoint는 usage_log와 같은 요청 스코프(UsageScope)에서 채운다 —
-- 턴 안의 요청은 턴 종료 시 turn_id를 받아 쓰이고, 스코프 밖(공유 프리페치가 다른 요청에서
-- 시작된 경우 등)은 아는 만큼만 채운다(NULL 허용). FK는 없다: 대화 파기(sessions CASCADE)가
-- 이 표를 지우지 않는다 — 질의 원문에 사용자 입력이 섞일 수 있으니 보존 정책은 따로 정한다.
--
-- 적용(재적용 안전): mysql -h <RDS> -u <user> -p <db> < scripts/web_search_log.sql
-- 순서: 이 DDL → 코드 배포(새 코드는 표가 없으면 verify_schema에서 기동 실패). 끄기는
-- 환경변수 WEB_SEARCH_LOG_ENABLED=false(config web_search_log_enabled) — 표는 그대로 둔다.
CREATE TABLE IF NOT EXISTS web_search_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    app_name VARCHAR(128) NOT NULL,
    -- 기록 시각은 호출 당시 UTC(usage_log와 같은 관례). DB DEFAULT는 직접 INSERT 소비자용 기본값.
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    session_id VARCHAR(128) NULL,
    user_id VARCHAR(128) NULL,      -- Yes24 userNo 또는 익명 단일 사용자 id
    endpoint VARCHAR(32) NULL,      -- 'chat' | 'matrix' — run_agent_stream 호출부가 지정
    turn_id VARCHAR(256) NULL,      -- ADK invocation_id(usage_log.turn_id와 같은 값·타입)
    backend VARCHAR(16) NOT NULL,   -- 'grounding' | 'perplexity' | 'searchapi'
    search_type VARCHAR(16) NULL,   -- 퍼플렉시티 search_type('fast' | 'web'), 그 외 NULL
    -- 실제로 보낸 질의. 그라운딩은 계획된 각도들을 ' | '로 이은 값(프리페치는 사용자 메시지),
    -- SearchApi는 site: 연산자를 뺀 송신 질의.
    query TEXT NOT NULL,
    domain_filters JSON NULL,       -- 정규화된 도메인 필터(없으면 NULL)
    outcome VARCHAR(64) NOT NULL,   -- 'ok' | 'error:<예외 클래스명>'
    error TEXT NULL,                -- 실패 시 예외 repr 요약(120자)
    latency_ms INT NOT NULL,        -- 백엔드 호출 구간만(monotonic, 그라운딩 제목 보강 제외)
    result_count INT NULL,          -- results 원소 수(실패 NULL)
    -- 백엔드 결과 전체(잘라내지 않음). 퍼플렉시티: 응답 results 원문 목록. 그라운딩: 출처별
    -- {url, title, claims}(최종 URL·제목 보강 후). SearchApi: 출처별 {url, title, claims} 파싱 결과.
    results JSON NULL,
    -- 백엔드 원문 응답이 results와 다를 때만 — SearchApi 응답 본문 전체(text_blocks·reference_links 등).
    response JSON NULL,
    KEY idx_web_search_log_created_at (created_at),
    KEY idx_web_search_log_session_id (session_id),
    KEY idx_web_search_log_turn_id (turn_id),
    CONSTRAINT ck_web_search_log_app_name CHECK (CHAR_LENGTH(TRIM(app_name)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
