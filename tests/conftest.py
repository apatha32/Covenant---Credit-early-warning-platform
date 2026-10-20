"""Shared fixtures.

DB tests create a throwaway database on TEST_DATABASE_URL's server, apply the Supabase shim
plus every migration, and drop it afterwards. Each test runs inside a transaction that is
always rolled back, so tests never see each other's rows.

Without a reachable server DB tests are skipped, unless REQUIRE_DB=1 (as in CI).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tripwire_common.db import LOCAL_SHIM, apply_sql_files, migration_files

DEFAULT_TEST_DB = "postgresql://postgres:postgres@localhost:54329/postgres"


@pytest.fixture(scope="session")
def migrated_db_url() -> Iterator[str]:
    admin_url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB)
    try:
        admin = psycopg.connect(admin_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        message = f"no Postgres at TEST_DATABASE_URL ({exc}); run `make db-up`"
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail(message)
        pytest.skip(message)

    name = f"tripwire_test_{uuid.uuid4().hex[:10]}"
    admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    url = make_conninfo(admin_url, dbname=name)
    try:
        with psycopg.connect(url) as conn:
            apply_sql_files(conn, [LOCAL_SHIM, *migration_files()])
        yield url
    finally:
        admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def db(migrated_db_url: str) -> Iterator[psycopg.Connection]:
    """Superuser connection inside a transaction that is rolled back after the test."""
    with (
        psycopg.connect(migrated_db_url, autocommit=True) as conn,
        conn.transaction(force_rollback=True),
    ):
        yield conn
