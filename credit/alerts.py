"""Evaluate minimal SLO alert state without sending external notifications."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AlertPolicy:
    min_latency_samples: int = 20
    max_p95_ms: float = 500.0

    def __post_init__(self):
        if self.min_latency_samples < 1 or self.max_p95_ms <= 0:
            raise ValueError("alert thresholds must be positive")

    @classmethod
    def from_environment(cls):
        return cls(
            min_latency_samples=int(os.getenv("CREDIT_SLO_MIN_SAMPLES", "20")),
            max_p95_ms=float(os.getenv("CREDIT_SLO_P95_MS", "500")),
        )

    def evaluate(self, metrics: dict) -> dict:
        latency = metrics["latency_ms"]
        thresholds = {"min_latency_samples": self.min_latency_samples, "max_p95_ms": self.max_p95_ms}
        if metrics["system_failures"]:
            return {"mode": "evaluation-only-no-notification", "state": "critical",
                    "reasons": ["system_failures_observed"], "thresholds": thresholds,
                    "observed": {"system_failures": metrics["system_failures"],
                                 "latency_samples": latency["samples"], "p95_ms": latency["p95"]}}
        if latency["samples"] < self.min_latency_samples:
            return {"mode": "evaluation-only-no-notification", "state": "unknown",
                    "reasons": ["insufficient_latency_samples"], "thresholds": thresholds,
                    "observed": {"system_failures": 0, "latency_samples": latency["samples"],
                                 "p95_ms": latency["p95"]}}
        if latency["p95"] > self.max_p95_ms:
            return {"mode": "evaluation-only-no-notification", "state": "warning",
                    "reasons": ["p95_latency_exceeded"], "thresholds": thresholds,
                    "observed": {"system_failures": 0, "latency_samples": latency["samples"],
                                 "p95_ms": latency["p95"]}}
        return {"mode": "evaluation-only-no-notification", "state": "ok", "reasons": [],
                "thresholds": thresholds,
                "observed": {"system_failures": 0, "latency_samples": latency["samples"],
                             "p95_ms": latency["p95"]}}
