# Phase 3: Architecture

## Phase Goal
Design the system architecture including API, data flow, and integrations.

## Files to Create

```file:app/__init__.py
"""App package."""
```

```file:app/main.py
"""FastAPI application factory."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import router as v1_router
from app.config import settings

def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version='1.0.0',
        openapi_url='/api/v1/openapi.json',
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )
    app.include_router(v1_router, prefix='/api/v1')
    return app

app = create_app()
```

```file:app/config.py
"""Application settings."""
from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    PROJECT_NAME: str = 'API'
    DATABASE_URL: str = 'postgresql+asyncpg://user:pass@localhost/dbname'
    JWT_SECRET: str
    JWT_ALGORITHM: str = 'HS256'
    JWT_EXPIRE_MINUTES: int = 15

    class Config:
        env_file = '.env'
        case_sensitive = True

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```file:app/database.py
"""Async SQLAlchemy engine and session."""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.config import get_settings

settings = get_settings()
engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db() -> AsyncSession:
    async with async_session_maker() as session:
        yield session
```

```file:app/models/__init__.py
"""Database models."""
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
```

```file:app/schemas/__init__.py
"""Pydantic schemas."""
from pydantic import BaseModel
```

```file:app/api/v1/__init__.py
"""API v1 router."""
from fastapi import APIRouter
router = APIRouter()
```

```file:app/api/v1/router.py
"""API v1 root router."""
from fastapi import APIRouter
from app.api.v1 import health

router = APIRouter()
router.include_router(health.router, tags=['health'])
```

```file:app/api/v1/health.py
"""Health check endpoints."""
from fastapi import APIRouter
router = APIRouter()

@router.get('/health')
async def health():
    return {'status': 'ok'}

@router.get('/ready')
async def ready():
    return {'status': 'ready'}
```

## Done When
- uvicorn app.main:app --reload starts without errors
- GET /api/v1/health returns {'status': 'ok'}
- All files above exist and are non-trivial
