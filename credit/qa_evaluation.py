"""Execute annotated Q-A cases with explicit, versioned rule checks.

Keyword coverage and citation integrity are diagnostic signals, not a semantic
judge. Reports omit question/answer text, rows, SQL and exception messages.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from credit.agent import QuestionService
from credit.catalog import Catalog
from credit.demo import bootstrap
from credit.intent_router import DisabledIntentRouter
from credit.knowledge import build_documents
from credit.memory import MemoryStore
from credit.models import Question
from credit.retrieval import LocalRetrieval
from credit.security import DEMO_USERS

DATA = Path(__file__).parent / "data"


def citation_registry(catalog, db=None):
    registry = {}
    document = catalog.document
    catalog_citation = {
        "id": "catalog:credit-metrics", "title": "认证指标目录",
        "source": "credit/data/catalog.json", "version": document["version"],
        "updated_at": document["updated_at"], "domain": "credit",
    }
    registry[catalog_citation["id"]] = {
        "citation": catalog_citation, "roles": ["analyst", "operator"], "domain": "credit",
    }
    for kind, items in (("metric", catalog.metrics), ("field", catalog.fields)):
        for item in items.values():
            citation = catalog.citation(item, kind)
            registry[citation["id"]] = {"citation": citation, "roles": item["roles"], "domain": "credit"}
    for doc in build_documents(catalog):
        citation = {key: doc[key] for key in ("title", "source", "updated_at", "domain")}
        citation.update(id=f"chunk:{doc['chunk_id']}", version=doc.get("doc_version", doc["version"]))
        registry[citation["id"]] = {"citation": citation, "roles": doc["allowed_roles"],
                                     "domain": doc["domain"]}
    if db:
        with sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True) as connection:
            for table, release_status in (("ads_credit_daily", "FINAL"), ("ads_credit_realtime", "REALTIME")):
                rows = connection.execute(
                    f"SELECT DISTINCT source_batch,published_at,biz_date FROM {table}"
                ).fetchall()
                for source_batch, published_at, biz_date in rows:
                    citation = {"id": f"batch:{source_batch}:{biz_date}", "source": table,
                                "updated_at": published_at, "biz_date": biz_date, "version": source_batch,
                                "data_status": release_status, "release_status": release_status}
                    registry[citation["id"]] = {"citation": citation, "roles": ["analyst", "operator"],
                                                 "domain": "credit"}
    return registry


def _normal(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+|(?<=\d),(?=\d)", "", text)


def _action(result):
    units = result.sections or [result]
    if any(unit.status == "unavailable" for unit in units):
        return "unavailable"
    if any(unit.status == "needs_clarification" for unit in units):
        return "clarify"
    if all(unit.status in {"refused", "no_data"} for unit in units):
        return "refuse"
    return "answer" if all(unit.status == "ok" for unit in units) else "partial"


def _resolver_for(user):
    def resolve():
        return user
    return resolve


def build_rubrics(cases, definitions, catalog):
    """Combine hand-authored case facts with authorization-aware expectations."""
    label_intents = {
        "definition_explanation": ["definition"], "lineage_trace": ["lineage"],
        "schema": ["schema"], "field": ["field", "schema"],
        "refusal": ["refused", "unsupported"], "ambiguous_query": ["clarify"],
        "query": ["query"], "calculation": ["calculation"], "catalog_lookup": ["catalog"],
        "authorization": ["refused"], "unsupported": ["refused", "unsupported", "other"],
    }
    rubrics = {}
    for case in cases:
        spec = definitions[case["id"]]
        actor = spec.get("actor", "demo-analyst")
        user = DEMO_USERS[actor]
        intent_groups = [label_intents[label] for label in case["intent_labels"] if label in label_intents]
        if case["expected_action"] == "clarify" and not any("clarify" in group for group in intent_groups):
            intent_groups.append(["clarify"])
        citation_groups = []
        metric_id = catalog.match(case["question"])
        metric = catalog.metrics.get(metric_id) if metric_id else None
        asks_certified_content = any(label in case["intent_labels"] for label in (
            "definition", "lineage", "query", "calculation"))
        if (metric and asks_certified_content and case["expected_action"] != "refuse"
                and user.roles.intersection(metric["roles"])):
            citation_groups.append([f"metric:{metric_id}"])
        if "catalog_lookup" in case["intent_labels"]:
            citation_groups.append(["catalog:credit-metrics"])
        for kind, table in catalog.schema_table_matches(case["question"]):
            table_key = f"schema:{kind}:{table['id']}"
            matching_fields = [field for field in table.get("fields", [])
                               if any(alias and alias.casefold() in case["question"].casefold()
                                      for alias in (field.get("name", ""), field.get("name_en", "")))]
            if matching_fields:
                citation_groups.extend([[f"chunk:{table_key}:field:{field.get('name_en') or field.get('name')}" ]
                                       for field in matching_fields[:3]])
            else:
                citation_groups.append([f"chunk:{table_key}:table"])
        rubrics[case["id"]] = {
            "actor": actor, "fact_groups": spec["facts"], "intent_groups": intent_groups,
            "citation_groups": citation_groups,
            "forbid_query": spec.get("forbid_query", False),
            "deny_payload": spec.get("deny_payload", case["expected_action"] == "refuse" and actor != "demo-analyst"),
            "forbidden_terms": spec.get("forbidden_terms", []),
        }
    if set(definitions) != {case["id"] for case in cases}:
        raise ValueError("each Q-A case must have exactly one independent rubric")
    return rubrics


def score_case(case, rubric, result, registry):
    units = [result, *result.sections]
    # Citation labels and input questions cannot satisfy answer fact checks.
    text = _normal(" ".join(unit.message + " " + json.dumps(unit.rows, ensure_ascii=False) for unit in units))
    facts = [any(_normal(term) in text for term in group) for group in rubric["fact_groups"]]
    intents = {unit.intent for unit in units} - {"multi"}
    intent_hits = [bool(intents.intersection(group)) for group in rubric["intent_groups"]]
    citations = [citation for unit in units for citation in unit.citations]
    ids = {citation.get("id") for citation in citations}
    user = DEMO_USERS[rubric["actor"]]
    errors = []
    actual_action = _action(result)
    if actual_action != case["expected_action"]:
        errors.append("action_mismatch")
    if not all(facts):
        errors.append("missing_facts")
    if intent_hits and not all(intent_hits):
        errors.append("missing_intents")
    if not all(ids.intersection(group) for group in rubric["citation_groups"]):
        errors.append("missing_evidence")
    for citation in citations:
        registered = registry.get(citation.get("id"))
        if not registered or citation != registered["citation"]:
            errors.append("invalid_citation")
        if registered and (not user.roles.intersection(registered["roles"])
                           or registered["domain"] not in user.domains):
            errors.append("unauthorized_citation")
    if rubric.get("forbid_query") and any(unit.sql or unit.intent == "query" for unit in units):
        errors.append("unexpected_query")
    if rubric.get("deny_payload") and any(unit.rows or unit.citations or unit.sql for unit in units):
        errors.append("permission_payload_leak")
    if any(_normal(term) in text for term in rubric.get("forbidden_terms", [])):
        errors.append("forbidden_content")
    return {"case_id": case["id"], "passed": not errors, "errors": sorted(set(errors)),
            "actual_action": actual_action, "actual_intents": sorted(intents),
            "fact_coverage": sum(facts) / len(facts),
            "missing_fact_groups": [index for index, hit in enumerate(facts) if not hit],
            "intent_coverage": sum(intent_hits) / len(intent_hits) if intent_hits else None}


def validate_inputs(cases, rubrics):
    ids = [case["id"] for case in cases]
    if not ids or len(ids) != len(set(ids)) or set(ids) != set(rubrics):
        raise ValueError("Q-A cases must be nonempty, unique and exactly covered by rubrics")
    for case in cases:
        if case["expected_action"] not in {"answer", "clarify", "refuse"}:
            raise ValueError("unsupported expected action")
        # Replaying assistant prose as a user prompt would alter the task.
        if any(turn["role"] != "user" for turn in case.get("conversation_context", [])):
            raise ValueError("only user turns can be replayed by this evaluation adapter")
        rubric = rubrics[case["id"]]
        if rubric["actor"] not in DEMO_USERS:
            raise ValueError("unknown evaluation actor")
        for key in ("fact_groups", "intent_groups", "citation_groups"):
            groups = rubric[key]
            if (not isinstance(groups, list) or (key == "fact_groups" and not groups)
                    or any(not isinstance(group, list) or not group
                           or any(not isinstance(term, str) or not term.strip() for term in group)
                           for group in groups)):
                raise ValueError("invalid evaluation groups")


def evaluate(service_factory, cases, rubrics, catalog, *, db=None):
    validate_inputs(cases, rubrics)
    registry = citation_registry(catalog, db)
    rows, groups = [], {name: {} for name in ("difficulty", "capability", "boundary")}
    for case in cases:
        rubric = rubrics[case["id"]]
        try:
            # New service/memory per case isolates history; only explicit turns
            # are replayed, with identity supplied out of band by the rubric.
            service = service_factory()
            user = DEMO_USERS[rubric["actor"]]
            resolver = _resolver_for(user)
            session_id = "qa-" + uuid4().hex
            for turn in case.get("conversation_context", []):
                service.ask(Question(question=turn["question"], session_id=session_id), resolver)
            result = service.ask(Question(question=case["question"], session_id=session_id), resolver)
            row = score_case(case, rubric, result, registry)
        except Exception:
            row = {"case_id": case["id"], "passed": False, "errors": ["execution_error"],
                   "actual_action": "error", "actual_intents": [], "fact_coverage": None,
                   "intent_coverage": None, "missing_fact_groups": []}
        rows.append(row)
        for dimension, labels in (("difficulty", [case["difficulty"]]),
                                  ("capability", case["capability_labels"]),
                                  ("boundary", case["boundary_labels"] or ["none"])):
            for label in labels:
                group = groups[dimension].setdefault(label, {"total": 0, "passed": 0})
                group["total"] += 1
                group["passed"] += int(row["passed"])
    for dimension in groups.values():
        for group in dimension.values():
            group["pass_rate"] = group["passed"] / group["total"]
    passed = sum(row["passed"] for row in rows)
    def fingerprint(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"schema_version": 1, "generated_at": datetime.now(UTC).isoformat(),
            "dataset_label": "annotated-qa-development", "scoring_mode": "deterministic-rules-v1",
            "catalog_version": catalog.document["version"], "dataset_sha256": fingerprint(cases),
            "rubrics_sha256": fingerprint(rubrics), "total": len(rows), "passed_cases": passed,
            "failed_cases": len(rows) - passed, "rule_pass_rate": passed / len(rows),
            "passed": passed == len(rows), "groups": groups,
            "failure_counts": dict(Counter(error for row in rows for error in row["errors"])),
            "not_assessed": ["semantic_accuracy", "claim_level_grounding", "expression_quality",
                             "logical_coherence", "human_judge_alignment"],
            "results": rows}


def main():
    parser = argparse.ArgumentParser(description="执行 50 条问答开发集，输出规则检查基线，不调用付费模型")
    parser.add_argument("--cases", type=Path, default=DATA / "qa_evaluation.json")
    parser.add_argument("--rubrics", type=Path, default=DATA / "qa_evaluation_rubrics.json")
    parser.add_argument("--output", type=Path, default=Path("var/qa-evaluation-local.json"))
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    rubric_source = json.loads(args.rubrics.read_text(encoding="utf-8"))
    if rubric_source.get("schema_version") != 1 or not isinstance(rubric_source.get("cases"), dict):
        raise ValueError("unsupported rubric file schema")
    rubrics = build_rubrics(cases, rubric_source["cases"], Catalog())
    validate_inputs(cases, rubrics)
    catalog = Catalog()
    retrieval = LocalRetrieval(catalog)
    with tempfile.TemporaryDirectory(prefix="credit-qa-eval-") as temporary:
        root = Path(temporary)
        db = root / "evaluation.db"
        bootstrap(db, root / "sources")
        report = evaluate(lambda: QuestionService(db, MemoryStore(), catalog=catalog, retrieval=retrieval,
                                                  intent_router=DisabledIntentRouter()), cases, rubrics, catalog,
                          db=db)
    report.update(retrieval_backend="local", intent_router="disabled", sample_data="synthetic-12-months")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("total", "passed_cases", "failed_cases", "rule_pass_rate",
                                                "passed", "failure_counts")}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
