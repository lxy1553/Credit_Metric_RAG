"""Minimal, allow-listed operational traces; never persist request or result bodies."""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from credit.warehouse import connect, now

ALLOWED_NAMES = frozenset({"question", "catalog", "definition", "field", "schema", "lineage", "query",
                           "calculation", "exploration", "clarify", "other", "unsupported"})
SAFE_EVIDENCE_ID = re.compile(r"^(?:metric|chunk|schema):[A-Za-z0-9_:/.-]{1,160}$")
SAFE_METADATA = re.compile(r"^[A-Za-z0-9_:/+.-]{1,80}$")


def _safe_metadata(value, fallback=None):
    return value if isinstance(value, str) and SAFE_METADATA.fullmatch(value) else fallback


def save(db, trace_id, request_id, spans, router_mode, model, retrieval_mode,
         *, response_model=None, system_fingerprint=None):
    with closing(connect(db)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS question_trace_span (
            trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
            request_id TEXT, name TEXT NOT NULL, status TEXT NOT NULL,
            error_code TEXT, duration_ms REAL NOT NULL, evidence_ids TEXT NOT NULL,
            router_mode TEXT NOT NULL, model TEXT, retrieval_mode TEXT NOT NULL,
            response_model TEXT, system_fingerprint TEXT,
            created_at TEXT NOT NULL, PRIMARY KEY(trace_id, span_id))""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(question_trace_span)")}
        if "response_model" not in columns:
            conn.execute("ALTER TABLE question_trace_span ADD COLUMN response_model TEXT")
        if "system_fingerprint" not in columns:
            conn.execute("ALTER TABLE question_trace_span ADD COLUMN system_fingerprint TEXT")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn.execute("DELETE FROM question_trace_span WHERE created_at < ?", (cutoff,))
        for index, span in enumerate(spans):
            name = span["name"]
            if name not in ALLOWED_NAMES:
                continue
            status = span["status"] if span["status"] in {
                "ok", "refused", "unavailable", "needs_clarification", "no_data"
            } else "unavailable"
            evidence = [value for value in span.get("evidence_ids", [])[:50]
                        if isinstance(value, str) and SAFE_EVIDENCE_ID.fullmatch(value)]
            conn.execute("""INSERT OR REPLACE INTO question_trace_span (
                trace_id,span_id,parent_span_id,request_id,name,status,error_code,
                duration_ms,evidence_ids,router_mode,model,retrieval_mode,
                response_model,system_fingerprint,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (trace_id, f"s{index}", None if index == 0 else "s0", request_id,
                          name, status, None if status == "ok" else f"branch_{status}",
                          max(0.0, round(span["duration_ms"], 3)), json.dumps(evidence),
                          _safe_metadata(router_mode, "unknown"), _safe_metadata(model),
                          _safe_metadata(span.get("retrieval_mode", retrieval_mode), "unknown"),
                          _safe_metadata(response_model), _safe_metadata(system_fingerprint), now()))


def prune(db, *, retention_days: int = 30) -> int:
    """Remove expired optional trace spans; mandatory audit records remain untouched."""
    if not 1 <= retention_days <= 3650:
        raise ValueError("trace retention_days must be between 1 and 3650")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    with closing(connect(db)) as conn, conn:
        return conn.execute("DELETE FROM question_trace_span WHERE created_at < ?", (cutoff,)).rowcount


def get(db, trace_id, user_id):
    with closing(connect(db)) as conn:
        owner = conn.execute("SELECT 1 FROM query_audit WHERE trace_id=? AND user_id=?",
                             (trace_id, user_id)).fetchone()
        if not owner:
            return None
        try:
            rows = conn.execute("SELECT * FROM question_trace_span WHERE trace_id=? ORDER BY span_id",
                                (trace_id,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        return None
    spans = [{"span_id": row["span_id"], "parent_span_id": row["parent_span_id"],
              "name": row["name"], "status": row["status"], "error_code": row["error_code"],
              "duration_ms": row["duration_ms"], "evidence_ids": json.loads(row["evidence_ids"]),
              "retrieval_mode": row["retrieval_mode"]}
             for row in rows]
    return {"trace_id": trace_id, "request_id": rows[0]["request_id"], "spans": spans,
            "router_mode": rows[0]["router_mode"], "model": rows[0]["model"],
            "retrieval_mode": rows[0]["retrieval_mode"],
            "response_model": rows[0]["response_model"] if "response_model" in rows[0].keys() else None,
            "system_fingerprint": (rows[0]["system_fingerprint"]
                                   if "system_fingerprint" in rows[0].keys() else None)}
