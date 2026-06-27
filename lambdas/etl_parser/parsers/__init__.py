"""Parser implementations for the ETL pipeline.

Each function takes the file bytes and returns a list of normalized
``dict`` records. The shape of each record matches the DynamoDB Item entity
(see ``docs/data-model.md``).

Pure functions only — no AWS calls, no module-level state, no global
config. The Lambda handler is responsible for resource construction.
"""

from lambdas.etl_parser.parsers.csv_parser import parse as parse_csv
from lambdas.etl_parser.parsers.pdf_parser import parse as parse_pdf
from lambdas.etl_parser.parsers.xlsx_parser import parse as parse_xlsx

__all__ = ["parse_csv", "parse_pdf", "parse_xlsx"]