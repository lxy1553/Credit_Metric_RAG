import argparse
import calendar
import hashlib
import json
from datetime import date
from pathlib import Path

from credit.realtime import LoanEvent, reconcile, replay
from credit.warehouse import ENTITY_KEYS, connect, ingest

LEGACY_DAYS = ("2026-03-01", "2026-03-02")
MONTH_END_DAYS = tuple(
    f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
    for year, month in (
        (2025, 1), (2025, 2), (2025, 3), (2025, 4), (2025, 5), (2025, 6),
        (2025, 7), (2025, 8), (2025, 9), (2025, 10), (2025, 11), (2025, 12),
        (2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5), (2026, 6),
    )
)
DEMO_DAYS = tuple(sorted((*LEGACY_DAYS, *MONTH_END_DAYS)))


def sample_bundle(day: str) -> dict:
    if day not in DEMO_DAYS:
        raise ValueError("样例仅覆盖 2025-01 至 2026-06 的月末及 2026-03-01/02")
    second = day == "2026-03-02"
    if day in MONTH_END_DAYS:
        sample_date = date.fromisoformat(day)
        # Keep the original July 2025 onward history stable while assigning
        # deterministic values to the newly introduced first half of 2025.
        monthly_index = (sample_date.month - 1 if sample_date.year == 2025 and sample_date.month <= 6
                         else (sample_date.year - 2025) * 12 + sample_date.month - 7)
    else:
        monthly_index = None
    balance_1 = (1_000_000 - 15_000 * monthly_index) if monthly_index is not None else (900_000 if second else 1_000_000)
    balance_2 = (2_000_000 - 30_000 * monthly_index) if monthly_index is not None else (1_800_000 if second else 2_000_000)
    overdue_2 = (200_000 - 5_000 * monthly_index) if monthly_index is not None else (180_000 if second else 200_000)
    return {
        "batch_id": f"synthetic-{day}", "biz_date": day, "source": "synthetic-credit-v1",
        "customers": [
            {"customer_id": "C001", "product": "consumer", "domain": "credit",
             "risk_level": "medium" if date.fromisoformat(day) >= date(2026, 3, 2) else "low"},
            {"customer_id": "C002", "product": "business", "domain": "credit", "risk_level": "high"},
            {"customer_id": "X001", "product": "consumer", "domain": "restricted", "risk_level": "low"},
        ],
        "applications": [
            {"application_id": f"A-{day}-1", "customer_id": "C001", "approved": True},
            {"application_id": f"A-{day}-2", "customer_id": "C002",
             "approved": second if monthly_index is None else monthly_index % 2 == 0},
            {"application_id": f"A-{day}-3", "customer_id": "X001", "approved": True},
        ],
        "loans": [
            {"loan_id": "L001", "customer_id": "C001", "balance_cents": balance_1,
             "overdue_cents": 0},
            {"loan_id": "L002", "customer_id": "C002", "balance_cents": balance_2,
             "overdue_cents": overdue_2},
            {"loan_id": "X001", "customer_id": "X001", "balance_cents": 9999900, "overdue_cents": 0},
        ],
        "repayments": [
            {"repayment_id": f"R-{day}-1", "loan_id": "L001", "amount_cents": 100000},
            {"repayment_id": f"R-{day}-2", "loan_id": "L002", "amount_cents": 200000},
        ],
    }


def write_bundle(directory: Path, bundle: dict):
    """样例生成器以完成标识最后落盘；已存在不同样例时拒绝覆盖。"""
    directory.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(bundle, ensure_ascii=False, indent=2) + "\n").encode()
    path = directory / "bundle.json"
    if path.exists() and path.read_bytes() != raw:
        raise ValueError(f"已有不同内容的样例文件：{path}")
    path.write_bytes(raw)
    manifest = {"sha256": hashlib.sha256(raw).hexdigest(),
                "row_counts": {entity: len(bundle[entity]) for entity in ENTITY_KEYS}, "allow_empty": False}
    (directory / "_SUCCESS.json").write_text(json.dumps(manifest, indent=2) + "\n")


def bootstrap(db: Path, sources: Path, *, include_history: bool = True) -> dict:
    days = DEMO_DAYS if include_history else LEGACY_DAYS
    batches = []
    for day in days:
        bundle = sample_bundle(day)
        directory = sources / day
        write_bundle(directory, bundle)
        batches.append(ingest(db, directory))
        customers = {c["customer_id"]: c for c in bundle["customers"]}
        for loan in bundle["loans"]:
            customer = customers[loan["customer_id"]]
            replay(db, LoanEvent(event_id=f"E-{day}-{loan['loan_id']}", biz_date=day,
                                 loan_id=loan["loan_id"], version=1, product=customer["product"],
                                 domain=customer["domain"], balance_cents=loan["balance_cents"],
                                 overdue_cents=loan["overdue_cents"]))
    with connect(db) as conn:
        for cohort_index, cohort_day in enumerate(MONTH_END_DAYS):
            cohort = date.fromisoformat(cohort_day)
            for snapshot_day in MONTH_END_DAYS[cohort_index:]:
                snapshot = date.fromisoformat(snapshot_day)
                mob = (snapshot.year - cohort.year) * 12 + snapshot.month - cohort.month
                overdue_days = 4 if mob >= 2 else 3 if mob == 1 else 0
                conn.execute(
                    """INSERT OR IGNORE INTO exploration_vintage_snapshot VALUES
                    (?,?,?,?,?,?,?,?,?,?,?)""",
                    (snapshot_day, cohort.strftime("%Y-%m"), cohort.replace(day=1).isoformat(),
                     f"V-{cohort:%Y%m}", "business" if cohort_index % 2 else "consumer", "credit",
                     mob, overdue_days, 100_000 if mob >= 1 else 0, 1_000_000, "FINAL"),
                )
    vintage_rows = sum(range(1, len(MONTH_END_DAYS) + 1))
    return {"batches": batches, "reconciliation": reconcile(db, "2026-03-02"),
            "synthetic": True, "days": list(days), "exploration_vintage_rows": vintage_rows}


def main():
    parser = argparse.ArgumentParser(description="生成 12 个月末及两日合成数据，运行数仓和事件回放闭环")
    parser.add_argument("--db", type=Path, default=Path("var/credit.db"))
    parser.add_argument("--sources", type=Path, default=Path("var/sources"))
    args = parser.parse_args()
    print(json.dumps(bootstrap(args.db, args.sources), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
