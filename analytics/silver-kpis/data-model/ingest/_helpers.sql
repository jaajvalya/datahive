-- Shared helpers for silver → gold ingest (PostgreSQL)
-- Included by run_ingest.sql via \i

CREATE SCHEMA IF NOT EXISTS gold;

-- Cast silver text numerics safely
CREATE OR REPLACE FUNCTION gold.safe_numeric(p_text TEXT)
RETURNS NUMERIC
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_text IS NULL OR TRIM(p_text) = '' THEN NULL
        WHEN TRIM(p_text) ~ '^-?[0-9]+(\.[0-9]+)?$' THEN TRIM(p_text)::NUMERIC
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION gold.safe_bool(p_text TEXT)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_text IS NULL THEN NULL
        WHEN UPPER(TRIM(p_text)) IN ('Y', 'YES', 'TRUE', 'T', '1') THEN TRUE
        WHEN UPPER(TRIM(p_text)) IN ('N', 'NO', 'FALSE', 'F', '0') THEN FALSE
        ELSE NULL
    END;
$$;

-- Resolve customer_key; rows with unknown customer_id are skipped in facts via INNER JOIN
CREATE OR REPLACE VIEW gold.v_customer_lookup AS
SELECT customer_key, customer_id
FROM gold.dim_customer;
