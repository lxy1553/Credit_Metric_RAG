import multiprocessing
import os
import time
import uuid

import pytest
import redis
from fastapi.testclient import TestClient

from credit.agent import QuestionService
from credit.api import create_app, from_environment
from credit.memory import StatelessMemory
from credit.models import Question
from credit.redis_async_tasks import RedisQuestionTaskManager
from credit.security import DEMO_USERS
from credit.task_payloads import TaskPayloadCipher


def _run_task_worker_process(url, db_path, stream, key_prefix, request_id, output):
    task_client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    users = {user.user_id: user for user in DEMO_USERS.values()}
    manager = RedisQuestionTaskManager(
        QuestionService(db_path, StatelessMemory()), users, task_client,
        TaskPayloadCipher({"worker-test": b"p" * 32}, active_key_id="worker-test"),
        stream=stream, key_prefix=key_prefix, workers=1, lease_ms=1000,
    )
    deadline = time.monotonic() + 10
    status = "timeout"
    while time.monotonic() < deadline:
        status = task_client.hget(f"{key_prefix}:{request_id}", "status") or status
        if status in {"completed", "failed"}:
            break
        time.sleep(0.02)
    manager.close()
    task_client.close()
    output.put(status)


@pytest.fixture
def redis_task_clients(db):
    url = os.getenv("CREDIT_TASK_REDIS_URL")
    if not url:
        if os.getenv("CREDIT_TASK_REDIS_REQUIRED") == "1":
            pytest.fail("release preflight requires CREDIT_TASK_REDIS_URL")
        pytest.skip("set CREDIT_TASK_REDIS_URL to run Redis async task API tests")
    client_a = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    client_b = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    try:
        client_a.ping()
    except redis.RedisError as exc:
        client_a.close()
        client_b.close()
        if os.getenv("CREDIT_TASK_REDIS_REQUIRED") == "1":
            pytest.fail(f"release preflight task Redis is unavailable: {type(exc).__name__}")
        pytest.skip(f"task Redis is unavailable: {type(exc).__name__}")
    prefix = f"codex:test:redis-async:{uuid.uuid4().hex}"
    cipher = TaskPayloadCipher({"test-key": b"a" * 32}, active_key_id="test-key")
    users = {user.user_id: user for user in DEMO_USERS.values()}
    service_a = QuestionService(db, StatelessMemory())
    service_b = QuestionService(db, StatelessMemory())
    manager_a = RedisQuestionTaskManager(service_a, users, client_a, cipher,
                                         stream=f"{prefix}:stream", key_prefix=f"{prefix}:task")
    manager_b = RedisQuestionTaskManager(service_b, users, client_b, cipher,
                                         stream=f"{prefix}:stream", key_prefix=f"{prefix}:task")
    app_a = create_app(db, DEMO_USERS, question_tasks=manager_a)
    app_b = create_app(db, DEMO_USERS, question_tasks=manager_b)
    with TestClient(app_a) as test_a, TestClient(app_b) as test_b:
        yield test_a, test_b, client_a, prefix, manager_a, manager_b
    manager_a.close()
    manager_b.close()
    keys = list(client_a.scan_iter(match=f"{prefix}:*"))
    if keys:
        client_a.delete(*keys)
    client_a.close()
    client_b.close()


def test_async_question_api_shares_idempotent_result_and_owner_acl(redis_task_clients):
    client_a, client_b, redis_client, prefix, _, _ = redis_task_clients
    headers = {"Authorization": "Bearer demo-analyst", "Idempotency-Key": "shared-submit-1"}
    question = "逾期率如何计算"

    first = client_a.post("/v1/question-tasks", headers=headers, json={"question": question})
    retry = client_b.post("/v1/question-tasks", headers=headers, json={"question": question})
    request_id = first.json()["request_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client_b.get(f"/v1/question-tasks/{request_id}", headers=headers)
        if result.json()["status"] == "completed":
            break
        time.sleep(0.01)

    assert first.status_code == retry.status_code == 202
    assert retry.json()["request_id"] == request_id
    assert result.json()["status"] == "completed"
    assert result.json()["result"]["status"] == "ok"
    denied = client_b.get(f"/v1/question-tasks/{request_id}",
                          headers={"Authorization": "Bearer demo-visitor"})
    assert denied.status_code == 404
    state = redis_client.hgetall(f"{prefix}:task:{request_id}")
    assert question not in str(state)
    assert redis_client.xlen(f"{prefix}:stream") == 0


def test_environment_selects_redis_backend_and_readiness_checks_it(db, monkeypatch, redis_task_clients):
    url = os.environ["CREDIT_TASK_REDIS_URL"]
    monkeypatch.setenv("CREDIT_DEPLOYMENT_PROFILE", "local")
    monkeypatch.setenv("CREDIT_TASK_BACKEND", "redis")
    monkeypatch.setenv("CREDIT_TASK_REDIS_URL", url)
    monkeypatch.setenv("CREDIT_TASK_KEYRING", '{"local-test":"' + "c" * 64 + '"}')
    monkeypatch.setenv("CREDIT_TASK_ACTIVE_KEY_ID", "local-test")
    monkeypatch.setenv("CREDIT_AUTH_MODE", "demo")
    monkeypatch.setenv("CREDIT_DEMO_MODE", "1")
    monkeypatch.setenv("CREDIT_MEMORY", "redis")
    monkeypatch.setenv("CREDIT_REDIS_URL", url)
    monkeypatch.setenv("CREDIT_RESPONSE_CACHE", "off")
    monkeypatch.setenv("CREDIT_RETRIEVAL", "local")
    monkeypatch.setenv("CREDIT_DB", str(db))

    app = from_environment()
    with TestClient(app) as client:
        assert client.get("/readyz").json() == {"status": "ready"}
        assert isinstance(app.state.question_tasks, RedisQuestionTaskManager)


def test_api_submission_is_executed_and_recoverable_by_another_process(db):
    url = os.environ.get("CREDIT_TASK_REDIS_URL")
    if not url:
        if os.getenv("CREDIT_TASK_REDIS_REQUIRED") == "1":
            pytest.fail("release preflight requires CREDIT_TASK_REDIS_URL")
        pytest.skip("set CREDIT_TASK_REDIS_URL to run cross-process task tests")
    client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    prefix = f"codex:test:redis-process:{uuid.uuid4().hex}:task"
    stream = f"{prefix}:stream"
    cipher = TaskPayloadCipher({"worker-test": b"p" * 32}, active_key_id="worker-test")
    users = {user.user_id: user for user in DEMO_USERS.values()}
    service = QuestionService(db, StatelessMemory())
    manager = RedisQuestionTaskManager(service, users, client, cipher, stream=stream,
                                       key_prefix=prefix, workers=0)
    user = DEMO_USERS["demo-analyst"]
    receipt = manager.submit(Question(question="逾期率如何计算"), lambda: user, user)
    output = multiprocessing.get_context("spawn").Queue()
    process = multiprocessing.get_context("spawn").Process(
        target=_run_task_worker_process,
        args=(url, str(db), stream, prefix, receipt["request_id"], output),
    )
    process.start()
    process.join(timeout=12)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)

    assert process.exitcode == 0
    assert output.get(timeout=2) == "completed"
    state = manager.get(receipt["request_id"], user)
    assert state["status"] == "completed"
    assert state["result"]["status"] == "ok"
    manager.close()
    keys = list(client.scan_iter(match=f"{prefix}:*"))
    if keys:
        client.delete(*keys)
    client.close()
