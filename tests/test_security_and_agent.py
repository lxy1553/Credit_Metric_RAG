import threading
import time

import pytest

from credit.agent import QuestionService
from credit.catalog import Catalog
from credit.memory import MemoryStore, StatelessMemory
from credit.models import MetricQuery, Question, User
from credit.retrieval import LocalRetrieval
from credit.security import DEMO_USERS
from credit.sql_guard import SQLRejected, compile_query, guard


def auth(token="demo-analyst"):
    return {"Authorization": f"Bearer {token}"}


def test_home_page_has_one_natural_language_composer_without_demo_or_intent_switches(client):
    response = client.get("/")
    page = response.text

    assert response.status_code == 200
    assert page.count("<textarea") == 1
    assert 'id="identity"' not in page
    assert "data-q=" not in page


@pytest.mark.parametrize(("question", "expected_intent"), [
    ("申请通过率怎么计算", "definition"),
    ("申请通过率使用哪个字段", "field"),
    ("申请通过率的数据血缘", "lineage"),
    ("2026-03-02 实时逾期率", "query"),
])
def test_single_question_endpoint_routes_supported_intents(client, question, expected_intent):
    response = client.post("/v1/questions", headers=auth(), json={"question": question})

    assert response.status_code == 200
    assert response.json()["intent"] == expected_intent


def test_how_is_it_calculated_routes_to_metric_definition(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "逾期率如何计算",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "definition"
    assert "业务日末逾期本金 / 剩余本金" in body["message"]


def test_unknown_vintage_definition_and_lineage_are_explicitly_unavailable(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "vintage 如何计算，血缘",
    }).json()

    assert body["intent"] == "multi"
    sections = {section["intent"]: section for section in body["sections"]}
    assert set(sections) == {"definition", "lineage"}
    for section in sections.values():
        assert section["status"] in {"refused", "needs_clarification"}
        assert "Vintage" in section["message"]
        assert "认证" in section["message"]
        assert section["citations"] == []
        assert section["rows"] == []
        assert section["sql"] is None
    assert "通用业务概念参考" in sections["definition"]["message"]
    assert "MOB" in sections["definition"]["message"]
    assert "不能据此推定" in sections["definition"]["message"]
    assert "指标建设/治理工单" in sections["lineage"]["message"]
    assert "负责人" in sections["lineage"]["message"]
    assert "认证发布后" in sections["lineage"]["message"]


def test_unknown_metric_does_not_inherit_previous_session_metric(client):
    session = "do-not-reuse-old-metric"
    previous = client.post("/v1/questions", headers=auth(), json={
        "question": "申请通过率怎么计算", "session_id": session,
    }).json()
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "vintage 如何计算，血缘", "session_id": session,
    }).json()

    assert previous["metric_id"] == "approval_rate"
    assert body["status"] == "refused"
    assert body["metric_id"] is None
    assert "Vintage" in body["sections"][0]["message"]
    assert "Vintage" in body["sections"][1]["message"]
    assert all(section["metric_id"] is None for section in body["sections"])
    assert all(not section["citations"] for section in body["sections"])


def test_compound_metric_question_returns_each_intent_as_a_section(client):
    response = client.post("/v1/questions", headers=auth(), json={
        "question": "逾期率如何计算，口径，血缘是如何",
        "session_id": "multi-intent-overdue",
    })
    body = response.json()

    assert response.status_code == 200 and body["status"] == "ok"
    assert body["intent"] == "multi"
    assert [section["intent"] for section in body["sections"]] == ["definition", "lineage"]
    assert "业务日末逾期本金 / 剩余本金" in body["sections"][0]["message"]
    assert body["sections"][1]["message"] == (
        "bundle.loans → dwd_loan_snapshot → dws_credit_daily → ads_credit_daily"
    )
    assert {citation["id"] for citation in body["citations"]} >= {
        "metric:overdue_rate", "chunk:metric:overdue_rate:definition",
        "chunk:metric:overdue_rate:lineage",
    }
    audit = client.get(f"/v1/audit/{body['trace_id']}", headers=auth())
    assert audit.status_code == 200 and audit.json()["intent"] == "multi"


def test_colloquial_compound_question_preserves_definition_lineage_and_query(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "上月各渠道逾期率是多少？口径是什么？上游表有哪些",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "multi"
    sections = {section["intent"]: section for section in body["sections"]}
    assert list(sections) == ["definition", "lineage", "query"]
    assert "业务日末逾期本金 / 剩余本金" in sections["definition"]["message"]
    assert sections["lineage"]["message"] == (
        "bundle.loans → dwd_loan_snapshot → dws_credit_daily → ads_credit_daily"
    )
    assert sections["query"]["status"] == "unavailable"
    assert "渠道" in sections["query"]["message"]
    assert sections["query"]["rows"] == []
    assert sections["query"]["sql"] is None


def test_configured_intent_router_can_find_implicit_multiple_intents(client):
    class FixedRouter:
        def route(self, _question):
            return ["definition", "lineage"]

    client.app.state.service.intent_router = FixedRouter()
    body = client.post("/v1/questions", headers=auth(), json={"question": "逾期率呢"}).json()

    assert body["status"] == "ok"
    assert body["intent"] == "multi"
    assert [section["intent"] for section in body["sections"]] == ["definition", "lineage"]


def test_configured_intent_router_refuses_out_of_scope_without_reusing_session_context(client):
    class FixedRouter:
        def route(self, question):
            return ["definition"] if "申请通过率" in question else []

    client.app.state.service.intent_router = FixedRouter()
    session = "intent-router-out-of-scope"
    first = client.post("/v1/questions", headers=auth(), json={
        "question": "申请通过率怎么计算", "session_id": session,
    }).json()
    second = client.post("/v1/questions", headers=auth(), json={
        "question": "推荐一部电影", "session_id": session,
    }).json()

    assert first["status"] == "ok"
    assert second["status"] == "refused"
    assert second["intent"] == "other"
    assert second["metric_id"] is None
    assert "只处理信贷指标" in second["message"]


def test_date_scoped_upstream_question_does_not_trigger_a_numeric_query(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 逾期率的上游表有哪些",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "lineage"
    assert body["message"] == "bundle.loans → dwd_loan_snapshot → dws_credit_daily → ads_credit_daily"
    assert body["sql"] is None


def test_looking_up_a_metric_definition_does_not_trigger_a_numeric_query(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "查询逾期率口径",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "definition"
    assert "业务日末逾期本金 / 剩余本金" in body["message"]
    assert body["sql"] is None


def test_each_product_phrase_uses_the_certified_product_dimension(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 各产品逾期率是多少",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "query"
    assert {row["product"]: row["value"] for row in body["rows"]} == {
        "business": 0.1,
        "consumer": 0.0,
    }


def test_relative_month_in_compound_question_clarifies_only_the_numeric_part(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "上月逾期率是多少？口径是什么？上游表有哪些",
    }).json()

    assert body["status"] == "ok"
    sections = {section["intent"]: section for section in body["sections"]}
    assert list(sections) == ["definition", "lineage", "query"]
    assert sections["definition"]["status"] == "ok"
    assert sections["lineage"]["status"] == "ok"
    assert sections["query"]["status"] == "needs_clarification"
    assert "月末" in sections["query"]["message"]
    assert sections["query"]["rows"] == []


def test_source_batch_field_name_is_not_mistaken_for_lineage(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "来源批次字段是什么意思",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "field"
    assert [row["id"] for row in body["rows"]] == ["source_batch"]


def test_compound_question_checks_permissions_for_each_intent(client):
    body = client.post("/v1/questions", headers=auth("demo-visitor"), json={
        "question": "逾期率口径和血缘",
        "session_id": "multi-intent-visitor",
    }).json()

    assert body["status"] == "refused"
    assert [section["status"] for section in body["sections"]] == ["refused", "refused"]
    assert body["citations"] == []


def test_compound_intent_retrieval_branches_run_concurrently(db):
    class TrackingRetrieval:
        mode = "test"

        def __init__(self):
            self.delegate = LocalRetrieval(Catalog())
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def search(self, question, user):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.05)
                return self.delegate.search(question, user)
            finally:
                with self.lock:
                    self.active -= 1

    retrieval = TrackingRetrieval()
    service = QuestionService(db, StatelessMemory(), retrieval=retrieval)
    result = service.ask(
        Question(question="逾期率口径和血缘"), lambda: DEMO_USERS["demo-analyst"]
    )

    assert result.status == "ok"
    assert retrieval.max_active == 2


def test_failed_lineage_branch_does_not_discard_successful_definition(db):
    class FailingLineageRetrieval:
        mode = "test"

        def __init__(self):
            self.delegate = LocalRetrieval(Catalog())

        def search(self, question, user):
            if "数据血缘" in question:
                raise RuntimeError("retrieval unavailable")
            return self.delegate.search(question, user)

    service = QuestionService(db, StatelessMemory(), retrieval=FailingLineageRetrieval())
    result = service.ask(Question(question="逾期率口径和血缘"), lambda: DEMO_USERS["demo-analyst"])

    assert result.status == "ok"
    assert [section.intent for section in result.sections] == ["definition", "lineage"]
    assert result.sections[0].status == "ok"
    assert "业务日末逾期本金 / 剩余本金" in result.sections[0].message
    assert result.sections[1].status == "unavailable"
    assert result.sections[1].citations == []
    assert result.sections[1].rows == []


def test_compound_answer_hides_value_when_metric_versions_conflict(db):
    service = QuestionService(db, StatelessMemory())
    original_query = service._query

    def stale_query(*args, **kwargs):
        result = original_query(*args, **kwargs)
        for citation in result.citations:
            if citation.get("id") == "metric:overdue_rate":
                citation["version"] = "stale-version"
        return result

    service._query = stale_query
    result = service.ask(
        Question(question="2026-03-02 逾期率是多少？口径是什么"),
        lambda: DEMO_USERS["demo-analyst"],
    )

    assert result.status == "ok"
    sections = {section.intent: section for section in result.sections}
    assert sections["definition"].status == "ok"
    assert sections["query"].status == "unavailable"
    assert sections["query"].rows == []
    assert sections["query"].sql is None
    assert sections["query"].citations == []
    assert {citation["version"] for citation in result.citations
            if citation["id"] == "metric:overdue_rate"} == {service.catalog.document["version"]}


def test_compound_answer_rejects_stale_retrieved_definition_evidence(db):
    class StaleDefinitionRetrieval:
        mode = "test"

        def __init__(self):
            self.delegate = LocalRetrieval(Catalog())

        def search(self, question, user):
            response = self.delegate.search(question, user)
            if "指标口径" in question:
                for document in response.documents:
                    if document.get("id") == "overdue_rate":
                        document["doc_version"] = "stale-version"
            return response

    service = QuestionService(db, StatelessMemory(), retrieval=StaleDefinitionRetrieval())
    result = service.ask(
        Question(question="2026-03-02 逾期率是多少？口径是什么"),
        lambda: DEMO_USERS["demo-analyst"],
    )

    sections = {section.intent: section for section in result.sections}
    assert sections["definition"].status == "unavailable"
    assert sections["definition"].citations == []
    assert sections["query"].status == "ok"
    assert sections["query"].rows


def test_failed_new_metric_query_does_not_restore_old_session_metric(db):
    service = QuestionService(db, MemoryStore())
    def user():
        return DEMO_USERS["demo-analyst"]

    session_id = "partial-new-metric"
    first = service.ask(Question(question="2026-03-02 贷款余额", session_id=session_id), user)
    assert first.status == "ok"

    original_query = service._query

    def fail_overdue_query(request, user, **kwargs):
        if request.metric_id == "overdue_rate":
            raise RuntimeError("temporary query failure")
        return original_query(request, user, **kwargs)

    service._query = fail_overdue_query
    second = service.ask(Question(question="2026-03-02 逾期率是多少？口径是什么",
                                  session_id=session_id), user)
    followup = service.ask(Question(question="那按产品呢", session_id=session_id), user)

    assert second.status == "ok"
    assert {section.intent: section.status for section in second.sections} == {
        "definition": "ok", "query": "unavailable",
    }
    assert followup.status == "needs_clarification"
    assert followup.metric_id == "overdue_rate"
    assert followup.rows == []


def test_final_query_filters_server_domain_and_returns_citations(client):
    response = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-01 至 2026-03-02 贷款余额按产品查询", "session_id": "demo",
    })
    body = response.json()
    assert response.status_code == 200 and body["status"] == "ok"
    assert {row["value"] for row in body["rows"]} == {9000.0, 10000.0, 18000.0, 20000.0}
    assert all("99999" not in str(row) for row in body["rows"])
    assert {c["data_status"] for c in body["citations"][1:]} == {"FINAL"}


def test_certified_credit_metric_excludes_other_authorized_domains(db):
    user = User("mixed-domain", frozenset({"analyst"}), frozenset({"credit", "restricted"}))
    service = QuestionService(db, StatelessMemory())
    request = MetricQuery(metric_id="loan_balance", start_date="2026-03-02",
                          end_date="2026-03-02", group_by=[])

    result = service.query(request, lambda: user)

    assert result.status == "ok"
    assert result.rows == [{"value": 27000.0}]
    assert "'restricted'" not in result.sql


def test_permission_and_untrusted_identity_inputs(client):
    assert client.post("/v1/questions", json={"question": "申请通过率定义"}).status_code == 401
    assert client.post("/v1/questions", headers=auth("demo-visitor"), json={
        "question": "2026-03-02 逾期率", "roles": ["analyst"],
    }).status_code == 422
    refused = client.post("/v1/questions", headers=auth("demo-visitor"), json={
        "question": "2026-03-02 逾期率",
    }).json()
    assert refused["status"] == "refused" and refused["rows"] == []


def test_certified_schema_query_returns_base_and_donghua_sources(client):
    response = client.post("/v1/questions", headers=auth(), json={
        "question": "借据表有哪些字段",
    })
    body = response.json()
    assert response.status_code == 200 and body["status"] == "ok" and body["intent"] == "schema"
    assert len(body["rows"]) > 50
    assert {row["table_kind"] for row in body["rows"]} == {"base", "donghua_source"}
    assert any(row["field_name_en"] == "LOAN_NO" and row["primary_key"] is True for row in body["rows"])
    assert {citation["version"] for citation in body["citations"]} == {"v1.9", "2021-06-10"}
    assert all("chunk:" in citation["id"] for citation in body["citations"])
    assert body["sql"] is None

    visitor = client.post("/v1/questions", headers=auth("demo-visitor"), json={
        "question": "借据表有哪些字段",
    }).json()
    assert visitor["status"] == "refused" and visitor["rows"] == [] and visitor["citations"] == []


def test_sql_guard_requires_exact_certified_template():
    request = MetricQuery(metric_id="approval_rate", start_date="2026-03-01", end_date="2026-03-02")
    expected = compile_query(request, DEMO_USERS["demo-analyst"], Catalog())
    assert guard(expected, request, DEMO_USERS["demo-analyst"], Catalog()) == expected
    with pytest.raises(SQLRejected):
        guard(expected.replace("domain IN ('credit')", "(domain IN ('credit') OR 1=1)"),
              request, DEMO_USERS["demo-analyst"], Catalog())
    with pytest.raises(SQLRejected):
        guard("DELETE FROM ads_credit_daily", request, DEMO_USERS["demo-analyst"], Catalog())


def test_stock_metric_cannot_be_summed_across_days(client):
    body = client.post("/v1/metrics/query", headers=auth(), json={
        "metric_id": "loan_balance", "start_date": "2026-03-01", "end_date": "2026-03-02", "group_by": [],
    }).json()
    assert body["status"] == "refused"


def test_session_followup_and_identity_isolation(client):
    first = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 贷款余额", "session_id": "follow",
    }).json()
    second = client.post("/v1/questions", headers=auth(), json={
        "question": "那按产品呢", "session_id": "follow",
    }).json()
    visitor = client.post("/v1/questions", headers=auth("demo-visitor"), json={
        "question": "那按产品呢", "session_id": "follow",
    }).json()
    assert first["status"] == second["status"] == "ok" and len(second["rows"]) == 2
    assert visitor["status"] == "needs_clarification"


def test_switching_to_a_new_metric_does_not_reuse_previous_query_date_or_status(client):
    class LocalRouter:
        def route(self, _question):
            return None

    client.app.state.service.intent_router = LocalRouter()
    session = "new-metric-fresh-context"
    previous = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 REALTIME 贷款余额按产品查询", "session_id": session,
    }).json()
    current = client.post("/v1/questions", headers=auth(), json={
        "question": "申请通过率是多少", "session_id": session,
    }).json()

    assert previous["status"] == "ok"
    assert current["status"] == "needs_clarification"
    assert current["metric_id"] == "approval_rate"
    assert current["rows"] == [] and current["sql"] is None
    assert "日期" in current["message"]


def test_short_followup_uses_saved_context_without_sending_it_to_intent_model(client):
    class EmptyRouterForEllipsis:
        def __init__(self):
            self.questions = []

        def route(self, question):
            self.questions.append(question)
            return None if question.startswith("2026-") else []

    router = EmptyRouterForEllipsis()
    client.app.state.service.intent_router = router
    session = "contextual-followup-local-route"
    first = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 贷款余额", "session_id": session,
    }).json()
    second = client.post("/v1/questions", headers=auth(), json={
        "question": "那按产品呢", "session_id": session,
    }).json()

    assert first["status"] == "ok"
    assert second["status"] == "ok" and second["metric_id"] == "loan_balance"
    assert len(second["rows"]) == 2
    assert router.questions == ["2026-03-02 贷款余额"]


def test_vague_deictic_followup_clarifies_instead_of_repeating_last_query(client):
    class LocalRouter:
        def route(self, _question):
            return None

    client.app.state.service.intent_router = LocalRouter()
    session = "vague-deictic-followup"
    client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 贷款余额", "session_id": session,
    })
    current = client.post("/v1/questions", headers=auth(), json={
        "question": "那呢", "session_id": session,
    }).json()

    assert current["status"] == "needs_clarification"
    assert current["rows"] == [] and current["sql"] is None
    assert "口径" in current["message"] or "数值" in current["message"]


def test_conflicting_realtime_and_final_request_is_clarified(client):
    class LocalRouter:
        def route(self, _question):
            return None

    client.app.state.service.intent_router = LocalRouter()
    current = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 REALTIME FINAL 逾期率是多少",
    }).json()

    assert current["status"] == "needs_clarification"
    assert current["rows"] == [] and current["sql"] is None
    assert "REALTIME" in current["message"] and "FINAL" in current["message"]


def test_unqualified_short_question_without_session_context_asks_for_metric(client):
    class EmptyRouter:
        def route(self, _question):
            return []

    client.app.state.service.intent_router = EmptyRouter()
    current = client.post("/v1/questions", headers=auth(), json={
        "question": "那按产品呢",
    }).json()

    assert current["status"] == "needs_clarification"
    assert current["metric_id"] is None and current["rows"] == []
    assert "指标名称" in current["message"]


def test_local_router_unmatched_intent_returns_other_instead_of_crashing(client):
    class DisabledRouter:
        def route(self, _question):
            return None

    client.app.state.service.intent_router = DisabledRouter()
    response = client.post("/v1/questions", headers=auth(), json={
        "question": "推荐一部电影",
    })

    assert response.status_code == 200
    assert response.json()["status"] == "refused"
    assert response.json()["intent"] == "other"
    assert response.json()["rows"] == []
    assert response.json()["sql"] is None


def test_supplied_approval_counts_are_calculated_without_warehouse_query(client):
    from credit.intent_router import DisabledIntentRouter

    client.app.state.service.intent_router = DisabledIntentRouter()
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "100 笔申请中通过 60 笔，申请通过率是多少？",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "calculation"
    assert "60%" in body["message"]
    assert "题目给定" in body["message"]
    assert body["sql"] is None and body["rows"] == []
    assert any(citation["id"] == "metric:approval_rate" for citation in body["citations"])


def test_supplied_overdue_amounts_are_calculated_with_units_and_permissions(client):
    from credit.intent_router import DisabledIntentRouter

    client.app.state.service.intent_router = DisabledIntentRouter()
    question = "逾期本金 75 万、剩余本金 1000 万，逾期率是多少？"
    allowed = client.post("/v1/questions", headers=auth(), json={"question": question}).json()
    denied = client.post("/v1/questions", headers=auth("demo-visitor"),
                         json={"question": question}).json()

    assert allowed["status"] == "ok" and allowed["intent"] == "calculation"
    assert "7.5%" in allowed["message"] and "题目给定" in allowed["message"]
    assert allowed["sql"] is None and allowed["rows"] == []
    assert any(citation["id"] == "metric:overdue_rate" for citation in allowed["citations"])
    assert denied["status"] == "refused"
    assert denied["citations"] == [] and denied["rows"] == [] and denied["sql"] is None


def test_llm_query_label_cannot_override_explicit_question_operands(client):
    class QueryRouter:
        def route(self, _question):
            return ["query"]

    client.app.state.service.intent_router = QueryRouter()
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "100 笔申请中通过 60 笔，申请通过率是多少？",
    }).json()

    assert body["status"] == "ok" and body["intent"] == "calculation"
    assert "60%" in body["message"] and body["sql"] is None


def test_metric_keyword_alone_does_not_force_query(client):
    from credit.intent_router import DisabledIntentRouter

    client.app.state.service.intent_router = DisabledIntentRouter()
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "请用逾期率写一首诗",
    }).json()

    assert body["status"] == "refused" and body["intent"] == "other"
    assert body["rows"] == [] and body["citations"] == [] and body["sql"] is None


def test_two_explicit_ratio_targets_are_not_silently_reduced_to_one(client):
    from credit.intent_router import DisabledIntentRouter

    client.app.state.service.intent_router = DisabledIntentRouter()
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "逾期本金 75 万、剩余本金 1000 万，申请通过率和逾期率分别是多少？",
    }).json()

    assert body["status"] != "ok"
    assert body["intent"] != "calculation"
    assert body["sql"] is None and body["rows"] == []


def test_supplied_overdue_calculation_can_be_combined_with_definition(client):
    from credit.intent_router import DisabledIntentRouter

    client.app.state.service.intent_router = DisabledIntentRouter()
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "逾期本金 75 万、剩余本金 1000 万，逾期率是多少？口径是什么？",
    }).json()

    assert body["status"] == "ok"
    sections = {section["intent"]: section for section in body["sections"]}
    assert set(sections) == {"definition", "calculation"}
    assert sections["definition"]["status"] == "ok"
    assert sections["calculation"]["status"] == "ok"
    assert "7.5%" in sections["calculation"]["message"]
    assert all(section["sql"] is None for section in sections.values())


def test_clear_realtime_followup_reuses_metric_and_date_but_changes_status(client):
    class RouterWithEmptyEllipsis:
        def route(self, question):
            return None if question.startswith("2026-") else []

    client.app.state.service.intent_router = RouterWithEmptyEllipsis()
    session = "explicit-realtime-followup"
    first = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 FINAL 贷款余额", "session_id": session,
    }).json()
    current = client.post("/v1/questions", headers=auth(), json={
        "question": "那实时呢", "session_id": session,
    }).json()

    assert first["status"] == "ok" and first["data_status"] == "FINAL"
    assert current["status"] == "ok" and current["metric_id"] == "loan_balance"
    assert current["data_status"] == "REALTIME"


def test_explicit_date_followup_replaces_saved_date(client):
    class RouterWithEmptyEllipsis:
        def route(self, question):
            return None if question.startswith("2026-") else []

    client.app.state.service.intent_router = RouterWithEmptyEllipsis()
    session = "date-followup-replaces-date"
    client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 贷款余额", "session_id": session,
    })
    current = client.post("/v1/questions", headers=auth(), json={
        "question": "那 2026-03-01 呢？", "session_id": session,
    }).json()

    assert current["status"] == "ok" and current["metric_id"] == "loan_balance"
    assert len(current["rows"]) == 1
    assert current["rows"][0]["biz_date"] == "2026-03-01"


def test_refused_new_topic_clears_old_metric_context(client):
    class LocalRouter:
        def route(self, _question):
            return None

    client.app.state.service.intent_router = LocalRouter()
    session = "refused-topic-clears-context"
    client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 贷款余额", "session_id": session,
    })
    refused = client.post("/v1/questions", headers=auth(), json={
        "question": "M1+逾期率怎么计算", "session_id": session,
    }).json()
    followup = client.post("/v1/questions", headers=auth(), json={
        "question": "那按产品呢", "session_id": session,
    }).json()

    assert refused["status"] in {"refused", "needs_clarification"}
    assert followup["status"] == "needs_clarification"
    assert followup["metric_id"] is None and followup["rows"] == []


def test_memory_failure_degrades_to_stateless(db):
    class BrokenMemory:
        mode = "redis"

        def get(self, key):
            raise ConnectionError

        def set(self, key, state):
            raise ConnectionError

    service = QuestionService(db, BrokenMemory())
    result = service.ask(Question(question="那按产品呢", session_id="broken"),
                         lambda: DEMO_USERS["demo-analyst"])
    assert result.status == "needs_clarification" and result.memory_mode == "stateless"


def test_audit_is_scoped_to_current_user(client):
    result = client.post("/v1/questions", headers=auth(), json={"question": "申请通过率怎么计算"}).json()
    assert client.get(f"/v1/audit/{result['trace_id']}", headers=auth()).status_code == 200
    assert client.get(f"/v1/audit/{result['trace_id']}", headers=auth("demo-operator")).status_code == 404
