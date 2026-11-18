"""可执行事件回放器，用于验证去重、乱序处理和 REALTIME/FINAL 服务边界。"""

import hashlib
from contextlib import closing
from datetime import date
from pathlib import Path

from pydantic import Field, model_validator

from credit.warehouse import Record, connect, now


class LoanEvent(Record):
    event_id: str = Field(min_length=1, max_length=80)
    loan_id: str = Field(min_length=1, max_length=80)
    biz_date: str
    version: int = Field(gt=0)
    product: str = Field(pattern=r"^[a-z_]{1,30}$")
    domain: str = Field(pattern=r"^[a-z_]{1,30}$")
    balance_cents: int = Field(ge=0)
    overdue_cents: int = Field(ge=0)

    @model_validator(mode="after")
    def check(self):
        if date.fromisoformat(self.biz_date).isoformat() != self.biz_date:
            raise ValueError("非法业务日期")
        if self.overdue_cents > self.balance_cents:
            raise ValueError("逾期余额大于本金")
        return self


def replay(db: Path, event: LoanEvent) -> str:
    digest = hashlib.sha256(event.model_dump_json().encode()).hexdigest()
    with closing(connect(db)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute("SELECT digest FROM realtime_event WHERE event_id=?", (event.event_id,)).fetchone()
        if duplicate:
            if duplicate[0] != digest:
                raise ValueError("相同 event_id 的内容发生变化")
            return "duplicate"
        existing = conn.execute("SELECT * FROM rt_loan_state WHERE biz_date=? AND loan_id=?",
                                (event.biz_date, event.loan_id)).fetchone()
        if existing and existing["version"] == event.version:
            raise ValueError("同借据同版本出现不同 event_id，需要源端澄清")
        conn.execute("INSERT INTO realtime_event VALUES(?,?,?)", (event.event_id, digest, now()))
        if existing and existing["version"] > event.version:
            return "late_ignored"
        conn.execute("INSERT OR REPLACE INTO rt_loan_state VALUES(?,?,?,?,?,?,?,?)", (
            event.biz_date, event.loan_id, event.version, event.product, event.domain,
            event.balance_cents, event.overdue_cents, event.event_id,
        ))
        conn.execute("DELETE FROM ads_credit_realtime WHERE biz_date=?", (event.biz_date,))
        conn.execute("""INSERT INTO ads_credit_realtime
            SELECT biz_date, product, domain, 0, 0, SUM(balance_cents), SUM(overdue_cents), 0,
                   COUNT(*), 'event-stream', 'REALTIME', ?
            FROM rt_loan_state WHERE biz_date=? GROUP BY biz_date, product, domain""", (now(), event.biz_date))
    return "applied"


def reconcile(db: Path, day: str) -> dict:
    columns = ("loan_balance_cents", "overdue_balance_cents", "loan_count")
    with closing(connect(db)) as conn:
        values = []
        for table in ("ads_credit_daily", "ads_credit_realtime"):
            rows = conn.execute(f"SELECT * FROM {table} WHERE biz_date=?", (day,)).fetchall()
            values.append({(r["product"], r["domain"]): tuple(r[c] for c in columns) for r in rows})
    if not values[0] or not values[1]:
        return {"status": "MISSING", "biz_date": day}
    differences = [{"product": key[0], "domain": key[1], "final": values[0].get(key),
                    "realtime": values[1].get(key)} for key in sorted(values[0].keys() | values[1].keys())
                   if values[0].get(key) != values[1].get(key)]
    return {"status": "PASS" if not differences else "DIFF", "biz_date": day, "differences": differences,
            "note": "FINAL 由离线来源对账发布；此检查不将实时结果改名为 FINAL。"}
