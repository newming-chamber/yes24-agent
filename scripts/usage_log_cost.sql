-- usage_log 확장 — 비용 계산과 턴 결과 요약.
--
-- **왜 새 테이블이 아니라 컬럼 추가인가**: component='main' 행이 이미 턴당 정확히 1행이고,
-- runner의 마감 한 지점에서 전 경로를 지나며, session_id·model·latency_ms를 이미 갖고 있다.
-- 새 테이블은 같은 키·같은 수명·같은 실패정책의 행을 두 벌 만드는 일이다(삭제 우선).
--
-- **thinking_tokens가 없으면 금액이 안 나온다**: 우리 246행 실측에서 total-prompt-response가
-- 턴당 평균 964토큰이었다. 기록된 출력(661)보다 크다 — 사고 토큰이 과금되는데 세지 않아
-- 실제 출력을 2.5배 과소 계상하고 있었다(2026-09-03).
--
-- 전부 NULL 허용 — 서브콜 행은 종전 그대로다(latency_ms가 이미 세운 비대칭).
-- 적용: mysql -h <RDS> -u <user> -p <db> < scripts/usage_log_cost.sql
ALTER TABLE usage_log
  ADD COLUMN thinking_tokens INT NULL COMMENT 'thoughts_token_count — 과금 출력에 포함되나 종전 미기록',
  ADD COLUMN cached_tokens   INT NULL COMMENT 'cached_content_token_count — 프롬프트 중 할인 단가 대상',
  ADD COLUMN turn_id         VARCHAR(128) NULL COMMENT 'ADK invocation_id — events·turn_feedback과 같은 키',
  ADD COLUMN outcome         VARCHAR(16)  NULL COMMENT 'main 행만: ok|empty|timeout|error|aborted',
  ADD COLUMN llm_calls       SMALLINT     NULL COMMENT '이 행이 합산한 LLM 콜 수(비용의 1차 레버)',
  ADD COLUMN tool_calls      SMALLINT     NULL COMMENT 'main 행만: 이 턴에 돈 도구 수',
  ADD COLUMN cited_sources   SMALLINT     NULL COMMENT 'main 행만: 인용된 출처 수. tools>0 AND cited=0 = 무접지 의심',
  ADD KEY idx_usage_log_turn (turn_id);
