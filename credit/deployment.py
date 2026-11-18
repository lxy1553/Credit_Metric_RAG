"""Fail-closed validation for supported local and future production profiles."""

import os
from collections.abc import Mapping


class DeploymentConfigurationError(RuntimeError):
    """Raised when deployment settings exceed the guarantees of configured adapters."""


def validate_deployment_profile(environ: Mapping[str, str] | None = None) -> None:
    values = os.environ if environ is None else environ
    profile = values.get("CREDIT_DEPLOYMENT_PROFILE", "local").strip().lower()
    if profile not in {"local", "production"}:
        raise DeploymentConfigurationError("CREDIT_DEPLOYMENT_PROFILE must be local or production")

    def positive_int(name: str, default: str) -> int:
        raw = values.get(name, default)
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise DeploymentConfigurationError(f"{name} must be a positive integer") from exc
        if parsed < 1:
            raise DeploymentConfigurationError(f"{name} must be a positive integer")
        return parsed

    workers = positive_int("CREDIT_API_WORKERS", values.get("WEB_CONCURRENCY", "1"))
    instances = positive_int("CREDIT_API_INSTANCES", "1")
    positive_int("CREDIT_TASK_WORKERS", "1")
    task_backend = values.get("CREDIT_TASK_BACKEND", "local").strip().lower()

    if profile == "production":
        raise DeploymentConfigurationError(
            "production profile requires durable shared task backend and shared checkpoint adapters; "
            "those adapters are not implemented yet"
        )
    if task_backend not in {"local", "redis"}:
        raise DeploymentConfigurationError(f"task backend {task_backend!r} is not supported")
    if task_backend == "redis":
        if not values.get("CREDIT_TASK_REDIS_URL"):
            raise DeploymentConfigurationError("CREDIT_TASK_REDIS_URL is required for Redis task backend")
        if not values.get("CREDIT_TASK_KEYRING") or not values.get("CREDIT_TASK_ACTIVE_KEY_ID"):
            raise DeploymentConfigurationError(
                "CREDIT_TASK_KEYRING and CREDIT_TASK_ACTIVE_KEY_ID are required for Redis task backend"
            )
        if values.get("CREDIT_AUTH_MODE", "demo").strip().lower() != "demo":
            raise DeploymentConfigurationError(
                "CREDIT_TASK_BACKEND=redis currently requires CREDIT_AUTH_MODE=demo; "
                "shared OIDC identity resolution is not implemented"
            )
        if (values.get("CREDIT_MEMORY", "memory").strip().lower() != "redis"
                or not values.get("CREDIT_REDIS_URL")):
            raise DeploymentConfigurationError(
                "CREDIT_TASK_BACKEND=redis requires CREDIT_MEMORY=redis and CREDIT_REDIS_URL "
                "for cross-instance session continuity"
            )
        return
    if workers > 1:
        raise DeploymentConfigurationError("local task backend requires a single API worker")
    if instances > 1:
        raise DeploymentConfigurationError("local task backend requires a single API instance")
