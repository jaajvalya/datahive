-- Billing facts: invoice, payment, AR, dunning, balance (full refresh by natural key upsert)

BEGIN;

INSERT INTO gold.fact_invoice (
    invoice_id, customer_key, invoice_date, due_date, amount_due, tax_amount, total_amount, status
)
SELECT
    i.invoice_id,
    dc.customer_key,
    i.invoice_date,
    i.due_date,
    i.amount_due,
    i.tax_amount,
    COALESCE(i.total_amount, 0),
    i.status
FROM silver.dh_invoice i
INNER JOIN gold.dim_customer dc ON dc.customer_id = i.customer_id
WHERE i.invoice_id IS NOT NULL
ON CONFLICT (invoice_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    invoice_date = EXCLUDED.invoice_date,
    due_date = EXCLUDED.due_date,
    amount_due = EXCLUDED.amount_due,
    tax_amount = EXCLUDED.tax_amount,
    total_amount = EXCLUDED.total_amount,
    status = EXCLUDED.status;

INSERT INTO gold.fact_payment (
    payment_id, customer_key, invoice_id, payment_date, amount_paid, payment_method, status
)
SELECT
    p.payment_id,
    dc.customer_key,
    p.invoice_id,
    p.payment_date,
    COALESCE(p.amount_paid, 0),
    p.payment_method,
    p.status
FROM silver.dh_payment p
INNER JOIN gold.dim_customer dc ON dc.customer_id = p.customer_id
WHERE p.payment_id IS NOT NULL
ON CONFLICT (payment_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    invoice_id = EXCLUDED.invoice_id,
    payment_date = EXCLUDED.payment_date,
    amount_paid = EXCLUDED.amount_paid,
    payment_method = EXCLUDED.payment_method,
    status = EXCLUDED.status;

INSERT INTO gold.fact_ar_balance (
    ar_id, customer_key, invoice_id, outstanding_balance, days_past_due, aging_bucket
)
SELECT
    ar.ar_id,
    dc.customer_key,
    ar.invoice_id,
    ar.outstanding_balance,
    ar.days_past_due,
    ar.aging_bucket
FROM silver.dh_ar_accounts_receivable ar
INNER JOIN gold.dim_customer dc ON dc.customer_id = ar.customer_id
WHERE ar.ar_id IS NOT NULL
ON CONFLICT (ar_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    invoice_id = EXCLUDED.invoice_id,
    outstanding_balance = EXCLUDED.outstanding_balance,
    days_past_due = EXCLUDED.days_past_due,
    aging_bucket = EXCLUDED.aging_bucket;

INSERT INTO gold.fact_dunning_event (
    dunning_id, customer_key, invoice_id, dunning_stage, notice_date, action_taken, resolved_flag
)
SELECT
    d.dunning_id,
    dc.customer_key,
    d.invoice_id,
    d.dunning_stage,
    d.notice_date,
    d.action_taken,
    gold.safe_bool(d.resolved_flag)
FROM silver.dh_dunning d
INNER JOIN gold.dim_customer dc ON dc.customer_id = d.customer_id
WHERE d.dunning_id IS NOT NULL
ON CONFLICT (dunning_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    invoice_id = EXCLUDED.invoice_id,
    dunning_stage = EXCLUDED.dunning_stage,
    notice_date = EXCLUDED.notice_date,
    action_taken = EXCLUDED.action_taken,
    resolved_flag = EXCLUDED.resolved_flag;

INSERT INTO gold.fact_balance_snapshot (
    balance_id, customer_key, balance_type, current_balance, last_updated
)
SELECT
    b.balance_id,
    dc.customer_key,
    b.balance_type,
    b.current_balance,
    b.last_updated
FROM silver.dh_balance b
INNER JOIN gold.dim_customer dc ON dc.customer_id = b.customer_id
WHERE b.balance_id IS NOT NULL
ON CONFLICT (balance_id) DO UPDATE SET
    customer_key = EXCLUDED.customer_key,
    balance_type = EXCLUDED.balance_type,
    current_balance = EXCLUDED.current_balance,
    last_updated = EXCLUDED.last_updated;

COMMIT;
