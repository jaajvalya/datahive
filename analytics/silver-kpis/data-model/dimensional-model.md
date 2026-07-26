# Gold-layer dimensional data model

Target schema: **`gold`** (serving layer). Source: **`silver.dh_*`**. Pattern: **customer-centric star schema** with optional snapshot facts for balances and scores.

## Design principles

- **Grain first:** each fact table has one clear row grain; never mix grains in one fact without a bridge.
- **Conformed dimensions:** `dim_customer`, `dim_date`, `dim_geography`, `dim_product`, `dim_channel` reused across facts.
- **Degenerate dimensions:** keep natural keys (`invoice_id`, `ticket_id`, `cdr_id`) on facts for drill-through to silver.
- **SCD:** Type 1 for most telecom attributes; Type 2 only where history is required (e.g. plan changes) using `dh_plan.start_date` / contract dates.
- **Casting:** silver stores some measures as `text` (`duration_sec`, `monthly_fee_usd`, `data_allowance_gb`); cast in gold ETL.

## Dimensions

| Gold object | Grain | Primary silver sources | Key attributes |
|-------------|-------|------------------------|----------------|
| `dim_customer` | One row per `customer_id` | `dh_customer`, `dh_household`, latest scores | segment, account_status, registration_date, DOB-derived age band, household_size, income_band |
| `dim_account` | One row per `account_id` | `dh_account_hierarchy` | account_type, hierarchy_level, parent_account_id |
| `dim_party` | One row per `party_id` | `dh_party` | party_type, role, relationship_to_account |
| `dim_contact` | One row per `contact_id` | `dh_contact` | contact_type, is_primary, verified_flag |
| `dim_geography` | Surrogate `geo_key` | `dh_customer`, `dh_tower`, `dh_household` | city, state, zip, country, tower region |
| `dim_date` | Calendar date | Generated | day, week, month, quarter, fiscal flags |
| `dim_time` | Time of day | From timestamps on CDR, IP, QoE, tickets | hour, peak/off-peak band |
| `dim_plan` | One row per `plan_id` | `dh_plan` | plan_name, plan_type, monthly_fee_usd, allowances |
| `dim_package` | One row per `package_id` | `dh_package` | package_name, bundled_services, discount_pct |
| `dim_contract` | One row per `contract_id` | `dh_contract` | contract_type, term_months, status, auto_renew |
| `dim_asset` | One row per `asset_id` | `dh_installed_asset` | asset_type, manufacturer, status |
| `dim_sim` | One row per `sim_id` | `dh_sim_esim` | sim_type, status, activation_date |
| `dim_tower` | One row per `tower_id` | `dh_tower` | region, tower_type, capacity_status, lat/long |
| `dim_channel` | One row per channel code | Distinct from ticket, IVR, sentiment | channel name, care vs sales |
| `dim_agent` | One row per agent/technician id | `dh_case`, `dh_dispatch` | assigned_agent, technician_id |
| `dim_invoice_status` | Status code | `dh_invoice.status` | paid, open, written off, etc. |
| `dim_aging_bucket` | Bucket label | `dh_ar_accounts_receivable.aging_bucket` | 0–30, 31–60, … |
| `dim_dunning_stage` | Stage | `dh_dunning.dunning_stage` | stage order for funnel |
| `dim_product_recommendation` | Product | `dh_propensity_to_buy.product_recommended` | for upsell analytics |
| `dim_nba_action` | Action | `dh_next_best_action` | recommended_action, action_type |

## Fact tables

| Gold object | Grain | Measures | Dimension FKs |
|-------------|-------|----------|---------------|
| `fact_subscription_event` | Plan/package/contract/SIM change event (one row per entity version or start_date) | monthly_fee, discount_pct, term_months | customer, plan, package, contract, sim, date |
| `fact_call_detail` | One row per `cdr_id` | duration_sec, call count = 1 | customer, date, time, tower, geography |
| `fact_ip_session` | One row per `session_id` | bytes up/down, session duration | customer, date, time |
| `fact_bandwidth_daily` | One row per `usage_id` (customer + usage_date) | data_used_gb, peak_hour_usage_gb, throttled_flag | customer, date |
| `fact_qoe_measurement` | One row per `qoe_id` | latency_ms, jitter_ms, packet_loss_pct, signal_strength_dbm | customer, tower, date, time |
| `fact_invoice` | One row per `invoice_id` | amount_due, tax_amount, total_amount | customer, date, invoice_status |
| `fact_payment` | One row per `payment_id` | amount_paid | customer, invoice (deg), date, payment_method |
| `fact_ar_balance` | One row per `ar_id` or snapshot by invoice | outstanding_balance, days_past_due | customer, invoice, aging_bucket, date |
| `fact_dunning_event` | One row per `dunning_id` | resolved_flag | customer, invoice, dunning_stage, date |
| `fact_balance_snapshot` | One row per `balance_id` | current_balance | customer, balance_type, date (last_updated) |
| `fact_support_ticket` | One row per `ticket_id` | resolution time (resolved − created) | customer, channel, date, priority |
| `fact_support_case` | One row per `case_id` | case duration (closed − opened) | customer, agent, date, case_type |
| `fact_ivr_interaction` | One row per `ivr_log_id` | wait_time_sec, resolution_flag, transferred | customer, date, channel |
| `fact_store_visit` | One row per `visit_id` | duration_min | customer, store, date, visit_reason |
| `fact_field_dispatch` | One row per `dispatch_id` | resolution_time_hr | customer, technician, date, issue_type |
| `fact_customer_score_snapshot` | One row per customer + score_type + as-of date | churn_score, clv_estimate_usd, propensity_score, sentiment_score | customer, date, risk_segment, clv_segment |
| `fact_nba_recommendation` | One row per customer + generated_date + priority_rank | priority_rank | customer, nba_action, date |

## Aggregates (optional gold marts)

Pre-compute for BI performance:

| Mart | Grain | Typical use |
|------|-------|-------------|
| `agg_customer_360_daily` | customer + date | Single row per active customer per day with usage, revenue, care touches |
| `agg_revenue_monthly` | month + segment + region | Executive revenue |
| `agg_network_health_daily` | date + tower/region | NOC dashboards |
| `agg_care_queue_daily` | date + channel + priority | Operations |
| `agg_collections_weekly` | week + aging_bucket | Finance |

## ETL dependency order

1. Dimensions: date, geography, tower, channel, status buckets  
2. `dim_customer` (+ household join)  
3. Product dimensions: plan, package, contract, asset, sim  
4. Transaction facts: invoice → payment → AR → dunning  
5. Usage facts: CDR, IP, bandwidth, QoE  
6. Care facts: ticket, case, IVR, store, dispatch  
7. Score snapshot fact (latest or full history from silver)  
8. Aggregates  

## Sample join path (customer 360 daily)

```
dim_customer c
LEFT JOIN fact_invoice i ON i.customer_key = c.customer_key AND i.invoice_date = :d
LEFT JOIN fact_payment p ON p.customer_key = c.customer_key AND p.payment_date = :d
LEFT JOIN fact_bandwidth_daily b ON b.customer_key = c.customer_key AND b.usage_date = :d
LEFT JOIN fact_support_ticket t ON t.customer_key = c.customer_key AND DATE(t.created_ts) = :d
LEFT JOIN fact_customer_score_snapshot s ON s.customer_key = c.customer_key AND s.score_date = :d
```

See [gold-layer-ddl.sql](./gold-layer-ddl.sql) for table definitions and [ingest/run_ingest.sql](./ingest/run_ingest.sql) to load from silver.
