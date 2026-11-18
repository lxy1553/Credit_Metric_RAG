"""Separate liveness from strict traffic-readiness checks."""

from contextlib import closing

from credit.warehouse import connect


class ReadinessChecker:
    def __init__(self, db, retrieval, session_memory=None, task_queue=None):
        self.db, self.retrieval, self.session_memory = db, retrieval, session_memory
        self.task_queue = task_queue

    def check(self) -> bool:
        try:
            with closing(connect(self.db)) as conn:
                conn.execute("SELECT 1 FROM query_audit LIMIT 1").fetchone()
            if not self.retrieval.ready():
                return False
            if self.session_memory is not None and not self.session_memory.ready():
                return False
            return self.task_queue is None or bool(self.task_queue.ready())
        except Exception:
            return False
