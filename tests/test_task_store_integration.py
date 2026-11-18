import multiprocessing
import os
import time
import uuid

import pytest
import redis

from credit.task_payloads import TaskPayloadCipher
from credit.task_store import RedisTaskStore


def _claim_from_child(url, prefix, request_id, output):
    client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    cipher = TaskPayloadCipher({"test-key": b"s" * 32}, active_key_id="test-key")
    try:
        output.put(RedisTaskStore(client, cipher, key_prefix=prefix).claim(
            request_id, worker_id="worker-child"))
    finally:
        client.close()


@pytest.fixture
def task_stores():
    url = os.getenv("CREDIT_TASK_REDIS_URL")
    if not url:
        if os.getenv("CREDIT_TASK_REDIS_REQUIRED") == "1":
            pytest.fail("release preflight requires CREDIT_TASK_REDIS_URL")
        pytest.skip("set CREDIT_TASK_REDIS_URL to run Redis task store integration tests")
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
    prefix = f"codex:test:task-store:{uuid.uuid4().hex}"
    cipher = TaskPayloadCipher({"test-key": b"s" * 32}, active_key_id="test-key")
    yield (RedisTaskStore(client_a, cipher, key_prefix=prefix),
           RedisTaskStore(client_b, cipher, key_prefix=prefix), client_a)
    keys = list(client_a.scan_iter(match=f"{prefix}:*"))
    if keys:
        client_a.delete(*keys)
    client_a.close()
    client_b.close()


def test_task_metadata_and_encrypted_payload_are_shared_across_clients(task_stores):
    first, second, client = task_stores
    first.create("task-1", owner_hash="owner-a", metadata={"planned_intents": ["definition"]},
                 payload='{"question":"private question"}', ttl_seconds=60)

    state = second.get("task-1", owner_hash="owner-a")
    claimed = second.claim("task-1", worker_id="worker-b")
    stored = client.hgetall(first.redis_key("task-1"))

    assert state["status"] == "queued"
    assert state["metadata"] == {"planned_intents": ["definition"]}
    assert claimed["payload"] == '{"question":"private question"}'
    assert "private question" not in str(stored)
    assert second.get("task-1", owner_hash="owner-b") is None


def test_owner_can_extend_metadata_ttl_but_another_identity_cannot(task_stores):
    first, second, client = task_stores
    first.create("task-touch", owner_hash="owner-a", metadata={"expires_at": "old"},
                 payload='{"question":"touch"}', ttl_seconds=60)
    key = first.redis_key("task-touch")
    before = client.ttl(key)

    assert first.touch("task-touch", owner_hash="owner-b", metadata={"expires_at": "wrong"},
                       ttl_seconds=120) is False
    assert first.touch("task-touch", owner_hash="owner-a", metadata={"expires_at": "new"},
                       ttl_seconds=120) is True
    assert second.get("task-touch", owner_hash="owner-a")["metadata"]["expires_at"] == "new"
    assert client.ttl(key) > before


def test_task_registration_and_stream_enqueue_are_atomic_and_idempotent(task_stores):
    first, _, client = task_stores
    stream = f"{first.key_prefix}:stream"
    idem_key = f"{first.key_prefix}:idempotency:owner:retry"

    created = first.create_and_enqueue(
        "task-atomic", stream=stream, idempotency_key=idem_key, owner_hash="owner-a",
        metadata={"planned_intents": ["definition"]}, payload='{"question":"same"}',
        fingerprint="a" * 64, ttl_seconds=60,
    )
    retried = first.create_and_enqueue(
        "task-different-id", stream=stream, idempotency_key=idem_key, owner_hash="owner-a",
        metadata={}, payload='{"question":"same"}', fingerprint="a" * 64, ttl_seconds=60,
    )
    conflict = first.create_and_enqueue(
        "task-conflict", stream=stream, idempotency_key=idem_key, owner_hash="owner-a",
        metadata={}, payload='{"question":"different"}', fingerprint="b" * 64, ttl_seconds=60,
    )

    assert created == {"created": True, "request_id": "task-atomic"}
    assert retried == {"created": False, "request_id": "task-atomic"}
    assert conflict == {"conflict": True}
    assert client.xlen(stream) == 1
    assert first.get("task-atomic", owner_hash="owner-a")["status"] == "queued"
    before = client.ttl(idem_key)
    assert first.touch("task-atomic", owner_hash="owner-a", metadata={"expires_at": "extended"},
                       ttl_seconds=120)
    assert client.ttl(idem_key) > before


def test_shared_queue_limit_rejects_new_submission_atomically(task_stores):
    first, _, client = task_stores
    stream = f"{first.key_prefix}:limited-stream"
    result = first.create_and_enqueue(
        "task-limit-a", stream=stream, idempotency_key=None, owner_hash="owner-a", metadata={},
        payload='{"question":"first"}', fingerprint="c" * 64, ttl_seconds=60, max_outstanding=1,
    )
    full = first.create_and_enqueue(
        "task-limit-b", stream=stream, idempotency_key=None, owner_hash="owner-a", metadata={},
        payload='{"question":"second"}', fingerprint="d" * 64, ttl_seconds=60, max_outstanding=1,
    )

    assert result["created"] is True
    assert full == {"queue_full": True}
    assert client.xlen(stream) == 1


def test_only_one_worker_can_claim_and_complete_a_shared_task(task_stores):
    first, second, client = task_stores
    first.create("task-2", owner_hash="owner-a", metadata={}, payload='{"q":"x"}', ttl_seconds=60)

    claim_a = first.claim("task-2", worker_id="worker-a")
    claim_b = second.claim("task-2", worker_id="worker-b")
    completed = first.complete("task-2", worker_id="worker-a",
                               result_json='{"answer":"synthetic"}', ttl_seconds=60)

    assert claim_a["payload"] == '{"q":"x"}'
    assert claim_b is None
    assert completed is True
    assert second.get_result("task-2", owner_hash="owner-a") == '{"answer":"synthetic"}'
    assert second.get_result("task-2", owner_hash="owner-b") is None
    assert "result" not in client.hgetall(first.redis_key("task-2"))
    encrypted_result = client.get(first.result_key("task-2"))
    assert "synthetic" not in encrypted_result


def test_task_can_be_claimed_by_a_separate_worker_process(task_stores):
    first, second, client = task_stores
    first.create("task-process", owner_hash="owner-a", metadata={}, payload='{"q":"cross-process"}',
                 ttl_seconds=60)
    output = multiprocessing.get_context("spawn").Queue()
    process = multiprocessing.get_context("spawn").Process(
        target=_claim_from_child,
        args=(os.environ["CREDIT_TASK_REDIS_URL"], first.key_prefix, "task-process", output),
    )
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)

    assert process.exitcode == 0
    assert output.get(timeout=2) == {"request_id": "task-process", "payload": '{"q":"cross-process"}'}
    assert second.get("task-process", owner_hash="owner-a")["status"] == "running"


def test_stale_worker_cannot_publish_after_task_is_cancelled(task_stores):
    first, second, _ = task_stores
    first.create("task-3", owner_hash="owner-a", metadata={}, payload='{"q":"x"}', ttl_seconds=60)
    assert first.claim("task-3", worker_id="worker-a") is not None
    assert second.transition("task-3", expected="running", target="cancelled") is True

    assert first.complete("task-3", worker_id="worker-a",
                          result_json='{"answer":"late"}', ttl_seconds=60) is False
    assert second.get_result("task-3", owner_hash="owner-a") is None


def test_expired_worker_lease_can_be_reclaimed_and_stale_worker_cannot_publish(task_stores):
    first, second, _ = task_stores
    first.create("task-lease", owner_hash="owner-a", metadata={}, payload='{"q":"lease"}', ttl_seconds=60)

    assert first.claim("task-lease", worker_id="worker-a", lease_ms=10) is not None
    assert second.claim("task-lease", worker_id="worker-b", lease_ms=1000) is None
    time.sleep(0.02)
    assert second.claim("task-lease", worker_id="worker-b", lease_ms=1000) is not None
    assert first.complete("task-lease", worker_id="worker-a",
                          result_json='{"answer":"stale"}', ttl_seconds=60) is False
    assert second.complete("task-lease", worker_id="worker-b",
                           result_json='{"answer":"fresh"}', ttl_seconds=60) is True
    assert first.get_result("task-lease", owner_hash="owner-a") == '{"answer":"fresh"}'


def test_only_current_worker_can_extend_an_unexpired_lease(task_stores):
    first, second, _ = task_stores
    first.create("task-heartbeat", owner_hash="owner-a", metadata={}, payload='{"q":"heartbeat"}',
                 ttl_seconds=60)
    assert first.claim("task-heartbeat", worker_id="worker-a", lease_ms=100) is not None

    assert second.heartbeat("task-heartbeat", worker_id="worker-b", lease_ms=1000) is False
    assert first.heartbeat("task-heartbeat", worker_id="worker-a", lease_ms=1000) is True
    time.sleep(0.12)
    assert second.claim("task-heartbeat", worker_id="worker-b", lease_ms=1000) is None


def test_failed_task_can_be_resumed_with_new_encrypted_payload(task_stores):
    first, second, client = task_stores
    stream = f"{first.key_prefix}:resume-stream"
    first.create("task-resume", owner_hash="owner-a", metadata={"attempts": 1},
                 payload='{"question":"first"}', ttl_seconds=60)
    assert first.claim("task-resume", worker_id="worker-a") is not None
    assert first.fail("task-resume", worker_id="worker-a", failure_code="execution_failed") is True

    resumed = first.resume_and_enqueue("task-resume", stream=stream,
                                       metadata={"attempts": 2},
                                       payload='{"question":"explicit retry"}', ttl_seconds=60)

    assert resumed is True
    assert first.resume_and_enqueue("task-resume", stream=stream, metadata={},
                                    payload='{"question":"duplicate retry"}', ttl_seconds=60) is False
    assert client.xlen(stream) == 1
    assert second.get("task-resume", owner_hash="owner-a")["status"] == "queued"
    assert second.claim("task-resume", worker_id="worker-b")["payload"] == '{"question":"explicit retry"}'


def test_worker_metadata_failure_and_cancel_transitions_are_shared(task_stores):
    first, second, _ = task_stores
    first.create("task-state", owner_hash="owner-a", metadata={"attempts": 1},
                 payload='{"question":"state"}', ttl_seconds=60)
    assert first.claim("task-state", worker_id="worker-a") is not None

    assert first.update_metadata("task-state", worker_id="worker-a",
                                 metadata={"attempts": 1, "intent_progress": {"definition": "ok"}})
    assert second.get("task-state", owner_hash="owner-a")["metadata"]["intent_progress"] == {
        "definition": "ok",
    }
    assert first.fail("task-state", worker_id="worker-a", failure_code="execution_failed")
    assert second.get("task-state", owner_hash="owner-a")["status"] == "failed"

    first.create("task-cancel", owner_hash="owner-a", metadata={}, payload='{"question":"cancel"}',
                 ttl_seconds=60)
    assert second.cancel("task-cancel")
    assert first.claim("task-cancel", worker_id="worker-b") is None
