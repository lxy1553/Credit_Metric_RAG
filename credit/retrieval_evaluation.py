"""Reproducible retrieval checks; this is not an end-to-end answer/refusal benchmark."""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from credit.catalog import Catalog
from credit.embeddings import DEFAULT_MODEL, FastEmbedder
from credit.knowledge import build_documents
from credit.retrieval import ElasticsearchRetrieval, LocalRetrieval
from credit.security import DEMO_USERS

CASES_PATH = Path(__file__).parent / "data/retrieval_cases.jsonl"
TOP_K = 5


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _digest(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone is required")
    return parsed.astimezone(UTC)


def _citation_errors(document, certified, user, model, fingerprint, now) -> list[str]:
    """Validate the returned chunk itself, not merely a plausible citation ID."""
    errors = []
    reference = certified.get(document.get("chunk_id"))
    if reference is None:
        errors.append("unknown_chunk_id")
    else:
        for key in ("chunk_id", "parent_id", "id", "kind", "source", "version", "updated_at",
                    "effective_from", "effective_until", "domain", "certified", "title", "text"):
            if document.get(key) != reference.get(key):
                errors.append(f"catalog_mismatch:{key}")
        if set(document.get("allowed_roles", [])) != set(reference["allowed_roles"]):
            errors.append("catalog_mismatch:allowed_roles")
        if document.get("content_hash") != reference.get("content_hash"):
            errors.append("catalog_mismatch:content_hash")
    if document.get("certified") is not True:
        errors.append("not_certified")
    if document.get("domain") not in user.domains or not user.roles.intersection(
        document.get("allowed_roles", [])
    ):
        errors.append("acl_violation")
    if model and document.get("embedding_model") != model:
        errors.append("embedding_model_mismatch")
    if fingerprint and document.get("embedding_fingerprint") != fingerprint:
        errors.append("embedding_fingerprint_mismatch")
    for key in ("effective_from", "updated_at"):
        try:
            if _timestamp(document[key]) > now:
                errors.append(f"future:{key}")
        except (KeyError, TypeError, ValueError, AttributeError):
            errors.append(f"invalid_timestamp:{key}")
    if document.get("effective_until") is not None:
        try:
            if _timestamp(document["effective_until"]) <= now:
                errors.append("expired:effective_until")
        except (TypeError, ValueError, AttributeError):
            errors.append("invalid_timestamp:effective_until")
    return errors


def evaluate(retrieval, cases: list[dict] | None = None, *, catalog=None, now=None,
             dataset_label="development", index_metadata=None) -> dict:
    started_at, started = now or datetime.now(UTC), perf_counter()
    if started_at.tzinfo is None:
        raise ValueError("evaluation time must include a timezone")
    cases = load_cases() if cases is None else cases
    catalog = catalog or Catalog()
    certified = {item["chunk_id"]: item for item in build_documents(catalog)}
    source = getattr(retrieval, "primary", retrieval)
    model = getattr(source, "embedding_model", None)
    fingerprint = getattr(source, "embedding_fingerprint", None)
    mode = getattr(source, "mode", "unknown")
    rows = []
    for case in cases:
        user = DEMO_USERS[case["token"]]
        expected, forbidden = set(case["expected"]), set(case.get("forbidden", []))
        case_type = case.get("case_type", "positive" if expected else "acl_only")
        if case_type not in {"positive", "acl_only"}:
            raise ValueError(f"unsupported retrieval case type: {case_type}")
        if (case_type == "positive" and not expected) or (case_type == "acl_only" and not forbidden):
            raise ValueError("positive cases require expected IDs; ACL-only cases require forbidden IDs")
        expected_kinds = set(case.get("expected_kinds", []))
        try:
            response = retrieval.search(case["query"], user)
            documents = response.documents[:TOP_K]
            warnings = list(response.warnings)
            branches = getattr(response, "branches", {})
            backend = getattr(response, "backend", mode)
        except Exception as exc:
            documents, branches, backend = [], {}, mode
            warnings = [f"search_failed:{type(exc).__name__}"]
        required_branches = {"lexical", "dense"} if mode.startswith("elasticsearch") else set(branches)
        healthy = bool(branches) and not warnings and all(
            branches.get(branch) == "ok" for branch in required_branches
        ) and all(status == "ok" for status in branches.values())
        ids = [document.get("id") for document in documents]
        evidence = [{"chunk_id": document.get("chunk_id"),
                     "errors": _citation_errors(document, certified, user, model, fingerprint, started_at)}
                    for document in documents]
        matches = [(rank, document["id"]) for rank, document in enumerate(documents, 1)
                   if document.get("id") in expected
                   and (not expected_kinds or document.get("kind") in expected_kinds)]
        retrieved_relevant = {knowledge_id for _, knowledge_id in matches}
        rank = matches[0][0] if matches else None
        leakage = sorted(forbidden.intersection(ids))
        acl_documents = [item["chunk_id"] for item in evidence if "acl_violation" in item["errors"]]
        errors = sum(bool(item["errors"]) for item in evidence)
        recall = len(retrieved_relevant) / len(expected) if expected else None
        row_passed = healthy and not leakage and not errors and (not expected or recall == 1.0)
        rows.append({
            "case_id": case.get("case_id"), "query": case["query"], "case_type": case_type,
            "expected": sorted(expected), "expected_kinds": sorted(expected_kinds), "returned": ids,
            "rank": rank, "recall_at_5": recall, "reciprocal_rank_at_5": 1 / rank if rank else 0.0,
            "acl_leakage": leakage, "acl_violating_chunks": acl_documents,
            "citation_checks": evidence, "citation_error_count": errors,
            "warnings": warnings, "branches": branches, "backend": backend,
            "retrieval_healthy": healthy, "passed": row_passed,
        })
    relevant = [row for row in rows if row["case_type"] == "positive"]
    negative = [row for row in rows if row["case_type"] == "acl_only"]
    returned_count = sum(len(row["returned"]) for row in rows)
    citation_errors = sum(row["citation_error_count"] for row in rows)
    return {
        "schema_version": 2, "dataset_label": dataset_label,
        "interpretation": "Development/regression retrieval checks, not an unbiased production success estimate.",
        "refusal_evaluated": False,
        "refusal_note": "ACL-only cases test forbidden retrieval; they do not establish answer refusal correctness.",
        "citation_note": "Citation accuracy checks catalog metadata and chunk content, not generated-answer faithfulness.",
        "started_at": started_at.isoformat(), "completed_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(perf_counter() - started, 3),
        "catalog": {"version": catalog.document["version"], "updated_at": catalog.document["updated_at"],
                    "sha256": _digest(catalog.document), "chunk_count": len(certified)},
        "model": model, "embedding_fingerprint": fingerprint,
        "index": getattr(source, "index", None), "index_metadata": index_metadata or {},
        "retrieval_mode": mode, "cases_sha256": _digest(cases), "top_k": TOP_K,
        "cases": len(rows), "relevant_cases": len(relevant), "acl_only_cases": len(negative),
        "recall_at_5": sum(row["recall_at_5"] for row in relevant) / len(relevant) if relevant else None,
        "hit_rate_at_5": sum(row["rank"] is not None for row in relevant) / len(relevant) if relevant else None,
        "mrr_at_5": sum(row["reciprocal_rank_at_5"] for row in relevant) / len(relevant) if relevant else None,
        "acl_leakage_count": sum(len(row["acl_violating_chunks"])
                                 + len(set(row["acl_leakage"]) - {
                                     document_id for document_id, check in zip(row["returned"], row["citation_checks"], strict=True)
                                     if "acl_violation" in check["errors"]
                                 }) for row in rows),
        "acl_only_passed": sum(row["passed"] for row in negative),
        "unhealthy_cases": sum(not row["retrieval_healthy"] for row in rows),
        "citation_accuracy": (returned_count - citation_errors) / returned_count if returned_count else None,
        "citation_error_count": citation_errors,
        "passed": bool(rows) and all(row["passed"] for row in rows), "results": rows,
    }


def main():
    parser = argparse.ArgumentParser(description="评估 Elasticsearch 开发集混合检索、引用与 ACL")
    parser.add_argument("--url", default=os.getenv("CREDIT_ELASTICSEARCH_URL", "http://127.0.0.1:9200"))
    parser.add_argument("--index", default=os.getenv("CREDIT_ELASTICSEARCH_INDEX", "credit-knowledge-current"))
    parser.add_argument("--model", default=os.getenv("CREDIT_EMBEDDING_MODEL", DEFAULT_MODEL))
    parser.add_argument("--cache", type=Path, default=Path("var/models"))
    parser.add_argument("--local", action="store_true", help="使用认证本地词法检索进行快速回归")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--dataset-label", default="development")
    parser.add_argument("--output", type=Path, help="保存 JSON 评测报告（自动创建父目录）")
    args = parser.parse_args()
    catalog = Catalog()
    if args.local:
        retrieval = LocalRetrieval(catalog)
    else:
        embedder = FastEmbedder(args.model, args.cache)
        embedder.prepare(download=False)
        retrieval = ElasticsearchRetrieval(
            args.url, args.index, catalog.document["version"], args.model, embedder.embed_query,
            fingerprint=embedder.fingerprint, catalog=catalog,
        )
    report = evaluate(retrieval, load_cases(args.cases), catalog=catalog, dataset_label=args.dataset_label)
    report["cases_file"] = str(args.cases.resolve())
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
