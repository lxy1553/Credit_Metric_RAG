import pytest
from fastapi.testclient import TestClient

from credit.api import create_app
from credit.identity import AuthenticationError, DemoIdentityProvider, OidcJwtIdentityProvider
from credit.memory import MemoryStore
from credit.models import User
from credit.security import DEMO_USERS


def test_demo_identity_is_explicit_mapping_only():
    provider = DemoIdentityProvider(DEMO_USERS)
    assert provider.resolve("demo-analyst").user_id == "analyst-001"
    with pytest.raises(AuthenticationError):
        provider.resolve("analyst")


def test_oidc_claims_are_verified_by_injected_decoder_and_strictly_mapped():
    claims = {"sub": "oidc-42", "roles": ["analyst"], "domains": ["credit"], "policy_version": "policy-7"}
    provider = OidcJwtIdentityProvider(issuer="https://issuer", audience="credit-api", jwks_url="https://issuer/jwks",
                                       decoder=lambda token: claims if token == "verified" else (_ for _ in ()).throw(ValueError()))
    assert provider.resolve("verified") == User("oidc-42", frozenset({"analyst"}), frozenset({"credit"}), "policy-7")
    with pytest.raises(AuthenticationError):
        provider.resolve("bad")


@pytest.mark.parametrize("claims", [
    {"sub": "x", "roles": "analyst", "domains": ["credit"], "policy_version": "v1"},
    {"sub": "x", "roles": ["ADMIN!"], "domains": ["credit"], "policy_version": "v1"},
    {"sub": "x", "roles": ["analyst"], "domains": ["credit"], "policy_version": ""},
])
def test_oidc_rejects_untrusted_or_malformed_claims(claims):
    provider = OidcJwtIdentityProvider(issuer="https://issuer", audience="credit-api", jwks_url="https://issuer/jwks",
                                       decoder=lambda token: claims)
    with pytest.raises(AuthenticationError):
        provider.resolve("verified")


def test_api_uses_identity_provider_not_raw_request_role(db):
    provider = OidcJwtIdentityProvider(
        issuer="https://issuer", audience="credit-api", jwks_url="https://issuer/jwks",
        decoder=lambda token: {"sub": "oidc-42", "roles": ["visitor"], "domains": ["credit"], "policy_version": "v1"}
        if token == "verified" else (_ for _ in ()).throw(ValueError()),
    )
    client = TestClient(create_app(db, {}, MemoryStore(), identity_provider=provider))
    denied = client.post("/v1/questions", headers={"Authorization": "Bearer verified"},
                         json={"question": "2026-03-02 逾期率", "roles": ["analyst"]})
    assert denied.status_code == 422
    refused = client.post("/v1/questions", headers={"Authorization": "Bearer verified"},
                          json={"question": "2026-03-02 逾期率"})
    assert refused.json()["status"] == "refused"
