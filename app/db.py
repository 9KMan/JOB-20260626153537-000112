"""DynamoDB client and single-table access helpers.

The platform follows the **single-table** pattern. Every entity type lives in the
same physical table; partition keys carry the entity discriminator. This module
exposes:

* :func:`get_dynamodb_resource` — lazily-built boto3 resource.
* :func:`get_table` — the configured table handle.
* :class:`DataAccess` — high-level helpers that encode the PK/SK pattern.

The helpers in :class:`DataAccess` are intentionally thin wrappers over the
``Table`` resource. They translate API operations (create-item, list-items,
idempotency-check) into the key shapes defined in ``docs/data-model.md``. Keeping
the translation in one place means routes and Lambda handlers do not need to
know the key format — they ask ``DataAccess`` and it constructs the keys.

No calls to AWS happen at import time. All clients are constructed lazily on
first use so that ``import app.db`` is safe in any environment (including
Lambda cold-starts with IAM credentials that take a beat to become available).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from decimal import Decimal
from functools import lru_cache
from typing import Any, Iterator, Optional

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key construction
#
# Centralised so the rest of the codebase never spells out a PK/SK literal.
# ---------------------------------------------------------------------------


def user_pk(sub: str) -> str:
    return f"USER#{sub}"


def item_sk(item_id: str, created_at: int) -> str:
    return f"ITEM#{item_id}#v#{created_at}"


def item_sk_prefix(item_id: str) -> str:
    return f"ITEM#{item_id}#"  # for "find latest version of item X"


def item_sk_owner_prefix() -> str:
    return "ITEM#"  # for "list all items for this owner"


def report_sk(report_id: str) -> str:
    return f"REPORT#{report_id}"


def upload_sk(upload_id: str) -> str:
    return f"UPLOAD#{upload_id}"


def audit_sk(event_id: str, iso_date: Optional[str] = None) -> str:
    if iso_date is None:
        iso_date = time.strftime("%Y-%m-%d", time.gmtime())
    return f"AUDIT#{iso_date}#{event_id}"


def gsi1pk_for_item_status(status: str) -> str:
    return f"ITEM#{status}"


def gsi2pk_for_report(report_id: str) -> str:
    # Items and reports share GSI2 with the same prefix so we can join them.
    return f"ITEM#{report_id}"  # for items
    # NOTE: reports themselves don't need GSI2; they use the base table.


def gsi3pk_for_etag(etag: str) -> str:
    return f"ETAG#{etag}"


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def _client_kwargs(settings: Settings) -> dict[str, Any]:
    """Build kwargs for boto3 client/resource construction."""
    kwargs: dict[str, Any] = {
        "region_name": settings.aws_region,
        "config": BotoConfig(
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=5,
            read_timeout=10,
            user_agent_extra="serverless-data-platform/1.0",
        ),
    }
    if settings.dynamodb_endpoint_url:
        kwargs["endpoint_url"] = settings.dynamodb_endpoint_url
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return kwargs


@lru_cache(maxsize=1)
def get_dynamodb_resource(settings_id: int = 0) -> Any:
    """Return a cached boto3 DynamoDB *resource*.

    The ``settings_id`` is an integer identifier so that ``lru_cache`` can key
    off process-stable state (we keep this signature so that swapping settings
    at runtime is cheap and explicit).
    """
    settings = get_settings()
    return boto3.resource("dynamodb", **_client_kwargs(settings))


def get_table(settings: Optional[Settings] = None):
    """Return a handle to the configured DynamoDB table."""
    if settings is None:
        settings = get_settings()
    return get_dynamodb_resource().Table(settings.dynamodb_table_name)


def get_dynamodb_client():
    """Return a low-level DynamoDB client (used for batch operations)."""
    settings = get_settings()
    return boto3.client("dynamodb", **_client_kwargs(settings))


def reset_client_cache() -> None:
    """Clear the boto3 client cache. Useful for tests that swap environments."""
    get_dynamodb_resource.cache_clear()


# ---------------------------------------------------------------------------
# Decimal <-> native conversions
# ---------------------------------------------------------------------------


def _to_dynamodb_native(value: Any) -> Any:
    """Recursively convert floats to Decimals for DynamoDB compatibility."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamodb_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamodb_native(v) for v in value]
    return value


def _from_dynamodb_native(value: Any) -> Any:
    """Recursively convert Decimals back to int/float for JSON serialization."""
    if isinstance(value, Decimal):
        # If the Decimal has no fractional part and fits in an int, return int.
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: _from_dynamodb_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamodb_native(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# DataAccess — single-table helpers
# ---------------------------------------------------------------------------


class ConditionalCheckFailed(Exception):
    """Raised when a DynamoDB ConditionExpression fails."""


class DataAccess:
    """Encapsulates all single-table operations.

    Instances are cheap to construct; they hold no state beyond a reference to
    the table handle. Use one per request or share a singleton across handlers.
    """

    def __init__(self, table=None):
        self._table = table or get_table()

    # -- Users -------------------------------------------------------------

    def upsert_user(self, sub: str, email: str, display_name: str) -> dict[str, Any]:
        item = {
            "PK": user_pk(sub),
            "SK": "PROFILE",
            "Type": "USER",
            "Email": email,
            "DisplayName": display_name,
            "CreatedAt": _iso_now(),
            "UpdatedAt": _iso_now(),
        }
        self._table.put_item(Item=_to_dynamodb_native(item))
        return _from_dynamodb_native(item)

    def get_user(self, sub: str) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(Key={"PK": user_pk(sub), "SK": "PROFILE"})
        return _from_dynamodb_native(resp.get("Item"))

    # -- Items -------------------------------------------------------------

    def create_item(
        self,
        owner_sub: str,
        item_id: Optional[str] = None,
        title: str = "",
        body: Optional[dict[str, Any]] = None,
        status: str = "pending",
        source_upload_id: Optional[str] = None,
        report_id: Optional[str] = None,
    ) -> dict[str, Any]:
        item_id = item_id or str(uuid.uuid4())
        created_at = int(time.time())
        sk = item_sk(item_id, created_at)
        item = {
            "PK": user_pk(owner_sub),
            "SK": sk,
            "Type": "ITEM",
            "ItemId": item_id,
            "OwnerSub": owner_sub,
            "Title": title,
            "Body": body or {},
            "Status": status,
            "SourceUploadId": source_upload_id,
            "ReportId": report_id,
            "GSI1PK": gsi1pk_for_item_status(status),
            "GSI1SK": str(created_at),
            "GSI2PK": gsi2pk_for_report(report_id) if report_id else None,
            "GSI2SK": item_id if report_id else None,
            "CreatedAt": _iso_now(),
            "UpdatedAt": _iso_now(),
        }
        # Strip sparse GSI fields if absent.
        item = {k: v for k, v in item.items() if v is not None}
        try:
            self._table.put_item(
                Item=_to_dynamodb_native(item),
                ConditionExpression="attribute_not_exists(SK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConditionalCheckFailed(f"item {item_id}@{sk} already exists") from exc
            raise
        return _from_dynamodb_native(item)

    def get_item(self, owner_sub: str, item_id: str) -> Optional[dict[str, Any]]:
        """Return the latest version of an item, or None if not found."""
        resp = self._table.query(
            KeyConditionExpression=Key("PK").eq(user_pk(owner_sub))
            & Key("SK").begins_with(item_sk_prefix(item_id)),
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
        return _from_dynamodb_native(items[0]) if items else None

    def list_items(
        self,
        owner_sub: str,
        limit: int = 50,
        exclusive_start_key: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(user_pk(owner_sub))
            & Key("SK").begins_with(item_sk_owner_prefix()),
            "Limit": limit,
            "ScanIndexForward": False,
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = _to_dynamodb_native(exclusive_start_key)
        resp = self._table.query(**kwargs)
        return {
            "items": [_from_dynamodb_native(i) for i in resp.get("Items", [])],
            "last_evaluated_key": _from_dynamodb_native(resp.get("LastEvaluatedKey")),
        }

    def list_items_by_status(self, status: str, limit: int = 50) -> list[dict[str, Any]]:
        """GSI1 query: list items by status across all owners (admin view)."""
        resp = self._table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(gsi1pk_for_item_status(status)),
            Limit=limit,
            ScanIndexForward=False,
        )
        return [_from_dynamodb_native(i) for i in resp.get("Items", [])]

    def list_items_for_report(self, report_id: str) -> list[dict[str, Any]]:
        """GSI2 query: resolve items derived from a given report."""
        resp = self._table.query(
            IndexName="GSI2",
            KeyConditionExpression=Key("GSI2PK").eq(gsi2pk_for_report(report_id)),
        )
        return [_from_dynamodb_native(i) for i in resp.get("Items", [])]

    def update_item_status(
        self,
        owner_sub: str,
        item_id: str,
        new_status: str,
    ) -> Optional[dict[str, Any]]:
        existing = self.get_item(owner_sub, item_id)
        if existing is None:
            return None
        updated_at = int(time.time())
        # Preserve the original SK so we update the same record; rewrite GSI1PK
        # so status-filter queries see the move.
        new_item = dict(existing)
        new_item["Status"] = new_status
        new_item["GSI1PK"] = gsi1pk_for_item_status(new_status)
        new_item["GSI1SK"] = str(updated_at)
        new_item["UpdatedAt"] = _iso_now()
        try:
            self._table.put_item(
                Item=_to_dynamodb_native(new_item),
                ConditionExpression="attribute_exists(SK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
        return _from_dynamodb_native(new_item)

    # -- Reports -----------------------------------------------------------

    def upsert_report_idempotent(
        self,
        owner_sub: str,
        report_id: str,
        source_bucket: str,
        source_key: str,
        source_etag: str,
        format: str,
        item_count: int,
    ) -> tuple[bool, Optional[dict[str, Any]]]:
        """Create a REPORT if its ETag has not been seen before.

        Returns ``(created, item)``. If ``created`` is False, the existing record
        is returned and the caller should short-circuit (idempotency).
        """
        # Idempotency check via GSI3.
        seen = self._table.query(
            IndexName="GSI3",
            KeyConditionExpression=Key("GSI3PK").eq(gsi3pk_for_etag(source_etag)),
            Limit=1,
        )
        if seen.get("Items"):
            return False, _from_dynamodb_native(seen["Items"][0])

        item = {
            "PK": user_pk(owner_sub),
            "SK": report_sk(report_id),
            "Type": "REPORT",
            "ReportId": report_id,
            "OwnerSub": owner_sub,
            "SourceBucket": source_bucket,
            "SourceKey": source_key,
            "SourceETag": source_etag,
            "Format": format,
            "ItemCount": item_count,
            "Status": "parsed",
            "GSI1PK": f"REPORT#{time.strftime('%Y-%m', time.gmtime())}",
            "GSI1SK": str(int(time.time())),
            "GSI3PK": gsi3pk_for_etag(source_etag),
            "GSI3SK": f"{user_pk(owner_sub)}#{report_sk(report_id)}",
            "CreatedAt": _iso_now(),
            "UpdatedAt": _iso_now(),
        }
        item = {k: v for k, v in item.items() if v is not None}
        try:
            self._table.put_item(
                Item=_to_dynamodb_native(item),
                ConditionExpression="attribute_not_exists(SK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False, None
            raise
        return True, _from_dynamodb_native(item)

    # -- Audit -------------------------------------------------------------

    def append_audit(
        self,
        owner_sub: str,
        action: str,
        actor_sub: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        event_id = str(uuid.uuid4())
        item = {
            "PK": user_pk(owner_sub),
            "SK": audit_sk(event_id),
            "Type": "AUDIT",
            "EventId": event_id,
            "Action": action,
            "ActorSub": actor_sub,
            "Metadata": metadata or {},
            "CreatedAt": _iso_now(),
        }
        self._table.put_item(Item=_to_dynamodb_native(item))
        return _from_dynamodb_native(item)

    # -- Generic helpers ---------------------------------------------------

    def query_by_pk(
        self,
        pk: str,
        sk_prefix: Optional[str] = None,
        sk_between: Optional[tuple[Any, Any]] = None,
        limit: int = 100,
        scan_forward: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """Generic paginated query by PK, optionally filtered by SK.

        This is the workhorse helper for one-off access patterns not yet covered
        by the typed methods above.
        """
        cond = Key("PK").eq(pk)
        if sk_prefix is not None:
            cond = cond & Key("SK").begins_with(sk_prefix)
        if sk_between is not None:
            lo, hi = sk_between
            cond = cond & Key("SK").between(lo, hi)

        start_key: Optional[dict[str, Any]] = None
        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": cond,
                "Limit": limit,
                "ScanIndexForward": scan_forward,
            }
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = _to_dynamodb_native(start_key)
            resp = self._table.query(**kwargs)
            for item in resp.get("Items", []):
                yield _from_dynamodb_native(item)
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                return

    # -- Table admin -------------------------------------------------------

    @staticmethod
    def ensure_table(settings: Optional[Settings] = None) -> None:
        """Create the table + GSIs if absent (used in tests + local dev)."""
        if settings is None:
            settings = get_settings()
        client = get_dynamodb_client()
        try:
            client.describe_table(TableName=settings.dynamodb_table_name)
            return
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise

        # Bare-bones schema for local/CI; production is provisioned via CDK.
        client.create_table(
            TableName=settings.dynamodb_table_name,
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
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        waiter = client.get_waiter("table_exists")
        waiter.wait(TableName=settings.dynamodb_table_name)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Touch the unused import so it isn't flagged by linters we run on the template.
_ = TypeDeserializer
# Touch os so monkey-patchers in tests have a stable reference.
_ = os.environ