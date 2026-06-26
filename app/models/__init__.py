python
// app/models/__init__.py
"""Database models."""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass

