# Phase 4: Data Model

## Phase Goal
Define the data model, entities, relationships, and storage approach.

## Files to Create

```file:alembic.ini
[alembic]
script_location = alembic
prepend_sys_path = .
version_path_separator = os

[loggers]
keys = root,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_alembic]
level = INFO
handlers =
qualname = alembic
```

```file:alembic/env.py
"""Alembic async migration environment."""
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata

def run_migrations_offline() -> None:
    url = config.get_main_option('sqlalchemy.url')
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations():
    from app.config import get_settings
    settings = get_settings()
    configuration = {'sqlalchemy.url': settings.DATABASE_URL}
    connectable = async_engine_from_config(configuration, prefix='sqlalchemy.', poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()

def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio
    asyncio.run(run_async_migrations())
```

```file:alembic/script.py.mako
"""${message}

Revision ID: ${rev}
Revises: ${down_rev}
Create Date: ${create_date}
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
${imports if imports else ''}

revision: str = ${repr(rev)}
down_revision: Union[str, None] = ${repr(down_rev)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}
```

```file:alembic/versions/.gitkeep
# Migration files go here
```

## Done When
- alembic revision --autogenerate creates initial migration
- alembic upgrade head runs successfully
