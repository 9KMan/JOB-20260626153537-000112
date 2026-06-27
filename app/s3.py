"""S3 client + presigned URL helpers.

The API never proxies file uploads through the Lambda — clients PUT directly to
S3 using a presigned URL issued by :func:`generate_upload_presigned_url`. This
keeps the Lambda free of multipart parsing and bounded by request size limits,
and it lets clients stream large files.

Helpers:

* :func:`get_s3_client` — lazy boto3 S3 client.
* :func:`generate_upload_presigned_url` — issue a PUT URL.
* :func:`generate_download_presigned_url` — issue a GET URL for artifacts.
* :func:`head_object` — fetch metadata (ETag, size) for idempotency checks.

No module-level AWS calls.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Optional

import boto3
from botocore.config import Config as BotoConfig

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _client_kwargs(settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "region_name": settings.aws_region,
        "config": BotoConfig(
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=5,
            read_timeout=10,
            user_agent_extra="serverless-data-platform/1.0",
            signature_version="s3v4",
        ),
    }
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return kwargs


@lru_cache(maxsize=1)
def get_s3_client():
    settings = get_settings()
    return boto3.client("s3", **_client_kwargs(settings))


@lru_cache(maxsize=1)
def get_s3_resource():
    settings = get_settings()
    return boto3.resource("s3", **_client_kwargs(settings))


def reset_client_cache() -> None:
    get_s3_client.cache_clear()
    get_s3_resource.cache_clear()


def generate_upload_presigned_url(
    bucket: str,
    key: str,
    content_type: Optional[str] = None,
    expiry_seconds: Optional[int] = None,
) -> str:
    """Return a presigned PUT URL the client can use to upload directly to S3."""
    settings = get_settings()
    expiry = expiry_seconds or settings.s3_presign_expiry_seconds
    params: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    return get_s3_client().generate_presigned_url(
        "put_object",
        Params=params,
        ExpiresIn=expiry,
        HttpMethod="PUT",
    )


def generate_download_presigned_url(
    bucket: str,
    key: str,
    expiry_seconds: Optional[int] = None,
    response_content_disposition: Optional[str] = None,
) -> str:
    """Return a presigned GET URL for downloading an artifact."""
    settings = get_settings()
    expiry = expiry_seconds or settings.s3_presign_expiry_seconds
    params: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if response_content_disposition:
        params["ResponseContentDisposition"] = response_content_disposition
    return get_s3_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=expiry,
        HttpMethod="GET",
    )


def head_object(bucket: str, key: str) -> dict[str, Any]:
    """Return the HEAD response for an S3 object. Used for ETag-based idempotency."""
    return get_s3_client().head_object(Bucket=bucket, Key=key)


def get_object_bytes(bucket: str, key: str) -> bytes:
    """Read an S3 object into memory. Used by the ETL Lambda."""
    obj = get_s3_resource().Object(bucket, key)
    return obj.get()["Body"].read()