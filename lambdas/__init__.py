"""Parsers for the ETL Lambda.

Each parser converts a raw file (bytes) into a list of normalized records
(``dict``s) suitable for direct ``PutItem`` into DynamoDB. Parsers are pure:
no AWS calls, no I/O, no module-level state. This keeps them easy to unit-test.

Adding a new format:

1. Create a new module under :mod:`lambdas.etl_parser.parsers` that exposes a
   function ``parse(content: bytes, *, filename: str, **kwargs) -> list[dict]``.
2. Register it in :func:`lambdas.etl_parser.handler.parse_object` by extension.
"""

from lambdas.etl_parser.parsers.csv_parser import parse as parse_csv
from lambdas.etl_parser.parsers.pdf_parser import parse as parse_pdf
from lambdas.etl_parser.parsers.xlsx_parser import parse as parse_xlsx

__all__ = ["parse_csv", "parse_pdf", "parse_xlsx"]