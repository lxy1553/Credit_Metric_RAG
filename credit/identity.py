"""Identity providers. Demo tokens are isolated from production OIDC JWTs."""

import re
from collections.abc import Callable

from credit.models import User

_CLAIM_TOKEN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class AuthenticationError(ValueError):
    pass


class DemoIdentityProvider:
    """Only constructed when the explicit local demo mode is enabled."""

    def __init__(self, users: dict[str, User]):
        self.users = users

    def resolve(self, token: str) -> User:
        user = self.users.get(token)
        if user is None:
            raise AuthenticationError("invalid demo token")
        return user


class OidcJwtIdentityProvider:
    """RS256-only OIDC verifier with an allowlisted claim-to-policy mapping."""

    def __init__(self, *, issuer: str, audience: str, jwks_url: str, roles_claim="roles",
                 domains_claim="domains", policy_version_claim="policy_version",
                 decoder: Callable[[str], dict] | None = None):
        if not all(isinstance(value, str) and value for value in (issuer, audience, jwks_url)):
            raise ValueError("OIDC issuer, audience and JWKS URL are required")
        self.roles_claim, self.domains_claim = roles_claim, domains_claim
        self.policy_version_claim = policy_version_claim
        if decoder is not None:
            self._decode = decoder
            return
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - installation boundary
            raise RuntimeError("安装生产身份依赖：pip install -e '.[auth]'") from exc
        jwks = jwt.PyJWKClient(jwks_url, cache_keys=True)

        def decode(token: str) -> dict:
            signing_key = jwks.get_signing_key_from_jwt(token)
            return jwt.decode(token, signing_key.key, algorithms=["RS256"], issuer=issuer, audience=audience,
                              options={"require": ["exp", "iat", "sub"]})

        self._decode = decode

    @staticmethod
    def _claims(value, claim: str, *, allow_empty=False) -> frozenset[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) or not _CLAIM_TOKEN.fullmatch(item)
                                              for item in value):
            raise AuthenticationError(f"invalid {claim} claim")
        result = frozenset(value)
        if not result and not allow_empty:
            raise AuthenticationError(f"empty {claim} claim")
        return result

    def resolve(self, token: str) -> User:
        if not isinstance(token, str) or not token or len(token) > 16_384:
            raise AuthenticationError("invalid bearer token")
        try:
            claims = self._decode(token)
        except Exception as exc:
            raise AuthenticationError("JWT verification failed") from exc
        sub, policy = claims.get("sub"), claims.get(self.policy_version_claim)
        if not isinstance(sub, str) or not sub or len(sub) > 128:
            raise AuthenticationError("invalid subject claim")
        if not isinstance(policy, str) or not policy or len(policy) > 128:
            raise AuthenticationError("invalid policy version claim")
        return User(sub, self._claims(claims.get(self.roles_claim), self.roles_claim),
                    self._claims(claims.get(self.domains_claim), self.domains_claim, allow_empty=True), policy)
