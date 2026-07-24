# Customer 360 Synthetic Dataset

Generated from the domain pillars / key data entities defined in `customer360data.xlsx`.
All files are **CSV** (XML and Avro intentionally excluded per request).

- **Customer master is fixed at 100,000 unique customers** (`01_customer.csv`).
- Every other entity contains **10,000 sample rows**, each referencing a random subset
  of `customer_id` values from the 100,000-customer master (not full coverage — realistic,
  since not every customer has a ticket, invoice, dispatch, etc.).
- Seeded (`SEED=42`) for reproducibility.

## Files by domain pillar

| Pillar | File | Entity |
|---|---|---|
| Identity & Account | 01_customer.csv | Customer (100,000 unique) |
| | 02_party.csv | Party |
| | 03_contact.csv | Contact |
| | 04_household.csv | Household |
| | 05_account_hierarchy.csv | Account Hierarchy |
| Subscriptions & Assets | 06_plan.csv | Plan |
| | 07_package.csv | Package |
| | 08_installed_asset.csv | Installed Asset |
| | 09_contract.csv | Contract |
| | 10_sim_esim.csv | SIM/eSIM |
| Usage & Network | 11_tower.csv | Tower ID (reference/master, 10,000 towers) |
| | 12_cdr.csv | CDRs (Call Detail Records) |
| | 13_ip_data.csv | IP Data (sessions) |
| | 14_qoe_metrics.csv | QoE Metrics |
| | 15_bandwidth_usage.csv | Bandwidth Usage |
| Billing & Financials | 16_invoice.csv | Invoice |
| | 17_payment.csv | Payment |
| | 18_ar_accounts_receivable.csv | AR (Accounts Receivable) |
| | 19_balance.csv | Balance |
| | 20_dunning.csv | Dunning |
| Interactions & Support | 21_ticket.csv | Ticket |
| | 22_case.csv | Case |
| | 23_ivr_log.csv | IVR Log |
| | 24_store_visit.csv | Store Visit |
| | 25_dispatch.csv | Dispatch |
| Derived Analytics | 26_churn_score.csv | Churn Score |
| | 27_clv.csv | CLV (Customer Lifetime Value) |
| | 28_propensity_to_buy.csv | Propensity to Buy |
| | 29_sentiment.csv | Sentiment |
| | 30_next_best_action.csv | NBA (Next Best Action) |

## Referential integrity

- `customer_id` in every entity file is a valid FK into `01_customer.csv`.
- `tower_id` in `12_cdr.csv` / `14_qoe_metrics.csv` is a valid FK into `11_tower.csv`.
- `invoice_id` is shared/consistent across `16_invoice.csv`, `17_payment.csv`,
  `18_ar_accounts_receivable.csv`, and `20_dunning.csv` (same invoice, different lifecycle facts).
- `interaction_id` in `29_sentiment.csv` references `21_ticket.csv` (`ticket_id`).

## Regeneration

Source script: see project scratchpad `gen_synthetic.py`. Re-run with `py gen_synthetic.py`
after adjusting `N_CUSTOMERS` / `N_SAMPLE` / `SEED` at the top of the file to regenerate
with different volumes or a different random seed.
