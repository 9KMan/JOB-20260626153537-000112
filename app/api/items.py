"""CRUD endpoints for the ``Item`` entity.

Routes:

* ``POST   /items``          — create an item (Cognito JWT)
* ``GET    /items``          — list the caller's items (Cognito JWT)
* ``GET    /items/{id}``     — fetch one item (Cognito JWT)
* ``PUT    /items/{id}``     — update one item (Cognito JWT)
* ``DELETE /items/{id}``     — soft-delete an item (Cognito JWT)

All routes go through the :func:`current_user` dependency so a request without
a valid bearer token never reaches the handler. The DataAccess layer translates
Pydantic models into DynamoDB items using the keys defined in
``docs/data-model.md``.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.auth import CognitoUser, current_user
from app.db import ConditionalCheckFailed, DataAccess
from app.models.entities import Item, ItemCreate, ItemUpdate, ItemStatus

logger = logging.getLogger(__name__)
router = APIRouter()


def _dao() -> DataAccess:
    # Helper so tests can override via FastAPI dependency injection.
    return DataAccess()


@router.post("", status_code=status.HTTP_201_CREATED, response_model=Item, response_model_by_alias=False)
async def create_item(
    payload: ItemCreate,
    user: CognitoUser = Depends(current_user),
) -> Item:
    """Create an item owned by the authenticated caller."""
    dao = _dao()
    try:
        record = dao.create_item(
            owner_sub=user.sub,
            item_id=None,
            title=payload.title,
            body=payload.body,
            status=payload.status.value,
            source_upload_id=payload.source_upload_id,
            report_id=payload.report_id,
        )
    except ConditionalCheckFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    dao.append_audit(
        owner_sub=user.sub,
        action="item.created",
        actor_sub=user.sub,
        metadata={"item_id": record.get("ItemId")},
    )
    return Item.model_validate(_to_api_shape(record))


@router.get("", response_model=list[Item], response_model_by_alias=False)
async def list_items(
    user: CognitoUser = Depends(current_user),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
) -> list[Item]:
    """List the caller's items, newest first."""
    dao = _dao()
    exclusive_start_key = _decode_cursor(cursor) if cursor else None
    page = dao.list_items(
        owner_sub=user.sub, limit=limit, exclusive_start_key=exclusive_start_key
    )
    items = [Item.model_validate(_to_api_shape(i)) for i in page["items"]]
    return items


@router.get("/{item_id}", response_model=Item, response_model_by_alias=False)
async def get_item(
    item_id: str,
    user: CognitoUser = Depends(current_user),
) -> Item:
    """Fetch a single item by id. 404 if not found or not owned by caller."""
    dao = _dao()
    record = dao.get_item(owner_sub=user.sub, item_id=item_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
        )
    return Item.model_validate(_to_api_shape(record))


@router.put("/{item_id}", response_model=Item, response_model_by_alias=False)
async def update_item(
    item_id: str,
    payload: ItemUpdate,
    user: CognitoUser = Depends(current_user),
) -> Item:
    """Update an item. Only fields present in the payload are changed."""
    dao = _dao()
    existing = dao.get_item(owner_sub=user.sub, item_id=item_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
        )

    new_status = (payload.status or ItemStatus(existing["Status"])).value
    new_title = payload.title if payload.title is not None else existing.get("Title", "")
    new_body = payload.body if payload.body is not None else existing.get("Body", {})

    # Rebuild the record with mutated fields; this re-writes the same SK so we
    # update in place rather than append a new version.
    mutated = dict(existing)
    mutated["Title"] = new_title
    mutated["Body"] = new_body
    mutated["Status"] = new_status
    mutated["GSI1PK"] = f"ITEM#{new_status}"
    mutated["UpdatedAt"] = _now_iso()

    from app.config import get_settings
    from app.db import get_table, _to_dynamodb_native  # noqa: PLC0415

    table = get_table(get_settings())
    table.put_item(
        Item=_to_dynamodb_native(mutated),
        ConditionExpression="attribute_exists(SK)",
    )

    dao.append_audit(
        owner_sub=user.sub,
        action="item.updated",
        actor_sub=user.sub,
        metadata={"item_id": item_id, "new_status": new_status},
    )
    return Item.model_validate(_to_api_shape(mutated))


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: str,
    user: CognitoUser = Depends(current_user),
) -> JSONResponse:
    """Soft-delete an item by setting its status to ``archived``."""
    dao = _dao()
    updated = dao.update_item_status(user.sub, item_id, ItemStatus.ARCHIVED.value)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
        )
    dao.append_audit(
        owner_sub=user.sub,
        action="item.deleted",
        actor_sub=user.sub,
        metadata={"item_id": item_id},
    )
    return JSONResponse(status_code=204, content=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _to_api_shape(record: dict) -> dict:
    """Translate a DynamoDB-shaped dict into the API-facing shape.

    The DB stores PascalCase keys (``Title``, ``ItemId``, ...) and the
    response_model_by_alias=False setting on the routes causes FastAPI to
    re-serialize using camelCase field names, so this helper just strips
    DynamoDB-only fields and lets Pydantic do the rest.
    """
    # Drop DynamoDB-only fields if present.
    for key in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK"):
        record.pop(key, None)
    return record


def _decode_cursor(cursor: str) -> dict:
    """Decode a pagination cursor. Errors yield 400."""
    import base64
    import json

    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor"
        ) from exc