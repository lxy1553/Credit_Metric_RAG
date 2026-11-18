"""Redis-backed async question tasks shared by API and worker processes."""

import json
import math
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from credit.agent import _calculation_metric, detect_intents
from credit.async_tasks import (
    TaskExpired,
    TaskIdempotencyConflict,
    TaskNotFound,
    TaskNotResumable,
    TaskQueueFull,
    _owner_hash,
)
from credit.models import Question, Result, User
from credit.security import AccessDenied
from credit.task_queue import RedisTaskQueue
from credit.task_store import RedisTaskStore


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class RedisQuestionTaskManager:
    """At-least-once Redis worker backend with encrypted payload and result storage."""

    def __init__(self, service, users: dict[str, User], client, cipher, *,
                 stream="credit:question-tasks", key_prefix="credit:question-task",
                 workers=1, max_outstanding=20, idle_ttl=7200, max_ttl=86400,
                 result_ttl=900, lease_ms=30_000):
        if (workers < 0 or max_outstanding < 1 or idle_ttl < 1 or max_ttl < idle_ttl
                or result_ttl < 1 or lease_ms < 100):
            raise ValueError("invalid Redis task worker or TTL configuration")
        self.service = service
        self.users = users
        self.client = client
        self.cipher = cipher
        self.stream = stream
        self.key_prefix = key_prefix
        self.workers = workers
        self.max_outstanding = max_outstanding
        self.idle_ttl = idle_ttl
        self.max_ttl = max_ttl
        self.result_ttl = result_ttl
        self.lease_ms = lease_ms
        self.store = RedisTaskStore(client, cipher, key_prefix=key_prefix)
        self.queue = RedisTaskQueue(client, cipher, stream=stream, group=f"{stream}:workers")
        self.queue.prepare()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.closed = False
        self.lock = threading.Lock()
        for index in range(workers):
            worker_id = f"{uuid4().hex}-{index}"
            thread = threading.Thread(target=self._worker_loop, args=(worker_id,),
                                      name=f"redis-question-worker-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def _plan(self, question: Question) -> dict:
        intents = detect_intents(question.question, self.service.catalog)
        metric_id = (None if intents == ["other"] else
                     _calculation_metric(question.question, self.service.catalog)
                     if "calculation" in intents else self.service.catalog.match(question.question))
        if metric_id not in self.service.catalog.metrics:
            metric_id = None
        return {
            "planned_intents": intents,
            "intent_progress": {intent: "queued" for intent in intents},
            "metric_id": metric_id,
            "metric_version": self.service.catalog.document["version"] if metric_id else None,
            "metric_updated_at": self.service.catalog.document["updated_at"] if metric_id else None,
        }

    def _metadata(self, question: Question, now: float, plan: dict, *, attempts=1) -> dict:
        return {
            **plan,
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "expires_at": _iso(min(now + self.idle_ttl, now + self.max_ttl)),
            "hard_expires_at": _iso(now + self.max_ttl),
            "attempts": attempts,
            "failure_code": None,
            "citation_ids": [],
            "trace_id": None,
        }

    def submit(self, question: Question, resolver, user: User, idempotency_key: str | None = None) -> dict:
        if self.closed:
            raise TaskQueueFull
        now = time.time()
        request_id = uuid4().hex
        owner_hash = _owner_hash(user)
        payload = json.dumps({"user_id": user.user_id, "question": question.model_dump(mode="json")},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = self.cipher.fingerprint(payload)
        idempotency_index = None
        if idempotency_key:
            key_hash = self.cipher.fingerprint(f"idempotency-key:{idempotency_key}")
            idempotency_index = f"{self.key_prefix}:idempotency:{owner_hash}:{key_hash}"
        ttl_seconds = self.max_ttl
        created = self.store.create_and_enqueue(
            request_id, stream=self.stream, idempotency_key=idempotency_index, owner_hash=owner_hash,
            metadata=self._metadata(question, now, self._plan(question)), payload=payload,
            fingerprint=fingerprint, ttl_seconds=ttl_seconds, max_outstanding=self.max_outstanding,
        )
        if created.get("conflict"):
            raise TaskIdempotencyConflict
        if created.get("exists"):
            raise TaskQueueFull
        if created.get("queue_full"):
            raise TaskQueueFull
        actual_id = created["request_id"]
        state = self.store.get(actual_id, owner_hash=owner_hash)
        metadata = state["metadata"] if state else self._metadata(question, now, self._plan(question))
        return {"request_id": actual_id, "status": state["status"] if state else "queued",
                "created_at": metadata["created_at"], "expires_at": metadata["expires_at"]}

    def _lookup_user(self, user_id: str) -> User:
        user = self.users.get(user_id)
        if user is None:
            raise AccessDenied("task owner is no longer available")
        return user

    def _worker_loop(self, worker_id: str):
        while not self.stop_event.is_set():
            try:
                messages = self.queue.reclaim(worker_id, min_idle_ms=self.lease_ms, count=1)
                if not messages:
                    messages = self.queue.claim(worker_id, count=1, block_ms=250)
                for message in messages:
                    self._execute_message(message, worker_id)
            except Exception:
                self.stop_event.wait(0.2)

    def _execute_message(self, message, worker_id: str):
        claimed = self.store.claim(message.request_id, worker_id=worker_id, lease_ms=self.lease_ms)
        if claimed is None:
            record = self.client.hgetall(self.store.redis_key(message.request_id))
            if not record or record.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                self.queue.ack_and_delete(message.message_id)
            return
        if claimed["payload"] != message.payload:
            failed = self.store.fail(message.request_id, worker_id=worker_id,
                                     failure_code="payload_mismatch")
            if failed:
                self.queue.ack_and_delete(message.message_id)
            return
        try:
            payload = json.loads(claimed["payload"])
            owner_id = payload["user_id"]
            question = Question.model_validate(payload["question"])
            owner = self._lookup_user(owner_id)
            owner_hash = _owner_hash(owner)
            state = self.store.get(message.request_id, owner_hash=owner_hash)
            if state is None:
                raise AccessDenied("task owner changed")
            metadata = state["metadata"]
            planned = list(metadata.get("planned_intents", []))
            heartbeat_stop = threading.Event()
            heartbeat = threading.Thread(target=self._heartbeat_loop,
                                         args=(message.request_id, worker_id, heartbeat_stop), daemon=True)
            heartbeat.start()
            try:
                def save_metadata(**updates):
                    metadata.update(updates)
                    metadata["updated_at"] = _iso(time.time())
                    self.store.update_metadata(message.request_id, worker_id=worker_id, metadata=metadata)

                def report_plan(intents: list[str]):
                    nonlocal planned
                    planned = list(intents)
                    save_metadata(planned_intents=planned,
                                  intent_progress={intent: "running" for intent in planned})

                def report_progress(intent: str, status: str):
                    progress = dict(metadata.get("intent_progress", {}))
                    if intent in progress:
                        progress[intent] = status
                        save_metadata(intent_progress=progress)

                def resolver():
                    return self._lookup_user(owner_id)

                result = self.service.ask(question, resolver, progress_callback=report_progress,
                                          plan_callback=report_plan, request_id=message.request_id)
                sections = result.sections or []
                progress = ({section.intent: section.status for section in sections} if sections else
                            {result.intent: result.status})
                citation_ids = list(dict.fromkeys(citation["id"] for citation in result.citations
                                                  if citation.get("id")))[:50]
                metric_citation = next((citation for citation in result.citations
                                        if citation.get("id") == f"metric:{result.metric_id}"), None)
                save_metadata(
                    intent_progress=progress,
                    metric_id=result.metric_id or metadata.get("metric_id"),
                    metric_version=(metric_citation.get("version") if metric_citation
                                    else metadata.get("metric_version")),
                    metric_updated_at=(metric_citation.get("updated_at") if metric_citation
                                       else metadata.get("metric_updated_at")),
                    citation_ids=citation_ids,
                    trace_id=result.trace_id,
                )
                if self.store.complete(message.request_id, worker_id=worker_id,
                                       result_json=result.model_dump_json(), ttl_seconds=self.result_ttl):
                    self.queue.ack_and_delete(message.message_id)
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=1)
        except Exception:
            if self.store.fail(message.request_id, worker_id=worker_id, failure_code="execution_failed"):
                self.queue.ack_and_delete(message.message_id)

    def _heartbeat_loop(self, request_id: str, worker_id: str, stop: threading.Event):
        interval = max(0.05, self.lease_ms / 3000)
        while not stop.wait(interval):
            if not self.store.heartbeat(request_id, worker_id=worker_id, lease_ms=self.lease_ms):
                return

    def _record(self, request_id: str, user: User) -> dict:
        record = self.store.get(request_id, owner_hash=_owner_hash(user))
        if record is None:
            raise TaskNotFound
        metadata = record["metadata"]
        now = time.time()
        hard_expires = datetime.fromisoformat(metadata["hard_expires_at"]).timestamp()
        if now >= datetime.fromisoformat(metadata["expires_at"]).timestamp() or now >= hard_expires:
            raise TaskExpired
        next_expiry = min(now + self.idle_ttl, hard_expires)
        metadata["expires_at"] = _iso(next_expiry)
        if not self.store.touch(request_id, owner_hash=_owner_hash(user), metadata=metadata,
                                ttl_seconds=max(1, math.ceil(next_expiry - now))):
            raise TaskNotFound
        return record

    def get(self, request_id: str, user: User) -> dict:
        record = self._record(request_id, user)
        metadata = record["metadata"]
        status, result = record["status"], None
        try:
            self.service.authorize_checkpoint(metadata.get("planned_intents", []), metadata.get("metric_id"), user)
            if status == "completed":
                payload = self.store.get_result(request_id, owner_hash=_owner_hash(user))
                if payload is None:
                    status = "interrupted"
                    self.store.transition(request_id, expected="completed", target="interrupted")
                else:
                    result = Result.model_validate_json(payload)
                    self.service.authorize_result(result, user)
        except AccessDenied:
            self.store.transition(request_id, expected=status, target="interrupted")
            status, result = "interrupted", None
        messages = {
            "interrupted": "权限已变化或原结果已不可用；请在有效期内重新提交问题恢复",
            "failed": "任务执行失败；可在有效期内重新提交问题恢复",
        }
        return {"request_id": request_id, "status": status,
                "created_at": metadata["created_at"], "expires_at": metadata["expires_at"],
                "result": result.model_dump(mode="json") if result else None,
                "planned_intents": metadata.get("planned_intents", []),
                "intent_progress": metadata.get("intent_progress", {}),
                "metric_id": metadata.get("metric_id"), "metric_version": metadata.get("metric_version"),
                "metric_updated_at": metadata.get("metric_updated_at"),
                "citation_ids": metadata.get("citation_ids", []) if result else [],
                "trace_id": metadata.get("trace_id") if result else None,
                "resume_required": status in {"interrupted", "failed"}, "message": messages.get(status)}

    def resume(self, request_id: str, question: Question, resolver, user: User) -> dict:
        record = self._record(request_id, user)
        if record["status"] not in {"failed", "interrupted"}:
            raise TaskNotResumable
        metadata = self._metadata(question, time.time(), self._plan(question),
                                  attempts=record["metadata"].get("attempts", 1) + 1)
        payload = json.dumps({"user_id": user.user_id, "question": question.model_dump(mode="json")},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        resumed = self.store.resume_and_enqueue(request_id, stream=self.stream, metadata=metadata,
                                                payload=payload, ttl_seconds=self.max_ttl,
                                                max_outstanding=self.max_outstanding)
        if resumed is None:
            raise TaskQueueFull
        if not resumed:
            raise TaskNotResumable
        return {"request_id": request_id, "status": "queued", "created_at": metadata["created_at"],
                "expires_at": metadata["expires_at"]}

    def cancel(self, request_id: str, user: User):
        self._record(request_id, user)
        self.store.cancel(request_id)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=1)

    def ready(self) -> bool:
        if self.closed or not self.threads or not all(thread.is_alive() for thread in self.threads):
            return False
        try:
            self.client.ping()
            return True
        except Exception:
            return False
