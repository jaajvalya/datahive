-- Starter gold schema for Customer 360 KPIs (PostgreSQL)
-- Run after silver.dh_* is populated. Adjust types/indexes for production.

CREATE SCHEMA IF NOT EXISTS gold;

-- ---------- Dimensions (subset; extend per dimensional-model.md) ----------

CREATE TABLE IF NOT EXISTS gold.dim_date (
    date_key        DATE PRIMARY KEY,
    day_of_week     SMALLINT NOT NULL,
    week_of_year    SMALLINT NOT NULL,
    month_num       SMALLINT NOT NULL,
    quarter_num     SMALLINT NOT NULL,
    year_num        SMALLINT NOT NULL,
    is_weekend      BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS gold.dim_customer (
    customer_key        BIGSERIAL PRIMARY KEY,
    customer_id         TEXT NOT NULL UNIQUE,
    customer_segment    TEXT,
    account_status      TEXT,
    city                TEXT,
    state               TEXT,
    country             TEXT,
    registration_date   DATE,
    household_id        TEXT,
    household_income_band TEXT,
    current_churn_score DOUBLE PRECISION,
    current_clv_usd     DOUBLE PRECISION,
    current_risk_segment TEXT,
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gold.dim_tower (
    tower_key       BIGSERIAL PRIMARY KEY,
    tower_id        TEXT NOT NULL UNIQUE,
    region          TEXT,
    tower_type      TEXT,
    capacity_status TEXT
);

CREATE TABLE IF NOT EXISTS gold.dim_channel (
    channel_key   BIGSERIAL PRIMARY KEY,
    channel_code  TEXT NOT NULL UNIQUE,
    channel_group TEXT  -- care | retail | digital | field
);

-- ---------- Facts ----------

CREATE TABLE IF NOT EXISTS gold.fact_invoice (
    invoice_key     BIGSERIAL PRIMARY KEY,
    invoice_id      TEXT NOT NULL,
    customer_key    BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    invoice_date    DATE NOT NULL,
    due_date        DATE,
    amount_due      NUMERIC(18,2),
    tax_amount      NUMERIC(18,2),
    total_amount    NUMERIC(18,2) NOT NULL,
    status          TEXT,
    UNIQUE (invoice_id)
);

CREATE TABLE IF NOT EXISTS gold.fact_payment (
    payment_key     BIGSERIAL PRIMARY KEY,
    payment_id      TEXT NOT NULL,
    customer_key    BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    invoice_id      TEXT,
    payment_date    DATE NOT NULL,
    amount_paid     NUMERIC(18,2) NOT NULL,
    payment_method  TEXT,
    status          TEXT,
    UNIQUE (payment_id)
);

CREATE TABLE IF NOT EXISTS gold.fact_bandwidth_daily (
    usage_key       BIGSERIAL PRIMARY KEY,
    usage_id        TEXT NOT NULL UNIQUE,
    customer_key    BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    usage_date      DATE NOT NULL,
    data_used_gb    NUMERIC(18,4),
    peak_hour_gb    NUMERIC(18,4),
    throttled_flag  BOOLEAN
);

CREATE TABLE IF NOT EXISTS gold.fact_support_ticket (
    ticket_key      BIGSERIAL PRIMARY KEY,
    ticket_id       TEXT NOT NULL UNIQUE,
    customer_key    BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    created_ts      TIMESTAMPTZ NOT NULL,
    resolved_ts     TIMESTAMPTZ,
    ticket_type     TEXT,
    priority        TEXT,
    status          TEXT,
    channel         TEXT,
    resolution_min  NUMERIC GENERATED ALWAYS AS (
        EXTRACT(EPOCH FROM (resolved_ts - created_ts)) / 60.0
    ) STORED
);

CREATE TABLE IF NOT EXISTS gold.fact_customer_score_snapshot (
    score_key       BIGSERIAL PRIMARY KEY,
    customer_key    BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    as_of_date      DATE NOT NULL,
    churn_score     DOUBLE PRECISION,
    risk_segment    TEXT,
    clv_estimate_usd DOUBLE PRECISION,
    clv_segment     TEXT,
    UNIQUE (customer_key, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_fact_invoice_customer_date ON gold.fact_invoice (customer_key, invoice_date);
CREATE INDEX IF NOT EXISTS idx_fact_payment_date ON gold.fact_payment (payment_date);
CREATE INDEX IF NOT EXISTS idx_fact_bandwidth_date ON gold.fact_bandwidth_daily (usage_date);

-- Example ETL sketch (run as dbt model or scheduled job):
-- INSERT INTO gold.dim_customer (customer_id, ...)
-- SELECT customer_id, ... FROM silver.dh_customer
-- ON CONFLICT (customer_id) DO UPDATE SET ...;
