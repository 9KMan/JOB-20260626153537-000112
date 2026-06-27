"""ETL parser Lambda package.

Triggered by S3 ``ObjectCreated`` events. Responsibilities:

1. Validate the event source.
2. Download the object.
3. Dispatch to the right format parser based on file extension.
4. Persist parsed records to DynamoDB (idempotent on ETag).
5. On terminal failure, raise so the message lands in the DLQ.
"""

from lambdas.etl_parser.handler import handler as handler_function, parse_object

__all__ = ["handler_function", "parse_object"]