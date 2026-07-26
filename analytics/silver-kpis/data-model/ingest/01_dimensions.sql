-- Dimensions: load from silver (upsert). Run after gold-layer-ddl.sql and _helpers.sql

BEGIN;

-- Calendar: span observed silver dates ± 1 year padding
INSERT INTO gold.dim_date (date_key, day_of_week, week_of_year, month_num, quarter_num, year_num, is_weekend)
SELECT d::DATE,
       EXTRACT(ISODOW FROM d)::SMALLINT,
       EXTRACT(WEEK FROM d)::SMALLINT,
       EXTRACT(MONTH FROM d)::SMALLINT,
       EXTRACT(QUARTER FROM d)::SMALLINT,
       EXTRACT(YEAR FROM d)::SMALLINT,
       EXTRACT(ISODOW FROM d) IN (6, 7)
FROM generate_series(
    (SELECT COALESCE(MIN(dt), DATE '2020-01-01') FROM (
        SELECT registration_date AS dt FROM silver.dh_customer WHERE registration_date IS NOT NULL
        UNION ALL SELECT invoice_date FROM silver.dh_invoice WHERE invoice_date IS NOT NULL
        UNION ALL SELECT usage_date FROM silver.dh_bandwidth_usage WHERE usage_date IS NOT NULL
        UNION ALL SELECT payment_date FROM silver.dh_payment WHERE payment_date IS NOT NULL
    ) s) - INTERVAL '365 days',
    (SELECT COALESCE(MAX(dt), CURRENT_DATE) FROM (
        SELECT registration_date AS dt FROM silver.dh_customer WHERE registration_date IS NOT NULL
        UNION ALL SELECT invoice_date FROM silver.dh_invoice WHERE invoice_date IS NOT NULL
        UNION ALL SELECT usage_date FROM silver.dh_bandwidth_usage WHERE usage_date IS NOT NULL
        UNION ALL SELECT payment_date FROM silver.dh_payment WHERE payment_date IS NOT NULL
    ) s) + INTERVAL '365 days',
    INTERVAL '1 day'
) AS g(d)
ON CONFLICT (date_key) DO NOTHING;

-- Towers
INSERT INTO gold.dim_tower (tower_id, tower_name, region, tower_type, capacity_status, latitude, longitude)
SELECT tower_id, tower_name, region, tower_type, capacity_status, latitude, longitude
FROM silver.dh_tower
WHERE tower_id IS NOT NULL
ON CONFLICT (tower_id) DO UPDATE SET
    tower_name = EXCLUDED.tower_name,
    region = EXCLUDED.region,
    tower_type = EXCLUDED.tower_type,
    capacity_status = EXCLUDED.capacity_status,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude;

-- Channels from tickets, sentiment, and fixed care channels
INSERT INTO gold.dim_channel (channel_code, channel_group)
SELECT DISTINCT channel_code, channel_group
FROM (
    SELECT DISTINCT channel AS channel_code, 'care'::TEXT AS channel_group
    FROM silver.dh_ticket WHERE channel IS NOT NULL AND TRIM(channel) <> ''
    UNION
    SELECT DISTINCT source_channel, 'care'
    FROM silver.dh_sentiment WHERE source_channel IS NOT NULL AND TRIM(source_channel) <> ''
    UNION
    SELECT 'IVR', 'care'
    UNION
    SELECT 'Store', 'retail'
    UNION
    SELECT 'Field', 'field'
) ch
ON CONFLICT (channel_code) DO UPDATE SET channel_group = EXCLUDED.channel_group;

-- Customers (+ household + latest churn/clv)
INSERT INTO gold.dim_customer (
    customer_id, customer_segment, account_status, city, state, country, zip_code,
    registration_date, household_id, household_size, household_income_band,
    current_churn_score, current_clv_usd, current_risk_segment, current_clv_segment, updated_at
)
SELECT
    c.customer_id,
    c.customer_segment,
    c.account_status,
    c.city,
    c.state,
    c.country,
    c.zip_code,
    c.registration_date,
    h.household_id,
    h.household_size,
    h.household_income_band,
    cs.churn_score,
    clv.clv_estimate_usd,
    cs.risk_segment,
    clv.clv_segment,
    NOW()
FROM silver.dh_customer c
LEFT JOIN silver.dh_household h ON h.primary_customer_id = c.customer_id
LEFT JOIN LATERAL (
    SELECT churn_score, risk_segment
    FROM silver.dh_churn_score s
    WHERE s.customer_id = c.customer_id
    ORDER BY score_date DESC NULLS LAST
    LIMIT 1
) cs ON TRUE
LEFT JOIN LATERAL (
    SELECT clv_estimate_usd, clv_segment
    FROM silver.dh_clv v
    WHERE v.customer_id = c.customer_id
    ORDER BY calculation_date DESC NULLS LAST
    LIMIT 1
) clv ON TRUE
WHERE c.customer_id IS NOT NULL
ON CONFLICT (customer_id) DO UPDATE SET
    customer_segment = EXCLUDED.customer_segment,
    account_status = EXCLUDED.account_status,
    city = EXCLUDED.city,
    state = EXCLUDED.state,
    country = EXCLUDED.country,
    zip_code = EXCLUDED.zip_code,
    registration_date = EXCLUDED.registration_date,
    household_id = EXCLUDED.household_id,
    household_size = EXCLUDED.household_size,
    household_income_band = EXCLUDED.household_income_band,
    current_churn_score = EXCLUDED.current_churn_score,
    current_clv_usd = EXCLUDED.current_clv_usd,
    current_risk_segment = EXCLUDED.current_risk_segment,
    current_clv_segment = EXCLUDED.current_clv_segment,
    updated_at = NOW();

-- Account hierarchy
INSERT INTO gold.dim_account (account_id, customer_id, parent_account_id, account_type, hierarchy_level)
SELECT account_id, customer_id, parent_account_id, account_type, hierarchy_level
FROM silver.dh_account_hierarchy
WHERE account_id IS NOT NULL
ON CONFLICT (account_id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    parent_account_id = EXCLUDED.parent_account_id,
    account_type = EXCLUDED.account_type,
    hierarchy_level = EXCLUDED.hierarchy_level;

INSERT INTO gold.dim_party (party_id, customer_id, party_type, role, relationship_to_account)
SELECT party_id, customer_id, party_type, role, relationship_to_account
FROM silver.dh_party
WHERE party_id IS NOT NULL
ON CONFLICT (party_id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    party_type = EXCLUDED.party_type,
    role = EXCLUDED.role,
    relationship_to_account = EXCLUDED.relationship_to_account;

INSERT INTO gold.dim_contact (contact_id, customer_id, contact_type, contact_value, is_primary, verified_flag)
SELECT
    contact_id, customer_id, contact_type, contact_value,
    gold.safe_bool(is_primary),
    gold.safe_bool(verified_flag)
FROM silver.dh_contact
WHERE contact_id IS NOT NULL
ON CONFLICT (contact_id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    contact_type = EXCLUDED.contact_type,
    contact_value = EXCLUDED.contact_value,
    is_primary = EXCLUDED.is_primary,
    verified_flag = EXCLUDED.verified_flag;

-- Product dimensions
INSERT INTO gold.dim_plan (
    plan_id, customer_id, plan_name, plan_type, monthly_fee_usd,
    data_allowance_gb, voice_minutes, sms_allowance, start_date
)
SELECT
    plan_id, customer_id, plan_name, plan_type,
    gold.safe_numeric(monthly_fee_usd),
    gold.safe_numeric(data_allowance_gb),
    gold.safe_numeric(voice_minutes),
    gold.safe_numeric(sms_allowance),
    start_date
FROM silver.dh_plan
WHERE plan_id IS NOT NULL
ON CONFLICT (plan_id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    plan_name = EXCLUDED.plan_name,
    plan_type = EXCLUDED.plan_type,
    monthly_fee_usd = EXCLUDED.monthly_fee_usd,
    data_allowance_gb = EXCLUDED.data_allowance_gb,
    voice_minutes = EXCLUDED.voice_minutes,
    sms_allowance = EXCLUDED.sms_allowance,
    start_date = EXCLUDED.start_date;

INSERT INTO gold.dim_package (
    package_id, customer_id, package_name, bundled_services, discount_pct, start_date, end_date
)
SELECT package_id, customer_id, package_name, bundled_services, discount_pct, start_date, end_date
FROM silver.dh_package
WHERE package_id IS NOT NULL
ON CONFLICT (package_id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    package_name = EXCLUDED.package_name,
    bundled_services = EXCLUDED.bundled_services,
    discount_pct = EXCLUDED.discount_pct,
    start_date = EXCLUDED.start_date,
    end_date = EXCLUDED.end_date;

INSERT INTO gold.dim_contract (
    contract_id, customer_id, contract_type, start_date, term_months, end_date, auto_renew, status
)
SELECT
    contract_id, customer_id, contract_type,
    CASE WHEN start_date ~ '^\d{4}-\d{2}-\d{2}$' THEN start_date::DATE ELSE NULL END,
    term_months, end_date, auto_renew, status
FROM silver.dh_contract
WHERE contract_id IS NOT NULL
ON CONFLICT (contract_id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    contract_type = EXCLUDED.contract_type,
    start_date = EXCLUDED.start_date,
    term_months = EXCLUDED.term_months,
    end_date = EXCLUDED.end_date,
    auto_renew = EXCLUDED.auto_renew,
    status = EXCLUDED.status;

INSERT INTO gold.dim_asset (asset_id, customer_id, asset_type, manufacturer, install_date, status)
SELECT asset_id, customer_id, asset_type, manufacturer, install_date, status
FROM silver.dh_installed_asset
WHERE asset_id IS NOT NULL
ON CONFLICT (asset_id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    asset_type = EXCLUDED.asset_type,
    manufacturer = EXCLUDED.manufacturer,
    install_date = EXCLUDED.install_date,
    status = EXCLUDED.status;

INSERT INTO gold.dim_sim (sim_id, customer_id, sim_type, activation_date, status)
SELECT sim_id, customer_id, sim_type, activation_date, status
FROM silver.dh_sim_esim
WHERE sim_id IS NOT NULL
ON CONFLICT (sim_id) DO UPDATE SET
    customer_id = EXCLUDED.customer_id,
    sim_type = EXCLUDED.sim_type,
    activation_date = EXCLUDED.activation_date,
    status = EXCLUDED.status;

-- Agents from cases and field technicians
INSERT INTO gold.dim_agent (agent_id, agent_role)
SELECT agent_id, agent_role FROM (
    SELECT DISTINCT assigned_agent AS agent_id, 'case_agent'::TEXT AS agent_role
    FROM silver.dh_case WHERE assigned_agent IS NOT NULL AND TRIM(assigned_agent) <> ''
    UNION
    SELECT DISTINCT technician_id, 'field_technician'
    FROM silver.dh_dispatch WHERE technician_id IS NOT NULL AND TRIM(technician_id) <> ''
) a
ON CONFLICT (agent_id) DO UPDATE SET agent_role = EXCLUDED.agent_role;

COMMIT;
