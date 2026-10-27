.PHONY: setup lint format typecheck test check db-up db-down db-reset \
	demo-bundle cloud-migrate cloud-seed cloud-refresh cloud-size web-dev

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

# --- Hosted demo (Phase 6a). See docs/deploy.md. ---------------------------------------------
# Cloud values come from .env.cloud (git-ignored), never from .env, so local dev stays local.
CLOUD = set -a && . ./.env.cloud && set +a && TRIPWIRE_STORAGE=supabase

# Local training results -> data/demo_bundle/ (model, tier cutoffs, demo predictions, cases).
demo-bundle:
	uv run python -m tripwire_ml demo-export

cloud-migrate:
	$(CLOUD) supabase db push --db-url "$$DATABASE_URL"

cloud-seed:
	$(CLOUD) uv run python -m tripwire_ml demo-seed

cloud-refresh:
	$(CLOUD) uv run python -m tripwire_ml demo-refresh

cloud-size:
	$(CLOUD) psql "$$DATABASE_URL" -Atc "select pg_size_pretty(pg_database_size(current_database()))"

web-dev:
	cd apps/web && npm run dev
