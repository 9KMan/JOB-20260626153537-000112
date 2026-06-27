"""FastAPI application entry point.

This module defines the public HTTP surface of the platform. It is consumed in
two ways:

1. **Local development** — ``uvicorn app.main:app --reload``.
2. **AWS Lambda** — via the :data:`handler` Mangum adapter that translates
   API Gateway proxy events into ASGI invocations.

The router hierarchy:

    /health         liveness (no auth)
    /items          CRUD on parsed records (Cognito JWT)
    /uploads        presigned upload URL issuance (Cognito JWT)

Real-world endpoints would also include ``/users``, ``/reports``, and so on;
they are omitted here to keep the template focused but follow the same shape.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mangum import Mangum

from app.api import health, items, upload
from app.config import get_settings

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    """Build and return a configured :class:`FastAPI` instance.

    Factoring the constructor out lets tests build a fresh app per fixture and
    lets ``main`` produce a single module-level instance for production.
    """
    settings = get_settings()
    app = FastAPI(
        title="Serverless Data Platform API",
        version="1.0.0",
        description=(
            "Reference API for the AWS serverless data platform. "
            "All routes except /health require a Cognito-issued JWT."
        ),
        # Bug 157 fix: serialize camelCase in API responses even when Pydantic
        # models use PascalCase aliases for DynamoDB. Without this, response_model
        # dumps use aliases (PascalCase) which clients expect to be camelCase.
        response_model_by_alias=False,
    )

    # CORS — restrict in production via configuration. Open by default for local dev.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # tighten in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers.
    app.include_router(health.router, tags=["health"])
    app.include_router(items.router, prefix="/items", tags=["items"])
    app.include_router(upload.router, prefix="/uploads", tags=["uploads"])

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Any, exc: Exception) -> JSONResponse:
        # Log with structured context. Do NOT leak internals to the client.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    logger.info(
        "FastAPI app created (region=%s, table=%s, uploads_bucket=%s)",
        settings.aws_region,
        settings.dynamodb_table_name,
        settings.s3_uploads_bucket,
    )
    return app


# Module-level ASGI application. Uvicorn imports ``app`` directly.
app = create_app()

# Mangum adapter — this is what AWS Lambda invokes.
#
# Mangum translates API Gateway proxy events into ASGI scope/Receive/Send
# triples and back, so the same FastAPI app runs unchanged in Lambda and
# locally. ``lifespan="off"`` keeps Mangum from interfering with FastAPI's
# lifespan handling (which we don't use).
handler = Mangum(app, lifespan="off")


__all__ = ["app", "handler", "create_app"]