-- Care & field service facts

BEGIN;

INSERT INTO gold.fact_support_ticket (
    ticket_id, customer_key, created_ts, resolved_ts, ticket_type, priority, status, channel
)
SELECT
    t.ticket_id,
    dc.customer_key,
    t.created_date,
    t.resolved_date,
    t.ticket_type,
    t.priority,
    t.status,
    t.channel
FROM silver.dh_ticket t
INNER JOIN gold.dim_customer dc ON dc.customer_id = t.customer_id
WHERE t.ticket_id IS NOT NULL AND t.created_date IS NOT NULL
ON CONFLICT (ticket_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    created_ts = EXCLUDED.created_ts,
    resolved_ts = EXCLUDED.resolved_ts,
    ticket_type = EXCLUDED.ticket_type,
    priority = EXCLUDED.priority,
    status = EXCLUDED.status,
    channel = EXCLUDED.channel;

INSERT INTO gold.fact_support_case (
    case_id, customer_key, agent_key, case_type, opened_date, closed_date, status
)
SELECT
    c.case_id,
    dc.customer_key,
    da.agent_key,
    c.case_type,
    c.opened_date,
    c.closed_date,
    c.status
FROM silver.dh_case c
INNER JOIN gold.dim_customer dc ON dc.customer_id = c.customer_id
LEFT JOIN gold.dim_agent da ON da.agent_id = c.assigned_agent
WHERE c.case_id IS NOT NULL
ON CONFLICT (case_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    agent_key = EXCLUDED.agent_key,
    case_type = EXCLUDED.case_type,
    opened_date = EXCLUDED.opened_date,
    closed_date = EXCLUDED.closed_date,
    status = EXCLUDED.status;

INSERT INTO gold.fact_ivr_interaction (
    ivr_log_id, customer_key, call_ts, menu_path, wait_time_sec,
    resolution_flag, transferred_to_agent
)
SELECT
    l.ivr_log_id,
    dc.customer_key,
    l.call_date,
    l.menu_path,
    l.wait_time_sec,
    gold.safe_bool(l.resolution_flag),
    gold.safe_bool(l.transferred_to_agent)
FROM silver.dh_ivr_log l
INNER JOIN gold.dim_customer dc ON dc.customer_id = l.customer_id
WHERE l.ivr_log_id IS NOT NULL
ON CONFLICT (ivr_log_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    call_ts = EXCLUDED.call_ts,
    menu_path = EXCLUDED.menu_path,
    wait_time_sec = EXCLUDED.wait_time_sec,
    resolution_flag = EXCLUDED.resolution_flag,
    transferred_to_agent = EXCLUDED.transferred_to_agent;

INSERT INTO gold.fact_store_visit (
    visit_id, customer_key, store_id, visit_ts, visit_reason, duration_min, outcome
)
SELECT
    v.visit_id,
    dc.customer_key,
    v.store_id,
    v.visit_date,
    v.visit_reason,
    v.duration_min,
    v.outcome
FROM silver.dh_store_visit v
INNER JOIN gold.dim_customer dc ON dc.customer_id = v.customer_id
WHERE v.visit_id IS NOT NULL
ON CONFLICT (visit_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    store_id = EXCLUDED.store_id,
    visit_ts = EXCLUDED.visit_ts,
    visit_reason = EXCLUDED.visit_reason,
    duration_min = EXCLUDED.duration_min,
    outcome = EXCLUDED.outcome;

INSERT INTO gold.fact_field_dispatch (
    dispatch_id, customer_key, agent_key, dispatch_date, issue_type, resolution_time_hr, status
)
SELECT
    d.dispatch_id,
    dc.customer_key,
    da.agent_key,
    d.dispatch_date,
    d.issue_type,
    d.resolution_time_hr,
    d.status
FROM silver.dh_dispatch d
INNER JOIN gold.dim_customer dc ON dc.customer_id = d.customer_id
LEFT JOIN gold.dim_agent da ON da.agent_id = d.technician_id
WHERE d.dispatch_id IS NOT NULL
ON CONFLICT (dispatch_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    agent_key = EXCLUDED.agent_key,
    dispatch_date = EXCLUDED.dispatch_date,
    issue_type = EXCLUDED.issue_type,
    resolution_time_hr = EXCLUDED.resolution_time_hr,
    status = EXCLUDED.status;

COMMIT;
