import pytest

from credit.deployment import DeploymentConfigurationError, validate_deployment_profile


def test_local_task_backend_rejects_multiple_api_workers():
    with pytest.raises(DeploymentConfigurationError, match="single API worker"):
        validate_deployment_profile({
            "CREDIT_DEPLOYMENT_PROFILE": "local",
            "CREDIT_TASK_BACKEND": "local",
            "CREDIT_API_WORKERS": "2",
            "CREDIT_API_INSTANCES": "1",
        })


def test_local_task_backend_rejects_multiple_api_instances():
    with pytest.raises(DeploymentConfigurationError, match="single API instance"):
        validate_deployment_profile({
            "CREDIT_DEPLOYMENT_PROFILE": "local",
            "CREDIT_TASK_BACKEND": "local",
            "CREDIT_API_WORKERS": "1",
            "CREDIT_API_INSTANCES": "2",
        })


def test_single_process_local_profile_remains_usable():
    validate_deployment_profile({
        "CREDIT_DEPLOYMENT_PROFILE": "local",
        "CREDIT_TASK_BACKEND": "local",
        "CREDIT_API_WORKERS": "1",
        "CREDIT_API_INSTANCES": "1",
    })


def test_production_profile_fails_closed_without_a_shared_task_adapter():
    with pytest.raises(DeploymentConfigurationError, match="durable shared task backend"):
        validate_deployment_profile({
            "CREDIT_DEPLOYMENT_PROFILE": "production",
            "CREDIT_TASK_BACKEND": "local",
            "CREDIT_API_WORKERS": "1",
            "CREDIT_API_INSTANCES": "1",
        })


def test_unsupported_task_adapter_is_not_silently_accepted():
    with pytest.raises(DeploymentConfigurationError, match="not supported"):
        validate_deployment_profile({
            "CREDIT_DEPLOYMENT_PROFILE": "local",
            "CREDIT_TASK_BACKEND": "unknown",
            "CREDIT_API_WORKERS": "1",
            "CREDIT_API_INSTANCES": "1",
        })


def test_local_redis_task_backend_allows_multiple_api_instances_with_required_secrets():
    validate_deployment_profile({
        "CREDIT_DEPLOYMENT_PROFILE": "local",
        "CREDIT_TASK_BACKEND": "redis",
        "CREDIT_TASK_REDIS_URL": "redis://127.0.0.1:6390/0",
        "CREDIT_TASK_KEYRING": '{"local-v1":"' + "a" * 64 + '"}',
        "CREDIT_TASK_ACTIVE_KEY_ID": "local-v1",
        "CREDIT_MEMORY": "redis",
        "CREDIT_REDIS_URL": "redis://127.0.0.1:6390/0",
        "CREDIT_AUTH_MODE": "demo",
        "CREDIT_API_WORKERS": "2",
        "CREDIT_API_INSTANCES": "2",
    })


@pytest.mark.parametrize("overrides, expected", [
    ({"CREDIT_TASK_REDIS_URL": "", "CREDIT_TASK_KEYRING": "{}"}, "CREDIT_TASK_REDIS_URL"),
    ({"CREDIT_TASK_REDIS_URL": "redis://localhost", "CREDIT_TASK_KEYRING": ""},
     "CREDIT_TASK_KEYRING"),
    ({"CREDIT_TASK_REDIS_URL": "redis://localhost", "CREDIT_TASK_KEYRING": "{}",
      "CREDIT_AUTH_MODE": "oidc"}, "CREDIT_AUTH_MODE=demo"),
    ({"CREDIT_TASK_REDIS_URL": "redis://localhost", "CREDIT_TASK_KEYRING": "{}",
      "CREDIT_MEMORY": "memory"}, "CREDIT_MEMORY=redis"),
])
def test_local_redis_backend_rejects_missing_or_non_resolvable_identity_config(overrides, expected):
    settings = {
        "CREDIT_DEPLOYMENT_PROFILE": "local", "CREDIT_TASK_BACKEND": "redis",
        "CREDIT_TASK_REDIS_URL": "redis://localhost", "CREDIT_TASK_KEYRING": '{"k":"' + "a" * 64 + '"}',
        "CREDIT_TASK_ACTIVE_KEY_ID": "k", "CREDIT_AUTH_MODE": "demo",
        "CREDIT_MEMORY": "redis", "CREDIT_REDIS_URL": "redis://localhost",
        "CREDIT_API_WORKERS": "1", "CREDIT_API_INSTANCES": "1",
        **overrides,
    }
    with pytest.raises(DeploymentConfigurationError, match=expected):
        validate_deployment_profile(settings)


@pytest.mark.parametrize("settings", [
    {"CREDIT_DEPLOYMENT_PROFILE": "unknown"},
    {"CREDIT_API_WORKERS": "zero"},
    {"CREDIT_API_INSTANCES": "0"},
])
def test_invalid_deployment_settings_fail_closed(settings):
    with pytest.raises(DeploymentConfigurationError):
        validate_deployment_profile(settings)
