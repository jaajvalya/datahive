-- Derived analytics facts: scores, propensity, sentiment, NBA

BEGIN;

INSERT INTO gold.fact_customer_score_snapshot (
    customer_key, as_of_date, clv_estimate_usd, clv_segment
)
SELECT dc.customer_key, clv.calculation_date, clv.clv_estimate_usd, clv.clv_segment
FROM silver.dh_clv clv
INNER JOIN gold.dim_customer dc ON dc.customer_id = clv.customer_id
WHERE clv.calculation_date IS NOT NULL
ON CONFLICT (customer_key, as_of_date) DO UPDATE SET
    clv_estimate_usd = EXCLUDED.clv_estimate_usd,
    clv_segment = EXCLUDED.clv_segment;

INSERT INTO gold.fact_customer_score_snapshot (
    customer_key, as_of_date, churn_score, risk_segment
)
SELECT dc.customer_key, cs.score_date, cs.churn_score, cs.risk_segment
FROM silver.dh_churn_score cs
INNER JOIN gold.dim_customer dc ON dc.customer_id = cs.customer_id
WHERE cs.score_date IS NOT NULL
ON CONFLICT (customer_key, as_of_date) DO UPDATE SET
    churn_score = EXCLUDED.churn_score,
    risk_segment = EXCLUDED.risk_segment;

-- Propensity (append-style; truncate for idempotent full reload)
TRUNCATE gold.fact_propensity;

INSERT INTO gold.fact_propensity (customer_key, product_recommended, propensity_score, score_ts)
SELECT
    dc.customer_key,
    p.product_recommended,
    p.propensity_score,
    p.score_date
FROM silver.dh_propensity_to_buy p
INNER JOIN gold.dim_customer dc ON dc.customer_id = p.customer_id;

TRUNCATE gold.fact_sentiment;

INSERT INTO gold.fact_sentiment (
    customer_key, interaction_id, sentiment_score, sentiment_label, source_channel, analysis_date
)
SELECT
    dc.customer_key,
    s.interaction_id,
    s.sentiment_score,
    s.sentiment_label,
    s.source_channel,
    s.analysis_date
FROM silver.dh_sentiment s
INNER JOIN gold.dim_customer dc ON dc.customer_id = s.customer_id;

TRUNCATE gold.fact_nba_recommendation;

INSERT INTO gold.fact_nba_recommendation (
    customer_key, recommended_action, action_type, priority_rank, generated_date
)
SELECT
    dc.customer_key,
    n.recommended_action,
    n.action_type,
    n.priority_rank,
    n.generated_date
FROM silver.dh_next_best_action n
INNER JOIN gold.dim_customer dc ON dc.customer_id = n.customer_id;

COMMIT;
