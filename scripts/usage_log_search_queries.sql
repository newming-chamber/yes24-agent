-- usage_log에 검색 그라운딩 쿼리 수 열을 더한다(2026-10-27). Gemini 3 그라운딩은 실행된 검색
-- 쿼리 1건당 과금이라(config llm_prices search_usd_per_kquery) 토큰만으로는 어드민 비용이 과소
-- 집계된다. 값은 web_grounding 행만 싣는다(grounding_metadata.web_search_queries 길이) — 다른 행·
-- 기존 행은 NULL이고 비용 계산은 NULL을 0으로 친다(열 추가 전 비용은 계속 과소). 신규 DB는
-- usage_log.sql에 이미 들어 있다. 재적용 안전: 열이 있으면 SELECT 1만 실행된다(MySQL엔
-- ADD COLUMN IF NOT EXISTS가 없다). 메타데이터만 바꾸는 ALGORITHM=INSTANT를 명시한다 — 불가하면
-- INPLACE·COPY로 조용히 떨어지지 않고 오류로 멈춘다(model_routing.sql 관례).
--
-- 적용: mysql -h <RDS> -u <user> -p <db> < scripts/usage_log_search_queries.sql
-- 순서: 이 DDL → 코드 배포. 운영 뷰(operational_views.sql)는 usage_log 열을 나열하지 않아(집계만)
-- 재적용이 필요 없다. 새 코드는 열이 없으면 기동 시 verify_schema에서 실패한다.
SET @has_search_queries := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'usage_log' AND column_name = 'search_queries'
);
SET @ddl := IF(
    @has_search_queries = 0,
    'ALTER TABLE usage_log ADD COLUMN search_queries INT NULL COMMENT ''그라운딩 서브콜이 실행한 검색 쿼리 수(쿼리당 과금). NULL = 그라운딩 외 행·열 추가 전 기록, 0 = 그라운딩 콜이 검색을 안 돌림'', ALGORITHM=INSTANT',
    'SELECT 1'
);
PREPARE usage_log_search_queries_ddl FROM @ddl;
EXECUTE usage_log_search_queries_ddl;
DEALLOCATE PREPARE usage_log_search_queries_ddl;
