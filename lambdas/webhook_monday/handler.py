"""Monday.com inbound webhook handler.

Flow:

1. Read the JSON body and the ``Authorization`` header (HMAC-SHA256 signature).
2. Verify the signature using ``MONDAY_WEBHOOK_SECRET``.
3. Decode the webhook payload (challenge / event).
4. For each ``change`` event, look up the corresponding ITEM via GSI2 and update
   its status in DynamoDB.
5. Post a confirmation comment back to Monday via :class:`MondayClient`.

A 401 is returned on signature failure. A 200 is returned on success — including
when the event is irrelevant (e.g. an unsupported ``type``). The latter is
intentional: Monday retries on non-2xx, and re-processing an irrelevant event
will keep failing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

from lambdas.webhook_monday.client import MondayClient

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy handler for the Monday webhook.

    ``event`` is the API Gateway proxy v2 event:

        {
          "headers": {"authorization": "..."},
          "body": "...",
          "isBase64Encoded": False
        }
    """
    headers = event.get("headers", {})
    raw_body = event.get("body", "")
    if event.get("isBase64Encoded"):
        import base64

        raw_body = base64.b64decode(raw_body).decode("utf-8")

    secret = os.environ.get("MONDAY_WEBHOOK_SECRET", "")
    if not secret:
        logger.error("MONDAY_WEBHOOK_SECRET is not configured")
        return _response(500, {"detail": "webhook not configured"})

    signature = headers.get("authorization") or headers.get("Authorization") or ""
    if not verify_signature(raw_body, signature, secret):
        logger.warning("Monday webhook signature verification failed")
        return _response(401, {"detail": "invalid signature"})

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return _response(400, {"detail": "invalid JSON"})

    # Monday sends a "challenge" handshake when the webhook is first created.
    challenge = payload.get("challenge")
    if challenge:
        return _response(200, {"challenge": challenge})

    event_type = payload.get("type") or payload.get("event", {}).get("type")
    logger.info("Monday webhook event: type=%s", event_type)

    try:
        handled = _handle_event(payload)
    except Exception:
        logger.exception("Failed to handle Monday webhook event")
        # Re-raise so Lambda retries / DLQs.
        raise

    return _response(200, {"ok": True, "handled": handled})


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def verify_signature(body: str, signature_header: str, secret: str) -> bool:
    """Verify a Monday.com webhook signature.

    Monday uses ``HMAC-SHA256(secret, body)`` and ships the result as a
    hex-encoded digest in the ``Authorization`` header.
    """
    if not signature_header:
        return False
    # Be tolerant: support ``Bearer <hex>``, ``<hex>``, and ``sha256=<hex>``.
    sig = signature_header.strip()
    for prefix in ("Bearer ", "bearer ", "sha256="):
        if sig.startswith(prefix):
            sig = sig[len(prefix):]
            break
    expected = hmac.new(
        secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig.lower(), expected.lower())


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------


def _handle_event(payload: dict[str, Any]) -> bool:
    """Route a Monday event to the appropriate handler. Returns True if handled."""
    event = payload.get("event") or payload
    event_type = event.get("type") or payload.get("type")

    if event_type == "change_column_value":
        return _handle_column_change(event.get("columnValue") or event, event.get("pulseId") or event.get("itemId"))
    if event_type == "create_pulse":
        return _handle_create_pulse(event.get("pulseId") or event.get("itemId"))
    if event_type == "update_status":
        return _handle_status_update(
            event.get("pulseId") or event.get("itemId"),
            event.get("value"),
        )
    logger.info("Ignoring unsupported Monday event type: %s", event_type)
    return False


def _handle_column_change(column_value: dict[str, Any], item_id: Any) -> bool:
    if item_id is None:
        return False
    return _sync_item_status(str(item_id), str(column_value.get("value", {}).get("label", "")))


def _handle_create_pulse(item_id: Any) -> bool:
    if item_id is None:
        return False
    return _sync_item_status(str(item_id), "created")


def _handle_status_update(item_id: Any, new_status: Any) -> bool:
    if item_id is None:
        return False
    return _sync_item_status(str(item_id), str(new_status or ""))


def _sync_item_status(monday_item_id: str, new_status: str) -> bool:
    """Find the matching DynamoDB item and update its status."""
    from app.config import get_settings  # noqa: PLC0415
    from app.db import DataAccess, get_table, _to_dynamodb_native  # noqa: PLC0415

    if not new_status:
        return False

    settings = get_settings()
    table = get_table(settings)
    # The ETL writes ``GSI2SK=<item_id>``. We don't have a direct Monday→Item
    # mapping without an extra index, so we scan GSI2 with a filter as a
    # fallback. In production, add a dedicated GSI: ``GSI4PK=MONDAY#<id>``.
    response = table.scan(
        FilterExpression="contains(Body, :needle)",
        ExpressionAttributeValues={":needle": monday_item_id},
        Limit=50,
    )
    items = response.get("Items", [])
    if not items:
        logger.info("No DynamoDB item maps to Monday item %s", monday_item_id)
        return False

    dao = DataAccess(table=table)
    target_status = _normalise_status(new_status)
    for item in items:
        owner_sub = item.get("OwnerSub") or item.get("PK", "").removeprefix("USER#")
        item_id = item.get("ItemId")
        if not (owner_sub and item_id):
            continue
        dao.update_item_status(owner_sub=owner_sub, item_id=item_id, new_status=target_status)
        dao.append_audit(
            owner_sub=owner_sub,
            action="monday.sync",
            actor_sub="monday-webhook",
            metadata={"monday_item_id": monday_item_id, "new_status": target_status},
        )

    # Optional: comment back on Monday with the sync timestamp.
    try:
        client = MondayClient()
        client.add_update(
            item_id=int(monday_item_id),
            body=f"Synced to data platform at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        )
    except Exception as exc:  # pragma: no cover - best-effort comment
        logger.warning("Failed to post Monday confirmation: %s", exc)

    return True


def _normalise_status(value: str) -> str:
    v = value.strip().lower()
    if v in {"done", "complete", "completed"}:
        return "archived"
    if v in {"working on it", "in progress", "started"}:
        return "active"
    return "pending"


def _response(status_code: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }