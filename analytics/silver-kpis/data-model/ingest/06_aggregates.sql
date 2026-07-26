-- Daily customer 360 aggregate (rebuilt each run)

BEGIN;

TRUNCATE gold.agg_customer_360_daily;

WITH activity_dates AS (
    SELECT customer_key, invoice_date AS activity_date, total_amount AS invoice_total, 0::NUMERIC AS payment_total,
           0::NUMERIC AS data_gb, 0 AS tickets, 0 AS calls
    FROM gold.fact_invoice
    UNION ALL
    SELECT customer_key, payment_date, 0, amount_paid, 0, 0, 0
    FROM gold.fact_payment
    UNION ALL
    SELECT customer_key, usage_date, 0, 0, data_used_gb, 0, 0
    FROM gold.fact_bandwidth_daily
    UNION ALL
    SELECT customer_key, created_ts::DATE, 0, 0, 0, 1, 0
    FROM gold.fact_support_ticket
    UNION ALL
    SELECT customer_key, call_start_ts::DATE, 0, 0, 0, 0, 1
    FROM gold.fact_call_detail
    WHERE call_start_ts IS NOT NULL
)
INSERT INTO gold.agg_customer_360_daily (
    customer_key, activity_date, invoice_total, payment_total, data_used_gb, ticket_count, call_count
)
SELECT
    customer_key,
    activity_date,
    COALESCE(SUM(invoice_total), 0),
    COALESCE(SUM(payment_total), 0),
    COALESCE(SUM(data_gb), 0),
    COALESCE(SUM(tickets), 0)::INTEGER,
    COALESCE(SUM(calls), 0)::INTEGER
FROM activity_dates
WHERE activity_date IS NOT NULL
GROUP BY customer_key, activity_date;

COMMIT;
