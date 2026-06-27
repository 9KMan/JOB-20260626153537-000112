"""Centralised configuration for the data platform API.

All runtime configuration is loaded from environment variables (with optional
``.env`` support via ``pydantic-settings``). No values are baked in at import
time, which means tests can override any setting with a simple monkey-patch on
``os.environ`` before the application instantiates.

The :class:`Settings` object is intentionally a *singleton*: every module that
needs configuration imports ``get_settings()`` rather than constructing its own
``Settings()`` instance. This guarantees consistent behaviour and makes it
trivial to swap in a test double via dependency overrides.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- AWS --------------------------------------------------------------
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
    aws_account_id: str = Field(default="000000000000", alias="AWS_ACCOUNT_ID")
    aws_access_key_id: Optional[str] = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: Optional[str] = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")

    # ---- DynamoDB ---------------------------------------------------------
    dynamodb_table_name: str = Field(
        default="serverless_data_platform_core", alias="DYNAMODB_TABLE_NAME"
    )
    # Optional override pointing at LocalStack / DynamoDB Local.
    dynamodb_endpoint_url: Optional[str] = Field(default=None, alias="DYNAMODB_ENDPOINT_URL")

    # ---- S3 ---------------------------------------------------------------
    s3_uploads_bucket: str = Field(
        default="serverless-data-platform-uploads", alias="S3_UPLOADS_BUCKET"
    )
    s3_artifacts_bucket: str = Field(
        default="serverless-data-platform-artifacts", alias="S3_ARTIFACTS_BUCKET"
    )
    s3_presign_expiry_seconds: int = Field(default=900, alias="S3_PRESIGN_EXPIRY_SECONDS")

    # ---- Cognito ----------------------------------------------------------
    cognito_user_pool_id: str = Field(default="us-east-1_xxxxxxxxx", alias="COGNITO_USER_POOL_ID")
    cognito_app_client_id: str = Field(
        default="xxxxxxxxxxxxxxxxxxxxxxxxxx", alias="COGNITO_APP_CLIENT_ID"
    )
    cognito_region: str = Field(default="us-east-1", alias="COGNITO_REGION")
    # Bug 158 fix: expected issuer claim value, derived from pool id + region.
    # Validated against the JWT's `iss` claim during verification.
    cognito_issuer: Optional[str] = Field(default=None, alias="COGNITO_ISSUER")
    # Useful for local development; MUST be "false" in any deployed environment.
    cognito_auth_bypass: bool = Field(default=False, alias="COGNITO_AUTH_BYPASS")

    # ---- Monday.com -------------------------------------------------------
    monday_api_token: Optional[str] = Field(default=None, alias="MONDAY_API_TOKEN")
    monday_webhook_secret: Optional[str] = Field(default=None, alias="MONDAY_WEBHOOK_SECRET")

    # ---- API --------------------------------------------------------------
    api_base_url: str = Field(default="https://api.example.com", alias="API_BASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ---- ETL --------------------------------------------------------------
    etl_max_retries: int = Field(default=3, alias="ETL_MAX_RETRIES")
    etl_backoff_base_seconds: float = Field(default=0.5, alias="ETL_BACKOFF_BASE_SECONDS")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` instance.

    Using ``lru_cache`` ensures that environment variables are read exactly once
    per process. Tests can clear the cache with ``get_settings.cache_clear()``.
    """
    return Settings()