from credit.demo import bootstrap
from credit.evaluation import evaluate


def test_synthetic_agent_evaluation_matches_current_other_intent_contract(tmp_path):
    db = tmp_path / "credit.db"
    bootstrap(db, tmp_path / "sources")

    report = evaluate(db)

    assert report["failed"] == 0
    other = {case["question"]: case for case in report["cases"]
             if case["expected_intent"] == "other"}
    assert other["利润是多少"]["expected_status"] == "refused"
    assert other["昨天的贷款余额"]["expected_status"] == "refused"
