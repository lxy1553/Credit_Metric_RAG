"""Exact, policy-scoped cache for static certified knowledge answers.

This deliberately is not a semantic cache: a near-match may refer to another
metric or a different permission boundary. Numeric and REALTIME queries are
also excluded by the caller because their freshness must come from the
warehouse, not a response cache.
"""

import hashlib
import json
import threading
import time

import redis

from credit.models import User
from credit.resilience import CircuitBreaker, retry_call


def cache_key(user: User, question: str, catalog: dict) -> str:
    scope = {
        "format": "credit-response-cache-v1",
        "user_id": user.user_id,
        "roles": sorted(user.roles),
        "domains": sorted(user.domains),
        "policy_version": user.policy_version,
        "question": question,
        "catalog_version": catalog["version"],
        "catalog_updated_at": catalog["updated_at"],
        "catalog_effective_from": catalog.get("effective_from"),
    }
    serialized = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "credit:answer:" + hashlib.sha256(serialized.encode()).hexdigest()


def data_cache_key(user: User, request: dict, catalog: dict, publication: list[dict]) -> str:
    """Bind a FINAL result to exactly the releases visible for its date range."""
    scope = {
        "format": "credit-final-data-cache-v1",
        "user_id": user.user_id,
        "roles": sorted(user.roles),
        "domains": sorted(user.domains),
        "policy_version": user.policy_version,
        "request": request,
        "catalog_version": catalog["version"],
        "publication": publication,
    }
    serialized = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "credit:final:" + hashlib.sha256(serialized.encode()).hexdigest()


class NullResultCache:
    mode = "off"

    def get(self, key):
        return None

    def set(self, key, value):
        return None


class MemoryResultCache:
    mode = "memory-exact"

    def __init__(self, ttl=300, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.entries, self.lock = {}, threading.Lock()

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            if entry[0] <= self.clock():
                del self.entries[key]
                return None
            return json.loads(entry[1])

    def set(self, key, value):
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.lock:
            if len(self.entries) >= 1000 and key not in self.entries:
                del self.entries[min(self.entries, key=lambda item: self.entries[item][0])]
            self.entries[key] = (self.clock() + self.ttl, serialized)


class RedisResultCache:
    mode = "redis-exact"

    def __init__(self, url: str, ttl=300, client=None):
        self.client = client or redis.Redis.from_url(
            url, socket_connect_timeout=0.2, socket_timeout=0.2, decode_responses=True,
        )
        self.ttl, self.breaker = ttl, CircuitBreaker("response-cache-redis")

    def get(self, key):
        try:
            raw = retry_call(self.breaker, lambda: self.client.get(key))
            return json.loads(raw) if raw else None
        except Exception:
            return None  # Cache loss is a miss, never a data-service outage.

    def set(self, key, value):
        try:
            payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            retry_call(self.breaker, lambda: self.client.setex(key, self.ttl, payload))
        except Exception:
            return None
