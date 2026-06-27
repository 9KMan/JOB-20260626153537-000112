"""API routers package.

Each submodule declares a :class:`fastapi.APIRouter` instance named ``router``
that is included by :mod:`app.main`. Routers contain no business logic — they
delegate to :class:`app.db.DataAccess` and return Pydantic models.
"""

from app.api import health, items, upload

__all__ = ["health", "items", "upload"]