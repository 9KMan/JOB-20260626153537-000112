"""Tests for the Cognito JWT verifier in :mod:`app.auth`.

These tests stand up an in-process RSA keypair, mint Cognito-shaped JWTs
with ``python-jose``, and verify that :func:`app.auth.verify_cognito_jwt`
correctly accepts good tokens and rejects bad ones (expired, wrong issuer,
missing header).

The FastAPI side of auth — i.e. :func:`current_user` returning a 401 when
the bearer token is missing — is also covered here.
"""

from __future__ import annotations

import time
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_jwt_passes_verification(
    make_jwt: Any,
    client: Any,
) -> None:
    """A JWT signed with the test key, with the right iss/aud/exp, is accepted."""
    from app.auth import verify_cognito_jwt
    from app.config import get_settings

    token = make_jwt(sub="alice", username="alice", email="alice@example.com")

    settings = get_settings()
    claims = verify_cognito_jwt(token, settings)

    assert claims["sub"] == "alice"
    assert claims["email"] == "alice@example.com"
    assert claims["iss"].endswith(settings.cognito_user_pool_id)
    assert claims["aud"] == settings.cognito_app_client_id


# ---------------------------------------------------------------------------
# Expired token
# ---------------------------------------------------------------------------


def test_expired_jwt_is_rejected(make_jwt: Any) -> None:
    """An expired JWT (exp in the past) yields a 401 with ``Token expired``."""
    from fastapi import HTTPException

    from app.auth import verify_cognito_jwt
    from app.config import get_settings

    # ``expires_in`` is a delta from now; pass a negative number to force
    # the token to be already expired.
    token = make_jwt(expires_in=-60)

    with pytest.raises(HTTPException) as exc_info:
        verify_cognito_jwt(token, get_settings())

    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Wrong issuer
# ---------------------------------------------------------------------------


def test_wrong_issuer_is_rejected(make_jwt: Any) -> None:
    """A token whose ``iss`` does not match the user pool is rejected."""
    from fastapi import HTTPException

    from app.auth import verify_cognito_jwt
    from app.config import get_settings

    token = make_jwt(
        issuer="https://cognito-idp.us-east-1.amazonaws.com/eu-west-1_WrongPool",
    )

    with pytest.raises(HTTPException) as exc_info:
        verify_cognito_jwt(token, get_settings())

    assert exc_info.value.status_code == 401
    # python-jose surfaces issuer mismatches as "Invalid issuer" or similar.
    assert "invalid" in exc_info.value.detail.lower() or "issuer" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Missing Authorization header
# ---------------------------------------------------------------------------


def test_missing_authorization_header_returns_401(client: Any) -> None:
    """A request with no Authorization header is rejected at 401 by
    :func:`app.auth.current_user`.

    We hit any protected route (e.g. ``/items``) without setting the
    Authorization header; because ``client`` overrides ``current_user``
    to a fake user, we instead exercise the extractor directly by sending
    a request without overriding ``current_user``. To make this meaningful,
    we rebuild a minimal FastAPI app for this test that does NOT install
    the dependency override.
    """
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.auth import current_user
    from app.config import get_settings

    # Clear settings cache so the new env (if any) takes effect.
    get_settings.cache_clear()  # type: ignore[attr-defined]

    app = FastAPI()

    @app.get("/protected")
    async def _protected(user: Any = Depends(current_user)) -> dict[str, Any]:
        return {"sub": user.sub}

    # NOTE: no dependency_overrides — we want the real extractor to run.
    with TestClient(app) as test_client:
        response = test_client.get("/protected")

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    assert "missing" in response.json()["detail"].lower() or "authorization" in response.json()["detail"].lower()