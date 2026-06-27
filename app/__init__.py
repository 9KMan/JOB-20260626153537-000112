"""Top-level application package.

This package contains the FastAPI application that powers the platform's public
HTTP API. The application is intended to be run either:

  * locally via ``uvicorn app.main:app`` for development, or
  * on AWS Lambda behind API Gateway via the ``handler`` Mangum adapter
    defined in :mod:`app.main`.

The package layout is intentionally flat: every importable symbol that a Lambda
runtime might need at cold-start is reachable without descending into ``api/``
or ``models/`` subpackages, which keeps the cold-start path short.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]