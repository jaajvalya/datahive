# Silver schema — KPI catalog and analytics data model

This folder documents **Customer 360** analytics built from PostgreSQL schema **`silver`** in the `datahive` database (29 `dh_*` tables). Table definitions were captured from live catalog metadata via `postgres_store.table_structure()`.

## Contents

| File | Purpose |
|------|---------|
| [silver-schema-catalog.json](./silver-schema-catalog.json) | Machine-readable column-level catalog for all silver tables |
| [kpis/kpi-catalog.md](./kpis/kpi-catalog.md) | Exhaustive KPI list by domain pillar, with formulas and source columns |
| [data-model/dimensional-model.md](./data-model/dimensional-model.md) | Recommended gold-layer star schema (facts, dimensions, grains) |
| [data-model/entity-relationships.md](./data-model/entity-relationships.md) | Silver FK-style relationships and hub-and-spoke view |
| [data-model/gold-layer-ddl.sql](./data-model/gold-layer-ddl.sql) | Gold facts and dimensions DDL |
| [data-model/ingest/](./data-model/ingest/) | SQL scripts to load silver → gold (`run_ingest.sql`) |

## Silver table inventory (29)

**Identity & account:** `dh_customer`, `dh_party`, `dh_contact`, `dh_household`, `dh_account_hierarchy`

**Subscriptions & assets:** `dh_plan`, `dh_package`, `dh_installed_asset`, `dh_contract`, `dh_sim_esim`

**Usage & network:** `dh_tower`, `dh_cdr`, `dh_ip_data`, `dh_qoe_metrics`, `dh_bandwidth_usage`

**Billing & financials:** `dh_invoice`, `dh_payment`, `dh_ar_accounts_receivable`, `dh_balance`, `dh_dunning`

**Interactions & support:** `dh_ticket`, `dh_case`, `dh_ivr_log`, `dh_store_visit`, `dh_dispatch`

**Derived analytics:** `dh_churn_score`, `dh_clv`, `dh_propensity_to_buy`, `dh_sentiment`, `dh_next_best_action`

## How to use

1. Implement gold models in schema `gold` (see DDL starter) by joining silver on documented keys.
2. Map each dashboard metric to an entry in `kpis/kpi-catalog.md` (grain + SQL sketch).
3. Refresh `silver-schema-catalog.json` after schema changes:

```powershell
Set-Location c:\Users\raman\Projects\datahive
python -c "import json, postgres_store as ps; s=ps.asset_schemas()[0]; print(json.dumps({s:{t['name']:ps.table_structure(s,t['name']) for t in ps.list_tables(s)}}, indent=2, default=str))" | Out-File analytics\silver-kpis\silver-schema-catalog.json -Encoding utf8
```
