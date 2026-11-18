import sqlite3
from pathlib import Path

from credit.demo import bootstrap, sample_bundle


def test_expanding_history_preserves_existing_july_2025_demo_values():
    july = sample_bundle("2025-07-31")

    assert july["loans"][0]["balance_cents"] == 1_000_000
    assert july["loans"][1]["balance_cents"] == 2_000_000
    assert july["loans"][1]["overdue_cents"] == 200_000
    assert july["applications"][1]["approved"] is True


def test_default_demo_covers_month_ends_and_preserves_legacy_days(tmp_path: Path):
    db = tmp_path / "credit.db"
    summary = bootstrap(db, tmp_path / "sources")

    assert {day[:7] for day in summary["days"]} == {
        *(f"2025-{month:02d}" for month in range(1, 13)),
        *(f"2026-{month:02d}" for month in range(1, 7)),
    }
    assert {"2026-03-01", "2026-03-02", "2026-06-30"} <= set(summary["days"])
    with sqlite3.connect(db) as connection:
        actual_months = {
            row[0] for row in connection.execute(
                "SELECT DISTINCT substr(biz_date, 1, 7) FROM dwd_loan_snapshot"
            )
        }
    assert actual_months == {day[:7] for day in summary["days"]}


def test_default_demo_covers_every_month_of_2025_for_registered_metrics(tmp_path: Path):
    db = tmp_path / "credit.db"
    summary = bootstrap(db, tmp_path / "sources")

    months = {f"2025-{month:02d}" for month in range(1, 13)}
    assert months <= {day[:7] for day in summary["days"]}
    with sqlite3.connect(db) as connection:
        actual_months = {
            row[0] for row in connection.execute(
                "SELECT DISTINCT substr(biz_date, 1, 7) FROM ads_credit_daily"
            )
        }
        vintage_cohorts = {
            row[0] for row in connection.execute(
                "SELECT DISTINCT cohort_month FROM exploration_vintage_snapshot "
                "WHERE snapshot_date='2025-12-31'"
            )
        }
    assert months <= actual_months
    assert months <= vintage_cohorts
