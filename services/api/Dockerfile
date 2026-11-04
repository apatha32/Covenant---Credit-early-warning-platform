# Covenant API (FastAPI). Build from the repo root:  docker build -f services/api/Dockerfile .
# Installs only the API package and what it imports (common, pipeline matcher, ingest); the
# repo layout is kept so config/ resolves the same way as in development.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
COPY config config
RUN uv sync --package tripwire-api --no-dev --locked

ENV PATH=/app/.venv/bin:$PATH TRIPWIRE_ENV=prod PYTHONUNBUFFERED=1
RUN useradd --create-home app
USER app
EXPOSE 8000
CMD ["python", "-m", "tripwire_api"]
