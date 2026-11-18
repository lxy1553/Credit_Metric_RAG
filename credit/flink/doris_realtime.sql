CREATE DATABASE IF NOT EXISTS credit_demo;

CREATE TABLE IF NOT EXISTS credit_demo.realtime_aggregate (
    biz_date DATE NOT NULL,
    product VARCHAR(30) NOT NULL,
    domain VARCHAR(30) NOT NULL,
    loan_balance_cents BIGINT NOT NULL,
    overdue_balance_cents BIGINT NOT NULL,
    loan_count BIGINT NOT NULL,
    kafka_offset BIGINT NOT NULL
)
UNIQUE KEY (biz_date, product, domain)
DISTRIBUTED BY HASH (biz_date, product, domain) BUCKETS 1
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true",
    "function_column.sequence_col" = "kafka_offset"
);
