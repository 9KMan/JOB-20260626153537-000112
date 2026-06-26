python
// app/api/v1/health.py
"""Health check endpoints."""
from fastapi import APIRouter

router = APIRouter()


@router.get('/health')
async def health():
    return {'status': 'ok'}


@router.get('/ready')
async def ready():
    return {'status': 'ready'}

