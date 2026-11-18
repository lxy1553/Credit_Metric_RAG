"""Development-set evaluation for deterministic and DeepSeek intent routing."""

import argparse
import json
import math
import time
from pathlib import Path
from typing import Callable, Sequence

from credit.agent import detect_intents
from credit.catalog import Catalog
from credit.intent_router import ALLOWED_INTENTS, DeepSeekIntentRouter


def load_cases(path: Path) -> list[dict]:
    cases = json.loads(path.read_text())
    if not isinstance(cases, list) or not cases:
        raise ValueError("evaluation dataset must be a non-empty JSON array")
    seen_ids = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("each evaluation case must be a JSON object")
        case_id, category = case.get("id"), case.get("category")
        question, expected = case.get("question"), case.get("expected_intents")
        if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
            raise ValueError("evaluation case IDs must be non-empty and unique")
        seen_ids.add(case_id)
        if not isinstance(category, str) or not category:
            raise ValueError(f"case {case_id}: category is required")
        if not isinstance(question, str) or not question.strip() or len(question) > 1000:
            raise ValueError(f"case {case_id}: question must contain 1-1000 characters")
        if not isinstance(expected, list):
            raise ValueError(f"case {case_id}: expected_intents must be an array")
        if any(not isinstance(intent, str) or intent not in ALLOWED_INTENTS for intent in expected):
            raise ValueError(f"case {case_id}: unsupported expected intent")
        if len(set(expected)) != len(expected):
            raise ValueError(f"case {case_id}: duplicate expected intent")
    return cases


def score_intents(expected: Sequence[Sequence[str]], predicted: Sequence[Sequence[str]]) -> dict:
    if len(expected) != len(predicted):
        raise ValueError("expected and predicted lists must have equal length")
    exact = unsupported_total = unsupported_correct = true_positive = false_positive = false_negative = 0
    for expected_case, predicted_case in zip(expected, predicted, strict=True):
        expected_set, predicted_set = set(expected_case), set(predicted_case)
        exact += expected_set == predicted_set
        true_positive += len(expected_set & predicted_set)
        false_positive += len(predicted_set - expected_set)
        false_negative += len(expected_set - predicted_set)
        if not expected_set or expected_set == {"other"}:
            unsupported_total += 1
            unsupported_correct += predicted_set == expected_set
    count = len(expected)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return {
        "total": count,
        "exact_match_accuracy": exact / count if count else 0.0,
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": f1,
        "unsupported_total": unsupported_total,
        "unsupported_correct": unsupported_correct,
        "unsupported_accuracy": unsupported_correct / unsupported_total if unsupported_total else None,
    }


def evaluate_cases(cases: list[dict], model_route: Callable[[str], list[str] | None]) -> dict:
    catalog = Catalog()
    expected, baseline, model, effective = [], [], [], []
    case_results, latencies, failed = [], [], 0
    for case in cases:
        question = case["question"]
        expected_intents = case["expected_intents"]
        deterministic = detect_intents(question, catalog)
        started = time.perf_counter()
        try:
            predicted = model_route(question)
            if predicted == []:
                predicted = ["other"]
            if predicted is not None and (
                not isinstance(predicted, list)
                or any(intent not in ALLOWED_INTENTS for intent in predicted)
                or len(set(predicted)) != len(predicted)
            ):
                predicted = None
        except Exception:
            predicted = None
        elapsed_ms = (time.perf_counter() - started) * 1000
        latencies.append(elapsed_ms)
        expected.append(expected_intents)
        baseline.append(deterministic)
        status = "ok" if predicted is not None else "error_fallback"
        if predicted is None:
            failed += 1
            effective.append(deterministic)
        else:
            model.append((expected_intents, predicted))
            effective.append(predicted)
        case_results.append({
            "id": case["id"], "category": case["category"], "expected_intents": expected_intents,
            "baseline_intents": deterministic, "deepseek_intents": predicted,
            "deepseek_status": status, "latency_ms": round(elapsed_ms, 2),
            "effective_intents": effective[-1],
        })

    successful_latencies = [row["latency_ms"] for row in case_results if row["deepseek_status"] == "ok"]
    successful_latencies.sort()
    p95_index = max(0, math.ceil(0.95 * len(successful_latencies)) - 1)
    model_report = score_intents([row[0] for row in model], [row[1] for row in model])
    model_report.update({"evaluated": len(model), "failed": failed,
                         "mean_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
                         "successful_p95_latency_ms": successful_latencies[p95_index]
                         if successful_latencies else None})
    return {
        "baseline": score_intents(expected, baseline),
        "deepseek": model_report,
        "with_fallback": score_intents(expected, effective),
        "case_results": case_results,
    }


def main():
    parser = argparse.ArgumentParser(description="Compare local rules with DeepSeek on synthetic intent cases")
    parser.add_argument("--dataset", type=Path, default=Path("credit/data/intent_evaluation.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--online", action="store_true",
                        help="explicitly send the synthetic evaluation questions to DeepSeek")
    args = parser.parse_args()
    if not args.online:
        parser.error("--online is required; without it no questions are sent to DeepSeek")
    router = DeepSeekIntentRouter.from_environment()
    if not router.enabled:
        parser.error("configure CREDIT_INTENT_ROUTER=deepseek and DEEPSEEK_API_KEY before online evaluation")
    cases = load_cases(args.dataset)
    report = {"provider": "DeepSeek", "model": router.model, "dataset_count": len(cases),
              **evaluate_cases(cases, router.route)}
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")
    if report["deepseek"]["failed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
