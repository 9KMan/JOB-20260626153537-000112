"""Pydantic entity definitions for the data platform API.

These models are the wire format of the HTTP API *and* the canonical shape of
records persisted in DynamoDB. They are deliberately kept close to the storage
representation so that round-trips do not require manual translation.

Each model carries:

* API-facing validators (e.g. ``title`` is required and bounded).
* DynamoDB-side metadata (``pk_attr``, ``sk_attr``) on the *class* via
  ``Config`` so :class:`app.db.DataAccess` can introspect keys without
  duplicating literals.

We use Pydantic v2 (``model_config = ConfigDict(...)``) for performance and
strict typing.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ItemStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# USER
# ---------------------------------------------------------------------------


class User(BaseModel):
    """Platform user (1:1 with a Cognito user)."""

    model_config = ConfigDict(extra="ignore")

    sub: str = Field(..., description="Cognito subject identifier")
    email: str
    display_name: str = Field("", alias="DisplayName")
    role: str = "operator"
    monday_person_id: Optional[str] = Field(None, alias="MondayPersonId")
    created_at: Optional[str] = Field(None, alias="CreatedAt")

    @classmethod
    def pk(cls, sub: str) -> str:
        return f"USER#{sub}"


# ---------------------------------------------------------------------------
# ITEM
# ---------------------------------------------------------------------------


class ItemBase(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    title: str = Field(..., min_length=1, max_length=512, alias="Title")
    body: dict[str, Any] = Field(default_factory=dict, alias="Body")
    status: ItemStatus = Field(default=ItemStatus.PENDING, alias="Status")


class ItemCreate(ItemBase):
    """Payload for ``POST /items``."""

    source_upload_id: Optional[str] = Field(None, alias="SourceUploadId")
    report_id: Optional[str] = Field(None, alias="ReportId")


class ItemUpdate(BaseModel):
    """Payload for ``PUT /items/{id}``. All fields optional for partial updates."""

    model_config = ConfigDict(extra="ignore")

    title: Optional[str] = Field(None, min_length=1, max_length=512)
    body: Optional[dict[str, Any]] = None
    status: Optional[ItemStatus] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else v


class Item(ItemBase):
    """A persisted item record."""

    item_id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="ItemId")
    owner_sub: str = Field(..., alias="OwnerSub")
    source_upload_id: Optional[str] = Field(None, alias="SourceUploadId")
    report_id: Optional[str] = Field(None, alias="ReportId")
    created_at: Optional[str] = Field(None, alias="CreatedAt")
    updated_at: Optional[str] = Field(None, alias="UpdatedAt")

    @classmethod
    def pk(cls, owner_sub: str) -> str:
        return f"USER#{owner_sub}"

    @classmethod
    def sk(cls, item_id: str, created_at_unix: int) -> str:
        return f"ITEM#{item_id}#v#{created_at_unix}"


# ---------------------------------------------------------------------------
# REPORT
# ---------------------------------------------------------------------------


class ReportFormat(str, enum.Enum):
    CSV = "csv"
    XLSX = "xlsx"
    PDF = "pdf"


class Report(BaseModel):
    """Ingestion manifest. One per uploaded file."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    report_id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="ReportId")
    owner_sub: str = Field(..., alias="OwnerSub")
    source_bucket: str = Field(..., alias="SourceBucket")
    source_key: str = Field(..., alias="SourceKey")
    source_etag: str = Field(..., alias="SourceETag")
    format: ReportFormat
    item_count: int = Field(0, alias="ItemCount")
    status: str = "received"
    created_at: Optional[str] = Field(None, alias="CreatedAt")

    @classmethod
    def pk(cls, owner_sub: str) -> str:
        return f"USER#{owner_sub}"

    @classmethod
    def sk(cls, report_id: str) -> str:
        return f"REPORT#{report_id}"


# ---------------------------------------------------------------------------
# UPLOAD
# ---------------------------------------------------------------------------


class PresignRequest(BaseModel):
    """Payload for ``POST /uploads/presign``."""

    model_config = ConfigDict(extra="ignore")

    filename: str = Field(..., min_length=1, max_length=512)
    content_type: str = Field("application/octet-stream", alias="contentType")
    size_bytes: int = Field(..., gt=0, le=100 * 1024 * 1024, alias="sizeBytes")


class PresignResponse(BaseModel):
    """Return shape for ``POST /uploads/presign``."""

    model_config = ConfigDict(extra="ignore")

    upload_id: str
    object_key: str
    url: str
    method: str = "PUT"
    expires_at: str


# ---------------------------------------------------------------------------
# AUDIT
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    """An audit log entry. Append-only."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="EventId")
    action: str
    actor_sub: str = Field(..., alias="ActorSub")
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = Field(None, alias="CreatedAt")