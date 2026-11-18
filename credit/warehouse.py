"""原子批次接入、ODS 留痕、SCD2 重建、DWS 对账和 FINAL 发布。金额统一使用分。"""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as conn:
        conn.executescript(Path(__file__).with_name("schema.sql").read_text())
        conn.commit()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Customer(Record):
    customer_id: str = Field(min_length=1, max_length=80)
    product: str = Field(pattern=r"^[a-z_]{1,30}$")
    domain: str = Field(pattern=r"^[a-z_]{1,30}$")
    risk_level: str = Field(pattern=r"^(low|medium|high)$")
    is_deleted: bool = False


class Application(Record):
    application_id: str = Field(min_length=1, max_length=80)
    customer_id: str
    approved: bool


class Loan(Record):
    loan_id: str = Field(min_length=1, max_length=80)
    customer_id: str
    balance_cents: int = Field(ge=0)
    overdue_cents: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_balance(self):
        if self.overdue_cents > self.balance_cents:
            raise ValueError("逾期余额不能超过贷款余额")
        return self


class Repayment(Record):
    repayment_id: str = Field(min_length=1, max_length=80)
    loan_id: str
    amount_cents: int = Field(gt=0)


class Bundle(Record):
    batch_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    biz_date: str
    source: str = Field(min_length=1, max_length=100)
    customers: list[Customer]
    applications: list[Application]
    loans: list[Loan]
    repayments: list[Repayment]

    @model_validator(mode="after")
    def check_relations(self):
        if date.fromisoformat(self.biz_date).isoformat() != self.biz_date:
            raise ValueError("业务日期必须为 YYYY-MM-DD")
        for name, key in ENTITY_KEYS.items():
            records = getattr(self, name)
            if len({getattr(record, key) for record in records}) != len(records):
                raise ValueError(f"{name} 主键重复")
        customers = {c.customer_id: c for c in self.customers if not c.is_deleted}
        if any(r.customer_id not in customers for r in [*self.applications, *self.loans]):
            raise ValueError("客户关联失败或客户已删除")
        loans = {r.loan_id for r in self.loans}
        if any(r.loan_id not in loans for r in self.repayments):
            raise ValueError("还款借据关联失败")
        return self


ENTITY_KEYS = {
    "customers": "customer_id", "applications": "application_id",
    "loans": "loan_id", "repayments": "repayment_id",
}


def load_bundle(directory: Path) -> tuple[Bundle, str]:
    marker = directory / "_SUCCESS.json"
    if not marker.is_file():
        raise ValueError("文件未完成：缺少 _SUCCESS.json")
    raw = (directory / "bundle.json").read_bytes()
    manifest = json.loads(marker.read_text())
    digest = hashlib.sha256(raw).hexdigest()
    if manifest.get("sha256") != digest:
        raise ValueError("文件 SHA256 与完成标识不一致")
    bundle = Bundle.model_validate(json.loads(raw))
    counts = {name: len(getattr(bundle, name)) for name in ENTITY_KEYS}
    if manifest.get("row_counts") != counts:
        raise ValueError("记录数与完成标识不一致")
    if not bundle.customers and manifest.get("allow_empty") is not True:
        raise ValueError("空客户快照需要显式 allow_empty")
    return bundle, digest


def _rebuild(conn: sqlite3.Connection):
    """学习数据量下按日期全重放，使迟到批次也生成正确的历史区间。生产应按影响分区重算。"""
    for table in ("dim_customer_scd2", "dwd_application", "dwd_loan_snapshot", "dwd_repayment",
                  "dws_credit_daily", "ads_credit_daily", "quality_check"):
        conn.execute(f"DELETE FROM {table}")
    current = {}
    for batch in conn.execute("SELECT * FROM ingestion_batch ORDER BY biz_date").fetchall():
        records = {name: [] for name in ENTITY_KEYS}
        for row in conn.execute("SELECT entity, payload FROM ods_record WHERE batch_id=?", (batch["batch_id"],)):
            records[row["entity"]].append(json.loads(row["payload"]))
        bundle = Bundle.model_validate({
            "batch_id": batch["batch_id"], "biz_date": batch["biz_date"], "source": batch["source"], **records,
        })
        day, batch_id = bundle.biz_date, bundle.batch_id
        customers = {c.customer_id: c.model_dump() for c in bundle.customers}
        # 完整快照中消失的客户转换为删除版本；显式 is_deleted 同样适用。
        for key, previous in current.items():
            if key not in customers:
                customers[key] = {**previous, "is_deleted": True}
        for key, customer in customers.items():
            if current.get(key) == customer:
                continue
            conn.execute("UPDATE dim_customer_scd2 SET valid_to=? WHERE customer_id=? AND valid_to IS NULL",
                         (day, key))
            conn.execute("INSERT INTO dim_customer_scd2 VALUES(?,?,?,?,?,?,?,?)", (
                key, customer["product"], customer["domain"], customer["risk_level"], day, None,
                int(customer["is_deleted"]), batch_id,
            ))
        current = customers
        groups = {}

        def group(customer_id, customers=customers, groups=groups):
            c = customers[customer_id]
            key = (c["product"], c["domain"])
            return key, groups.setdefault(key, [0, 0, 0, 0, 0, 0])

        for application in bundle.applications:
            (product, domain), values = group(application.customer_id)
            conn.execute("INSERT INTO dwd_application VALUES(?,?,?,?,?,?,?)", (
                application.application_id, application.customer_id, day, product, domain,
                int(application.approved), batch_id,
            ))
            values[0] += 1
            values[1] += int(application.approved)
        for loan in bundle.loans:
            (product, domain), values = group(loan.customer_id)
            conn.execute("INSERT INTO dwd_loan_snapshot VALUES(?,?,?,?,?,?,?,?)", (
                loan.loan_id, day, loan.customer_id, product, domain, loan.balance_cents,
                loan.overdue_cents, batch_id,
            ))
            values[2] += loan.balance_cents
            values[3] += loan.overdue_cents
            values[5] += 1
        loans = {loan.loan_id: loan for loan in bundle.loans}
        for repayment in bundle.repayments:
            (product, domain), values = group(loans[repayment.loan_id].customer_id)
            conn.execute("INSERT INTO dwd_repayment VALUES(?,?,?,?,?,?,?)", (
                repayment.repayment_id, repayment.loan_id, day, product, domain, repayment.amount_cents, batch_id,
            ))
            values[4] += repayment.amount_cents
        for (product, domain), values in sorted(groups.items()):
            conn.execute("INSERT INTO dws_credit_daily VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (day, product, domain, *values, batch_id))
        # 独立从 DWD SQL 汇总，并与来源逐项对账后才发布。
        checks = {
            "applications_count": ("SELECT COUNT(*) FROM dwd_application WHERE biz_date=?", len(bundle.applications)),
            "approved_count": ("SELECT COALESCE(SUM(approved),0) FROM dwd_application WHERE biz_date=?",
                               sum(a.approved for a in bundle.applications)),
            "loans_count": ("SELECT COUNT(*) FROM dwd_loan_snapshot WHERE biz_date=?", len(bundle.loans)),
            "loan_balance": ("SELECT COALESCE(SUM(balance_cents),0) FROM dwd_loan_snapshot WHERE biz_date=?",
                             sum(loan.balance_cents for loan in bundle.loans)),
            "overdue_balance": ("SELECT COALESCE(SUM(overdue_cents),0) FROM dwd_loan_snapshot WHERE biz_date=?",
                                sum(loan.overdue_cents for loan in bundle.loans)),
            "repayment_amount": ("SELECT COALESCE(SUM(amount_cents),0) FROM dwd_repayment WHERE biz_date=?",
                                 sum(r.amount_cents for r in bundle.repayments)),
        }
        dws_columns = ["application_count", "approved_count", "loan_count", "loan_balance_cents",
                       "overdue_balance_cents", "repayment_cents"]
        for (name, (sql, expected)), column in zip(checks.items(), dws_columns, strict=True):
            actual = conn.execute(sql, (day,)).fetchone()[0]
            aggregated = conn.execute(
                f"SELECT COALESCE(SUM({column}),0) FROM dws_credit_daily WHERE biz_date=?", (day,),
            ).fetchone()[0]
            if actual != expected or aggregated != actual:
                raise ValueError(f"质量门禁失败：{name}")
            conn.execute("INSERT INTO quality_check VALUES(?,?,?,?)",
                         (batch_id, name, "PASS", json.dumps({"source": expected, "dwd": actual, "dws": aggregated})))
        conn.execute("INSERT INTO ads_credit_daily SELECT *, 'FINAL', ? FROM dws_credit_daily WHERE biz_date=?",
                     (batch["loaded_at"], day))
        conn.execute("UPDATE ingestion_batch SET status='FINAL' WHERE batch_id=?", (batch_id,))


def ingest(db: Path, directory: Path) -> dict:
    bundle, digest = load_bundle(directory)
    initialize(db)
    with closing(connect(db)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM ingestion_batch WHERE batch_id=? OR biz_date=?",
                                (bundle.batch_id, bundle.biz_date)).fetchone()
        if existing:
            if existing["digest"] == digest and existing["batch_id"] == bundle.batch_id:
                return {"status": "unchanged", "batch_id": bundle.batch_id}
            raise ValueError("已存在不同内容的同日或同 ID 批次；必须走版本化更正流程")
        conn.execute("INSERT INTO ingestion_batch VALUES(?,?,?,?,?,?)",
                     (bundle.batch_id, bundle.biz_date, digest, "LOADED", bundle.source, now()))
        for entity, key in ENTITY_KEYS.items():
            conn.executemany("INSERT INTO ods_record VALUES(?,?,?,?)", [
                (bundle.batch_id, entity, getattr(record, key), record.model_dump_json())
                for record in getattr(bundle, entity)
            ])
        _rebuild(conn)
    return {"status": "FINAL", "batch_id": bundle.batch_id, "quality_checks": 6}


def main():
    parser = argparse.ArgumentParser(description="导入含 bundle.json 和 _SUCCESS.json 的合成批次")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--db", type=Path, default=Path("var/credit.db"))
    args = parser.parse_args()
    print(json.dumps(ingest(args.db, args.directory), ensure_ascii=False))


if __name__ == "__main__":
    main()
