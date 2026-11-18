import time

import httpx
from fastapi.testclient import TestClient

from credit import traces
from credit.api import create_app
from credit.catalog import Catalog
from credit.intent_router import DeepSeekIntentRouter
from credit.memory import MemoryStore
from credit.response_cache import MemoryResultCache
from credit.retrieval import ElasticsearchRetrieval, FallbackRetrieval, LocalRetrieval
from credit.security import DEMO_USERS
from credit.warehouse import connect


def test_owner_can_inspect_sanitized_branch_trace(client):
    question = "逾期率口径和血缘，token=secret SELECT * FROM loan"
    response = client.post("/v1/questions", headers={"Authorization": "Bearer demo-analyst"},
                           json={"question": question})
    assert response.status_code == 200
    trace_id = response.json()["trace_id"]
    trace = client.get(f"/v1/traces/{trace_id}", headers={"Authorization": "Bearer demo-analyst"})
    assert trace.status_code == 200
    body = trace.json()
    assert body["trace_id"] == trace_id
    assert body["request_id"] is None
    assert {span["name"] for span in body["spans"]} >= {"question", "definition", "lineage"}
    assert all(span["duration_ms"] >= 0 for span in body["spans"])
    assert question not in str(body)
    assert "secret" not in str(body)
    assert "analyst-001" not in str(body)
    assert "SELECT" not in str(body)
    assert client.get(f"/v1/traces/{trace_id}",
                      headers={"Authorization": "Bearer demo-operator"}).status_code == 404


def test_async_request_id_is_correlated_without_storing_question(client):
    question = "逾期率口径和血缘，password=secret"
    accepted = client.post("/v1/question-tasks", headers={"Authorization": "Bearer demo-analyst"},
                           json={"question": question})
    assert accepted.status_code == 202
    request_id = accepted.json()["request_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = client.get(f"/v1/question-tasks/{request_id}",
                           headers={"Authorization": "Bearer demo-analyst"}).json()
        if state["status"] == "completed":
            break
        time.sleep(0.01)
    assert state["status"] == "completed"
    trace_id = state["trace_id"]
    trace = client.get(f"/v1/traces/{trace_id}",
                       headers={"Authorization": "Bearer demo-analyst"})
    assert trace.status_code == 200
    assert trace.json()["request_id"] == request_id
    assert "secret" not in str(trace.json())


def test_cached_answer_has_its_own_trace(db):
    client = TestClient(create_app(db, DEMO_USERS, MemoryStore(), result_cache=MemoryResultCache()))
    headers = {"Authorization": "Bearer demo-analyst"}
    question = {"question": "贷款余额的血缘"}
    first = client.post("/v1/questions", headers=headers, json=question).json()
    second = client.post("/v1/questions", headers=headers, json=question).json()

    assert first["status"] == second["status"] == "ok"
    assert second["from_cache"] is True
    assert first["trace_id"] != second["trace_id"]
    trace = client.get(f"/v1/traces/{second['trace_id']}", headers=headers)
    assert trace.status_code == 200
    assert {span["name"] for span in trace.json()["spans"]} >= {"question", "lineage"}


def test_trace_records_actual_intent_router_fallback(db):
    client = TestClient(create_app(db, DEMO_USERS, MemoryStore()))
    client.app.state.service.intent_router = DeepSeekIntentRouter(
        "test-secret", client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(503))))
    headers = {"Authorization": "Bearer demo-analyst"}

    result = client.post("/v1/questions", headers=headers, json={"question": "贷款余额的血缘"}).json()
    trace = client.get(f"/v1/traces/{result['trace_id']}", headers=headers).json()

    assert result["status"] == "ok"
    assert trace["router_mode"] == "deterministic-fallback"


def test_trace_records_successful_deepseek_routing(db):
    client = TestClient(create_app(db, DEMO_USERS, MemoryStore()))
    client.app.state.service.intent_router = DeepSeekIntentRouter(
        "test-secret", client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={
                "model": "deepseek-flash", "system_fingerprint": "fp_test_release_1",
                "choices": [{"message": {"content": '{"intents":["lineage"]}'}}],
            }))))
    headers = {"Authorization": "Bearer demo-analyst"}

    result = client.post("/v1/questions", headers=headers, json={"question": "贷款余额的血缘"}).json()
    trace = client.get(f"/v1/traces/{result['trace_id']}", headers=headers).json()

    assert result["status"] == "ok"
    assert trace["router_mode"] == "deepseek-intent-only"
    assert trace["model"] == "deepseek-flash"
    assert trace["response_model"] == "deepseek-flash"
    assert trace["system_fingerprint"] == "fp_test_release_1"


def test_failed_deepseek_call_has_no_response_model_or_fingerprint(db):
    client = TestClient(create_app(db, DEMO_USERS, MemoryStore()))
    client.app.state.service.intent_router = DeepSeekIntentRouter(
        "test-secret", client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(503))))
    headers = {"Authorization": "Bearer demo-analyst"}

    result = client.post("/v1/questions", headers=headers, json={"question": "贷款余额的血缘"}).json()
    trace = client.get(f"/v1/traces/{result['trace_id']}", headers=headers).json()

    assert result["status"] == "ok"
    assert trace["router_mode"] == "deterministic-fallback"
    assert trace["response_model"] is None
    assert trace["system_fingerprint"] is None


def test_trace_records_actual_local_retrieval_after_elasticsearch_fallback(db):
    catalog = Catalog()
    primary = ElasticsearchRetrieval(
        "http://es", "credit-test", catalog.document["version"], "test-embedding",
        lambda _question: [0.1, 0.2],
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(503))), catalog=catalog)
    retrieval = FallbackRetrieval(primary, LocalRetrieval(catalog))
    client = TestClient(create_app(db, DEMO_USERS, MemoryStore(), retrieval=retrieval))
    headers = {"Authorization": "Bearer demo-analyst"}

    result = client.post("/v1/questions", headers=headers, json={"question": "贷款余额的血缘"}).json()
    trace = client.get(f"/v1/traces/{result['trace_id']}", headers=headers).json()

    assert result["status"] == "ok"
    assert trace["retrieval_mode"] == "certified-lexical"


def test_multi_intent_trace_keeps_retrieval_backend(client):
    headers = {"Authorization": "Bearer demo-analyst"}
    result = client.post("/v1/questions", headers=headers, json={
        "question": "逾期率口径和血缘",
    }).json()
    trace = client.get(f"/v1/traces/{result['trace_id']}", headers=headers).json()

    assert result["status"] == "ok" and result["intent"] == "multi"
    assert trace["retrieval_mode"] == "certified-lexical"
    branch_modes = {span["name"]: span["retrieval_mode"] for span in trace["spans"]
                    if span["name"] in {"definition", "lineage"}}
    assert branch_modes == {"definition": "certified-lexical", "lineage": "certified-lexical"}


def test_structured_query_has_a_trace(client):
    headers = {"Authorization": "Bearer demo-analyst"}
    result = client.post("/v1/metrics/query", headers=headers, json={
        "metric_id": "loan_balance", "start_date": "2026-03-02", "end_date": "2026-03-02",
    }).json()

    assert result["status"] == "ok"
    trace = client.get(f"/v1/traces/{result['trace_id']}", headers=headers)
    assert trace.status_code == 200
    assert {span["name"] for span in trace.json()["spans"]} >= {"question", "query"}


def test_exploration_question_has_a_trace(client):
    headers = {"Authorization": "Bearer demo-analyst"}
    result = client.post("/v1/questions", headers=headers, json={
        "question": "探索试算 2026-03-01 申请拒绝率",
    }).json()

    assert result["status"] == "ok" and result["certification_status"] == "NON_CERTIFIED"
    trace = client.get(f"/v1/traces/{result['trace_id']}", headers=headers)
    assert trace.status_code == 200
    assert {span["name"] for span in trace.json()["spans"]} >= {"question", "exploration"}


def test_structured_exploration_has_a_trace(client):
    headers = {"Authorization": "Bearer demo-analyst"}
    result = client.post("/v1/explorations", headers=headers, json={
        "metric_id": "application_rejection_rate", "start_date": "2026-03-01",
        "end_date": "2026-03-01", "accept_non_certified": True,
    }).json()

    assert result["status"] == "ok" and result["certification_status"] == "NON_CERTIFIED"
    trace = client.get(f"/v1/traces/{result['trace_id']}", headers=headers)
    assert trace.status_code == 200
    assert {span["name"] for span in trace.json()["spans"]} >= {"question", "exploration"}


def test_trace_rejects_secret_bearing_evidence_ids(client, db):
    headers = {"Authorization": "Bearer demo-analyst"}
    result = client.post("/v1/questions", headers=headers, json={"question": "贷款余额的血缘"}).json()
    traces.save(db, result["trace_id"], None, [{
        "name": "question", "status": "ok", "duration_ms": 1.0,
        "evidence_ids": ["metric:loan_balance", "chunk:token=secret", "schema:base:FTM AGT LOAN"],
    }], "disabled", None, "certified-lexical")

    trace = client.get(f"/v1/traces/{result['trace_id']}", headers=headers).json()
    assert trace["spans"][0]["evidence_ids"] == ["metric:loan_balance"]
    assert "secret" not in str(trace)


def test_trace_does_not_store_secret_bearing_model_metadata(client, db):
    headers = {"Authorization": "Bearer demo-analyst"}
    result = client.post("/v1/questions", headers=headers, json={"question": "贷款余额的血缘"}).json()
    traces.save(db, result["trace_id"], None, [{
        "name": "question", "status": "ok", "duration_ms": 1.0, "evidence_ids": [],
    }], "deepseek-intent-only token=secret", "deepseek-flash?key=secret",
                "certified-lexical password=secret")

    trace = client.get(f"/v1/traces/{result['trace_id']}", headers=headers).json()
    assert trace["router_mode"] == "unknown"
    assert trace["model"] is None
    assert trace["retrieval_mode"] == "unknown"
    assert "secret" not in str(trace)


def test_trace_retention_prunes_only_expired_spans_not_audit(client, db):
    headers = {"Authorization": "Bearer demo-analyst"}
    old = client.post("/v1/questions", headers=headers, json={"question": "贷款余额的血缘"}).json()
    recent = client.post("/v1/questions", headers=headers, json={"question": "申请笔数定义"}).json()
    with connect(db) as conn:
        conn.execute("UPDATE question_trace_span SET created_at=? WHERE trace_id=?",
                     ("2020-01-01T00:00:00+00:00", old["trace_id"]))

    removed = traces.prune(db, retention_days=30)

    assert removed >= 1
    assert client.get(f"/v1/traces/{old['trace_id']}", headers=headers).status_code == 404
    assert client.get(f"/v1/traces/{recent['trace_id']}", headers=headers).status_code == 200
    assert client.get(f"/v1/audit/{old['trace_id']}", headers=headers).status_code == 200


def test_new_trace_write_enforces_retention_without_deleting_audit(client, db):
    headers = {"Authorization": "Bearer demo-analyst"}
    old = client.post("/v1/questions", headers=headers, json={"question": "贷款余额的血缘"}).json()
    with connect(db) as conn:
        conn.execute("UPDATE question_trace_span SET created_at=? WHERE trace_id=?",
                     ("2020-01-01T00:00:00+00:00", old["trace_id"]))

    current = client.post("/v1/questions", headers=headers, json={"question": "申请笔数定义"}).json()

    assert client.get(f"/v1/traces/{current['trace_id']}", headers=headers).status_code == 200
    assert client.get(f"/v1/traces/{old['trace_id']}", headers=headers).status_code == 404
    assert client.get(f"/v1/audit/{old['trace_id']}", headers=headers).status_code == 200
