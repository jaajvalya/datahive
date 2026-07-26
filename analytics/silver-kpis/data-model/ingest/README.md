# Silver → gold ingest scripts

PostgreSQL scripts load **`silver.dh_*`** into **`gold`** objects defined in [../gold-layer-ddl.sql](../gold-layer-ddl.sql), following [../dimensional-model.md](../dimensional-model.md).

## Run order

| Step | File | Purpose |
|------|------|---------|
| 0 | `../gold-layer-ddl.sql` | Create/extend gold tables |
| 1 | `_helpers.sql` | `safe_numeric`, `safe_bool` helpers |
| 2 | `01_dimensions.sql` | `dim_date`, `dim_tower`, `dim_channel`, `dim_customer`, product & agent dims |
| 3 | `02_facts_billing.sql` | Invoice, payment, AR, dunning, balance |
| 4 | `03_facts_usage_network.sql` | CDR, IP, bandwidth, QoE |
| 5 | `04_facts_care.sql` | Ticket, case, IVR, store, dispatch |
| 6 | `05_facts_analytics.sql` | Score snapshots, propensity, sentiment, NBA |
| 7 | `06_aggregates.sql` | `agg_customer_360_daily` |

## One-command load

From the **ingest** directory (so `\ir` paths resolve):

```powershell
Set-Location c:\Users\raman\Projects\datahive\analytics\silver-kpis\data-model\ingest
psql "host=localhost port=5432 dbname=datahive user=postgres" -f run_ingest.sql
```

Or run files individually in the order above.

## Load semantics

- **Dimensions:** `INSERT … ON CONFLICT DO UPDATE` (Type 1 upsert).
- **Most facts:** upsert on natural key (`invoice_id`, `cdr_id`, etc.).
- **Analytics facts without stable keys:** `TRUNCATE` then `INSERT` (`fact_propensity`, `fact_sentiment`, `fact_nba_recommendation`).
- **Aggregates:** full rebuild of `agg_customer_360_daily` each run.
- Rows whose `customer_id` is missing from `dim_customer` are **skipped** on facts (inner join).

## Prerequisites

- `silver` schema populated (e.g. synthetic CSV load into `dh_*` tables).
- PostgreSQL client (`psql`) with rights to create objects in schema `gold`.

## Optional: SQL only (no psql meta)

Execute in order: DDL → helpers → `01` … `06` using any SQL client that supports multi-statement scripts.
