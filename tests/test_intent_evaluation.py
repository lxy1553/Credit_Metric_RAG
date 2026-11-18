from credit.intent_evaluation import evaluate_cases, score_intents


def test_score_intents_separates_exact_match_from_micro_recall():
    report = score_intents(
        [["definition", "lineage"], []],
        [["definition"], []],
    )

    assert report["total"] == 2
    assert report["exact_match_accuracy"] == 0.5
    assert report["micro_precision"] == 1.0
    assert report["micro_recall"] == 0.5
    assert report["micro_f1"] == 2 / 3
    assert report["unsupported_correct"] == 1


def test_evaluation_uses_local_router_when_deepseek_call_fails():
    cases = [
        {"id": "definition", "category": "single", "question": "逾期率如何计算",
         "expected_intents": ["definition"]},
        {"id": "off-topic", "category": "unsupported", "question": "推荐一部电影",
         "expected_intents": ["other"]},
    ]

    report = evaluate_cases(cases, lambda question: None if question == "逾期率如何计算" else [])

    assert report["baseline"]["exact_match_accuracy"] == 1.0
    assert report["deepseek"]["evaluated"] == 1
    assert report["deepseek"]["failed"] == 1
    assert report["deepseek"]["exact_match_accuracy"] == 1.0
    assert report["with_fallback"]["exact_match_accuracy"] == 1.0
    assert report["case_results"][0]["deepseek_status"] == "error_fallback"
    assert "question" not in report["case_results"][0]


def test_evaluation_distinguishes_valid_empty_intents_from_api_failure():
    cases = [
        {"id": "off-topic", "category": "unsupported", "question": "推荐一部电影",
         "expected_intents": ["other"]},
    ]

    report = evaluate_cases(cases, lambda _question: [])

    assert report["deepseek"]["failed"] == 0
    assert report["deepseek"]["unsupported_correct"] == 1
    assert report["case_results"][0]["deepseek_status"] == "ok"


def test_load_cases_rejects_unknown_expected_intents(tmp_path):
    import json

    from credit.intent_evaluation import load_cases

    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{
        "id": "unsafe", "category": "invalid", "question": "test", "expected_intents": ["sql"]
    }]))

    try:
        load_cases(path)
    except ValueError as error:
        assert "unsupported expected intent" in str(error)
    else:
        raise AssertionError("invalid intent labels must not enter the regression set")


def test_development_set_covers_explicit_exploration_opt_in():
    import json
    from pathlib import Path

    cases = json.loads(Path("credit/data/intent_evaluation.json").read_text())
    case = next(item for item in cases if item["id"] == "explicit-isolated-exploration")
    report = evaluate_cases([case], lambda _question: None)

    assert report["baseline"]["exact_match_accuracy"] == 1.0
    assert report["case_results"][0]["effective_intents"] == ["exploration"]


def test_local_fallback_recognizes_colloquial_credit_questions():
    import json
    from pathlib import Path

    from credit.agent import detect_intents
    from credit.catalog import Catalog

    cases = json.loads(Path("credit/data/intent_evaluation.json").read_text())
    selected = {case["id"]: case for case in cases if case["id"] in {
        "implicit-definition-numerator-denominator", "implicit-query-product-performance",
        "implicit-lineage-source-location", "implicit-query-trend",
    }}
    catalog = Catalog()

    assert len(selected) == 4
    for case in selected.values():
        assert detect_intents(case["question"], catalog) == case["expected_intents"]


def test_local_fallback_treats_unrelated_arithmetic_as_other():
    from credit.agent import detect_intents
    from credit.catalog import Catalog

    assert detect_intents("请计算 2 + 2", Catalog()) == ["other"]
