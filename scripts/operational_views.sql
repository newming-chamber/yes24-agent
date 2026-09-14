-- Human-readable projections, not additional copies of operational facts.
CREATE OR REPLACE SQL SECURITY INVOKER VIEW chat_turns AS
SELECT app_name,user_id,session_id,turn_id,started_at AS asked_at,
       completed_at,user_text AS user_message,text AS assistant_message,status,error,
       sources,process,meta,rbti_applied,
       JSON_EXTRACT(sources,'$[*].id') AS cited_ids,
       JSON_VALUE(process,'$.elapsed_ms' RETURNING UNSIGNED) AS elapsed_ms,
       history_saved
FROM chat_turn;

CREATE OR REPLACE SQL SECURITY INVOKER VIEW chat_turn_activity AS
SELECT t.*,u.total_tokens AS attributable_total_tokens,u.main_total_tokens,
       u.llm_rows,COALESCE(f.likes,0) AS likes,COALESCE(f.dislikes,0) AS dislikes,
       COALESCE(c.clicks,0) AS clicks
FROM chat_turns t
LEFT JOIN (
    SELECT app_name,user_id,session_id,turn_id,SUM(total_tokens) AS total_tokens,
           SUM(CASE WHEN component='main' THEN total_tokens ELSE 0 END) AS main_total_tokens,
           COUNT(*) AS llm_rows
    FROM usage_log WHERE turn_id IS NOT NULL
    GROUP BY app_name,user_id,session_id,turn_id
) u ON u.app_name=t.app_name AND u.user_id=t.user_id
    AND u.session_id=t.session_id AND u.turn_id=t.turn_id
LEFT JOIN (
    SELECT app_name,user_id,session_id,turn_id,
           SUM(rating='up') AS likes,SUM(rating='down') AS dislikes
    FROM turn_feedback GROUP BY app_name,user_id,session_id,turn_id
) f ON f.app_name=t.app_name AND f.user_id=t.user_id
    AND f.session_id=t.session_id AND f.turn_id=t.turn_id
LEFT JOIN (
    SELECT app_name,user_id,session_id,turn_id,COUNT(*) AS clicks
    FROM turn_click GROUP BY app_name,user_id,session_id,turn_id
) c ON c.app_name=t.app_name AND c.user_id=t.user_id
    AND c.session_id=t.session_id AND c.turn_id=t.turn_id;

CREATE OR REPLACE SQL SECURITY INVOKER VIEW user_activity AS
SELECT u.id,u.user_no,u.user_login_id,u.is_active,u.rbti,
       u.rate_limit_rpm,u.rate_limit_rpd,
       COALESCE(k.active_keys,0) AS active_keys,
       COALESCE(r.requests_last_minute,0) AS requests_last_minute,
       COALESCE(r.requests_last_day,0) AS requests_last_day,
       r.last_request_at,u.created_at,u.updated_at
FROM users u
LEFT JOIN (
    SELECT user_id,SUM(is_active=1) AS active_keys FROM auth_keys GROUP BY user_id
) k ON k.user_id=u.id
LEFT JOIN (
    SELECT user_id,SUM(requested_at>NOW(3)-INTERVAL 1 MINUTE) AS requests_last_minute,
           COUNT(*) AS requests_last_day,MAX(requested_at) AS last_request_at
    FROM rate_limit_log WHERE requested_at>NOW(3)-INTERVAL 1 DAY
    GROUP BY user_id
) r ON r.user_id=u.id;
