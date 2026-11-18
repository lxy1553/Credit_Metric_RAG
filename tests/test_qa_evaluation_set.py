import json
from collections import Counter
from pathlib import Path

DATASET = Path("credit/data/qa_evaluation.json")


def test_qa_evaluation_set_has_requested_size_and_difficulty_distribution():
    cases = json.loads(DATASET.read_text())

    assert len(cases) == 50
    assert Counter(case["difficulty"] for case in cases) == {
        "simple": 20,
        "medium": 20,
        "hard": 10,
    }


def test_each_case_has_answer_evidence_intents_and_boundary_labels():
    cases = json.loads(DATASET.read_text())
    required = {"id", "question", "standard_answer", "evidence_sources",
                "capability_labels", "intent_labels", "difficulty", "boundary_labels",
                "expected_action"}

    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert required <= case.keys()
        assert case["question"].strip()
        assert case["standard_answer"].strip()
        assert case["evidence_sources"]
        assert case["capability_labels"]
        assert case["intent_labels"]
        assert isinstance(case["boundary_labels"], list)
        for evidence in case["evidence_sources"]:
            assert {"source", "locator"} <= evidence.keys()
        for turn in case.get("conversation_context", []):
            assert turn["role"] in {"user", "assistant"}
            assert turn["question"].strip()
        if "conversation_context" in case:
            assert case["context_expectation"].strip()


def test_boundary_matrix_covers_requested_risks():
    cases = json.loads(DATASET.read_text())
    covered = {label for case in cases for label in case["boundary_labels"]}

    assert {"no_answer", "version_conflict", "permission_boundary", "colloquial",
            "multi_intent", "negation", "time_ambiguity"} <= covered


def test_capability_matrix_covers_requested_qa_skills():
    cases = json.loads(DATASET.read_text())
    covered = {label for case in cases for label in case["capability_labels"]}

    assert {"factual_query", "definition_explanation", "lineage_trace",
            "multi_hop_reasoning", "comparison_analysis", "calculation_reasoning",
            "ambiguous_query", "refusal"} <= covered


def test_difficulty_and_expected_action_use_controlled_values():
    cases = json.loads(DATASET.read_text())

    assert {case["difficulty"] for case in cases} <= {"simple", "medium", "hard"}
    assert {case["expected_action"] for case in cases} <= {"answer", "clarify", "refuse"}


def test_local_evidence_references_point_to_existing_project_sources():
    cases = json.loads(DATASET.read_text())

    sources = {evidence["source"] for case in cases for evidence in case["evidence_sources"]}
    assert sources - {"question_constraints"}
    assert all(Path(source).is_file() for source in sources if source != "question_constraints")
