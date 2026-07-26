-- Gold schema DDL for Customer 360 (PostgreSQL)
-- Prerequisite: silver.dh_* tables populated. Run before ingest/*.sql

CREATE SCHEMA IF NOT EXISTS gold;

-- ---------- Calendar ----------

CREATE TABLE IF NOT EXISTS gold.dim_date (
    date_key        DATE PRIMARY KEY,
    day_of_week     SMALLINT NOT NULL,
    week_of_year    SMALLINT NOT NULL,
    month_num       SMALLINT NOT NULL,
    quarter_num     SMALLINT NOT NULL,
    year_num        SMALLINT NOT NULL,
    is_weekend      BOOLEAN NOT NULL
);

-- ---------- Core dimensions ----------

CREATE TABLE IF NOT EXISTS gold.dim_customer (
    customer_key          BIGSERIAL PRIMARY KEY,
    customer_id           TEXT NOT NULL UNIQUE,
    customer_segment      TEXT,
    account_status        TEXT,
    city                  TEXT,
    state                 TEXT,
    country               TEXT,
    zip_code              TEXT,
    registration_date     DATE,
    household_id          TEXT,
    household_size        INTEGER,
    household_income_band TEXT,
    current_churn_score   DOUBLE PRECISION,
    current_clv_usd       DOUBLE PRECISION,
    current_risk_segment  TEXT,
    current_clv_segment   TEXT,
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gold.dim_tower (
    tower_key       BIGSERIAL PRIMARY KEY,
    tower_id        TEXT NOT NULL UNIQUE,
    tower_name      TEXT,
    region          TEXT,
    tower_type      TEXT,
    capacity_status TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS gold.dim_channel (
    channel_key   BIGSERIAL PRIMARY KEY,
    channel_code  TEXT NOT NULL UNIQUE,
    channel_group TEXT
);

CREATE TABLE IF NOT EXISTS gold.dim_account (
    account_key         BIGSERIAL PRIMARY KEY,
    account_id          TEXT NOT NULL UNIQUE,
    customer_id         TEXT,
    parent_account_id   TEXT,
    account_type        TEXT,
    hierarchy_level     INTEGER
);

CREATE TABLE IF NOT EXISTS gold.dim_party (
    party_key                   BIGSERIAL PRIMARY KEY,
    party_id                    TEXT NOT NULL UNIQUE,
    customer_id                 TEXT,
    party_type                  TEXT,
    role                        TEXT,
    relationship_to_account     TEXT
);

CREATE TABLE IF NOT EXISTS gold.dim_contact (
    contact_key     BIGSERIAL PRIMARY KEY,
    contact_id      TEXT NOT NULL UNIQUE,
    customer_id     TEXT,
    contact_type    TEXT,
    contact_value   TEXT,
    is_primary      BOOLEAN,
    verified_flag   BOOLEAN
);

CREATE TABLE IF NOT EXISTS gold.dim_plan (
    plan_key          BIGSERIAL PRIMARY KEY,
    plan_id           TEXT NOT NULL UNIQUE,
    customer_id       TEXT,
    plan_name         TEXT,
    plan_type         TEXT,
    monthly_fee_usd   NUMERIC(18,2),
    data_allowance_gb NUMERIC(18,4),
    voice_minutes     NUMERIC(18,2),
    sms_allowance     NUMERIC(18,2),
    start_date        DATE
);

CREATE TABLE IF NOT EXISTS gold.dim_package (
    package_key       BIGSERIAL PRIMARY KEY,
    package_id        TEXT NOT NULL UNIQUE,
    customer_id       TEXT,
    package_name      TEXT,
    bundled_services  TEXT,
    discount_pct      DOUBLE PRECISION,
    start_date        DATE,
    end_date          DATE
);

CREATE TABLE IF NOT EXISTS gold.dim_contract (
    contract_key    BIGSERIAL PRIMARY KEY,
    contract_id     TEXT NOT NULL UNIQUE,
    customer_id     TEXT,
    contract_type   TEXT,
    start_date      DATE,
    term_months     INTEGER,
    end_date        DATE,
    auto_renew      TEXT,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS gold.dim_asset (
    asset_key     BIGSERIAL PRIMARY KEY,
    asset_id      TEXT NOT NULL UNIQUE,
    customer_id   TEXT,
    asset_type    TEXT,
    manufacturer  TEXT,
    install_date  DATE,
    status        TEXT
);

CREATE TABLE IF NOT EXISTS gold.dim_sim (
    sim_key         BIGSERIAL PRIMARY KEY,
    sim_id          TEXT NOT NULL UNIQUE,
    customer_id     TEXT,
    sim_type        TEXT,
    activation_date DATE,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS gold.dim_agent (
    agent_key   BIGSERIAL PRIMARY KEY,
    agent_id    TEXT NOT NULL UNIQUE,
    agent_role  TEXT
);

-- ---------- Facts: billing ----------

CREATE TABLE IF NOT EXISTS gold.fact_invoice (
    invoice_key     BIGSERIAL PRIMARY KEY,
    invoice_id      TEXT NOT NULL UNIQUE,
    customer_key    BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    invoice_date    DATE NOT NULL,
    due_date        DATE,
    amount_due      NUMERIC(18,2),
    tax_amount      NUMERIC(18,2),
    total_amount    NUMERIC(18,2) NOT NULL,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS gold.fact_payment (
    payment_key     BIGSERIAL PRIMARY KEY,
    payment_id      TEXT NOT NULL UNIQUE,
    customer_key    BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    invoice_id      TEXT,
    payment_date    DATE NOT NULL,
    amount_paid     NUMERIC(18,2) NOT NULL,
    payment_method  TEXT,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS gold.fact_ar_balance (
    ar_key               BIGSERIAL PRIMARY KEY,
    ar_id                TEXT NOT NULL UNIQUE,
    customer_key         BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    invoice_id           TEXT,
    outstanding_balance  NUMERIC(18,2),
    days_past_due        INTEGER,
    aging_bucket         TEXT
);

CREATE TABLE IF NOT EXISTS gold.fact_dunning_event (
    dunning_key     BIGSERIAL PRIMARY KEY,
    dunning_id      TEXT NOT NULL UNIQUE,
    customer_key    BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    invoice_id      TEXT,
    dunning_stage   TEXT,
    notice_date     DATE,
    action_taken    TEXT,
    resolved_flag   BOOLEAN
);

CREATE TABLE IF NOT EXISTS gold.fact_balance_snapshot (
    balance_key      BIGSERIAL PRIMARY KEY,
    balance_id       TEXT NOT NULL UNIQUE,
    customer_key     BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    balance_type     TEXT,
    current_balance  NUMERIC(18,2),
    last_updated     TIMESTAMPTZ
);

-- ---------- Facts: usage & network ----------

CREATE TABLE IF NOT EXISTS gold.fact_call_detail (
    cdr_key       BIGSERIAL PRIMARY KEY,
    cdr_id        TEXT NOT NULL UNIQUE,
    customer_key  BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    tower_key     BIGINT REFERENCES gold.dim_tower(tower_key),
    call_type     TEXT,
    call_start_ts TIMESTAMPTZ,
    duration_sec  NUMERIC(18,2)
);

CREATE TABLE IF NOT EXISTS gold.fact_ip_session (
    session_key           BIGSERIAL PRIMARY KEY,
    session_id            TEXT NOT NULL UNIQUE,
    customer_key          BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    session_start         TIMESTAMPTZ,
    session_duration_min  NUMERIC(18,2),
    bytes_uploaded_mb     NUMERIC(18,4),
    bytes_downloaded_mb   NUMERIC(18,4),
    apn                   TEXT
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

CREATE TABLE IF NOT EXISTS gold.fact_qoe_measurement (
    qoe_key             BIGSERIAL PRIMARY KEY,
    qoe_id              TEXT NOT NULL UNIQUE,
    customer_key        BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    tower_key           BIGINT REFERENCES gold.dim_tower(tower_key),
    measured_at         TIMESTAMPTZ,
    latency_ms          NUMERIC(18,4),
    jitter_ms           NUMERIC(18,4),
    packet_loss_pct     NUMERIC(18,4),
    signal_strength_dbm NUMERIC(18,4),
    video_quality_score TEXT
);

-- ---------- Facts: care ----------

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

CREATE TABLE IF NOT EXISTS gold.fact_support_case (
    case_key      BIGSERIAL PRIMARY KEY,
    case_id       TEXT NOT NULL UNIQUE,
    customer_key  BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    agent_key     BIGINT REFERENCES gold.dim_agent(agent_key),
    case_type     TEXT,
    opened_date   DATE,
    closed_date   DATE,
    status        TEXT,
    duration_days NUMERIC GENERATED ALWAYS AS (
        (closed_date - opened_date)::NUMERIC
    ) STORED
);

CREATE TABLE IF NOT EXISTS gold.fact_ivr_interaction (
    ivr_key              BIGSERIAL PRIMARY KEY,
    ivr_log_id           TEXT NOT NULL UNIQUE,
    customer_key         BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    call_ts              TIMESTAMPTZ,
    menu_path            TEXT,
    wait_time_sec        INTEGER,
    resolution_flag      BOOLEAN,
    transferred_to_agent BOOLEAN
);

CREATE TABLE IF NOT EXISTS gold.fact_store_visit (
    visit_key     BIGSERIAL PRIMARY KEY,
    visit_id      TEXT NOT NULL UNIQUE,
    customer_key  BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    store_id      TEXT,
    visit_ts      TIMESTAMPTZ,
    visit_reason  TEXT,
    duration_min  INTEGER,
    outcome       TEXT
);

CREATE TABLE IF NOT EXISTS gold.fact_field_dispatch (
    dispatch_key       BIGSERIAL PRIMARY KEY,
    dispatch_id        TEXT NOT NULL UNIQUE,
    customer_key       BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    agent_key          BIGINT REFERENCES gold.dim_agent(agent_key),
    dispatch_date      DATE,
    issue_type         TEXT,
    resolution_time_hr   NUMERIC(18,2),
    status             TEXT
);

-- ---------- Facts: analytics ----------

CREATE TABLE IF NOT EXISTS gold.fact_customer_score_snapshot (
    score_key        BIGSERIAL PRIMARY KEY,
    customer_key     BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    as_of_date       DATE NOT NULL,
    churn_score      DOUBLE PRECISION,
    risk_segment     TEXT,
    clv_estimate_usd DOUBLE PRECISION,
    clv_segment      TEXT,
    UNIQUE (customer_key, as_of_date)
);

CREATE TABLE IF NOT EXISTS gold.fact_propensity (
    propensity_key      BIGSERIAL PRIMARY KEY,
    customer_key        BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    product_recommended TEXT,
    propensity_score    DOUBLE PRECISION,
    score_ts            TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS gold.fact_sentiment (
    sentiment_key    BIGSERIAL PRIMARY KEY,
    customer_key     BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    interaction_id   TEXT,
    sentiment_score  DOUBLE PRECISION,
    sentiment_label  TEXT,
    source_channel   TEXT,
    analysis_date    DATE
);

CREATE TABLE IF NOT EXISTS gold.fact_nba_recommendation (
    nba_key             BIGSERIAL PRIMARY KEY,
    customer_key        BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    recommended_action  TEXT,
    action_type         TEXT,
    priority_rank       INTEGER,
    generated_date      DATE
);

-- ---------- Aggregate mart (optional serving layer) ----------

CREATE TABLE IF NOT EXISTS gold.agg_customer_360_daily (
    customer_key     BIGINT NOT NULL REFERENCES gold.dim_customer(customer_key),
    activity_date    DATE NOT NULL,
    invoice_total    NUMERIC(18,2) DEFAULT 0,
    payment_total    NUMERIC(18,2) DEFAULT 0,
    data_used_gb     NUMERIC(18,4) DEFAULT 0,
    ticket_count     INTEGER DEFAULT 0,
    call_count       INTEGER DEFAULT 0,
    PRIMARY KEY (customer_key, activity_date)
);

CREATE INDEX IF NOT EXISTS idx_fact_invoice_customer_date ON gold.fact_invoice (customer_key, invoice_date);
CREATE INDEX IF NOT EXISTS idx_fact_payment_date ON gold.fact_payment (payment_date);
CREATE INDEX IF NOT EXISTS idx_fact_bandwidth_date ON gold.fact_bandwidth_daily (usage_date);
CREATE INDEX IF NOT EXISTS idx_fact_call_start ON gold.fact_call_detail (call_start_ts);
CREATE INDEX IF NOT EXISTS idx_fact_qoe_measured ON gold.fact_qoe_measurement (measured_at);
