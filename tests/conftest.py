"""Shared pytest fixtures for the AWS serverless data-platform template.

The fixtures in this module wire up a mocked AWS environment backed by
``moto`` so tests can exercise the FastAPI routes and Lambda handlers
end-to-end without hitting real AWS.

What's provided:

* :func:`aws_credentials` - dummy credentials + AWS_DEFAULT_REGION so any
  accidental real-SDK client still talks to moto.
* :func:`dynamodb_table` - a single-table resource matching the production
  schema (PK/SK + GSI1/GSI2/GSI3).
* :func:`s3_bucket` - the uploads bucket the ETL handler reads from.
* :func:`client` - a FastAPI :class:`TestClient` with ``current_user``
  overridden to a fixed :class:`CognitoUser` and the ``DataAccess`` layer
  pointed at the moto table.
* :func:`fake_jwt_factory` / :func:`make_jwt` - helpers for tests that need
  to mint Cognito-shaped JWTs signed with a local RSA key.

All fixtures are function-scoped by default so each test gets a clean slate.
The tests must not mutate module-level caches that survive across tests; if a
test needs to override settings it should use :func:`monkeypatch` or
``app.config.get_settings.cache_clear()``.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import boto3
import pytest

# ---------------------------------------------------------------------------
# Make the template importable as ``app.*`` and ``lambdas.*`` from tests/.
# ---------------------------------------------------------------------------
TEMPLATE_ROOT = Path(__file__).resolve().parent.parent
if str(TEMPLATE_ROOT) not in sys.path:
    sys.path.insert(0, str(TEMPLATE_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_env() -> None:
    """Set sane defaults for anything not explicitly overridden by a test."""
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
    os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
    os.environ.setdefault("DYNAMODB_TABLE_NAME", "serverless_data_platform_core")
    os.environ.setdefault("S3_UPLOADS_BUCKET", "serverless-data-platform-uploads")
    os.environ.setdefault("S3_ARTIFACTS_BUCKET", "serverless-data-platform-artifacts")
    os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-1_testpool")
    os.environ.setdefault("COGNITO_APP_CLIENT_ID", "test-client-id")
    os.environ.setdefault("COGNITO_REGION", "us-east-1")
    # Cognito auth is bypassed by default in items tests via dependency
    # override, but the auth-specific tests will flip this off.
    os.environ.setdefault("COGNITO_AUTH_BYPASS", "false")


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force dummy credentials so accidental real clients still talk to moto."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------


@pytest.fixture
def dynamodb_table(aws_credentials: None):
    """Provision the single-table schema (PK/SK + 3 GSIs) inside a moto context.

    The schema here mirrors the production table that ``app.db.DataAccess``
    expects (PK/SK + GSI1/GSI2/GSI3) and the GSIs used by the platform:

    * ``GSI1`` - status / time-ordered listing
    * ``GSI2`` - report-id resolution
    * ``GSI3`` - ETag-based idempotency

    Returns a ``boto3.resource('dynamodb').Table`` handle.
    """
    # Imported lazily so the moto import is only required when tests run.
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        table_name = os.environ["DYNAMODB_TABLE_NAME"]

        client.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
                {"AttributeName": "GSI2PK", "AttributeType": "S"},
                {"AttributeName": "GSI2SK", "AttributeType": "S"},
                {"AttributeName": "GSI3PK", "AttributeType": "S"},
                {"AttributeName": "GSI3SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI2",
                    "KeySchema": [
                        {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI3",
                    "KeySchema": [
                        {"AttributeName": "GSI3PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI3SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Wait for the table to become ACTIVE (moto is usually immediate but
        # we don't want to depend on that).
        waiter = client.get_waiter("table_exists")
        waiter.wait(TableName=table_name)

        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.Table(table_name)

        # Reset any cached clients from a previous fixture so they re-resolve
        # against this moto context.
        try:
            from app.db import reset_client_cache

            reset_client_cache()
        except Exception:
            pass

        yield table


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


@pytest.fixture
def s3_bucket(aws_credentials: None):
    """Create the uploads bucket inside moto and return its name.

    Yields the bucket name; tests should use the ``boto3.client('s3')`` they
    construct locally (or import the one cached in ``app.s3``) to interact
    with it. The S3 client will see the moto context because ``mock_aws``
    patches at the botocore layer.
    """
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        bucket = os.environ["S3_UPLOADS_BUCKET"]
        client.create_bucket(Bucket=bucket)

        try:
            from app.s3 import reset_client_cache

            reset_client_cache()
        except Exception:
            pass

        yield bucket


# ---------------------------------------------------------------------------
# FastAPI TestClient + auth override
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_user() -> dict[str, Any]:
    """The :class:`CognitoUser` substituted into ``current_user`` for tests.

    Tests can override the ``sub`` (and other fields) by depending on this
    fixture and monkey-patching the returned dict, or by replacing the
    override entirely via ``app.dependency_overrides``.
    """
    return {
        "sub": "test-user-sub",
        "username": "test-user",
        "email": "test@example.com",
        "cognito_groups": ["operators"],
        "raw_claims": {"sub": "test-user-sub"},
    }


@pytest.fixture
def client(dynamodb_table, auth_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    """Build a FastAPI ``TestClient`` with deps pointing at the moto table.

    The fixture:

    * Forces ``COGNITO_AUTH_BYPASS=true`` so ``app.auth.current_user`` returns
      the placeholder user without verifying a JWT. The auth-specific tests
      override this.
    * Overrides ``current_user`` via ``app.dependency_overrides`` so the test
      always sees ``auth_user`` regardless of JWT state.
    * Resets ``app.db.get_dynamodb_resource`` so the route's ``DataAccess``
      picks up the moto-backed table.
    """
    # Lazy imports keep top-level imports cheap and avoid touching moto at
    # collection time.
    from fastapi.testclient import TestClient

    from app.auth import CognitoUser, current_user
    from app.config import get_settings
    from app.db import reset_client_cache

    # Force the auth-bypass off and let the dependency override do the work.
    monkeypatch.setenv("COGNITO_AUTH_BYPASS", "false")

    # Reset cached boto3 clients so they pick up the moto context.
    reset_client_cache()
    get_settings.cache_clear()  # type: ignore[attr-defined]

    def _override_current_user() -> CognitoUser:
        return CognitoUser(
            sub=auth_user["sub"],
            username=auth_user["username"],
            email=auth_user["email"],
            cognito_groups=list(auth_user["cognito_groups"]),
            raw_claims=dict(auth_user["raw_claims"]),
        )

    from app.main import create_app

    app = create_app()
    app.dependency_overrides[current_user] = _override_current_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Cognito JWT helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[Any, bytes]:
    """Generate a single RSA keypair reused across the test session.

    Returns ``(private_key, public_pem_bytes)``. Tests that need a JWKS
    document convert the public PEM into JWK form via :func:`_public_pem_to_jwk`.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_key, public_pem


@pytest.fixture(scope="session")
def rsa_jwk(rsa_keypair: tuple[Any, bytes]) -> dict[str, Any]:
    """The JWK form of :func:`rsa_keypair` (what a Cognito JWKS endpoint returns)."""
    private_key, public_pem = rsa_keypair
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

    public_key = serialization.load_pem_public_key(public_pem)
    if not isinstance(public_key, RSAPublicKey):
        raise RuntimeError("expected an RSA public key")

    numbers = public_key.public_numbers()
    import base64

    def _b64u(value: int) -> str:
        # URL-safe base64 with no padding (JWK convention).
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": "test-key-1",
        "n": _b64u(numbers.n),
        "e": _b64u(numbers.e),
    }


@pytest.fixture
def jwks_payload(rsa_jwk: dict[str, Any]) -> dict[str, Any]:
    """A minimal JWKS document pointing at our test key."""
    return {"keys": [rsa_jwk]}


@pytest.fixture
def make_jwt(
    rsa_keypair: tuple[Any, bytes],
    rsa_jwk: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """Factory that mints Cognito-shaped JWTs signed with the test key.

    Also patches :func:`app.auth._get_jwks` and the module-level JWKS cache
    so :func:`app.auth.verify_cognito_jwt` will trust tokens signed with
    the test key, no network calls required.

    Returns a callable ``make_jwt(sub=..., **claims) -> str`` where extra
    ``claims`` override defaults (``iss``, ``aud``, ``exp``, ``token_use``,
    ``cognito:username``).
    """
    from app import auth as app_auth

    private_key, _public_pem = rsa_keypair

    # Reset and seed the JWKS cache so verify_cognito_jwt finds the test key.
    app_auth.clear_jwks_cache()
    pool_id = os.environ["COGNITO_USER_POOL_ID"]
    region = os.environ["COGNITO_REGION"]
    url = app_auth._jwks_url(pool_id, region)
    import time as _time

    app_auth._JWKS_CACHE[url] = (_time.monotonic(), {"keys": [rsa_jwk]})

    def _factory(
        sub: str = "test-user-sub",
        username: Optional[str] = None,
        email: Optional[str] = "test@example.com",
        groups: Optional[list[str]] = None,
        expires_in: int = 3600,
        issuer: Optional[str] = None,
        audience: Optional[str] = None,
        token_use: str = "id",
        **extra: Any,
    ) -> str:
        import time as _time

        from jose import jwt

        now = int(_time.time())
        claims: dict[str, Any] = {
            "sub": sub,
            "cognito:username": username or sub,
            "email": email,
            "cognito:groups": list(groups or []),
            "iss": issuer or f"https://cognito-idp.{region}.amazonaws.com/{pool_id}",
            "aud": audience or os.environ["COGNITO_APP_CLIENT_ID"],
            "iat": now,
            "exp": now + expires_in,
            "token_use": token_use,
        }
        claims.update(extra)

        return jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": rsa_jwk["kid"]},
        )

    return _factory


@pytest.fixture
def auth_headers(make_jwt: Callable[..., str]) -> dict[str, str]:
    """Convenience: a ready-to-use ``Authorization`` header for happy-path tests."""
    return {"Authorization": f"Bearer {make_jwt()}"}