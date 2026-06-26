python
// app/config.py
"""Application settings."""
from functools import lru_cache

from pydantic_settings import BaseSettings


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


settings = get_settings()
