"""Read path for the raw layer: find the latest landed file for a company and load it."""

from __future__ import annotations

from io import BytesIO
from typing import Literal

import polars as pl
import psycopg
from psycopg import sql

from tripwire_common.storage import Storage

RawKind = Literal["submissions", "companyfacts"]
_INDEXES: dict[str, str] = {
    "submissions": "submissions_index",
    "companyfacts": "companyfacts_index",
}


def latest_cik_path(conn: psycopg.Connection, kind: RawKind, cik: str) -> str | None:
    """Storage prefix of the most recently landed payload for this company, if any."""
    table = sql.Identifier("raw", _INDEXES[kind])
    query = sql.SQL(
        "select storage_path from {} where cik = %s order by fetched_at desc, id desc limit 1"
    ).format(table)
    row = conn.execute(query, [cik]).fetchone()
    return str(row[0]) if row else None


def read_parquet(storage: Storage, key: str) -> pl.DataFrame:
    return pl.read_parquet(BytesIO(storage.read_bytes(key)))
