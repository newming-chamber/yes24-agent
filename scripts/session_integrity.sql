-- 기존 정규화 DB용 후속 제약. 신규 DB는 각 CREATE TABLE 파일에 동일 제약이 있다.
-- ADK sessions와 앱 소유권 열·인덱스 정렬 이후 적용한다.
-- 재실행 판정은 migrate_database.py의 마이그레이션 단계가 담당한다.
ALTER TABLE chat_turn
    ADD CONSTRAINT fk_chat_turn_session FOREIGN KEY (app_name, user_id, session_id)
        REFERENCES sessions (app_name, user_id, id) ON DELETE CASCADE ON UPDATE RESTRICT;

ALTER TABLE session_ui
    ADD CONSTRAINT fk_session_ui_session FOREIGN KEY (app_name, user_id, session_id)
        REFERENCES sessions (app_name, user_id, id) ON DELETE CASCADE ON UPDATE RESTRICT;

ALTER TABLE turn_feedback
    ADD CONSTRAINT fk_turn_feedback_session FOREIGN KEY (app_name, user_id, session_id)
        REFERENCES sessions (app_name, user_id, id) ON DELETE CASCADE ON UPDATE RESTRICT;

ALTER TABLE turn_feedback
    ADD CONSTRAINT ck_turn_feedback_rating CHECK (BINARY rating IN ('up', 'down'));
