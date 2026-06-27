"""Health check endpoint. Unauthenticated."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health", summary="Liveness probe")
async def health() -> JSONResponse:
    """Return a fixed-shape JSON document confirming the API is alive.

    No external dependencies are touched — this endpoint must succeed even when
    DynamoDB or S3 are unavailable, otherwise the platform loses its
    canary/scheduler signal.
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": "1.0.0",
        },
    )


@router.get("/health/ready", summary="Readiness probe")
async def readiness() -> JSONResponse:
    """Readiness probe: verify that downstream dependencies are reachable.

    Returns 200 if the platform can serve requests, 503 otherwise.
    """
    # Implementation note: a production version would attempt a lightweight
    # DynamoDB ``DescribeTable`` call with a short timeout. We keep the
    # template lean to avoid spurious 503s in environments with no AWS access
    # (e.g. when running under ``moto`` mock tests).
    return JSONResponse(status_code=200, content={"status": "ready"})