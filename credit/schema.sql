PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS ingestion_batch (
    batch_id TEXT PRIMARY KEY, biz_date TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('LOADED', 'FINAL')),
    source TEXT NOT NULL, loaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ods_record (
    batch_id TEXT NOT NULL REFERENCES ingestion_batch(batch_id),
    entity TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(batch_id, entity, record_id)
);
CREATE TABLE IF NOT EXISTS dim_customer_scd2 (
    customer_id TEXT NOT NULL, product TEXT NOT NULL, domain TEXT NOT NULL,
    risk_level TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
    is_deleted INTEGER NOT NULL CHECK(is_deleted IN (0,1)), source_batch TEXT NOT NULL,
    PRIMARY KEY(customer_id, valid_from)
);
CREATE TABLE IF NOT EXISTS dwd_application (
    application_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
    biz_date TEXT NOT NULL, product TEXT NOT NULL, domain TEXT NOT NULL,
    approved INTEGER NOT NULL, source_batch TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dwd_loan_snapshot (
    loan_id TEXT NOT NULL, biz_date TEXT NOT NULL, customer_id TEXT NOT NULL,
    product TEXT NOT NULL, domain TEXT NOT NULL, balance_cents INTEGER NOT NULL,
    overdue_cents INTEGER NOT NULL, source_batch TEXT NOT NULL,
    PRIMARY KEY(loan_id, biz_date)
);
CREATE TABLE IF NOT EXISTS dwd_repayment (
    repayment_id TEXT PRIMARY KEY, loan_id TEXT NOT NULL, biz_date TEXT NOT NULL,
    product TEXT NOT NULL, domain TEXT NOT NULL, amount_cents INTEGER NOT NULL,
    source_batch TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dws_credit_daily (
    biz_date TEXT NOT NULL, product TEXT NOT NULL, domain TEXT NOT NULL,
    application_count INTEGER NOT NULL, approved_count INTEGER NOT NULL,
    loan_balance_cents INTEGER NOT NULL, overdue_balance_cents INTEGER NOT NULL,
    repayment_cents INTEGER NOT NULL, loan_count INTEGER NOT NULL,
    source_batch TEXT NOT NULL, PRIMARY KEY(biz_date, product, domain)
);
CREATE TABLE IF NOT EXISTS ads_credit_daily (
    biz_date TEXT NOT NULL, product TEXT NOT NULL, domain TEXT NOT NULL,
    application_count INTEGER NOT NULL, approved_count INTEGER NOT NULL,
    loan_balance_cents INTEGER NOT NULL, overdue_balance_cents INTEGER NOT NULL,
    repayment_cents INTEGER NOT NULL, loan_count INTEGER NOT NULL,
    source_batch TEXT NOT NULL, data_status TEXT NOT NULL CHECK(data_status='FINAL'),
    published_at TEXT NOT NULL, PRIMARY KEY(biz_date, product, domain)
);
-- Isolated synthetic source used only by reviewed NON_CERTIFIED Vintage templates.
CREATE TABLE IF NOT EXISTS exploration_vintage_snapshot (
    snapshot_date TEXT NOT NULL, cohort_month TEXT NOT NULL, disburse_date TEXT NOT NULL,
    loan_id TEXT NOT NULL, product TEXT NOT NULL, domain TEXT NOT NULL,
    mob INTEGER NOT NULL CHECK(mob >= 0), overdue_days INTEGER NOT NULL CHECK(overdue_days >= 0),
    overdue_principal_cents INTEGER NOT NULL CHECK(overdue_principal_cents >= 0),
    disbursed_amount_cents INTEGER NOT NULL CHECK(disbursed_amount_cents > 0),
    data_status TEXT NOT NULL CHECK(data_status='FINAL'),
    PRIMARY KEY(snapshot_date, loan_id)
);
CREATE TABLE IF NOT EXISTS quality_check (
    batch_id TEXT NOT NULL, check_name TEXT NOT NULL, status TEXT NOT NULL,
    detail TEXT NOT NULL, PRIMARY KEY(batch_id, check_name)
);
CREATE TABLE IF NOT EXISTS realtime_event (
    event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, accepted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rt_loan_state (
    biz_date TEXT NOT NULL, loan_id TEXT NOT NULL, version INTEGER NOT NULL,
    product TEXT NOT NULL, domain TEXT NOT NULL, balance_cents INTEGER NOT NULL,
    overdue_cents INTEGER NOT NULL, event_id TEXT NOT NULL,
    PRIMARY KEY(biz_date, loan_id)
);
CREATE TABLE IF NOT EXISTS ads_credit_realtime (
    biz_date TEXT NOT NULL, product TEXT NOT NULL, domain TEXT NOT NULL,
    application_count INTEGER NOT NULL DEFAULT 0, approved_count INTEGER NOT NULL DEFAULT 0,
    loan_balance_cents INTEGER NOT NULL, overdue_balance_cents INTEGER NOT NULL,
    repayment_cents INTEGER NOT NULL DEFAULT 0, loan_count INTEGER NOT NULL,
    source_batch TEXT NOT NULL, data_status TEXT NOT NULL CHECK(data_status='REALTIME'),
    published_at TEXT NOT NULL, PRIMARY KEY(biz_date, product, domain)
);
CREATE TABLE IF NOT EXISTS query_audit (
    trace_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, intent TEXT NOT NULL,
    status TEXT NOT NULL, metric_id TEXT, row_count INTEGER NOT NULL,
    query_hash TEXT, policy_version TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS question_trace_span (
    trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
    request_id TEXT, name TEXT NOT NULL, status TEXT NOT NULL,
    error_code TEXT, duration_ms REAL NOT NULL, evidence_ids TEXT NOT NULL,
    router_mode TEXT NOT NULL, model TEXT, retrieval_mode TEXT NOT NULL,
    response_model TEXT, system_fingerprint TEXT,
    created_at TEXT NOT NULL, PRIMARY KEY(trace_id, span_id)
);
