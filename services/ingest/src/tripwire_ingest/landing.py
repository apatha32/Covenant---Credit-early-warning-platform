"""Land raw payloads as Parquet in Storage and index them in raw.*.

Idempotency: storage keys contain the sha256 of the raw payload, and each index has a unique
(key, checksum). Re-fetching unchanged data writes nothing and counts as 'unchanged'.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl
import psycopg
from psycopg import sql

from tripwire_common.runs import RunLog
from tripwire_common.storage import Storage
from tripwire_ingest.fred import FredPayload, parse_series
from tripwire_ingest.sec import (
    RawPayload,
    SubmissionsPayload,
    frame_to_parquet,
    parse_companyfacts,
    parse_submissions,
    parse_tickers,
    sha256,
)

_CIK_INDEXES = {"submissions": "submissions_index", "companyfacts": "companyfacts_index"}


@dataclass(frozen=True)
class Landed:
    storage_path: str
    is_new: bool


def _land_cik_files(
    conn: psycopg.Connection,
    storage: Storage,
    kind: str,
    cik: str,
    checksum: str,
    files: dict[str, pl.DataFrame],
    run: RunLog,
) -> Landed:
    table = sql.Identifier("raw", _CIK_INDEXES[kind])
    prefix = f"sec/{kind}/cik={cik}/sha256={checksum}"
    seen = conn.execute(
        sql.SQL("select 1 from {} where cik = %s and checksum = %s").format(table), [cik, checksum]
    ).fetchone()
    if seen:
        run.counts["unchanged"] += 1
        return Landed(prefix, is_new=False)
    for name, frame in files.items():
        storage.write_bytes(f"{prefix}/{name}.parquet", frame_to_parquet(frame))
        run.counts[f"{name}_rows"] += frame.height
    conn.execute(
        sql.SQL(
            """insert into {} (cik, storage_path, checksum, run_id) values (%s, %s, %s, %s)
               on conflict (cik, checksum) do nothing"""
        ).format(table),
        [cik, prefix, checksum, run.id],
    )
    run.counts["landed_new"] += 1
    return Landed(prefix, is_new=True)


def land_submissions(
    conn: psycopg.Connection, storage: Storage, payload: SubmissionsPayload, run: RunLog
) -> tuple[Landed, pl.DataFrame, pl.DataFrame]:
    """Parses first (so a schema change fails before anything is written)."""
    company, filings = parse_submissions(payload)
    run.counts["pages_fetched"] += len(payload.pages)
    landed = _land_cik_files(
        conn, storage, "submissions", payload.cik, payload.checksum,
        {"company": company, "filings": filings}, run,
    )  # fmt: skip
    return landed, company, filings


def land_companyfacts(
    conn: psycopg.Connection, storage: Storage, payload: RawPayload, run: RunLog
) -> Landed:
    facts, tags = parse_companyfacts(payload)
    return _land_cik_files(
        conn, storage, "companyfacts", payload.cik, payload.checksum,
        {"facts": facts, "tags": tags}, run,
    )  # fmt: skip


def _land_source_file(
    conn: psycopg.Connection,
    storage: Storage,
    source: str,
    source_key: str,
    checksum: str,
    frame: pl.DataFrame,
    run: RunLog,
) -> Landed:
    path = f"{source}/{source_key}/sha256={checksum}/data.parquet"
    seen = conn.execute(
        "select 1 from raw.source_files where source = %s and source_key = %s and checksum = %s",
        [source, source_key, checksum],
    ).fetchone()
    if seen:
        run.counts["unchanged"] += 1
        return Landed(path, is_new=False)
    storage.write_bytes(path, frame_to_parquet(frame))
    conn.execute(
        """insert into raw.source_files (source, source_key, storage_path, checksum, run_id)
           values (%s, %s, %s, %s, %s) on conflict (source, source_key, checksum) do nothing""",
        [source, source_key, path, checksum, run.id],
    )
    run.counts["landed_new"] += 1
    run.counts["rows"] += frame.height
    return Landed(path, is_new=True)


def land_tickers(
    conn: psycopg.Connection, storage: Storage, raw: bytes, run: RunLog
) -> tuple[Landed, pl.DataFrame]:
    frame = parse_tickers(raw)
    landed = _land_source_file(
        conn, storage, "sec_tickers", "company_tickers", sha256(raw), frame, run
    )
    return landed, frame


def land_fred_series(
    conn: psycopg.Connection, storage: Storage, payload: FredPayload, run: RunLog
) -> Landed:
    frame = parse_series(payload)
    return _land_source_file(conn, storage, "fred", payload.series_id, payload.checksum, frame, run)
