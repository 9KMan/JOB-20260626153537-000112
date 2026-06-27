"""Monday.com GraphQL v2 outbound client.

A thin, typed wrapper over the GraphQL endpoint. Used both by the webhook
handler (to update a Monday task with sync status) and by background jobs
(to reconcile state.

The client is deliberately stateless — every method takes its inputs as
arguments and returns plain Python types. This keeps it easy to unit-test
by stubbing ``requests.Session``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class MondayError(Exception):
    """Raised when the Monday API returns a non-zero error payload."""


class MondayClient:
    """Minimal GraphQL v2 client for Monday.com.

    Usage::

        client = MondayClient(token="...")
        boards = client.list_boards()
    """

    DEFAULT_URL = "https://api.monday.com/v2"

    def __init__(
        self,
        token: Optional[str] = None,
        endpoint: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 10.0,
    ) -> None:
        self.token = token or os.environ.get("MONDAY_API_TOKEN", "")
        if not self.token:
            raise ValueError("MondayClient requires a token (or MONDAY_API_TOKEN env var)")
        self.endpoint = endpoint or self.DEFAULT_URL
        self._session = session or requests.Session()
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_boards(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return a list of boards accessible to the API token."""
        data = self._execute(
            query="query ($limit: Int) { boards(limit: $limit) { id name } }",
            variables={"limit": limit},
        )
        return data.get("boards", []) or []

    def get_item(self, item_id: int) -> dict[str, Any]:
        """Fetch a single item by Monday ID."""
        data = self._execute(
            query="query ($id: [ID!]) { items(ids: $id) { id name state } }",
            variables={"id": [str(item_id)]},
        )
        items = data.get("items") or []
        return items[0] if items else {}

    def add_update(self, item_id: int, body: str) -> dict[str, Any]:
        """Post an update (comment) on an item."""
        data = self._execute(
            query=(
                "mutation ($item_id: ID!, $body: String!) "
                "{ create_update(item_id: $item_id, body: $body) { id } }"
            ),
            variables={"item_id": str(item_id), "body": body},
        )
        return data.get("create_update") or {}

    def change_status(self, item_id: int, column_id: str, value: str) -> dict[str, Any]:
        """Update a status column on an item."""
        data = self._execute(
            query=(
                "mutation ($item_id: ID!, $column_id: String!, $value: JSON) "
                "{ change_simple_column_value("
                "item_id: $item_id, column_id: $column_id, value: $value) { id } }"
            ),
            variables={
                "item_id": str(item_id),
                "column_id": column_id,
                "value": value,
            },
        )
        return data.get("change_simple_column_value") or {}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execute(self, *, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self._session.post(
            self.endpoint,
            json={"query": query, "variables": variables},
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
                "API-Version": "2024-01",
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if "errors" in payload and payload["errors"]:
            raise MondayError(f"Monday API error: {payload['errors']}")
        return payload.get("data") or {}


# Touch Any so static checkers don't flag it as unused.
_ = Any