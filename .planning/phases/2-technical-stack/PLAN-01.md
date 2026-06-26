# Phase 2: Technical Stack

## Phase Goal
Select and justify the technology stack, frameworks, and tools.

## Tech Stack
Python, PostgreSQL

## Files to Create

```file:requirements.txt
fastapi>=0.111.0
uvicorn[standard]>=0.30.0
sqlalchemy[asyncio]>=2.0.30
alembic>=1.13.1
pydantic>=2.7.4
pydantic-settings>=2.3.0
python-jose[cryptography]>=3.3.0
passlib[bcrypt]>=1.7.4
python-multipart>=0.0.9
httpx>=0.27.0
python-dotenv>=1.0.1
pytest>=8.2.2
pytest-asyncio>=0.23.7

```

```file:.env.example
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/dbname
SECRET_KEY=***replace-with-random-secret***
```

```file:Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || true
COPY . .
EXPOSE 8000
CMD uvicorn app.main:app --reload
```

```file:docker-compose.yml
version: '3.9'
services:
  api:
    build: .
    ports: ['8000:8000']
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: appdb
      POSTGRES_USER: appuser
      POSTGRES_PASSWORD: apppassword
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ['CMD-SHELL', 'pg_isready -U appuser']
      interval: 5s
      timeout: 5s
      retries: 5
volumes:
  pgdata:
```

## Done When
- requirements.txt lists all dependencies
- .env.example documents all environment variables
- Dockerfile builds: docker build .
- docker-compose.yml starts all services: docker compose up
