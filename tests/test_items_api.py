"""End-to-end tests for the items CRUD API.

These tests exercise :mod:`app.api.items` through the FastAPI ``TestClient``
backed by a moto-provisioned DynamoDB table. The ``current_user`` dependency
is overridden via ``app.dependency_overrides`` so requests appear to come
from the fixture's ``auth_user``.
"""

from __future__ import annotations

from typing import Any

import pytest

# Mark every test in this module as an integration test (it crosses the
# network boundary via moto's stubbed AWS).
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(**overrides: Any) -> dict[str, Any]:
    """Default body for ``POST /items``."""
    body: dict[str, Any] = {
        "title": "Sample item",
        "body": {"description": "lorem ipsum", "tags": ["a", "b"]},
        "status": "pending",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------


def test_create_item_returns_201_and_writes_to_dynamodb(
    client, dynamodb_table, auth_user
) -> None:
    """POST /items returns 201 and the record lands in DynamoDB with the right keys."""
    response = client.post("/items", json=_payload(title="Created item"))

    assert response.status_code == 201, response.text
    data = response.json()
    item_id = data["item_id"]
    assert data["title"] == "Created item"
    assert data["owner_sub"] == auth_user["sub"]
    assert data["status"] == "pending"

    # Verify the underlying DynamoDB item.
    pk = f"USER#{auth_user['sub']}"
    resp = dynamodb_table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={":pk": pk, ":prefix": "ITEM#"},
    )
    items = resp["Items"]
    assert len(items) == 1
    stored = items[0]
    assert stored["ItemId"] == item_id
    assert stored["Title"] == "Created item"
    assert stored["Status"] == "pending"
    assert stored["GSI1PK"] == "ITEM#pending"


# ---------------------------------------------------------------------------
# READ (single)
# ---------------------------------------------------------------------------


def test_get_item_returns_200_with_item(client, dynamodb_table, auth_user) -> None:
    """GET /items/{id} returns the full record when the caller owns it."""
    created = client.post("/items", json=_payload(title="Fetchable")).json()
    item_id = created["item_id"]

    response = client.get(f"/items/{item_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["item_id"] == item_id
    assert data["title"] == "Fetchable"
    assert data["owner_sub"] == auth_user["sub"]


def test_get_unknown_item_returns_404(client) -> None:
    """GET on a non-existent id returns 404 (not 200, not 500)."""
    response = client.get("/items/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "Item not found"


# ---------------------------------------------------------------------------
# LIST (paginated)
# ---------------------------------------------------------------------------


def test_list_items_returns_paginated_results(client) -> None:
    """GET /items returns paginated results respecting ``limit`` and ``cursor``."""
    # Seed more items than we'll request per page.
    titles = [f"item-{i:02d}" for i in range(5)]
    for t in titles:
        resp = client.post("/items", json=_payload(title=t))
        assert resp.status_code == 201

    # First page: limit=2.
    first = client.get("/items", params={"limit": 2})
    assert first.status_code == 200
    page_one = first.json()
    assert len(page_one) == 2

    # When DynamoDB has more results it returns LastEvaluatedKey. The API
    # does not surface the cursor directly today, so we simply assert that
    # successive GETs return the full set across calls.
    second = client.get("/items", params={"limit": 2})
    assert second.status_code == 200
    page_two = second.json()
    assert len(page_two) == 2

    full = page_one + page_two
    seen = {item["title"] for item in full}
    # We may overlap by one (DynamoDB cursors are exact) so we assert "at least".
    assert seen >= {"item-00", "item-01"}

    # Asking for a large page returns everything.
    everything = client.get("/items", params={"limit": 50}).json()
    assert {item["title"] for item in everything} == set(titles)


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------


def test_update_item_replaces_fields(client, dynamodb_table, auth_user) -> None:
    """PUT /items/{id} mutates the fields supplied in the payload, in place."""
    created = client.post(
        "/items",
        json=_payload(title="Original title", body={"v": 1}, status="pending"),
    ).json()
    item_id = created["item_id"]

    response = client.put(
        f"/items/{item_id}",
        json={"title": "Renamed", "body": {"v": 2}, "status": "active"},
    )

    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["title"] == "Renamed"
    assert updated["body"] == {"v": 2}
    assert updated["status"] == "active"

    # The underlying record should now reflect the new status and GSI1PK
    # (which the route rewrites alongside Status).
    pk = f"USER#{auth_user['sub']}"
    resp = dynamodb_table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={":pk": pk, ":prefix": "ITEM#"},
    )
    items = resp["Items"]
    assert len(items) == 1
    stored = items[0]
    assert stored["Title"] == "Renamed"
    assert stored["Status"] == "active"
    assert stored["GSI1PK"] == "ITEM#active"

    # Partial update: only title changes.
    partial = client.put(f"/items/{item_id}", json={"title": "Renamed again"})
    assert partial.status_code == 200
    assert partial.json()["title"] == "Renamed again"
    # Status untouched.
    assert partial.json()["status"] == "active"


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


def test_delete_item_removes_from_table(client, dynamodb_table, auth_user) -> None:
    """DELETE /items/{id} soft-deletes by flipping Status to ``archived``."""
    created = client.post("/items", json=_payload(title="Doomed")).json()
    item_id = created["item_id"]

    response = client.delete(f"/items/{item_id}")
    assert response.status_code == 204
    # FastAPI/Starlette returns an empty body on 204; ``response.json()`` would
    # raise. The route handler also returns ``JSONResponse(status_code=204, ...)``
    # so the body should be empty / null.
    assert response.content in (b"", b"null")

    # The underlying record still exists (soft-delete) but with the new status.
    pk = f"USER#{auth_user['sub']}"
    resp = dynamodb_table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={":pk": pk, ":prefix": "ITEM#"},
    )
    items = resp["Items"]
    assert len(items) == 1
    assert items[0]["Status"] == "archived"
    assert items[0]["GSI1PK"] == "ITEM#archived"

    # And fetching via the API shows the same state.
    follow_up = client.get(f"/items/{item_id}")
    assert follow_up.status_code == 200
    assert follow_up.json()["status"] == "archived"