"""Excel parser using openpyxl.

We use ``pandas`` + ``openpyxl`` together because pandas handles the row→dict
mapping cleanly while openpyxl is the underlying engine. Each sheet produces a
batch of records; the first sheet's records are returned as the primary batch.

If the workbook contains multiple sheets, only the first non-empty sheet is
processed. The Lambda handler is responsible for emitting a structured log line
if multi-sheet workbooks are detected (future enhancement: write per-sheet
REPORT records).
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd


def _normalize_column(name: str) -> str:
    return (
        str(name).strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
    )


def parse(content: bytes, *, filename: str = "", **kwargs: Any) -> list[dict[str, Any]]:
    """Parse an XLSX byte stream into a list of normalized records."""
    try:
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, engine="openpyxl")
    except Exception as exc:  # openpyxl raises a wide variety of exceptions
        raise ValueError(f"Failed to parse XLSX: {exc}") from exc

    if not sheets:
        return []

    # Process the first non-empty sheet.
    for _sheet_name, df in sheets.items():
        if df is None or df.empty:
            continue
        df = df.rename(columns=_normalize_column)
        df = df.where(pd.notnull(df), None)
        return df.to_dict(orient="records")

    return []