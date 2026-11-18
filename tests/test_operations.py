from types import SimpleNamespace

from credit.alerts import AlertPolicy
from credit.operations import OperationsMetrics


def test_operations_summary_is_platform_only_and_aggregated(client):
    question = "申请通过率怎么计算"
    response = client.post("/v1/questions", headers={"Authorization": "Bearer demo-analyst"},
                           json={"question": question})
    assert response.status_code == 200

    assert client.get("/v1/operations/summary", headers={"Authorization": "Bearer demo-analyst"}).status_code == 403
    summary = client.get("/v1/operations/summary", headers={"Authorization": "Bearer demo-platform"})
    assert summary.status_code == 200
    body = summary.json()
    assert body["scope"] == "process-local-aggregates-only"
    assert body["completed_requests"] == 1
    assert body["outcomes"] == {"ok": 1}
    assert body["intents"] == {"definition": 1}
    assert body["latency_ms"]["samples"] == 1 and body["latency_ms"]["p50"] is not None
    # The summary is intentionally unable to reveal data, identities or request contents.
    assert question not in str(body)
    assert "analyst-001" not in str(body)
    assert "SELECT" not in str(body)


def test_slo_evaluator_handles_low_samples_latency_and_system_failures():
    metrics = OperationsMetrics()
    policy = AlertPolicy(min_latency_samples=2, max_p95_ms=10)
    assert policy.evaluate(metrics.summary())["state"] == "unknown"

    metrics.record(SimpleNamespace(status="ok", intent="query", from_cache=False), 0.001)
    metrics.record(SimpleNamespace(status="ok", intent="query", from_cache=False), 0.020)
    warning = policy.evaluate(metrics.summary())
    assert warning["state"] == "warning" and warning["reasons"] == ["p95_latency_exceeded"]

    metrics.record_system_failure()
    critical = policy.evaluate(metrics.summary())
    assert critical["state"] == "critical" and critical["reasons"] == ["system_failures_observed"]


def test_alert_endpoint_is_platform_only_and_unknown_with_one_sample(client):
    client.post("/v1/questions", headers={"Authorization": "Bearer demo-analyst"},
                json={"question": "申请通过率怎么计算"})
    assert client.get("/v1/operations/alerts", headers={"Authorization": "Bearer demo-analyst"}).status_code == 403
    response = client.get("/v1/operations/alerts", headers={"Authorization": "Bearer demo-platform"})
    assert response.status_code == 200
    assert response.json()["state"] == "unknown"
    assert response.json()["mode"] == "evaluation-only-no-notification"
