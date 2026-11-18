import os
import uuid

import pytest
import redis

from credit.task_payloads import TaskPayloadCipher
from credit.task_queue import RedisTaskQueue


@pytest.fixture
def task_queue():
    url = os.getenv("CREDIT_TASK_REDIS_URL")
    if not url:
        if os.getenv("CREDIT_TASK_REDIS_REQUIRED") == "1":
            pytest.fail("release preflight requires CREDIT_TASK_REDIS_URL")
        pytest.skip("set CREDIT_TASK_REDIS_URL to run the Redis task queue integration tests")
    client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    try:
        client.ping()
    except redis.RedisError as exc:
        if os.getenv("CREDIT_TASK_REDIS_REQUIRED") == "1":
            pytest.fail(f"release preflight task Redis is unavailable: {type(exc).__name__}")
        pytest.skip(f"task Redis is unavailable: {type(exc).__name__}")
    suffix = uuid.uuid4().hex
    stream = f"codex:test:task-queue:{suffix}"
    queue = RedisTaskQueue(
        client, TaskPayloadCipher({"test-key": b"t" * 32}, active_key_id="test-key"),
        stream=stream, group=f"workers-{suffix}",
    )
    queue.prepare()
    yield client, queue, stream
    client.delete(stream)
    client.close()


def test_queue_stores_ciphertext_and_delivers_only_once_until_acknowledged(task_queue):
    client, queue, stream = task_queue
    queue.enqueue("request-1", "private credit question")
    stored = client.xrange(stream)

    assert len(stored) == 1
    assert "private credit question" not in str(stored[0][1])
    first = queue.claim("worker-a", count=1, block_ms=10)
    second = queue.claim("worker-b", count=1, block_ms=10)

    assert [(item.request_id, item.payload) for item in first] == [
        ("request-1", "private credit question"),
    ]
    assert second == []
    queue.ack(first[0].message_id)
    assert queue.claim("worker-b", count=1, block_ms=10) == []


def test_queue_can_reclaim_a_pending_message_after_worker_loss(task_queue):
    _, queue, _ = task_queue
    queue.enqueue("request-2", "resume this after worker loss")
    claimed = queue.claim("worker-a", count=1, block_ms=10)

    recovered = queue.reclaim("worker-b", min_idle_ms=0, count=10)

    assert len(recovered) == 1
    assert recovered[0].message_id == claimed[0].message_id
    assert recovered[0].request_id == "request-2"
    assert recovered[0].payload == "resume this after worker loss"
    queue.ack(recovered[0].message_id)


def test_acknowledged_task_payload_is_removed_from_the_stream(task_queue):
    client, queue, stream = task_queue
    queue.enqueue("request-delete", "private question")
    claimed = queue.claim("worker-a", count=1, block_ms=10)[0]

    assert queue.ack_and_delete(claimed.message_id) is True
    assert client.xlen(stream) == 0
