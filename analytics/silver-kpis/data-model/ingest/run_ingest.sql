-- Full silver → gold ingest (psql)
-- Usage from repo root:
--   psql "host=localhost dbname=datahive user=postgres" -v ON_ERROR_STOP=1 -f analytics/silver-kpis/data-model/ingest/run_ingest.sql

\set ON_ERROR_STOP on

\echo '=== Gold DDL ==='
\ir ../gold-layer-ddl.sql

\echo '=== Helpers ==='
\ir _helpers.sql

\echo '=== 01 Dimensions ==='
\ir 01_dimensions.sql

\echo '=== 02 Billing facts ==='
\ir 02_facts_billing.sql

\echo '=== 03 Usage & network facts ==='
\ir 03_facts_usage_network.sql

\echo '=== 04 Care facts ==='
\ir 04_facts_care.sql

\echo '=== 05 Analytics facts ==='
\ir 05_facts_analytics.sql

\echo '=== 06 Aggregates ==='
\ir 06_aggregates.sql

\echo '=== Gold ingest complete ==='
