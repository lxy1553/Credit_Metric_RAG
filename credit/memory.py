import hashlib
import json
import threading
import time
from datetime import date
from typing import Literal

import redis
from pydantic import BaseModel, ConfigDict, Field

from credit.models import User
from credit.resilience import CircuitBreaker, retry_call


class TaskState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    data_status: Literal["FINAL", "REALTIME"] = "FINAL"
    dimensions: list[Literal["biz_date", "product"]] = Field(default_factory=lambda: ["biz_date"])
    pending: list[Literal["metric", "dates", "data_status"]] = Field(default_factory=list)
    citation_ids: list[str] = Field(default_factory=list, max_length=10)
    trace_ids: list[str] = Field(default_factory=list, max_length=5)
    # 保存结构化任务摘要，无原始问题、SQL、结果、权限或敏感明细。
    recent_tasks: list[str] = Field(default_factory=list, max_length=3)
    turns: int = 0


def session_key(user: User, session_id: str) -> str:
    scope = [user.user_id, sorted(user.roles), sorted(user.domains), user.policy_version, session_id]
    return "credit:task:" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()


class MemoryStore:
    mode = "memory-demo"

    def __init__(self, ttl=900, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.entries = {}
        self.lock = threading.Lock()

    def get(self, key: str) -> TaskState | None:
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            if entry[0] <= self.clock():
                del self.entries[key]
                return None
            return TaskState.model_validate_json(entry[1])

    def ready(self) -> bool:
        return True

    def set(self, key: str, state: TaskState):
        with self.lock:
            expired = [k for k, value in self.entries.items() if value[0] <= self.clock()]
            for k in expired:
                del self.entries[k]
            if len(self.entries) >= 1000 and key not in self.entries:
                del self.entries[min(self.entries, key=lambda k: self.entries[k][0])]
            self.entries[key] = (self.clock() + self.ttl, state.model_dump_json())


class StatelessMemory:
    mode = "stateless"

    def get(self, key):
        return None

    def ready(self):
        return True

    def set(self, key, state):
        return None


class RedisMemory:
    mode = "redis"

    def __init__(self, url: str, ttl=900, client=None):
        self.client = client or redis.Redis.from_url(
            url, socket_connect_timeout=0.2, socket_timeout=0.2, decode_responses=True,
        )
        self.ttl = ttl
        self.breaker = CircuitBreaker("redis")

    def get(self, key):
        raw = retry_call(self.breaker, lambda: self.client.get(key))
        return TaskState.model_validate_json(raw) if raw else None

    def ready(self):
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def set(self, key, state):
        retry_call(self.breaker, lambda: self.client.setex(key, self.ttl, state.model_dump_json()))
