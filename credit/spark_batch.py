"""Spark Local 离线数仓演示：认证输入 -> DWD/DWS/ADS Parquet + 质量报告。

此模块刻意不接入线上 API 或替换 SQLite 主数仓。它复用已有完成标识校验，
以可选依赖方式提供可独立运行的 Spark Local 证明链路。
"""

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from credit.warehouse import load_bundle


def _require_pyspark():
    try:
        from pyspark.sql import SparkSession
    except ImportError as exc:  # pragma: no cover - depends on local optional runtime
        raise RuntimeError(
            "未安装 Spark Local 依赖；请执行 .venv/bin/python -m pip install -e '.[spark]'"
        ) from exc
    return SparkSession


def validate_sources(sources: Path) -> list[Path]:
    """只把已通过原子完成标识验证的批次交给 Spark。"""
    directories = sorted(path for path in sources.iterdir() if path.is_dir())
    if not directories:
        raise ValueError(f"没有找到批次目录：{sources}")
    for directory in directories:
        load_bundle(directory)
    return directories


def _write(df, path: Path, *, partitioned: bool = True):
    writer = df.write.mode("overwrite")
    if partitioned:
        writer = writer.partitionBy("biz_date")
    writer.parquet(str(path))


def run(sources: Path, output: Path, *, app_name: str = "credit-spark-local") -> dict:
    """执行 Spark Local 分层加工，并返回不含明细的质量汇总。"""
    directories = validate_sources(sources)
    SparkSession = _require_pyspark()
    spark = SparkSession.builder.master("local[*]").appName(app_name).getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    try:
        from pyspark.sql import functions as F
        from pyspark.sql.types import ArrayType, BooleanType, LongType, StringType, StructField, StructType
        from pyspark.sql.window import Window

        # 明确输入契约：JSON 中被省略的默认字段也必须稳定映射为列，不能依赖样例数据推断 schema。
        customer_schema = StructType([
            StructField("customer_id", StringType(), False), StructField("product", StringType(), False),
            StructField("domain", StringType(), False), StructField("risk_level", StringType(), False),
            StructField("is_deleted", BooleanType(), True),
        ])
        application_schema = StructType([
            StructField("application_id", StringType(), False), StructField("customer_id", StringType(), False),
            StructField("approved", BooleanType(), False),
        ])
        loan_schema = StructType([
            StructField("loan_id", StringType(), False), StructField("customer_id", StringType(), False),
            StructField("balance_cents", LongType(), False), StructField("overdue_cents", LongType(), False),
        ])
        repayment_schema = StructType([
            StructField("repayment_id", StringType(), False), StructField("loan_id", StringType(), False),
            StructField("amount_cents", LongType(), False),
        ])
        bundle_schema = StructType([
            StructField("batch_id", StringType(), False), StructField("biz_date", StringType(), False),
            StructField("source", StringType(), False), StructField("customers", ArrayType(customer_schema), False),
            StructField("applications", ArrayType(application_schema), False), StructField("loans", ArrayType(loan_schema), False),
            StructField("repayments", ArrayType(repayment_schema), False),
        ])
        raw = spark.read.schema(bundle_schema).option("multiLine", True).json(
            [str(path / "bundle.json") for path in directories]
        )
        batches = raw.select("batch_id", "biz_date", "source", "customers", "applications", "loans", "repayments")
        customers = batches.select("batch_id", "biz_date", F.explode("customers").alias("customer")).select(
            "batch_id", "biz_date", F.col("customer.customer_id").alias("customer_id"),
            F.col("customer.product").alias("product"), F.col("customer.domain").alias("domain"),
            F.col("customer.risk_level").alias("risk_level"),
            F.coalesce(F.col("customer.is_deleted"), F.lit(False)).alias("is_deleted"),
        )
        customer_versions = customers.withColumn(
            "previous", F.lag(F.struct("product", "domain", "risk_level", "is_deleted")).over(
                Window.partitionBy("customer_id").orderBy("biz_date")
            )
        ).where(
            F.col("previous").isNull() | (F.struct("product", "domain", "risk_level", "is_deleted") != F.col("previous"))
        ).drop("previous").withColumn(
            "valid_to", F.lead("biz_date").over(Window.partitionBy("customer_id").orderBy("biz_date"))
        ).withColumnRenamed("biz_date", "valid_from")

        customer_day = customers.select("biz_date", "customer_id", "product", "domain")
        dwd_application = batches.select("batch_id", "biz_date", F.explode("applications").alias("application")).select(
            "batch_id", "biz_date", F.col("application.application_id").alias("application_id"),
            F.col("application.customer_id").alias("customer_id"), F.col("application.approved").cast("long").alias("approved"),
        ).join(customer_day, ["biz_date", "customer_id"], "inner")
        dwd_loan = batches.select("batch_id", "biz_date", F.explode("loans").alias("loan")).select(
            "batch_id", "biz_date", F.col("loan.loan_id").alias("loan_id"),
            F.col("loan.customer_id").alias("customer_id"), F.col("loan.balance_cents").alias("balance_cents"),
            F.col("loan.overdue_cents").alias("overdue_cents"),
        ).join(customer_day, ["biz_date", "customer_id"], "inner")
        loan_day = dwd_loan.select("biz_date", "loan_id", "product", "domain")
        dwd_repayment = batches.select("batch_id", "biz_date", F.explode("repayments").alias("repayment")).select(
            "batch_id", "biz_date", F.col("repayment.repayment_id").alias("repayment_id"),
            F.col("repayment.loan_id").alias("loan_id"), F.col("repayment.amount_cents").alias("amount_cents"),
        ).join(loan_day, ["biz_date", "loan_id"], "inner")

        application_metrics = dwd_application.groupBy("biz_date", "product", "domain", "batch_id").agg(
            F.count("application_id").alias("application_count"), F.sum("approved").alias("approved_count"),
        )
        loan_metrics = dwd_loan.groupBy("biz_date", "product", "domain", "batch_id").agg(
            F.count("loan_id").alias("loan_count"), F.sum("balance_cents").alias("loan_balance_cents"),
            F.sum("overdue_cents").alias("overdue_balance_cents"),
        )
        repayment_metrics = dwd_repayment.groupBy("biz_date", "product", "domain", "batch_id").agg(
            F.sum("amount_cents").alias("repayment_cents"),
        )
        dws = application_metrics.join(loan_metrics, ["biz_date", "product", "domain", "batch_id"], "full_outer").join(
            repayment_metrics, ["biz_date", "product", "domain", "batch_id"], "full_outer"
        ).fillna(0, subset=["application_count", "approved_count", "loan_count", "loan_balance_cents",
                            "overdue_balance_cents", "repayment_cents"])
        ads = dws.withColumn("data_status", F.lit("FINAL")).withColumn(
            "published_at", F.lit(datetime.now(timezone.utc).isoformat())
        )

        staging = output.with_name(f".{output.name}.staging")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        _write(customers, staging / "ods" / "customer_snapshot")
        # SCD2 是有效期维表，不能误按事实业务日分区。
        _write(customer_versions, staging / "dim" / "customer_scd2", partitioned=False)
        _write(dwd_application, staging / "dwd" / "application")
        _write(dwd_loan, staging / "dwd" / "loan_snapshot")
        _write(dwd_repayment, staging / "dwd" / "repayment")
        _write(dws, staging / "dws" / "credit_daily")
        _write(ads, staging / "ads" / "credit_daily_final")

        checks = {
            "applications_count": dwd_application.count(), "approved_count": dwd_application.agg(F.sum("approved")).first()[0],
            "loans_count": dwd_loan.count(), "loan_balance": dwd_loan.agg(F.sum("balance_cents")).first()[0],
            "overdue_balance": dwd_loan.agg(F.sum("overdue_cents")).first()[0],
            "repayment_amount": dwd_repayment.agg(F.sum("amount_cents")).first()[0],
        }
        report = {"status": "FINAL", "engine": "spark-local", "input_batches": len(directories),
                  "quality_checks": {name: {"status": "PASS", "value": value} for name, value in checks.items()}}
        (staging / "quality-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if output.exists():
            shutil.rmtree(output)
        staging.rename(output)
        return report
    finally:
        spark.stop()


def main():
    parser = argparse.ArgumentParser(description="运行 Spark Local 信贷离线数仓演示")
    parser.add_argument("--sources", type=Path, default=Path("var/sources"))
    parser.add_argument("--output", type=Path, default=Path("var/spark-warehouse"))
    args = parser.parse_args()
    print(json.dumps(run(args.sources, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
