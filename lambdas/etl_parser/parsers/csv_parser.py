"""CSV parser using pandas.

The CSV layout we accept is one record per row, with the first row as a header.
Column names are normalized to lowercase snake_case and stripped of surrounding
whitespace so downstream code can rely on a stable schema.
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd


def _normalize_column(name: str) -> str:
    return (
        name.strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
    )


def parse(content: bytes, *, filename: str = "", **kwargs: Any) -> list[dict[str, Any]]:
    """Parse a CSV byte stream into a list of normalized records.

    Returns an empty list if the file has no data rows. Raises
    :class:`ValueError` if the file is not parseable as CSV.
    """
    try:
        df = pd.read_csv(io.BytesIO(content))
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as exc:
        raise ValueError(f"Failed to parse CSV: {exc}") from exc

    if df.empty:
        return []

    df = df.rename(columns=_normalize_column)
    # Replace NaN with None so DynamoDB stores nulls, not the string "nan".
    df = df.where(pd.notnull(df), None)
    return df.to_dict(orient="records")