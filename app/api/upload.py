"""Upload endpoint: presigned S3 URL issuance.

The flow is two-step:

1. Client calls ``POST /uploads/presign`` with filename + size.
2. Server returns a presigned PUT URL and an ``upload_id``.
3. Client uploads directly to S3.
4. S3 emits ``ObjectCreated`` → ETL Lambda parses → records become available.

This module never proxies file bytes through the API.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import CognitoUser, current_user
from app.config import get_settings
from app.db import DataAccess
from app.models.entities import PresignRequest, PresignResponse
from app.s3 import generate_upload_presigned_url

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/presign", response_model=PresignResponse, status_code=status.HTTP_201_CREATED)
async def presign_upload(
    payload: PresignRequest,
    user: CognitoUser = Depends(current_user),
) -> PresignResponse:
    """Issue a presigned PUT URL for the caller to upload a file."""
    settings = get_settings()
    upload_id = str(uuid.uuid4())
    # The object key includes the user sub so the ETL Lambda can resolve
    # ownership without an extra lookup.
    safe_name = payload.filename.replace("/", "_").replace("..", "_")
    object_key = f"uploads/{user.sub}/{upload_id}/{safe_name}"

    url = generate_upload_presigned_url(
        bucket=settings.s3_uploads_bucket,
        key=object_key,
        content_type=payload.content_type,
        expiry_seconds=settings.s3_presign_expiry_seconds,
    )

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=settings.s3_presign_expiry_seconds)
    ).isoformat()

    # Audit trail.
    dao = DataAccess()
    dao.append_audit(
        owner_sub=user.sub,
        action="upload.presigned",
        actor_sub=user.sub,
        metadata={
            "upload_id": upload_id,
            "object_key": object_key,
            "size_bytes": payload.size_bytes,
            "content_type": payload.content_type,
            "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )

    return PresignResponse(
        upload_id=upload_id,
        object_key=object_key,
        url=url,
        method="PUT",
        expires_at=expires_at,
    )


@router.get("/{upload_id}", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def get_upload_status(upload_id: str, user: CognitoUser = Depends(current_user)):
    """Stub: a real implementation would read the UPLOAD record + ETL status."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Upload status endpoint not yet implemented",
    )