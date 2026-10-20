.PHONY: setup lint format typecheck test check db-up db-down db-reset

setup:
	uv sync --all-packages

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest

check: lint typecheck test

db-up:
	docker compose up -d --wait db

db-down:
	docker compose down

# Wipe the local database volume and apply shim + all migrations from scratch.
db-reset:
	docker compose down -v
	docker compose up -d --wait db
	uv run python scripts/apply_migrations.py --with-shim
