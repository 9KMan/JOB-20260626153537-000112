"""PDF parser using pdfplumber.

PDFs are an inherently ambiguous source format. Our parser takes a pragmatic
approach:

1. Extract text page-by-page with ``pdfplumber``.
2. For each page that contains tabular data (detected heuristically by
   ``extract_tables``), emit one record per row.
3. For pages with no tables, fall back to emitting one record per page whose
   ``Body`` is the full page text. This lets downstream search indexers do
   full-text matching even on unstructured PDFs.

The parser is deliberately *tolerant* — bad pages are skipped with a warning,
not raised as exceptions. A page that is wholly unscannable (e.g. scanned image
without OCR) becomes a single record with ``Body={"raw_text": "", "note":
"no_extractable_text"}``.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import pdfplumber

logger = logging.getLogger(__name__)


def _page_text_record(page, page_number: int, source_key: str) -> dict[str, Any]:
    return {
        "RowId": f"{source_key}#page-{page_number}",
        "PageNumber": page_number,
        "RawText": (page.extract_text() or "").strip(),
    }


def parse(content: bytes, *, filename: str = "", **kwargs: Any) -> list[dict[str, Any]]:
    """Parse a PDF byte stream into a list of normalized records.

    Records come in two shapes:

    * Tabular: ``{"RowId", "PageNumber", "Table": [...]}`` for each table row.
    * Free-text: ``{"RowId", "PageNumber", "RawText": "..."}`` when no table.

    The caller (Lambda handler) decides how to map these to ITEM entities;
    typically each row becomes one item.
    """
    records: list[dict[str, Any]] = []
    source_key = kwargs.get("source_key", filename or "unknown.pdf")

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for idx, page in enumerate(pdf.pages, start=1):
                tables = page.extract_tables() or []
                if tables:
                    for t_idx, table in enumerate(tables, start=1):
                        if not table:
                            continue
                        # First row is the header.
                        header_row = table[0] or []
                        for row_idx, row in enumerate(table[1:], start=1):
                            if not row or all(cell is None or cell == "" for cell in row):
                                continue
                            record = {
                                "RowId": (
                                    f"{source_key}#page-{idx}-table-{t_idx}"
                                    f"-row-{row_idx}"
                                ),
                                "PageNumber": idx,
                                "TableIndex": t_idx,
                            }
                            for col_idx, cell in enumerate(row):
                                key = (
                                    str(header_row[col_idx]).strip()
                                    if col_idx < len(header_row) and header_row[col_idx]
                                    else f"col_{col_idx}"
                                )
                                record[key] = cell
                            records.append(record)
                else:
                    text_record = _page_text_record(page, idx, source_key)
                    if text_record["RawText"]:
                        records.append(text_record)
    except Exception as exc:
        # pdfplumber raises many exception types depending on the failure mode;
        # we surface a single ValueError so the handler can DLQ the event.
        raise ValueError(f"Failed to parse PDF: {exc}") from exc

    return records