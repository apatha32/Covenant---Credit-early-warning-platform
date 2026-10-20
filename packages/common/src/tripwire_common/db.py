"""Postgres connection and migration helpers."""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

from tripwire_common.settings import REPO_ROOT, get_settings

MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
LOCAL_SHIM = REPO_ROOT / "supabase" / "local" / "supabase_shim.sql"

log = logging.getLogger(__name__)


def connect(url: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    """Open a connection to DATABASE_URL (or `url`)."""
    return psycopg.connect(url or get_settings().require("database_url"), autocommit=autocommit)


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Migration files in apply order (Supabase CLI convention: timestamp-prefixed names)."""
    return sorted(directory.glob("*.sql"))


def apply_sql_files(conn: psycopg.Connection, files: list[Path]) -> None:
    """Run each file in its own transaction; stops at the first failure."""
    for path in files:
        log.info("applying sql file", extra={"file": path.name})
        with conn.transaction():
            conn.execute(path.read_text())  # trusted repo SQL
