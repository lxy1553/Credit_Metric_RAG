import json

import pytest

from credit import qa_evaluation as qa
from credit.agent import QuestionService, detect_intents
from credit.catalog import Catalog
from credit.intent_router import DisabledIntentRouter
from credit.memory import MemoryStore
from credit.models import Question, Result, ResultSection


def case(case_id="test", **overrides):
    return {"id": case_id, "question": "逾期率的口径是什么", "standard_answer": "逾期本金除以剩余本金",
            "difficulty": "simple", "capability_labels": ["definition_explanation"],
            "boundary_labels": [], "intent_labels": ["definition"], "expected_action": "answer",
            **overrides}


def rubric(**overrides):
    return {"actor": "demo-analyst", "fact_groups": [["逾期本金"], ["剩余本金"]],
            "intent_groups": [["definition"]], "citation_groups": [["metric:overdue_rate"]],
            "forbid_query": True, **overrides}


def answer(**overrides):
    catalog = Catalog()
    return Result(**{"status": "ok", "intent": "definition", "message": "业务日末逾期本金除以剩余本金",
                     "citations": [catalog.citation(catalog.metrics["overdue_rate"])], **overrides})


def test_checks_answer_content_without_counting_words_only_present_in_citations():
    registry = qa.citation_registry(Catalog())
    assert qa.score_case(case(), rubric(), answer(), registry)["passed"]
    incomplete = answer(message="请参阅逾期本金说明")
    scored = qa.score_case(case(), rubric(), incomplete, registry)
    assert not scored["passed"]
    assert scored["fact_coverage"] == 0.5
    assert "missing_facts" in scored["errors"]


def test_rejects_unknown_and_stale_citations_even_when_answer_words_match():
    registry = qa.citation_registry(Catalog())
    for citation in ({"id": "invented"}, {**answer().citations[0], "version": "obsolete"}):
        scored = qa.score_case(case(), rubric(), answer(citations=[citation]), registry)
        assert not scored["passed"]
        assert "invalid_citation" in scored["errors"]


def test_checks_multi_intent_sections_and_negated_query_constraint():
    expected = rubric(intent_groups=[["definition"], ["lineage"]], citation_groups=[])
    result = answer(intent="multi", message="处理中", sections=[
        ResultSection(intent="definition", title="口径", status="ok", message="逾期本金/剩余本金"),
        ResultSection(intent="lineage", title="血缘", status="ok", message="dwd_loan_snapshot"),
    ])
    assert qa.score_case(case(), expected, result, qa.citation_registry(Catalog()))["passed"]
    result.sections.append(ResultSection(intent="query", title="数值", status="ok", message="值", sql="SELECT 1"))
    scored = qa.score_case(case(), expected, result, qa.citation_registry(Catalog()))
    assert "unexpected_query" in scored["errors"]


def test_permission_refusal_cannot_pass_with_metadata_or_text_leaks():
    expected = rubric(actor="demo-visitor", fact_groups=[["无权"]], intent_groups=[["refused"]],
                      citation_groups=[], deny_payload=True, forbidden_terms=["逾期本金"])
    result = answer(status="refused", intent="refused", message="无权，但公式是逾期本金/剩余本金")
    scored = qa.score_case(case(expected_action="refuse"), expected, result, qa.citation_registry(Catalog()))
    assert "forbidden_content" in scored["errors"]
    assert "permission_payload_leak" in scored["errors"]
    assert "unauthorized_citation" in scored["errors"]


def test_runner_replays_context_with_isolated_sessions_and_redacts_outputs(db):
    cases = [case("context", question="那口径呢", conversation_context=[
        {"role": "user", "question": "2026-03-02 逾期率的口径是什么"}]),
        case("fresh", question="那口径呢", expected_action="clarify")]
    rubrics = {"context": rubric(), "fresh": rubric(fact_groups=[["指标"]],
               intent_groups=[["clarify"]], citation_groups=[])}
    report = qa.evaluate(lambda: QuestionService(db, MemoryStore(), intent_router=DisabledIntentRouter()),
                         cases, rubrics, Catalog())
    assert report["total"] == 2
    assert report["passed_cases"] == 2, report["results"]
    assert report["groups"]["difficulty"]["simple"]["total"] == 2
    assert "那口径呢" not in json.dumps(report, ensure_ascii=False)
    assert "逾期本金" not in json.dumps(report, ensure_ascii=False)


def test_runner_records_failure_and_continues_without_exposing_exception_text():
    class BrokenService:
        def ask(self, *_args):
            raise RuntimeError("sensitive-source-value")

    report = qa.evaluate(BrokenService, [case("a"), case("b")],
                         {"a": rubric(), "b": rubric()}, Catalog())
    assert report["total"] == 2
    assert report["passed_cases"] == 0
    assert report["failure_counts"]["execution_error"] == 2
    assert "sensitive-source-value" not in json.dumps(report)


def test_missing_rubrics_empty_cases_and_unsupported_context_fail_closed():
    for cases, rubrics in [([], {}), ([case()], {}),
                           ([case(conversation_context=[{"role": "assistant", "question": "hello"}])],
                            {"test": rubric()})]:
        with pytest.raises(ValueError):
            qa.evaluate(lambda: None, cases, rubrics, Catalog())


def test_shipped_rubrics_cover_all_50_cases_and_keep_denied_identity_out_of_band():
    cases = json.loads((qa.DATA / "qa_evaluation.json").read_text())
    source = json.loads((qa.DATA / "qa_evaluation_rubrics.json").read_text())
    rubrics = qa.build_rubrics(cases, source["cases"], Catalog())
    qa.validate_inputs(cases, rubrics)
    assert len(rubrics) == 50
    assert rubrics["S15"]["actor"] == "demo-visitor"
    assert rubrics["H05"]["deny_payload"] is True
    assert rubrics["S18"]["forbid_query"] is True


def test_negated_value_request_does_not_create_a_query_branch(db):
    catalog = Catalog()
    assert detect_intents("不要查数值，只告诉我逾期率的口径", catalog) == ["definition"]
    assert "query" in detect_intents("2026-03-02 逾期率是多少", catalog)
    service = QuestionService(db, intent_router=DisabledIntentRouter())
    result = service.ask(Question(question="不要查数值，只告诉我逾期率的口径"),
                         lambda: qa.DEMO_USERS["demo-analyst"])
    assert result.intent == "definition"
    assert result.status == "ok"
    assert result.sql is None and result.rows == []


def test_catalog_version_question_returns_certified_catalog_metadata(db):
    catalog = Catalog()
    service = QuestionService(db, intent_router=DisabledIntentRouter(), catalog=catalog)

    result = service.ask(Question(question="当前认证指标目录版本和更新时间是什么？"),
                         lambda: qa.DEMO_USERS["demo-analyst"])

    assert result.status == "ok"
    assert result.intent == "catalog"
    assert catalog.document["version"] in result.message
    assert catalog.document["updated_at"] in result.message
    assert result.citations[0]["id"] == "catalog:credit-metrics"
    assert result.citations[0]["version"] == catalog.document["version"]


def test_catalog_lookup_eval_label_and_citation_are_supported():
    catalog = Catalog()
    cases = [case("S01", question="当前认证指标目录版本是什么？",
                  standard_answer="demo-v2", capability_labels=["factual_query"],
                  intent_labels=["catalog_lookup"], expected_action="answer")]
    definitions = {"S01": {"facts": [["demo-v2"]]}}
    rubrics = qa.build_rubrics(cases, definitions, catalog)
    assert rubrics["S01"]["intent_groups"] == [["catalog"]]
    assert rubrics["S01"]["citation_groups"] == [["catalog:credit-metrics"]]
    citation = qa.citation_registry(catalog)["catalog:credit-metrics"]["citation"]
    result = Result(status="ok", intent="catalog", message="目录版本 demo-v2", citations=[citation])
    assert qa.score_case(cases[0], rubrics["S01"], result,
                         qa.citation_registry(catalog))["passed"]


def test_catalog_version_is_denied_to_visitor(db):
    service = QuestionService(db, intent_router=DisabledIntentRouter())

    result = service.ask(Question(question="当前认证指标目录版本是什么？"),
                         lambda: qa.DEMO_USERS["demo-visitor"])

    assert result.status == "refused"
    assert result.citations == []
    assert result.rows == []


def test_accepts_only_data_citations_found_in_the_evaluation_warehouse(db):
    registry = qa.citation_registry(Catalog(), db)
    metric_citation = Catalog().citation(Catalog().metrics["overdue_rate"])
    valid_id = next(key for key in registry if key.startswith("batch:synthetic-2026-03-02:"))
    data_citation = registry[valid_id]["citation"]
    result = answer(intent="query", sql="SELECT approved_template", citations=[metric_citation, data_citation])
    numeric_case = case(question="2026-03-02 逾期率是多少", intent_labels=["query"])
    numeric_rubric = qa.build_rubrics([numeric_case], {"test": {"facts": [["逾期本金"]]}}, Catalog())["test"]
    assert qa.score_case(numeric_case, numeric_rubric, result, registry)["passed"]
    bad = {**data_citation, "source": "private_customer_table"}
    result.citations = [metric_citation, bad]
    assert "invalid_citation" in qa.score_case(numeric_case, numeric_rubric, result, registry)["errors"]
