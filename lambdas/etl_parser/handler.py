"""S3 ObjectCreated → parse → DynamoDB ETL handler.

Triggered by an EventBridge rule on ``s3:ObjectCreated:*`` events for the
``uploads`` bucket. The handler is intentionally thin glue: it does the I/O
and idempotency bookkeeping, then delegates parsing to the format-specific
modules.

Design constraints:

* **No module-level AWS calls.** All boto3 clients are constructed lazily so
  the module is importable in test contexts where AWS isn't available.
* **Idempotent.** Two complementary safeguards:
    1. GSI3 lookup by ``ETag`` — short-circuit if this upload was processed.
    2. Conditional ``PutItem`` on the REPORT record — race-safe.
* **Retryable.** Transient AWS errors propagate so Lambda retries; parse
  failures raise after a small in-handler retry loop so we don't waste
  Lambda retries on bad input. The CDK provisions an SQS DLQ for events
  that fail twice.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from lambdas.etl_parser.parsers import parse_csv, parse_pdf, parse_xlsx

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class TerminalError(Exception):
    """A failure that should NOT be retried (e.g. malformed input)."""


class TransientError(Exception):
    """A failure that SHOULD be retried (e.g. DynamoDB throttling)."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler.

    The event shape is either:

    * Direct S3 notification (legacy):
        ``{"Records": [{"s3": {"bucket": {"name": "..."}, "object": {"key": "..."}}}]}``
    * EventBridge S3 event:
        ``{"detail": {"bucket": {"name": "..."}, "object": {"key": "..."}}}``

    Returns a summary dict for CloudWatch Logs Insights.
    """
    logger.info("ETL handler invoked: %s", _truncate(event))
    records = _extract_records(event)
    summaries = [process_record(r) for r in records]
    return {"processed": len(summaries), "summaries": summaries}


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def process_record(record: dict[str, Any]) -> dict[str, Any]:
    """Process one S3 record end-to-end."""
    bucket, key, etag = _extract_s3_identifiers(record)
    logger.info("Processing s3://%s/%s (etag=%s)", bucket, key, etag)

    if not _allowed_bucket(bucket):
        raise TerminalError(f"Bucket {bucket!r} not in allowed list")

    if not _allowed_suffix(key):
        raise TerminalError(f"Key {key!r} has unsupported extension")

    content = _download_with_retry(bucket, key)
    parsed = parse_object(content, key)
    logger.info("Parsed %d records from %s", len(parsed), key)

    # Determine the owner. The presigned URL embeds the user sub in the key:
    #   uploads/<sub>/<upload_id>/<filename>
    owner_sub = _owner_from_key(key) or "system"

    summary = write_to_dynamodb(
        owner_sub=owner_sub,
        source_bucket=bucket,
        source_key=key,
        source_etag=etag,
        parsed_records=parsed,
        filename=key,
    )
    summary["object_size"] = len(content)
    return summary


def parse_object(content: bytes, key: str) -> list[dict[str, Any]]:
    """Dispatch to the right parser based on file extension."""
    suffix = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    if suffix == "csv":
        return parse_csv(content, filename=key)
    if suffix in ("xlsx", "xlsm"):
        return parse_xlsx(content, filename=key)
    if suffix == "pdf":
        return parse_pdf(content, filename=key, source_key=key)
    raise TerminalError(f"Unsupported file extension: .{suffix}")


def write_to_dynamodb(
    owner_sub: str,
    source_bucket: str,
    source_key: str,
    source_etag: str,
    parsed_records: list[dict[str, Any]],
    filename: str,
) -> dict[str, Any]:
    """Persist parsed records to DynamoDB. Idempotent on ETag."""
    from app.config import get_settings  # noqa: PLC0415 (avoid circular at import)
    from app.db import DataAccess  # noqa: PLC0415

    settings = get_settings()
    dao = DataAccess()

    # 1. Report-level idempotency via GSI3.
    import uuid

    report_id = str(uuid.uuid4())
    created, _existing = dao.upsert_report_idempotent(
        owner_sub=owner_sub,
        report_id=report_id,
        source_bucket=source_bucket,
        source_key=source_key,
        source_etag=source_etag,
        format=_format_from_key(filename),
        item_count=len(parsed_records),
    )
    if not created:
        logger.info("Report already exists for etag=%s; short-circuiting", source_etag)
        return {
            "report_id": None,
            "items_written": 0,
            "duplicate": True,
        }

    # 2. Write each parsed record as an ITEM.
    items_written = 0
    for record in parsed_records:
        title = _derive_title(record, filename)
        body = _derive_body(record)
        try:
            dao.create_item(
                owner_sub=owner_sub,
                title=title,
                body=body,
                status="active",
                source_upload_id=None,
                report_id=report_id,
            )
            items_written += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Concurrent worker beat us; safe to ignore.
                continue
            raise

    return {
        "report_id": report_id,
        "items_written": items_written,
        "duplicate": False,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_records(event: dict[str, Any]) -> list[dict[str, Any]]:
    if "Records" in event and event["Records"]:
        return event["Records"]
    if "detail" in event and "bucket" in event["detail"]:
        return [{"s3": event["detail"]}]
    raise TerminalError(f"Unrecognised event shape: {_truncate(event)}")


def _extract_s3_identifiers(record: dict[str, Any]) -> tuple[str, str, str]:
    """Return (bucket, key, etag) from a record."""
    s3 = record.get("s3", record)
    bucket_obj = s3.get("bucket", {})
    bucket = bucket_obj.get("name") or bucket_obj.get("arn", "").split(":")[-1]
    obj = s3.get("object", {})
    key = obj.get("key", "")
    etag = obj.get("etag") or obj.get("eTag") or ""
    return bucket, key, etag


def _allowed_bucket(bucket: str) -> bool:
    allowed = os.environ.get("ALLOWED_UPLOAD_BUCKETS", "")
    if not allowed:
        return True  # no restriction configured
    return bucket in {b.strip() for b in allowed.split(",") if b.strip()}


def _allowed_suffix(key: str) -> bool:
    return key.lower().endswith((".csv", ".xlsx", ".xlsm", ".pdf"))


def _format_from_key(key: str) -> str:
    if key.lower().endswith(".csv"):
        return "csv"
    if key.lower().endswith((".xlsx", ".xlsm")):
        return "xlsx"
    if key.lower().endswith(".pdf"):
        return "pdf"
    return "unknown"


def _owner_from_key(key: str) -> Optional[str]:
    """Extract the user sub from a presigned-upload key.

    Expected shape: ``uploads/<sub>/<upload_id>/<filename>``.
    """
    parts = key.split("/", 2)
    if len(parts) >= 2 and parts[0] == "uploads":
        return parts[1] or None
    return None


def _derive_title(record: dict[str, Any], filename: str) -> str:
    for key in ("title", "name", "subject", "description"):
        if key in record and record[key]:
            return str(record[key])[:512]
    # Fall back to a synthesised title.
    return f"{_format_from_key(filename).upper()} row from {filename.rsplit('/', 1)[-1]}"


def _derive_body(record: dict[str, Any]) -> dict[str, Any]:
    # Treat ``RowId``, ``PageNumber``, etc. as metadata; everything else is body.
    meta_keys = {"RowId", "PageNumber", "TableIndex"}
    return {k: v for k, v in record.items() if k not in meta_keys}


def _download_with_retry(bucket: str, key: str, *, max_attempts: int = 3) -> bytes:
    """Download an S3 object with bounded retries on transient errors."""
    s3 = boto3.client("s3")
    delay = float(os.environ.get("ETL_BACKOFF_BASE_SECONDS", "0.5"))
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = s3.get_object(Bucket=bucket, Key=key)
            return resp["Body"].read()
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("NoSuchKey", "NoSuchBucket"):
                raise TerminalError(f"S3 object missing: {code}") from exc
            last_exc = exc
            logger.warning(
                "S3 download attempt %d/%d failed: %s", attempt, max_attempts, exc
            )
            time.sleep(delay * (2 ** (attempt - 1)))
    raise TransientError(f"S3 download failed after {max_attempts} attempts: {last_exc}")


def _truncate(obj: Any, limit: int = 500) -> str:
    s = str(obj)
    return s if len(s) <= limit else s[:limit] + "..."


# Touch Optional so it remains importable for downstream type hints.
_ = Optional