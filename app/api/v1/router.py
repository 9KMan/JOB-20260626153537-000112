python
// app/api/v1/router.py
"""API v1 root router."""
from fastapi import APIRouter

from app.api.v1 import health

router = APIRouter()
router.include_router(health.router, tags=['health'])

