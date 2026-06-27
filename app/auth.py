"""Cognito JWT verification.

This module is the single source of truth for *who is calling the API*. It exposes:

* :func:`verify_cognito_jwt` — low-level verifier that decodes and validates a JWT
  against the public keys published by the user pool's ``cognito-idp`` endpoint.
* :func:`current_user` — FastAPI dependency that pulls the bearer token out of the
  ``Authorization`` header and resolves it to a :class:`CognitoUser`.
* :class:`CognitoUser` — the principal object passed into route handlers.

The verifier is deliberately defensive: it caches JWKS keys for ten minutes
(in-process) so a Lambda warm invocation does not hit Cognito on every request,
but it rotates them well before any sane key-rotation window. The cache is *not*
shared across Lambda instances; that is fine because the cache cost per
instance is negligible and the alternative (DynamoDB-backed cache) introduces a
new failure mode for marginal benefit.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests
from fastapi import Depends, Header, HTTPException, status
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS_TTL_SECONDS = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CognitoUser:
    """Authenticated principal extracted from a verified JWT."""

    sub: str
    username: str
    email: Optional[str]
    cognito_groups: list[str]
    raw_claims: dict[str, Any]

    def has_group(self, group: str) -> bool:
        return group in self.cognito_groups


# ---------------------------------------------------------------------------
# JWKS retrieval
# ---------------------------------------------------------------------------


def _jwks_url(user_pool_id: str, region: str) -> str:
    return (
        f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        "/.well-known/jwks.json"
    )


def _get_jwks(user_pool_id: str, region: str) -> dict[str, Any]:
    """Return the JWKS for the configured user pool, with TTL-based caching."""
    url = _jwks_url(user_pool_id, region)
    now = time.monotonic()
    cached = _JWKS_CACHE.get(url)
    if cached and (now - cached[0]) < _JWKS_TTL_SECONDS:
        return cached[1]
    response = requests.get(url, timeout=5)
    response.raise_for_status()
    jwks = response.json()
    _JWKS_CACHE[url] = (now, jwks)
    return jwks


def _signing_key(jwks: dict[str, Any], kid: str) -> Optional[dict[str, Any]]:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def clear_jwks_cache() -> None:
    """Drop cached JWKS. Useful in tests."""
    _JWKS_CACHE.clear()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_cognito_jwt(token: str, settings: Settings) -> dict[str, Any]:
    """Verify a Cognito-issued JWT and return its claims.

    Raises :class:`HTTPException` (401) on any verification failure.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Malformed token header: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    kid = unverified_header.get("kid")
    if not kid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing 'kid' header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        jwks = _get_jwks(settings.cognito_user_pool_id, settings.cognito_region)
    except requests.RequestException as exc:
        logger.error("Failed to fetch Cognito JWKS: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication backend unavailable",
        ) from exc

    key = _signing_key(jwks, kid)
    if key is None:
        # Cache may be stale; drop and refetch once.
        clear_jwks_cache()
        try:
            jwks = _get_jwks(settings.cognito_user_pool_id, settings.cognito_region)
            key = _signing_key(jwks, kid)
        except requests.RequestException:
            key = None
        if key is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Signing key not found",
                headers={"WWW-Authenticate": "Bearer"},
            )

    try:
        expected_issuer = settings.cognito_issuer or (
            f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/"
            f"{settings.cognito_user_pool_id}"
        )
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.cognito_app_client_id,
            issuer=expected_issuer,
            options={"verify_at_hash": False, "verify_iss": True},
        )
    except ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return claims


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def _extract_bearer(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1]


def current_user(
    authorization: Optional[str] = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> CognitoUser:
    """FastAPI dependency: extract & verify the bearer token, return the principal.

    Set ``COGNITO_AUTH_BYPASS=true`` to skip verification in local dev only. This
    guard is intentionally simple — it must never be ``true`` in a deployed
    environment.
    """
    if settings.cognito_auth_bypass:
        # Local development only. Caller is the placeholder user.
        return CognitoUser(
            sub="local-dev-user",
            username="local-dev",
            email="local@example.com",
            cognito_groups=["admin"],
            raw_claims={"sub": "local-dev-user", "cognito:groups": ["admin"]},
        )

    token = _extract_bearer(authorization)
    claims = verify_cognito_jwt(token, settings)

    groups_claim = claims.get("cognito:groups", [])
    if isinstance(groups_claim, str):
        # Cognito sometimes returns a single string when only one group exists.
        groups_claim = [groups_claim]

    return CognitoUser(
        sub=claims.get("sub", ""),
        username=claims.get("cognito:username", claims.get("sub", "")),
        email=claims.get("email"),
        cognito_groups=list(groups_claim),
        raw_claims=claims,
    )


def require_group(group: str):
    """Build a dependency that asserts the caller belongs to ``group``."""

    def _checker(user: CognitoUser = Depends(current_user)) -> CognitoUser:
        if not user.has_group(group):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Group '{group}' required",
            )
        return user

    return _checker


# Re-export the ``json`` module so tests can monkey-patch the cache deterministically
# without importing ``json`` themselves.
__all__ = [
    "CognitoUser",
    "current_user",
    "require_group",
    "verify_cognito_jwt",
    "clear_jwks_cache",
]
# Touch json to silence linters warning about unused import; we keep it available
# for downstream monkey-patching in tests.
_ = json