"""Short-lived async question tasks with metadata-only durable checkpoints."""

import hashlib
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from credit.agent import _calculation_metric, detect_intents
from credit.models import Question, Result, User
from credit.security import AccessDenied
from credit.warehouse import connect


class TaskNotFound(Exception):
    pass


class TaskExpired(Exception):
    pass


class TaskNotResumable(Exception):
    pass


class TaskQueueFull(Exception):
    pass


class TaskIdempotencyConflict(Exception):
    pass


def _owner_hash(user: User) -> str:
    return hashlib.sha256(user.user_id.encode("utf-8")).hexdigest()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class QuestionTaskManager:
    """Persists only task metadata; prompts and results remain in process memory."""

    def __init__(self, db: Path, service, *, workers=4, max_outstanding=20,
                 idle_ttl=7200, max_ttl=86400, result_ttl=900, clock=time.time):
        self.db = Path(db)
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="question-task")
        self.max_outstanding = max_outstanding
        self.idle_ttl = idle_ttl
        self.max_ttl = max_ttl
        self.result_ttl = result_ttl
        self.clock = clock
        self.lock = threading.RLock()
        self.results: dict[str, tuple[float, Result]] = {}
        self.futures: dict[str, Future] = {}
        self.closed = False
        with closing(connect(self.db)) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS question_task_checkpoint (
                request_id TEXT PRIMARY KEY,
                owner_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                hard_expires_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                failure_code TEXT,
                planned_intents TEXT NOT NULL DEFAULT '[]',
                intent_progress TEXT NOT NULL DEFAULT '{}',
                metric_id TEXT,
                metric_version TEXT,
                metric_updated_at TEXT,
                citation_ids TEXT NOT NULL DEFAULT '[]',
                trace_id TEXT,
                idempotency_key_hash TEXT,
                idempotency_salt TEXT,
                request_fingerprint TEXT
            )""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(question_task_checkpoint)")}
            additions = {
                "planned_intents": "TEXT NOT NULL DEFAULT '[]'",
                "intent_progress": "TEXT NOT NULL DEFAULT '{}'",
                "metric_id": "TEXT",
                "metric_version": "TEXT",
                "metric_updated_at": "TEXT",
                "citation_ids": "TEXT NOT NULL DEFAULT '[]'",
                "trace_id": "TEXT",
                "idempotency_key_hash": "TEXT",
                "idempotency_salt": "TEXT",
                "request_fingerprint": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE question_task_checkpoint ADD COLUMN {name} {definition}")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_question_task_idempotency
                ON question_task_checkpoint(owner_hash, idempotency_key_hash)
                WHERE idempotency_key_hash IS NOT NULL""")
            conn.execute("DELETE FROM question_task_checkpoint WHERE expires_at<=? OR hard_expires_at<=?",
                         (self.clock(), self.clock()))
            # A process-local result or worker cannot survive a restart. Leave only
            # metadata and require an explicit, reauthenticated resume request.
            conn.execute("""UPDATE question_task_checkpoint
                SET status='interrupted', updated_at=?, failure_code='process_restart'
                WHERE status IN ('queued','running','completed')""", (self.clock(),))

    def _record(self, request_id: str, user: User):
        with closing(connect(self.db)) as conn:
            row = conn.execute(
                "SELECT * FROM question_task_checkpoint WHERE request_id=? AND owner_hash=?",
                (request_id, _owner_hash(user)),
            ).fetchone()
        if row is None:
            raise TaskNotFound
        return dict(row)

    def _touch(self, request_id: str, record: dict) -> dict:
        now = self.clock()
        if now >= record["expires_at"] or now >= record["hard_expires_at"]:
            with closing(connect(self.db)) as conn, conn:
                conn.execute("DELETE FROM question_task_checkpoint WHERE request_id=?", (request_id,))
            with self.lock:
                self.results.pop(request_id, None)
            raise TaskExpired
        expires_at = min(now + self.idle_ttl, record["hard_expires_at"])
        with closing(connect(self.db)) as conn, conn:
            conn.execute("UPDATE question_task_checkpoint SET updated_at=?, expires_at=? WHERE request_id=?",
                         (now, expires_at, request_id))
        record.update(updated_at=now, expires_at=expires_at)
        return record

    def _outstanding(self, conn=None) -> int:
        if conn is None:
            with closing(connect(self.db)) as local_conn:
                return self._outstanding(local_conn)
        active = {row[0] for row in conn.execute(
            "SELECT request_id FROM question_task_checkpoint WHERE status IN ('queued','running')"
        )}
        # Cancellation suppresses publication but cannot stop a running thread.
        active.update(request_id for request_id, future in list(self.futures.items()) if not future.done())
        return len(active)

    def _plan(self, question: Question) -> dict:
        intents = detect_intents(question.question, self.service.catalog)
        metric_id = (None if intents == ["other"] else
                     _calculation_metric(question.question, self.service.catalog)
                     if "calculation" in intents else self.service.catalog.match(question.question))
        has_metric = metric_id in self.service.catalog.metrics if metric_id else False
        return {
            "planned_intents": intents,
            "intent_progress": {intent: "queued" for intent in intents},
            "metric_id": metric_id if has_metric else None,
            "metric_version": self.service.catalog.document["version"] if has_metric else None,
            "metric_updated_at": self.service.catalog.document["updated_at"] if has_metric else None,
        }

    def submit(self, question: Question, resolver, user: User, idempotency_key: str | None = None) -> dict:
        now = self.clock()
        request_id = uuid4().hex
        plan = self._plan(question)
        owner_hash = _owner_hash(user)
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest() if idempotency_key else None
        salt = uuid4().hex if key_hash else None
        payload = question.model_dump_json().encode("utf-8")
        with self.lock:
            if self.closed:
                raise TaskQueueFull
            self._purge_results(now)
            with closing(connect(self.db)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM question_task_checkpoint WHERE expires_at<=? OR hard_expires_at<=?",
                             (now, now))
                if key_hash:
                    existing = conn.execute("""SELECT request_id,status,created_at,expires_at,
                        idempotency_salt,request_fingerprint FROM question_task_checkpoint
                        WHERE owner_hash=? AND idempotency_key_hash=?""", (owner_hash, key_hash)).fetchone()
                    if existing:
                        fingerprint = hashlib.sha256(
                            bytes.fromhex(existing["idempotency_salt"]) + payload
                        ).hexdigest()
                        if fingerprint != existing["request_fingerprint"]:
                            raise TaskIdempotencyConflict
                        return {"request_id": existing["request_id"], "status": existing["status"],
                                "created_at": _iso(existing["created_at"]),
                                "expires_at": _iso(existing["expires_at"])}
                if self._outstanding(conn) >= self.max_outstanding:
                    raise TaskQueueFull
                fingerprint = hashlib.sha256(bytes.fromhex(salt) + payload).hexdigest() if salt else None
                conn.execute("""INSERT INTO question_task_checkpoint
                    (request_id,owner_hash,status,created_at,updated_at,expires_at,hard_expires_at,
                     planned_intents,intent_progress,metric_id,metric_version,metric_updated_at,
                     idempotency_key_hash,idempotency_salt,request_fingerprint)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (request_id, owner_hash, "queued", now, now,
                        now + self.idle_ttl, now + self.max_ttl, json.dumps(plan["planned_intents"]),
                        json.dumps(plan["intent_progress"]), plan["metric_id"], plan["metric_version"],
                        plan["metric_updated_at"], key_hash, salt, fingerprint))
            self._enqueue(request_id, question, resolver)
        return {"request_id": request_id, "status": "queued", "created_at": _iso(now),
                "expires_at": _iso(min(now + self.idle_ttl, now + self.max_ttl))}

    def _enqueue(self, request_id: str, question: Question, resolver):
        future = self.executor.submit(self._execute, request_id, question, resolver)
        self.futures[request_id] = future
        future.add_done_callback(lambda _: self.futures.pop(request_id, None))

    def _purge_results(self, now: float):
        expired = [request_id for request_id, (expires_at, _) in self.results.items() if expires_at <= now]
        for request_id in expired:
            self.results.pop(request_id, None)

    def _invalidate_checkpoint(self, request_id: str, record: dict, now: float):
        progress = {intent: "invalidated" for intent in json.loads(record["planned_intents"])}
        with self.lock:
            self.results.pop(request_id, None)
        with closing(connect(self.db)) as conn, conn:
            conn.execute("""UPDATE question_task_checkpoint SET status='interrupted',updated_at=?,
                failure_code='permission_changed',metric_id=NULL,metric_version=NULL,metric_updated_at=NULL,
                citation_ids='[]',trace_id=NULL,intent_progress=? WHERE request_id=?""",
                         (now, json.dumps(progress), request_id))
        record.update(status="interrupted", failure_code="permission_changed", metric_id=None,
                      metric_version=None, metric_updated_at=None, citation_ids="[]", trace_id=None,
                      intent_progress=json.dumps(progress))

    def _execute(self, request_id: str, question: Question, resolver):
        with closing(connect(self.db)) as conn, conn:
            row = conn.execute("SELECT planned_intents FROM question_task_checkpoint WHERE request_id=?",
                               (request_id,)).fetchone()
            if row:
                planned = json.loads(row["planned_intents"])
                progress = json.dumps({intent: "running" for intent in planned})
                updated = conn.execute("""UPDATE question_task_checkpoint SET status='running',
                    intent_progress=?,updated_at=? WHERE request_id=? AND status='queued'""",
                                       (progress, self.clock(), request_id)).rowcount
            else:
                updated = 0
        if not updated:
            return
        planned = json.loads(row["planned_intents"]) if row else []
        try:
            def report_plan(intents: list[str]):
                nonlocal planned
                planned = list(intents)
                progress = {intent: "running" for intent in planned}
                with self.lock:
                    with closing(connect(self.db)) as conn, conn:
                        conn.execute("""UPDATE question_task_checkpoint
                            SET planned_intents=?,intent_progress=?,updated_at=?
                            WHERE request_id=? AND status='running'""",
                                     (json.dumps(planned), json.dumps(progress), self.clock(), request_id))

            # The service resolves identity before routing and again at every tool call.
            def report_progress(intent: str, status: str):
                try:
                    with self.lock:
                        with closing(connect(self.db)) as conn, conn:
                            row = conn.execute(
                                "SELECT intent_progress FROM question_task_checkpoint "
                                "WHERE request_id=? AND status='running'", (request_id,)
                            ).fetchone()
                            if row is None:
                                return
                            progress = json.loads(row["intent_progress"])
                            if intent not in progress:
                                return
                            progress[intent] = status
                            conn.execute("UPDATE question_task_checkpoint SET intent_progress=?,updated_at=? "
                                         "WHERE request_id=? AND status='running'",
                                         (json.dumps(progress), self.clock(), request_id))
                except Exception:
                    # Best-effort progress metadata must not fail the question itself.
                    return

            result = self.service.ask(question, resolver, progress_callback=report_progress,
                                      plan_callback=report_plan, request_id=request_id)
            sections = result.sections or []
            progress = ({section.intent: section.status for section in sections} if sections else
                        {result.intent: result.status})
            citation_ids = list(dict.fromkeys(citation["id"] for citation in result.citations
                                              if citation.get("id")))[:50]
            metric_citation = next((citation for citation in result.citations
                                    if citation.get("id") == f"metric:{result.metric_id}"), None)
            with self.lock:
                with closing(connect(self.db)) as conn:
                    state = conn.execute("SELECT status FROM question_task_checkpoint WHERE request_id=?",
                                         (request_id,)).fetchone()
                if not state or state["status"] == "cancelled":
                    return
                self.results[request_id] = (self.clock() + self.result_ttl, result)
            with closing(connect(self.db)) as conn, conn:
                conn.execute("""UPDATE question_task_checkpoint SET status='completed', updated_at=?,
                    failure_code=NULL,intent_progress=?,metric_id=COALESCE(?,metric_id),
                    metric_version=COALESCE(?,metric_version),metric_updated_at=COALESCE(?,metric_updated_at),
                    citation_ids=?,trace_id=? WHERE request_id=? AND status='running'""",
                    (self.clock(), json.dumps(progress), result.metric_id,
                     metric_citation.get("version") if metric_citation else None,
                     metric_citation.get("updated_at") if metric_citation else None,
                     json.dumps(citation_ids), result.trace_id, request_id))
        except Exception:
            # Do not persist exception text: it may contain question or data fragments.
            with closing(connect(self.db)) as conn, conn:
                conn.execute("""UPDATE question_task_checkpoint SET status='failed', updated_at=?,
                    failure_code='execution_failed',intent_progress=? WHERE request_id=? AND status='running'""",
                             (self.clock(), json.dumps({intent: "failed" for intent in planned}), request_id))

    def get(self, request_id: str, user: User) -> dict:
        record = self._touch(request_id, self._record(request_id, user))
        now = self.clock()
        with self.lock:
            self._purge_results(now)
            stored = self.results.get(request_id)
            result = stored[1] if stored else None
        status = record["status"]
        if status == "completed" and result is not None:
            try:
                self.service.authorize_result(result, user)
            except AccessDenied:
                self._invalidate_checkpoint(request_id, record, now)
                status, result = "interrupted", None
        elif record["metric_id"] or json.loads(record["planned_intents"]):
            try:
                self.service.authorize_checkpoint(json.loads(record["planned_intents"]), record["metric_id"], user)
            except AccessDenied:
                self._invalidate_checkpoint(request_id, record, now)
                status = "interrupted"
        if status == "completed" and result is None:
            status = "interrupted"
            record["failure_code"] = "result_cache_expired"
            with closing(connect(self.db)) as conn, conn:
                conn.execute("UPDATE question_task_checkpoint SET status='interrupted',failure_code=? WHERE request_id=?",
                             (record["failure_code"], request_id))
        messages = {
            "interrupted": "权限已变化或原结果已不可用；请在有效期内重新提交问题恢复",
            "failed": "任务执行失败；可在有效期内重新提交问题恢复",
        }
        return {"request_id": request_id, "status": status,
                "created_at": _iso(record["created_at"]), "expires_at": _iso(record["expires_at"]),
                "result": result.model_dump(mode="json") if result else None,
                "planned_intents": json.loads(record["planned_intents"]),
                "intent_progress": json.loads(record["intent_progress"]),
                "metric_id": record["metric_id"], "metric_version": record["metric_version"],
                "metric_updated_at": record["metric_updated_at"],
                "citation_ids": json.loads(record["citation_ids"]) if result else [],
                "trace_id": record["trace_id"] if result else None,
                "resume_required": status in {"interrupted", "failed"},
                "message": messages.get(status)}

    def resume(self, request_id: str, question: Question, resolver, user: User) -> dict:
        record = self._touch(request_id, self._record(request_id, user))
        if record["status"] not in {"interrupted", "failed"}:
            raise TaskNotResumable
        plan = self._plan(question)
        with self.lock:
            if self.closed:
                raise TaskQueueFull
            if self._outstanding() >= self.max_outstanding:
                raise TaskQueueFull
            with closing(connect(self.db)) as conn, conn:
                claimed = conn.execute("""UPDATE question_task_checkpoint SET status='queued',updated_at=?,
                    expires_at=?,attempts=attempts+1,failure_code=NULL,planned_intents=?,intent_progress=?,
                    metric_id=?,metric_version=?,metric_updated_at=?,citation_ids='[]',trace_id=NULL
                    WHERE request_id=? AND status IN ('interrupted','failed')""", (self.clock(), min(self.clock() + self.idle_ttl,
                        record["hard_expires_at"]), json.dumps(plan["planned_intents"]),
                        json.dumps(plan["intent_progress"]), plan["metric_id"], plan["metric_version"],
                        plan["metric_updated_at"], request_id))
                if claimed.rowcount != 1:
                    raise TaskNotResumable
            self.results.pop(request_id, None)
            self._enqueue(request_id, question, resolver)
        return {"request_id": request_id, "status": "queued", "created_at": _iso(record["created_at"]),
                "expires_at": _iso(min(self.clock() + self.idle_ttl, record["hard_expires_at"]))}

    def cancel(self, request_id: str, user: User):
        self._touch(request_id, self._record(request_id, user))
        with closing(connect(self.db)) as conn, conn:
            conn.execute("UPDATE question_task_checkpoint SET status='cancelled',updated_at=? WHERE request_id=?",
                         (self.clock(), request_id))
        with self.lock:
            self.results.pop(request_id, None)
            future = self.futures.get(request_id)
            if future:
                future.cancel()

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
