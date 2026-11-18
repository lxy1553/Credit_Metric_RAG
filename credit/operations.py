"""Privacy-preserving, process-local service telemetry for the learning demo."""

import math
import threading
from collections import Counter
from datetime import datetime, timezone


class OperationsMetrics:
    """Keep only aggregate counts and bounded latency samples.

    Questions, SQL, result rows, user identifiers and trace IDs intentionally
    never enter this component. A real deployment would export similarly
    minimized metrics to a controlled telemetry backend.
    """

    def __init__(self, sample_limit=1000):
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.sample_limit = sample_limit
        self.lock = threading.Lock()
        self.total = self.cache_hits = self.system_failures = 0
        self.outcomes, self.intents = Counter(), Counter()
        self.latencies = []

    def record(self, result, duration_seconds: float):
        with self.lock:
            self.total += 1
            self.outcomes[result.status] += 1
            self.intents[result.intent] += 1
            self.cache_hits += int(result.from_cache)
            self.latencies.append(max(0.0, duration_seconds))
            if len(self.latencies) > self.sample_limit:
                del self.latencies[:len(self.latencies) - self.sample_limit]

    def record_rejection(self, reason: str):
        with self.lock:
            self.outcomes[reason] += 1

    def record_system_failure(self):
        with self.lock:
            self.system_failures += 1

    @staticmethod
    def _percentile(samples, percentile):
        if not samples:
            return None
        index = max(0, math.ceil(len(samples) * percentile) - 1)
        return round(sorted(samples)[index] * 1000, 3)

    def summary(self) -> dict:
        with self.lock:
            samples = list(self.latencies)
            return {
                "scope": "process-local-aggregates-only",
                "started_at": self.started_at,
                "completed_requests": self.total,
                "cache_hits": self.cache_hits,
                "cache_hit_ratio_of_completed_requests": round(self.cache_hits / self.total, 4) if self.total else 0.0,
                "system_failures": self.system_failures,
                "outcomes": dict(sorted(self.outcomes.items())),
                "intents": dict(sorted(self.intents.items())),
                "latency_ms": {"samples": len(samples), "p50": self._percentile(samples, 0.5),
                               "p95": self._percentile(samples, 0.95)},
            }
