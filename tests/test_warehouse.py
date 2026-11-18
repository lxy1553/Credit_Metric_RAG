import json
import sqlite3

import pytest

from credit.demo import sample_bundle, write_bundle
from credit.realtime import LoanEvent, reconcile, replay
from credit.warehouse import ingest


def test_ingestion_is_idempotent_and_publishes_only_after_quality(db, tmp_path):
    directory = tmp_path / "again"
    write_bundle(directory, sample_bundle("2026-03-01"))
    assert ingest(db, directory)["status"] == "unchanged"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quality_check WHERE status='PASS'").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM ads_credit_daily WHERE data_status='FINAL'").fetchone()[0] == 6


def test_complete_marker_blocks_partial_and_changed_batches(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "bundle.json").write_text("{}")
    with pytest.raises(ValueError, match="_SUCCESS"):
        ingest(tmp_path / "x.db", directory)
    directory = tmp_path / "changed"
    write_bundle(directory, sample_bundle("2026-03-01"))
    marker = json.loads((directory / "_SUCCESS.json").read_text())
    marker["sha256"] = "0" * 64
    (directory / "_SUCCESS.json").write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="SHA256"):
        ingest(tmp_path / "x.db", directory)


def test_scd2_and_realtime_replay(db):
    with sqlite3.connect(db) as conn:
        history = conn.execute(
            "SELECT risk_level,valid_from,valid_to FROM dim_customer_scd2 WHERE customer_id='C001' ORDER BY valid_from"
        ).fetchall()
    assert history == [("low", "2026-03-01", "2026-03-02"), ("medium", "2026-03-02", None)]
    event = LoanEvent(event_id="E-new", loan_id="L001", biz_date="2026-03-02", version=2,
                      product="consumer", domain="credit", balance_cents=800000, overdue_cents=0)
    assert replay(db, event) == "applied"
    assert replay(db, event) == "duplicate"
    assert reconcile(db, "2026-03-02")["status"] == "DIFF"
    late = event.model_copy(update={"event_id": "E-late", "version": 1, "balance_cents": 1})
    assert replay(db, late) == "late_ignored"
