"""Reproducible local release gate for the learning project's runnable scope."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from credit.catalog import Catalog
from credit.embeddings import DEFAULT_MODEL, FastEmbedder
from credit.evaluation import evaluate as evaluate_warehouse
from credit.retrieval import ElasticsearchRetrieval
from credit.retrieval_evaluation import evaluate as evaluate_retrieval

ROOT = Path(__file__).parent.parent


def _command(args: list[str]) -> int:
    return subprocess.run(args, cwd=ROOT, check=False, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode


def _task_redis_available(url: str) -> bool:
    if not url:
        return False
    import redis

    client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    try:
        client.ping()
        return True
    except redis.RedisError:
        return False
    finally:
        client.close()


def release_report(db: Path, retrieval, *, command_runner=_command,
                   task_redis_url: str | None = None, task_redis_probe=_task_redis_available,
                   warehouse_evaluator=evaluate_warehouse, retrieval_evaluator=evaluate_retrieval) -> dict:
    """Return only release-safe summaries, never cases, questions, SQL or results."""
    pytest_code = command_runner([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"])
    ruff_code = command_runner([sys.executable, "-m", "ruff", "check", "--no-cache", "credit", "tests"])
    task_redis_url = os.getenv("CREDIT_TASK_REDIS_URL", "") if task_redis_url is None else task_redis_url
    task_redis_ok = task_redis_probe(task_redis_url)
    task_queue_code = None
    if task_redis_ok:
        # Integration fixtures must fail rather than silently skip if Redis
        # disappears between this preflight probe and pytest startup.
        previous_required = os.environ.get("CREDIT_TASK_REDIS_REQUIRED")
        os.environ["CREDIT_TASK_REDIS_REQUIRED"] = "1"
        try:
            task_queue_code = command_runner([
                sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
                "tests/test_task_queue_integration.py",
                "tests/test_task_store_integration.py",
                "tests/test_redis_async_tasks.py",
            ])
        finally:
            if previous_required is None:
                os.environ.pop("CREDIT_TASK_REDIS_REQUIRED", None)
            else:
                os.environ["CREDIT_TASK_REDIS_REQUIRED"] = previous_required
    warehouse = warehouse_evaluator(db)
    retrieval_report = retrieval_evaluator(retrieval)
    checks = {
        "unit_tests": {"passed": pytest_code == 0},
        "static_analysis": {"passed": ruff_code == 0},
        "task_queue_integration": {
            "passed": task_redis_ok and task_queue_code == 0,
            "reason": None if task_redis_ok else "not_configured" if not task_redis_url else "unavailable",
            "integration_tests_passed": task_queue_code == 0 if task_queue_code is not None else False,
        },
        "warehouse_evaluation": {
            "passed": warehouse["failed"] == 0,
            "total": warehouse["total"], "passed_cases": warehouse["passed"],
            "failed_cases": warehouse["failed"], "success_rate": warehouse["success_rate"],
        },
        "retrieval_evaluation": {
            "passed": retrieval_report["passed"], "dataset_label": retrieval_report["dataset_label"],
            "cases": retrieval_report["cases"], "recall_at_5": retrieval_report["recall_at_5"],
            "mrr_at_5": retrieval_report["mrr_at_5"], "acl_leakage_count": retrieval_report["acl_leakage_count"],
            "citation_accuracy": retrieval_report["citation_accuracy"],
            "unhealthy_cases": retrieval_report["unhealthy_cases"],
        },
    }
    return {
        "schema_version": 1, "generated_at": datetime.now(UTC).isoformat(),
        "scope": "local-learning-release-gate", "passed": all(check["passed"] for check in checks.values()),
        "catalog": {"version": Catalog().document["version"]}, "checks": checks,
    }


def main():
    parser = argparse.ArgumentParser(description="运行本地发布前验收并输出脱敏聚合报告")
    parser.add_argument("--db", type=Path, default=Path("var/credit.db"))
    parser.add_argument("--url", default=os.getenv("CREDIT_ELASTICSEARCH_URL", "http://127.0.0.1:9200"))
    parser.add_argument("--index", default=os.getenv("CREDIT_ELASTICSEARCH_INDEX", "credit-knowledge-current"))
    parser.add_argument("--model", default=os.getenv("CREDIT_EMBEDDING_MODEL", DEFAULT_MODEL))
    parser.add_argument("--cache", type=Path, default=Path("var/models"))
    parser.add_argument("--output", type=Path, default=Path("var/release-preflight.json"))
    args = parser.parse_args()
    catalog, embedder = Catalog(), FastEmbedder(args.model, args.cache)
    embedder.prepare(download=False)
    retrieval = ElasticsearchRetrieval(args.url, args.index, catalog.document["version"], args.model,
                                       embedder.embed_query, fingerprint=embedder.fingerprint, catalog=catalog)
    report = release_report(args.db, retrieval)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
