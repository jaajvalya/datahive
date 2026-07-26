# KPI catalog (silver → gold)

Each KPI lists **definition**, **formula**, **grain**, **silver sources**, and **recommended gold object**. Dimensions in brackets are typical slice/dice attributes.

Notation: `COUNT(DISTINCT x)`, `SUM`, `AVG`, `SAFE_DIV(a,b)` = divide with zero guard.

---

## 1. Identity & account (`dh_customer`, `dh_party`, `dh_contact`, `dh_household`, `dh_account_hierarchy`)

| KPI ID | Name | Formula | Grain | Silver tables | Gold |
|--------|------|---------|-------|---------------|------|
| ID-01 | Total customers | `COUNT(DISTINCT customer_id)` | All time / as-of | `dh_customer` | `dim_customer` |
| ID-02 | Active customers | `COUNT(DISTINCT customer_id) WHERE account_status = 'Active'` | Daily snapshot | `dh_customer` | `dim_customer` |
| ID-03 | New registrations | `COUNT(DISTINCT customer_id)` | Day/week/month | `dh_customer.registration_date` | `dim_customer`, `dim_date` |
| ID-04 | Registration growth rate | `(new_this_period - new_prior) / new_prior` | Period | `dh_customer` | mart |
| ID-05 | Customers by segment | `COUNT(DISTINCT customer_id)` | Segment | `customer_segment` | `dim_customer` |
| ID-06 | Customers by account status | `COUNT(DISTINCT customer_id)` | Status | `account_status` | `dim_customer` |
| ID-07 | Geographic distribution | `COUNT(DISTINCT customer_id)` | city, state, country | `dh_customer` | `dim_geography` |
| ID-08 | Average customer age | `AVG(CURRENT_DATE - date_of_birth)` | Segment/geo | `dh_customer` | `dim_customer` |
| ID-09 | Gender mix | `% customers by gender` | Segment | `dh_customer.gender` | `dim_customer` |
| ID-10 | Household count | `COUNT(DISTINCT household_id)` | Geo | `dh_household` | dim |
| ID-11 | Avg household size | `AVG(household_size)` | Income band | `dh_household` | dim |
| ID-12 | Customers by income band | `COUNT(DISTINCT primary_customer_id)` | Band | `household_income_band` | join household |
| ID-13 | Multi-account customers | `COUNT(DISTINCT customer_id) HAVING COUNT(account_id) > 1` | Customer | `dh_account_hierarchy` | `dim_account` |
| ID-14 | Enterprise account depth | `AVG(MAX(hierarchy_level))` | account_type | `dh_account_hierarchy` | `dim_account` |
| ID-15 | Party roles per customer | `AVG(COUNT(party_id))` | Customer | `dh_party` | `dim_party` |
| ID-16 | Contact points per customer | `AVG(COUNT(contact_id))` | Customer | `dh_contact` | `dim_contact` |
| ID-17 | Primary contact coverage | `% customers with is_primary contact` | Segment | `dh_contact` | dim |
| ID-18 | Verified contact rate | `% contacts WHERE verified_flag` | contact_type | `dh_contact` | dim |
| ID-19 | Email completeness | `% customers with non-null email` | Segment | `dh_customer` | dim |
| ID-20 | Account hierarchy orphan rate | `% accounts with null parent where level > 1` | Type | `dh_account_hierarchy` | QA |

---

## 2. Subscriptions & assets (`dh_plan`, `dh_package`, `dh_contract`, `dh_installed_asset`, `dh_sim_esim`)

| KPI ID | Name | Formula | Grain | Silver | Gold |
|--------|------|---------|-------|--------|------|
| SUB-01 | Active plans | `COUNT(DISTINCT plan_id)` | Plan type | `dh_plan` | `dim_plan` |
| SUB-02 | Plans per customer | `AVG(COUNT(plan_id))` | Segment | `dh_plan` | fact_subscription |
| SUB-03 | ARPU (plan list) | `AVG(CAST(monthly_fee_usd AS NUMERIC))` | plan_type, segment | `dh_plan` | fact + customer |
| SUB-04 | MRR (estimated) | `SUM(CAST(monthly_fee_usd AS NUMERIC))` | Month | `dh_plan` | aggregate |
| SUB-05 | Plan type mix | `% plans by plan_type` | Month | `dh_plan` | dim |
| SUB-06 | Data allowance distribution | `AVG(CAST(data_allowance_gb AS NUMERIC))` | plan_name | `dh_plan` | dim |
| SUB-07 | Active packages | `COUNT(DISTINCT package_id) WHERE end_date IS NULL OR end_date >= CURRENT_DATE` | Day | `dh_package` | `dim_package` |
| SUB-08 | Bundle adoption rate | `% customers with ≥1 package` | Segment | `dh_package` | fact |
| SUB-09 | Average bundle discount | `AVG(discount_pct)` | package_name | `dh_package` | dim |
| SUB-10 | Package churn | `COUNT WHERE end_date in period` | Month | `dh_package` | fact |
| SUB-11 | Active contracts | `COUNT(DISTINCT contract_id) WHERE status = 'Active'` | Day | `dh_contract` | `dim_contract` |
| SUB-12 | Contract renewal rate | `renewed / eligible` | Month | `auto_renew`, dates | fact |
| SUB-13 | Avg contract term | `AVG(term_months)` | contract_type | `dh_contract` | dim |
| SUB-14 | Contract expiry pipeline | `COUNT WHERE end_date in next N days` | Week | `dh_contract` | mart |
| SUB-15 | Installed assets | `COUNT(DISTINCT asset_id)` | asset_type | `dh_installed_asset` | `dim_asset` |
| SUB-16 | Assets per customer | `AVG(COUNT(asset_id))` | Segment | `dh_installed_asset` | fact |
| SUB-17 | Asset install velocity | `COUNT WHERE install_date in period` | Month | `dh_installed_asset` | fact |
| SUB-18 | In-service asset rate | `% status = 'Active'` | asset_type | `dh_installed_asset` | dim |
| SUB-19 | Active SIMs / eSIMs | `COUNT(DISTINCT sim_id) WHERE status = 'Active'` | sim_type | `dh_sim_esim` | `dim_sim` |
| SUB-20 | eSIM share | `% sim_type = 'eSIM'` | Segment | `dh_sim_esim` | dim |
| SUB-21 | SIM activation rate | `activations / shipments` | Week | `activation_date` | fact |
| SUB-22 | Multi-SIM customers | `% customers with >1 active sim` | Segment | `dh_sim_esim` | fact |
| SUB-23 | Cross-sell index | `AVG(count distinct: plan + package + asset)` | Segment | multiple | mart |

---

## 3. Usage & network (`dh_cdr`, `dh_ip_data`, `dh_bandwidth_usage`, `dh_qoe_metrics`, `dh_tower`)

| KPI ID | Name | Formula | Grain | Silver | Gold |
|--------|------|---------|-------|--------|------|
| USG-01 | Total call attempts | `COUNT(cdr_id)` | Day/hour | `dh_cdr` | `fact_call_detail` |
| USG-02 | Total talk time | `SUM(CAST(duration_sec AS NUMERIC))` | Day | `dh_cdr` | fact |
| USG-03 | Avg call duration | `AVG(CAST(duration_sec AS NUMERIC))` | call_type | `dh_cdr` | fact |
| USG-04 | Calls per customer | `COUNT / DISTINCT customers` | Period | `dh_cdr` | agg |
| USG-05 | Call type mix | `% by call_type` | Day | `dh_cdr` | fact |
| USG-06 | MOU (minutes of use) | `SUM(duration_sec)/60` | Month/customer | `dh_cdr` | agg |
| USG-07 | Peak hour call volume | `COUNT WHERE EXTRACT(hour FROM call_start_ts) in peak` | Hour | `dh_cdr` | fact + time |
| USG-08 | IP sessions | `COUNT(session_id)` | Day | `dh_ip_data` | `fact_ip_session` |
| USG-09 | Total data downloaded | `SUM(bytes_downloaded_mb)` | Day/customer | `dh_ip_data` | fact |
| USG-10 | Total data uploaded | `SUM(bytes_uploaded_mb)` | Day | `dh_ip_data` | fact |
| USG-11 | Avg session duration | `AVG(CAST(session_duration_min AS NUMERIC))` | APN | `dh_ip_data` | fact |
| USG-12 | Sessions per customer | `COUNT / DISTINCT customer_id` | Period | `dh_ip_data` | agg |
| USG-13 | Daily data usage (GB) | `SUM(data_used_gb)` | Day/segment | `dh_bandwidth_usage` | `fact_bandwidth_daily` |
| USG-14 | Peak hour data share | `SUM(peak_hour_usage_gb) / SUM(data_used_gb)` | Day | `dh_bandwidth_usage` | fact |
| USG-15 | Throttle rate | `% rows WHERE throttled_flag` | Day/segment | `dh_bandwidth_usage` | fact |
| USG-16 | Heavy users (top decile) | `% customers above P90 usage` | Month | `dh_bandwidth_usage` | agg |
| USG-17 | Avg latency | `AVG(latency_ms)` | tower/region | `dh_qoe_metrics` | `fact_qoe` |
| USG-18 | Avg jitter | `AVG(jitter_ms)` | tower | `dh_qoe_metrics` | fact |
| USG-19 | Avg packet loss | `AVG(packet_loss_pct)` | tower | `dh_qoe_metrics` | fact |
| USG-20 | Avg signal strength | `AVG(signal_strength_dbm)` | tower | `dh_qoe_metrics` | fact |
| USG-21 | Poor QoE rate | `% WHERE latency_ms > T OR packet_loss_pct > T` | Day/region | `dh_qoe_metrics` | mart |
| USG-22 | Video quality distribution | `% by video_quality_score` | Region | `dh_qoe_metrics` | fact |
| USG-23 | Towers under capacity stress | `COUNT WHERE capacity_status = 'Congested'` | Region | `dh_tower` | `dim_tower` |
| USG-24 | QoE incidents per tower | `COUNT(qoe_id) / tower` | Day | qoe + tower | mart |
| USG-25 | Usage vs allowance | `SUM(data_used_gb) / plan allowance` | Customer/month | bandwidth + plan | mart |
| USG-26 | Network attachment diversity | `COUNT(DISTINCT tower_id)` per customer | Period | cdr/qoe | agg |

---

## 4. Billing & financials (`dh_invoice`, `dh_payment`, `dh_ar`, `dh_balance`, `dh_dunning`)

| KPI ID | Name | Formula | Grain | Silver | Gold |
|--------|------|---------|-------|--------|------|
| FIN-01 | Gross billed revenue | `SUM(total_amount)` | invoice_date month | `dh_invoice` | `fact_invoice` |
| FIN-02 | Net billed (ex tax) | `SUM(amount_due)` | Month | `dh_invoice` | fact |
| FIN-03 | Tax collected | `SUM(tax_amount)` | Month | `dh_invoice` | fact |
| FIN-04 | Invoice volume | `COUNT(invoice_id)` | Day | `dh_invoice` | fact |
| FIN-05 | Avg invoice value | `AVG(total_amount)` | Segment | `dh_invoice` + customer | fact |
| FIN-06 | Open invoice backlog | `SUM(total_amount) WHERE status = 'Open'` | Day | `dh_invoice` | mart |
| FIN-07 | On-time payment rate | `% paid before due_date` | Month | invoice + payment | mart |
| FIN-08 | DSO (days sales outstanding) | `AVG(days_past_due)` or standard formula | Month | `dh_ar_accounts_receivable` | fact_ar |
| FIN-09 | Total AR outstanding | `SUM(outstanding_balance)` | Day | `dh_ar_accounts_receivable` | fact_ar |
| FIN-10 | AR by aging bucket | `SUM(outstanding_balance)` | aging_bucket | `dh_ar` | dim_aging |
| FIN-11 | Past-due rate | `% AR with days_past_due > 0` | Day | `dh_ar` | mart |
| FIN-12 | Bad debt exposure | `SUM(outstanding) WHERE aging_bucket = '90+'` | Week | `dh_ar` | mart |
| FIN-13 | Payment volume | `COUNT(payment_id)` | Day | `dh_payment` | `fact_payment` |
| FIN-14 | Cash collected | `SUM(amount_paid) WHERE status = 'Completed'` | Day | `dh_payment` | fact |
| FIN-15 | Collection rate | `SUM(paid) / SUM(billed)` | Month | payment + invoice | mart |
| FIN-16 | Payment method mix | `% by payment_method` | Month | `dh_payment` | fact |
| FIN-17 | Failed payment rate | `% status = 'Failed'` | Month | `dh_payment` | fact |
| FIN-18 | Avg days to pay | `AVG(payment_date - invoice_date)` | Segment | payment + invoice | mart |
| FIN-19 | Customer wallet balance | `SUM(current_balance)` | balance_type | `dh_balance` | `fact_balance_snapshot` |
| FIN-20 | Prepaid vs postpaid balance mix | `SUM by balance_type` | Day | `dh_balance` | fact |
| FIN-21 | Dunning notices sent | `COUNT(dunning_id)` | notice_date | `dh_dunning` | `fact_dunning` |
| FIN-22 | Dunning resolution rate | `% resolved_flag = 'Y'` | Stage | `dh_dunning` | fact |
| FIN-23 | Dunning funnel conversion | `COUNT by dunning_stage` | Month | `dh_dunning` | mart |
| FIN-24 | Revenue at risk (dunning) | `SUM(invoice total) for customers in dunning` | Week | dunning + invoice | mart |
| FIN-25 | Billing dispute proxy | `invoices with dunning + open AR` | Customer | join | mart |
| FIN-26 | CLV-weighted revenue | `SUM(total_amount * clv_percentile)` | Segment | invoice + clv | mart |

---

## 5. Interactions & support (`dh_ticket`, `dh_case`, `dh_ivr_log`, `dh_store_visit`, `dh_dispatch`)

| KPI ID | Name | Formula | Grain | Silver | Gold |
|--------|------|---------|-------|--------|------|
| CARE-01 | Ticket volume | `COUNT(ticket_id)` | Day/channel | `dh_ticket` | `fact_support_ticket` |
| CARE-02 | Tickets per 1k customers | `1000 * tickets / customers` | Month | ticket + customer | mart |
| CARE-03 | First contact resolution (proxy) | `% resolved same day` | Channel | created/resolved | fact |
| CARE-04 | Avg ticket resolution time | `AVG(resolved_date - created_date)` | priority | `dh_ticket` | fact |
| CARE-05 | SLA breach rate | `% resolution > SLA by priority` | Day | ticket | mart |
| CARE-06 | Open ticket backlog | `COUNT WHERE status = 'Open'` | Day | `dh_ticket` | mart |
| CARE-07 | Ticket type mix | `% by ticket_type` | Month | `dh_ticket` | fact |
| CARE-08 | Priority distribution | `% by priority` | Channel | `dh_ticket` | fact |
| CARE-09 | Digital channel share | `% channel IN ('App','Web',...)` | Month | `dh_ticket` | dim_channel |
| CARE-10 | Case volume | `COUNT(case_id)` | Day | `dh_case` | `fact_support_case` |
| CARE-11 | Avg case handle time | `AVG(closed_date - opened_date)` | case_type | `dh_case` | fact |
| CARE-12 | Case reopen proxy | `% closed then new case < 7d` | Type | case | mart |
| CARE-13 | Agent workload | `COUNT(case_id) per assigned_agent` | Week | `dh_case` | dim_agent |
| CARE-14 | IVR call volume | `COUNT(ivr_log_id)` | Day | `dh_ivr_log` | `fact_ivr` |
| CARE-15 | Avg IVR wait time | `AVG(wait_time_sec)` | menu_path | `dh_ivr_log` | fact |
| CARE-16 | IVR containment rate | `% resolution_flag AND NOT transferred` | Day | `dh_ivr_log` | fact |
| CARE-17 | IVR transfer rate | `% transferred_to_agent = 'Y'` | menu_path | `dh_ivr_log` | fact |
| CARE-18 | Store visits | `COUNT(visit_id)` | store_id/day | `dh_store_visit` | `fact_store_visit` |
| CARE-19 | Avg visit duration | `AVG(duration_min)` | visit_reason | `dh_store_visit` | fact |
| CARE-20 | Visit outcome success rate | `% outcome = 'Resolved'/'Sale'` | Store | `dh_store_visit` | fact |
| CARE-21 | Retail conversion proxy | `% visit_reason = 'Upgrade'` with outcome sale | Month | store | mart |
| CARE-22 | Field dispatches | `COUNT(dispatch_id)` | Day | `dh_dispatch` | `fact_field_dispatch` |
| CARE-23 | Avg field resolution time | `AVG(resolution_time_hr)` | issue_type | `dh_dispatch` | fact |
| CARE-24 | Dispatch success rate | `% status = 'Completed'` | Technician | `dh_dispatch` | fact |
| CARE-25 | Truck roll cost proxy | `dispatches * cost_per_roll` | Region | dispatch | mart |
| CARE-26 | Omnichannel touches | `tickets + ivr + visits per customer` | Month | all care | agg |
| CARE-27 | Care cost per customer | `SUM(touches) / customers` | Segment | mart | mart |

---

## 6. Derived analytics & growth (`dh_churn_score`, `dh_clv`, `dh_propensity_to_buy`, `dh_sentiment`, `dh_next_best_action`)

| KPI ID | Name | Formula | Grain | Silver | Gold |
|--------|------|---------|-------|--------|------|
| AN-01 | Avg churn score | `AVG(churn_score)` | score_date | `dh_churn_score` | score snapshot |
| AN-02 | High-risk customer count | `COUNT WHERE risk_segment = 'High'` | Day | `dh_churn_score` | mart |
| AN-03 | Churn risk concentration | `% customers in High/Medium/Low` | Segment | churn | mart |
| AN-04 | Model coverage | `% customers with score_date in last N days` | Day | churn | QA |
| AN-05 | Total CLV (portfolio) | `SUM(clv_estimate_usd)` | calculation_date | `dh_clv` | snapshot |
| AN-06 | Avg CLV | `AVG(clv_estimate_usd)` | clv_segment | `dh_clv` | snapshot |
| AN-07 | CLV by segment | `SUM/AVG by clv_segment` | Month | clv + customer | mart |
| AN-08 | High-value customer share | `% clv_segment = 'Platinum/Gold'` | Region | clv | mart |
| AN-09 | Upsell propensity avg | `AVG(propensity_score)` | product | `dh_propensity_to_buy` | fact |
| AN-10 | Hot leads | `COUNT WHERE propensity_score > T` | product/day | propensity | mart |
| AN-11 | Propensity coverage | `COUNT scored / customers` | Week | propensity | QA |
| AN-12 | Avg sentiment score | `AVG(sentiment_score)` | channel | `dh_sentiment` | fact |
| AN-13 | Negative sentiment rate | `% sentiment_label = 'Negative'` | source_channel | `dh_sentiment` | fact |
| AN-14 | Sentiment on tickets | join `interaction_id = ticket_id` | ticket_type | sentiment + ticket | mart |
| AN-15 | NBA recommendation volume | `COUNT(*)` | generated_date | `dh_next_best_action` | fact_nba |
| AN-16 | Top recommended actions | `COUNT by recommended_action` | Week | nba | dim |
| AN-17 | Action type mix | `% by action_type` | Segment | nba | dim |
| AN-18 | Priority rank distribution | `COUNT by priority_rank` | Day | nba | fact |
| AN-19 | Churn-adjusted CLV | `SUM(clv * (1 - churn_score))` | Segment | clv + churn | mart |
| AN-20 | Save campaign target list | `High churn AND High CLV` | Customer | churn + clv | mart |

---

## 7. Cross-domain executive & operational KPIs

| KPI ID | Name | Formula | Grain | Sources |
|--------|------|---------|-------|---------|
| X-01 | Customer health index | Weighted composite: usage trend + payment + QoE + sentiment | Customer/week | usage, payment, qoe, sentiment |
| X-02 | Net promoter proxy | `% positive sentiment - % negative` | Month | `dh_sentiment` |
| X-03 | Revenue per GB | `SUM(invoice) / SUM(data_used_gb)` | Month | invoice + bandwidth |
| X-04 | Cost-to-serve index | `(care touches + dispatches) / revenue` | Segment | care + invoice |
| X-05 | Churn revenue at risk | `SUM(MRR or invoice) for High churn` | Month | churn + plan/invoice |
| X-06 | Product–network fit | `QoE poor rate among heavy users` | Region | qoe + bandwidth |
| X-07 | Collections effectiveness | `dunning resolved / dunning started` | Month | dunning |
| X-08 | Subscriber net adds (proxy) | `new registrations - inactive status` | Month | customer |
| X-09 | Convergence index | `% with plan + package + asset` | Segment | sub tables |
| X-10 | 360 engagement score | Normalized sum of usage + care + store visits | Customer/month | multiple |

---

## KPI count summary

| Domain | Table-backed KPIs listed |
|--------|----------------------------|
| Identity & account | 20 |
| Subscriptions & assets | 23 |
| Usage & network | 26 |
| Billing & financials | 26 |
| Interactions & support | 27 |
| Derived analytics | 20 |
| Cross-domain | 10 |
| **Total** | **152** |

Additional KPIs can be derived by combining dimensions (e.g. `[segment × region × month]` for any additive measure).

---

## Implementation checklist

1. Pick grain and gold fact from [dimensional-model.md](../data-model/dimensional-model.md).
2. Map filters to silver columns (status fields, date columns).
3. Cast text numerics in ETL (`duration_sec`, `monthly_fee_usd`, flags stored as text).
4. Document SLA thresholds (QoE, care) in BI layer, not in silver.
5. For as-of metrics (AR, balance, scores), use **snapshot date** on the fact or `LAST_VALUE` window in SQL.
