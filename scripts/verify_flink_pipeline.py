#!/usr/bin/env python3
"""Publish synthetic version/duplicate/late events and verify the Doris REALTIME result."""

import json
import subprocess
import time

from credit.kafka_events import DEFAULT_TOPIC, ensure_topic, publish
from credit.realtime import LoanEvent


def doris_balance(day: str = "2026-03-03", product: str = "consumer") -> tuple[int, int] | None:
    query = ("SELECT loan_balance_cents,loan_count FROM credit_demo.realtime_aggregate "
             f"WHERE biz_date='{day}' AND product='{product}' AND domain='credit'")
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "doris", "mysql", "-h127.0.0.1", "-P9030", "-uroot",
         "-N", "-e", query], capture_output=True, text=True, check=True, timeout=15,
    )
    values = result.stdout.strip().split("\t")
    return (int(values[0]), int(values[1])) if len(values) == 2 else None


def main() -> None:
    ensure_topic("127.0.0.1:9092")
    events = [
        LoanEvent(event_id="FLINK-DEMO-001-V1", loan_id="FLINK-DEMO-001", biz_date="2026-03-03",
                  version=1, product="consumer", domain="credit", balance_cents=100_000, overdue_cents=0),
        LoanEvent(event_id="FLINK-DEMO-001-V2", loan_id="FLINK-DEMO-001", biz_date="2026-03-03",
                  version=2, product="consumer", domain="credit", balance_cents=120_000, overdue_cents=0),
        LoanEvent(event_id="FLINK-DEMO-001-V2", loan_id="FLINK-DEMO-001", biz_date="2026-03-03",
                  version=2, product="consumer", domain="credit", balance_cents=120_000, overdue_cents=0),
        LoanEvent(event_id="FLINK-DEMO-001-LATE", loan_id="FLINK-DEMO-001", biz_date="2026-03-03",
                  version=1, product="consumer", domain="credit", balance_cents=90_000, overdue_cents=0),
    ]
    published = [publish(event, bootstrap_servers="127.0.0.1:9092") for event in events]
    deadline = time.monotonic() + 30
    observed = None
    while time.monotonic() < deadline:
        observed = doris_balance()
        if observed == (120_000, 1):
            break
        time.sleep(1)
    if observed != (120_000, 1):
        raise RuntimeError(f"Doris 聚合未达到预期：{observed}")
    time.sleep(3)
    observed = doris_balance()
    if observed != (120_000, 1):
        raise RuntimeError(f"重复或迟到事件修改了聚合：{observed}")
    moved = [
        LoanEvent(event_id="FLINK-DEMO-002-V1", loan_id="FLINK-DEMO-002", biz_date="2026-03-04",
                  version=1, product="consumer", domain="credit", balance_cents=150_000, overdue_cents=0),
        LoanEvent(event_id="FLINK-DEMO-002-V2", loan_id="FLINK-DEMO-002", biz_date="2026-03-04",
                  version=2, product="commercial", domain="credit", balance_cents=200_000, overdue_cents=0),
    ]
    published.extend(publish(event, bootstrap_servers="127.0.0.1:9092") for event in moved)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if doris_balance("2026-03-04", "consumer") == (0, 0) and (
            doris_balance("2026-03-04", "commercial") == (200_000, 1)
        ):
            break
        time.sleep(1)
    else:
        raise RuntimeError("借据产品变更后，Doris 两个分组的撤回与新增不一致")
    print(json.dumps({"status": "passed", "topic": DEFAULT_TOPIC,
                      "published_offsets": [item["offset"] for item in published],
                      "doris_balance_cents": observed[0], "doris_loan_count": observed[1],
                      "moved_product_balance_cents": 200_000}, ensure_ascii=False))


if __name__ == "__main__":
    main()
