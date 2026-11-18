import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from credit.agent import QuestionService
from credit.api import create_app
from credit.async_tasks import QuestionTaskManager, TaskExpired, TaskQueueFull
from credit.demo import bootstrap
from credit.memory import MemoryStore
from credit.models import Question, Result, User
from credit.security import DEMO_USERS


def auth(token="demo-analyst"):
    return {"Authorization": f"Bearer {token}"}


def test_async_question_task_returns_request_id_then_retrievable_result(client):
    accepted = client.post("/v1/question-tasks", headers=auth(), json={
        "question": "逾期率如何计算",
    })

    assert accepted.status_code == 202
    request_id = accepted.json()["request_id"]
    assert accepted.json()["status"] in {"queued", "running"}

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/v1/question-tasks/{request_id}", headers=auth())
        assert response.status_code == 200
        body = response.json()
        if body["status"] == "completed":
            break
        time.sleep(0.01)

    assert body["status"] == "completed"
    assert body["result"]["status"] == "ok"
    assert "业务日末逾期本金 / 剩余本金" in body["result"]["message"]


def test_compound_async_result_keeps_metric_definition_and_value_on_one_version(client):
    accepted = client.post("/v1/question-tasks", headers=auth(), json={
        "question": "2026-03-02 逾期率查询并说明口径",
    })
    assert accepted.status_code == 202
    request_id = accepted.json()["request_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        body = client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()
        if body["status"] == "completed":
            break
        time.sleep(0.01)

    sections = body["result"]["sections"]
    metric_citations = [citation for section in sections for citation in section["citations"]
                        if citation["id"] == "metric:overdue_rate"]
    assert len(metric_citations) >= 2
    assert len({citation["version"] for citation in metric_citations}) == 1
    assert len({citation["updated_at"] for citation in metric_citations}) == 1


def test_async_task_exposes_each_parallel_intent_as_it_finishes(client, monkeypatch):
    service = client.app.state.service
    original_invoke = service.workflow.invoke

    def delayed_lineage(payload):
        if payload.get("forced_intent") == "lineage":
            time.sleep(0.3)
        return original_invoke(payload)

    monkeypatch.setattr(service.workflow, "invoke", delayed_lineage)
    accepted = client.post("/v1/question-tasks", headers=auth(), json={
        "question": "逾期率口径和血缘",
    })
    assert accepted.status_code == 202
    request_id = accepted.json()["request_id"]

    deadline = time.monotonic() + 3
    observed_partial_progress = False
    while time.monotonic() < deadline:
        state = client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()
        progress = state["intent_progress"]
        if progress.get("definition") == "ok" and progress.get("lineage") == "running":
            observed_partial_progress = True
            break
        if state["status"] == "completed":
            break
        time.sleep(0.01)

    assert observed_partial_progress, state["intent_progress"]


def test_async_task_updates_planned_intents_from_configured_router(client):
    class FixedRouter:
        def route(self, _question):
            return ["definition", "lineage"]

    client.app.state.service.intent_router = FixedRouter()
    accepted = client.post("/v1/question-tasks", headers=auth(), json={"question": "逾期率呢"})
    request_id = accepted.json()["request_id"]

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()
        if state["status"] == "completed":
            break
        time.sleep(0.01)

    assert state["status"] == "completed"
    assert state["planned_intents"] == ["definition", "lineage"]
    assert state["intent_progress"] == {"definition": "ok", "lineage": "ok"}


def test_initial_other_plan_does_not_publish_metric_metadata(client):
    from credit.models import Question

    plan = client.app.state.question_tasks._plan(Question(question="请用逾期率写一首诗"))

    assert plan["planned_intents"] == ["other"]
    assert plan["metric_id"] is None
    assert plan["metric_version"] is None


def test_initial_calculation_plan_binds_the_certified_ratio_metric(client):
    from credit.models import Question

    plan = client.app.state.question_tasks._plan(Question(
        question="逾期本金 75 万、剩余本金 1000 万，逾期率是多少？"))

    assert plan["planned_intents"] == ["calculation"]
    assert plan["metric_id"] == "overdue_rate"
    assert plan["metric_version"]


def test_async_followup_uses_same_user_session_context_without_llm_guessing(client):
    class RouterThatRejectsEllipsis:
        def route(self, question):
            return None if question.startswith("2026-") else []

    client.app.state.service.intent_router = RouterThatRejectsEllipsis()

    def submit_and_wait(question):
        accepted = client.post("/v1/question-tasks", headers=auth(), json={
            "question": question, "session_id": "async-session-context",
        })
        assert accepted.status_code == 202
        request_id = accepted.json()["request_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()
            if state["status"] == "completed":
                return state["result"]
            time.sleep(0.01)
        raise AssertionError(f"async task did not complete: {state}")

    first = submit_and_wait("2026-03-02 贷款余额")
    second = submit_and_wait("那按产品呢")

    assert first["status"] == "ok"
    assert second["status"] == "ok" and second["metric_id"] == "loan_balance"
    assert len(second["rows"]) == 2


def test_async_question_task_is_not_visible_to_another_identity(client):
    accepted = client.post("/v1/question-tasks", headers=auth(), json={
        "question": "逾期率如何计算",
    })
    request_id = accepted.json()["request_id"]

    response = client.get(f"/v1/question-tasks/{request_id}", headers=auth("demo-visitor"))

    assert response.status_code == 404


def test_async_question_task_submission_requires_authentication(client):
    response = client.post("/v1/question-tasks", json={"question": "逾期率如何计算"})

    assert response.status_code == 401


def test_retrying_submit_with_same_idempotency_key_returns_original_request(client):
    headers = {**auth(), "Idempotency-Key": "retry-123"}
    first = client.post("/v1/question-tasks", headers=headers, json={"question": "逾期率如何计算"})
    retried = client.post("/v1/question-tasks", headers=headers, json={"question": "逾期率如何计算"})

    assert first.status_code == 202
    assert retried.status_code == 202
    assert retried.json()["request_id"] == first.json()["request_id"]


def test_idempotency_key_cannot_be_reused_for_a_different_question(client):
    headers = {**auth(), "Idempotency-Key": "retry-456"}
    first = client.post("/v1/question-tasks", headers=headers, json={"question": "逾期率如何计算"})
    conflict = client.post("/v1/question-tasks", headers=headers, json={"question": "贷款余额如何计算"})

    assert first.status_code == 202
    assert conflict.status_code == 409


def test_concurrent_retries_with_one_key_create_one_task(client):
    headers = {**auth(), "Idempotency-Key": "parallel-retry-1"}

    def submit():
        return client.post("/v1/question-tasks", headers=headers,
                           json={"question": "逾期率如何计算"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: submit(), range(2)))

    assert [response.status_code for response in responses] == [202, 202]
    assert len({response.json()["request_id"] for response in responses}) == 1


def test_idempotency_key_is_scoped_to_authenticated_user(client):
    analyst = client.post("/v1/question-tasks", headers={**auth(), "Idempotency-Key": "shared-key"},
                          json={"question": "逾期率如何计算"})
    visitor = client.post("/v1/question-tasks", headers={**auth("demo-visitor"),
                                                       "Idempotency-Key": "shared-key"},
                          json={"question": "逾期率如何计算"})

    assert analyst.status_code == 202
    assert visitor.status_code == 202
    assert analyst.json()["request_id"] != visitor.json()["request_id"]


def test_async_submission_uses_the_existing_per_user_rate_limit(client):
    client.app.state.question_tasks.max_outstanding = 1000
    responses = [client.post("/v1/question-tasks", headers=auth(), json={
        "question": "逾期率口径",
    }) for _ in range(60)]

    limited = client.post("/v1/question-tasks", headers=auth(), json={"question": "逾期率口径"})

    assert all(response.status_code == 202 for response in responses)
    assert limited.status_code == 429


def test_async_resume_requires_the_original_owner_and_explicit_question(client):
    accepted = client.post("/v1/question-tasks", headers=auth(), json={
        "question": "逾期率如何计算",
    })
    request_id = accepted.json()["request_id"]

    denied = client.post(f"/v1/question-tasks/{request_id}/resume", headers=auth("demo-visitor"),
                         json={"question": "逾期率如何计算"})

    assert denied.status_code == 404


def test_checkpoint_persists_no_question_or_result_payload(client, db: Path):
    question = "逾期率如何计算"
    accepted = client.post("/v1/question-tasks", headers=auth(), json={"question": question})
    request_id = accepted.json()["request_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/v1/question-tasks/{request_id}", headers=auth())
        if response.json()["status"] == "completed":
            break
        time.sleep(0.01)

    state = response.json()
    assert state["planned_intents"] == ["definition"]
    assert state["intent_progress"] == {"definition": "ok"}
    assert state["metric_id"] == "overdue_rate"
    metric_citation = next(c for c in state["result"]["citations"] if c["id"] == "metric:overdue_rate")
    assert state["metric_version"] == metric_citation["version"]
    assert state["metric_updated_at"] == metric_citation["updated_at"]
    assert state["trace_id"] == state["result"]["trace_id"]
    assert "metric:overdue_rate" in state["citation_ids"]
    with sqlite3.connect(db) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(question_task_checkpoint)")}
    assert not {"question", "prompt", "result", "result_json", "authorization"} & columns
    assert question.encode("utf-8") not in db.read_bytes()


def test_cached_task_result_is_invalidated_when_current_permission_is_revoked(db: Path, tmp_path: Path):
    bootstrap(db, tmp_path / "sources", include_history=False)

    class MutableIdentity:
        user = User("same-person", frozenset({"analyst"}), frozenset({"credit"}), "policy-1")

        def resolve(self, token):
            return self.user

    identity = MutableIdentity()
    with TestClient(create_app(db, {}, identity_provider=identity)) as client:
        response = client.post("/v1/question-tasks", headers=auth("signed-token"),
                               json={"question": "逾期率如何计算"})
        request_id = response.json()["request_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = client.get(f"/v1/question-tasks/{request_id}", headers=auth("signed-token")).json()
            if status["status"] == "completed":
                break
            time.sleep(0.01)

        identity.user = User("same-person", frozenset({"visitor"}), frozenset({"credit"}), "policy-2")
        revoked = client.get(f"/v1/question-tasks/{request_id}", headers=auth("signed-token"))

    assert revoked.status_code == 200
    assert revoked.json()["status"] == "interrupted"
    assert revoked.json()["result"] is None
    assert revoked.json()["metric_id"] is None
    assert revoked.json()["citation_ids"] == []
    assert revoked.json()["trace_id"] is None


def test_interrupted_task_can_resume_after_service_restart_with_resubmitted_question(db: Path):
    with TestClient(create_app(db, DEMO_USERS, MemoryStore())) as first_client:
        accepted = first_client.post("/v1/question-tasks", headers=auth(),
                                     json={"question": "逾期率如何计算"})
        request_id = accepted.json()["request_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = first_client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()
            if state["status"] == "completed":
                break
            time.sleep(0.01)

    with TestClient(create_app(db, DEMO_USERS, MemoryStore())) as restarted_client:
        interrupted = restarted_client.get(f"/v1/question-tasks/{request_id}", headers=auth())
        assert interrupted.status_code == 200
        assert interrupted.json()["status"] == "interrupted"
        resumed = restarted_client.post(f"/v1/question-tasks/{request_id}/resume", headers=auth(),
                                        json={"question": "逾期率如何计算"})
        assert resumed.status_code == 202
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = restarted_client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()
            if result["status"] == "completed":
                break
            time.sleep(0.01)

    assert result["status"] == "completed"
    assert result["result"]["status"] == "ok"


def test_only_one_concurrent_resume_claims_an_interrupted_task(client, monkeypatch):
    accepted = client.post("/v1/question-tasks", headers=auth(),
                           json={"question": "逾期率如何计算"})
    request_id = accepted.json()["request_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()
        if state["status"] == "completed":
            break
        time.sleep(0.01)
    assert state["status"] == "completed"

    manager = client.app.state.question_tasks
    with sqlite3.connect(manager.db) as connection:
        connection.execute("UPDATE question_task_checkpoint SET status='interrupted' WHERE request_id=?",
                           (request_id,))
    manager.results.pop(request_id, None)

    original_record = manager._record
    both_read_interrupted = threading.Barrier(2)

    def synchronized_record(task_id, user):
        record = original_record(task_id, user)
        both_read_interrupted.wait(timeout=2)
        return record

    monkeypatch.setattr(manager, "_record", synchronized_record)

    def resume():
        return client.post(f"/v1/question-tasks/{request_id}/resume", headers=auth(),
                           json={"question": "逾期率如何计算"}).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(lambda _: resume(), range(2)))

    assert statuses == [202, 409]


def test_cancelling_completed_task_removes_its_retrievable_result(client):
    accepted = client.post("/v1/question-tasks", headers=auth(), json={"question": "逾期率如何计算"})
    request_id = accepted.json()["request_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()
        if state["status"] == "completed":
            break
        time.sleep(0.01)

    cancelled = client.delete(f"/v1/question-tasks/{request_id}", headers=auth())
    state = client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()

    assert cancelled.status_code == 204
    assert state["status"] == "cancelled"
    assert state["result"] is None


def test_cancelling_running_task_prevents_late_result_publication(db: Path):
    started, release = threading.Event(), threading.Event()
    service = QuestionService(db, MemoryStore())

    def slow_answer(question, resolver, **kwargs):
        started.set()
        assert release.wait(3)
        return Result(status="ok", intent="definition", message="must not be published")

    service.ask = slow_answer
    manager = QuestionTaskManager(db, service)
    user = DEMO_USERS["demo-analyst"]
    try:
        accepted = manager.submit(Question(question="贷款余额口径"), lambda: user, user)
        request_id = accepted["request_id"]
        assert started.wait(1)

        manager.cancel(request_id, user)
        release.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and request_id in manager.futures:
            time.sleep(0.01)

        state = manager.get(request_id, user)
        assert state["status"] == "cancelled"
        assert state["result"] is None
        assert request_id not in manager.results
    finally:
        release.set()
        manager.close()


def test_cancelled_running_worker_still_occupies_queue_capacity(db: Path):
    started, release = threading.Event(), threading.Event()
    service = QuestionService(db, MemoryStore())

    def slow_answer(question, resolver, **kwargs):
        started.set()
        assert release.wait(3)
        return Result(status="ok", intent="definition", message="done")

    service.ask = slow_answer
    manager = QuestionTaskManager(db, service, workers=1, max_outstanding=1)
    user = DEMO_USERS["demo-analyst"]
    try:
        first = manager.submit(Question(question="贷款余额口径"), lambda: user, user)
        assert started.wait(1)
        manager.cancel(first["request_id"], user)

        with pytest.raises(TaskQueueFull):
            manager.submit(Question(question="逾期率口径"), lambda: user, user)

        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and first["request_id"] in manager.futures:
            time.sleep(0.01)
        second = manager.submit(Question(question="逾期率口径"), lambda: user, user)
        assert second["request_id"] != first["request_id"]
    finally:
        release.set()
        manager.close()


def test_checkpoint_expires_after_idle_ttl_or_absolute_maximum(db: Path):
    now = [100_000.0]
    started, release = threading.Event(), threading.Event()
    service = QuestionService(db, MemoryStore())

    def wait_for_resume(question, resolver, **kwargs):
        started.set()
        release.wait(3)
        return Result(status="ok", intent="definition", message="ok")

    service.ask = wait_for_resume
    manager = QuestionTaskManager(db, service, clock=lambda: now[0])
    user = User("ttl-user", frozenset({"analyst"}), frozenset({"credit"}))
    try:
        accepted = manager.submit(Question(question="贷款余额口径"), lambda: user, user)
        request_id = accepted["request_id"]
        assert started.wait(1)
        assert manager.get(request_id, user)["status"] == "running"
        assert manager._record(request_id, user)["expires_at"] == now[0] + 7200

        for hour in range(1, 24):
            now[0] = 100_000.0 + hour * 3600
            manager.get(request_id, user)
        record = manager._record(request_id, user)
        assert record["expires_at"] == 100_000.0 + 86400

        now[0] = 100_000.0 + 86400
        with pytest.raises(TaskExpired):
            manager.get(request_id, user)
        with sqlite3.connect(db) as connection:
            assert connection.execute("SELECT COUNT(*) FROM question_task_checkpoint WHERE request_id=?",
                                      (request_id,)).fetchone()[0] == 0
    finally:
        release.set()
        manager.close()


def test_in_memory_result_expires_before_the_longer_checkpoint_window(db: Path):
    now = [100_000.0]
    manager = QuestionTaskManager(db, QuestionService(db, MemoryStore()), result_ttl=900,
                                  clock=lambda: now[0])
    user = DEMO_USERS["demo-analyst"]
    try:
        accepted = manager.submit(Question(question="逾期率如何计算"), lambda: user, user)
        request_id = accepted["request_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = manager.get(request_id, user)
            if state["status"] == "completed":
                break
            time.sleep(0.01)
        assert state["status"] == "completed"

        now[0] += 901
        expired_result = manager.get(request_id, user)

        assert expired_result["status"] == "interrupted"
        assert expired_result["result"] is None
        assert expired_result["resume_required"] is True
    finally:
        manager.close()
