"""Run SQL as a Supabase client role (how PostgREST and supabase-js reach the database)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from psycopg import sql


@contextmanager
def acting_as(
    conn: psycopg.Connection,
    user: uuid.UUID | None,
    role: str = "authenticated",
    **claims: Any,
) -> Iterator[None]:
    """Run statements as a Supabase client role, with a JWT for `user` (plus extra claims,
    e.g. is_anonymous=True). Uses a savepoint so an expected error inside the block leaves the
    connection usable."""
    jwt = json.dumps({"sub": str(user), "role": role, **claims}) if user else ""
    with conn.transaction():
        conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
        conn.execute("select set_config('request.jwt.claims', %s, true)", [jwt])
        yield
        conn.execute("reset role")
        conn.execute("select set_config('request.jwt.claims', '', true)")


@contextmanager
def denied(conn: psycopg.Connection) -> Iterator[None]:
    """Expect permission denied / RLS violation (SQLSTATE 42501)."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        yield
