"""Deterministic end-to-end RAG/answer quality evaluation over synthetic cases."""

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from credit.agent import QuestionService
from credit.catalog import Catalog
from credit.embeddings import DEFAULT_MODEL, FastEmbedder
from credit.knowledge import build_documents
from credit.models import Question
from credit.retrieval import ElasticsearchRetrieval, LocalRetrieval
from credit.security import DEMO_USERS
from credit.warehouse import initialize


def build_cases(catalog: Catalog) -> list[dict]:
    """Build a stable 50-100 case suite using only synthetic catalog vocabulary."""
    cases = []

    def add(case_id, category, question, token, *, status, intent, metric_id=None, expected_text=None,
            field_id=None):
        cases.append({"case_id": case_id, "category": category, "question": question, "token": token,
                      "expected_status": status, "expected_intent": intent, "metric_id": metric_id,
                      "expected_text": expected_text, "field_id": field_id})

    for metric in catalog.metrics.values():
        token = "demo-analyst"
        name = metric["name"]
        for suffix in ("的口径是什么", "定义是什么", "怎么计算"):
            add(f"definition:{metric['id']}:{len(cases)}", "definition", name + suffix, token,
                status="ok", intent="definition", metric_id=metric["id"], expected_text=metric["definition"])
        add(f"lineage:{metric['id']}", "lineage", name + "的血缘来源有哪些", token,
            status="ok", intent="lineage", metric_id=metric["id"], expected_text=" → ".join(metric["lineage"]))

    for field in catalog.fields.values():
        token = "demo-analyst" if "analyst" in field["roles"] else "demo-operator"
        add(f"field:{field['id']}", "field", field["name"] + "字段是什么意思", token,
            status="ok", intent="field", field_id=field["id"])

    for metric in catalog.metrics.values():
        for token, role in (("demo-operator", "operator"), ("demo-visitor", "visitor")):
            if role not in metric["roles"]:
                add(f"acl:metric:{metric['id']}:{role}", "acl_refusal", metric["name"] + "定义", token,
                    status="refused", intent="refused", metric_id=metric["id"])
    for field in catalog.fields.values():
        add(f"acl:field:{field['id']}:visitor", "acl_refusal", field["name"] + "字段是什么", "demo-visitor",
            status="refused", intent="refused", field_id=field["id"])

    unsupported = ["客户手机号是多少", "个人身份证号是什么", "利润指标是多少", "不良率按监管口径怎么算",
                   "M1+逾期率是多少", "授信通过金额是多少", "客户名单有哪些", "预测下月逾期客户"]
    for index, question in enumerate(unsupported, 1):
        add(f"unsupported:{index:02d}", "unsupported_refusal", question, "demo-analyst",
            status="refused", intent="refused")

    metric_names = [item["name"] for item in list(catalog.metrics.values())[:6]]
    for index in range(5):
        question = f"{metric_names[index]}和{metric_names[index + 1]}分别是多少"
        add(f"conflict:{index + 1:02d}", "metric_conflict_refusal", question, "demo-analyst",
            status="refused", intent="refused")

    for index, question in enumerate(("昨天贷款余额", "最近申请通过率", "本月还款金额", "最近逾期率"), 1):
        add(f"clarify:{index:02d}", "missing_or_relative_time", question, "demo-analyst",
            status="needs_clarification", intent="clarify")

    if not 50 <= len(cases) <= 100:
        raise ValueError(f"RAG evaluation suite must contain 50-100 cases, got {len(cases)}")
    return cases


def _sha256(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate(db: Path, retrieval, *, catalog: Catalog | None = None, cases: list[dict] | None = None) -> dict:
    catalog = catalog or Catalog()
    cases = build_cases(catalog) if cases is None else cases
    certified_chunks = {doc["chunk_id"]: doc for doc in build_documents(catalog)}
    service = QuestionService(db, catalog=catalog, retrieval=retrieval)
    rows, category_counts, timings = [], Counter(), []

    for case in cases:
        started = perf_counter()
        result = service.ask(Question(question=case["question"]), lambda token=case["token"]: DEMO_USERS[token])
        timings.append((perf_counter() - started) * 1000)
        errors = []
        if result.status != case["expected_status"]:
            errors.append("status_mismatch")
        if result.intent != case["expected_intent"]:
            errors.append("intent_mismatch")
        category = case["category"]
        if category in {"definition", "lineage"} and result.metric_id != case["metric_id"]:
            errors.append("metric_mismatch")

        relevant_citations = []
        if category in {"definition", "lineage"}:
            if result.message != case["expected_text"]:
                errors.append("answer_not_catalog_faithful")
            expected_citation = catalog.citation(catalog.metrics[case["metric_id"]])
            if expected_citation not in result.citations:
                errors.append("missing_metric_citation")
            relevant_citations = [citation for citation in result.citations if citation.get("id", "").startswith("chunk:")]
            if not relevant_citations:
                errors.append("missing_retrieved_evidence")
        elif category == "field":
            matching = [row for row in result.rows if row.get("id") == case["field_id"]]
            expected_field = catalog.fields[case["field_id"]]
            if len(matching) != 1 or matching[0].get("definition") != expected_field["definition"]:
                errors.append("field_answer_mismatch")
            if matching and matching[0].get("table") != "ads_credit_daily":
                errors.append("field_table_mismatch")
            expected_citation = catalog.citation(expected_field, "field")
            if expected_citation not in result.citations:
                errors.append("missing_field_citation")
            relevant_citations = [citation for citation in result.citations if citation.get("id", "").startswith("chunk:")]
            if not relevant_citations:
                errors.append("missing_retrieved_evidence")

        citation_checks = []
        for citation in relevant_citations:
            chunk_id = citation.get("id", "").removeprefix("chunk:")
            canonical = certified_chunks.get(chunk_id)
            chunk_errors = []
            if canonical is None:
                chunk_errors.append("unknown_chunk")
            else:
                for key in ("title", "source", "version", "updated_at", "domain"):
                    if citation.get(key) != canonical.get(key):
                        chunk_errors.append(f"citation_mismatch:{key}")
                if category == "definition" and canonical["id"] != case["metric_id"]:
                    chunk_errors.append("evidence_metric_mismatch")
                if category == "lineage" and (canonical["id"] != case["metric_id"]
                                               or canonical["kind"] != "metric_lineage"):
                    chunk_errors.append("evidence_lineage_mismatch")
                if category == "field" and canonical["id"] != f"field:{case['field_id']}":
                    chunk_errors.append("evidence_field_mismatch")
            errors.extend(chunk_errors)
            citation_checks.append({"citation_id": citation.get("id"), "errors": chunk_errors})

        if category in {"acl_refusal", "unsupported_refusal", "metric_conflict_refusal"}:
            if result.citations or result.rows or result.sql:
                errors.append("refusal_leaked_result_or_citation")
        if result.warnings:
            errors.append("retrieval_or_service_warning")

        passed = not errors
        category_counts[f"{category}.passed" if passed else f"{category}.failed"] += 1
        rows.append({"case_id": case["case_id"], "category": category,
                     "expected_status": case["expected_status"], "actual_status": result.status,
                     "expected_intent": case["expected_intent"], "actual_intent": result.intent,
                     "expected_metric_id": case.get("metric_id"), "actual_metric_id": result.metric_id,
                     "citation_checks": citation_checks, "errors": errors, "passed": passed})

    categories = {}
    for case in cases:
        name = case["category"]
        group = categories.setdefault(name, {"total": 0, "passed": 0})
        group["total"] += 1
        group["passed"] += int(next(row["passed"] for row in rows if row["case_id"] == case["case_id"]))
    for group in categories.values():
        group["success_rate"] = group["passed"] / group["total"] if group["total"] else None
    refusal_rows = [row for row in rows if row["category"] in {
        "acl_refusal", "unsupported_refusal", "metric_conflict_refusal",
    }]
    answer_rows = [row for row in rows if row["category"] in {"definition", "lineage", "field"}]
    return {
        "schema_version": 1,
        "dataset_label": "synthetic-end-to-end-rag-quality",
        "interpretation": "合成回归集；精确一致性只适用于当前目录驱动的确定性回答，不代表线上或生成式模型效果。",
        "generated_at": datetime.now(UTC).isoformat(),
        "catalog_version": catalog.document["version"],
        "dataset_sha256": _sha256(cases),
        "case_count": len(rows), "categories": categories,
        "refusal_accuracy": sum(row["passed"] for row in refusal_rows) / len(refusal_rows) if refusal_rows else None,
        "grounded_answer_faithfulness": sum(row["passed"] for row in answer_rows) / len(answer_rows) if answer_rows else None,
        "citation_accuracy": (sum(not check["errors"] for row in answer_rows for check in row["citation_checks"])
                              / sum(len(row["citation_checks"]) for row in answer_rows)
                              if any(row["citation_checks"] for row in answer_rows) else None),
        "p50_latency_ms": round(sorted(timings)[len(timings) // 2], 2) if timings else None,
        "p95_latency_ms": round(sorted(timings)[min(len(timings) - 1, int(len(timings) * 0.95))], 2)
                           if timings else None,
        "passed": bool(rows) and all(row["passed"] for row in rows),
        "results": rows,
    }


def main():
    parser = argparse.ArgumentParser(description="运行 50-100 条合成 RAG 端到端质量评测")
    parser.add_argument("--url", default=os.getenv("CREDIT_ELASTICSEARCH_URL", "http://127.0.0.1:9200"))
    parser.add_argument("--index", default=os.getenv("CREDIT_ELASTICSEARCH_INDEX", "credit-knowledge-current"))
    parser.add_argument("--model", default=os.getenv("CREDIT_EMBEDDING_MODEL", DEFAULT_MODEL))
    parser.add_argument("--cache", type=Path, default=Path("var/models"))
    parser.add_argument("--cases-output", type=Path, default=Path("credit/data/rag_quality_cases.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("var/rag-quality-evaluation.json"))
    parser.add_argument("--local", action="store_true", help="显式使用认证本地词法回退进行逻辑验证")
    args = parser.parse_args()
    catalog = Catalog()
    if args.local:
        retrieval = LocalRetrieval(catalog)
    else:
        embedder = FastEmbedder(args.model, args.cache)
        embedder.prepare(download=False)
        retrieval = ElasticsearchRetrieval(args.url, args.index, catalog.document["version"], args.model,
                                           embedder.embed_query, fingerprint=embedder.fingerprint, catalog=catalog)
    cases = build_cases(catalog)
    args.cases_output.parent.mkdir(parents=True, exist_ok=True)
    args.cases_output.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
                                 encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="credit-rag-eval-") as temporary:
        db = Path(temporary) / "evaluation.db"
        initialize(db)
        report = evaluate(db, retrieval, catalog=catalog, cases=cases)
    report["dataset_file"] = args.cases_output.as_posix()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
