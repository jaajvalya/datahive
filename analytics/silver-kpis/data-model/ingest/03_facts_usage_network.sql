-- Usage & network facts: CDR, IP sessions, bandwidth, QoE

BEGIN;

INSERT INTO gold.fact_call_detail (
    cdr_id, customer_key, tower_key, call_type, call_start_ts, duration_sec
)
SELECT
    c.cdr_id,
    dc.customer_key,
    dt.tower_key,
    c.call_type,
    c.call_start_ts,
    gold.safe_numeric(c.duration_sec)
FROM silver.dh_cdr c
INNER JOIN gold.dim_customer dc ON dc.customer_id = c.customer_id
LEFT JOIN gold.dim_tower dt ON dt.tower_id = c.tower_id
WHERE c.cdr_id IS NOT NULL
ON CONFLICT (cdr_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    tower_key = EXCLUDED.tower_key,
    call_type = EXCLUDED.call_type,
    call_start_ts = EXCLUDED.call_start_ts,
    duration_sec = EXCLUDED.duration_sec;

INSERT INTO gold.fact_ip_session (
    session_id, customer_key, session_start, session_duration_min,
    bytes_uploaded_mb, bytes_downloaded_mb, apn
)
SELECT
    s.session_id,
    dc.customer_key,
    s.session_start,
    gold.safe_numeric(s.session_duration_min),
    s.bytes_uploaded_mb,
    s.bytes_downloaded_mb,
    s.apn
FROM silver.dh_ip_data s
INNER JOIN gold.dim_customer dc ON dc.customer_id = s.customer_id
WHERE s.session_id IS NOT NULL
ON CONFLICT (session_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    session_start = EXCLUDED.session_start,
    session_duration_min = EXCLUDED.session_duration_min,
    bytes_uploaded_mb = EXCLUDED.bytes_uploaded_mb,
    bytes_downloaded_mb = EXCLUDED.bytes_downloaded_mb,
    apn = EXCLUDED.apn;

INSERT INTO gold.fact_bandwidth_daily (
    usage_id, customer_key, usage_date, data_used_gb, peak_hour_gb, throttled_flag
)
SELECT
    u.usage_id,
    dc.customer_key,
    u.usage_date,
    u.data_used_gb,
    u.peak_hour_usage_gb,
    u.throttled_flag
FROM silver.dh_bandwidth_usage u
INNER JOIN gold.dim_customer dc ON dc.customer_id = u.customer_id
WHERE u.usage_id IS NOT NULL
ON CONFLICT (usage_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    usage_date = EXCLUDED.usage_date,
    data_used_gb = EXCLUDED.data_used_gb,
    peak_hour_gb = EXCLUDED.peak_hour_gb,
    throttled_flag = EXCLUDED.throttled_flag;

INSERT INTO gold.fact_qoe_measurement (
    qoe_id, customer_key, tower_key, measured_at,
    latency_ms, jitter_ms, packet_loss_pct, signal_strength_dbm, video_quality_score
)
SELECT
    q.qoe_id,
    dc.customer_key,
    dt.tower_key,
    q.timestamp,
    q.latency_ms,
    q.jitter_ms,
    q.packet_loss_pct,
    q.signal_strength_dbm,
    q.video_quality_score
FROM silver.dh_qoe_metrics q
INNER JOIN gold.dim_customer dc ON dc.customer_id = q.customer_id
LEFT JOIN gold.dim_tower dt ON dt.tower_id = q.tower_id
WHERE q.qoe_id IS NOT NULL
ON CONFLICT (qoe_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    tower_key = EXCLUDED.tower_key,
    measured_at = EXCLUDED.measured_at,
    latency_ms = EXCLUDED.latency_ms,
    jitter_ms = EXCLUDED.jitter_ms,
    packet_loss_pct = EXCLUDED.packet_loss_pct,
    signal_strength_dbm = EXCLUDED.signal_strength_dbm,
    video_quality_score = EXCLUDED.video_quality_score;

COMMIT;
