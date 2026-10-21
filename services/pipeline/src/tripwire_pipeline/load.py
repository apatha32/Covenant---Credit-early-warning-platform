"""Database writes for the pipeline. All of them are idempotent: re-running inserts nothing new.

Numeric values are passed as Decimal from their exact text, never as float8: Postgres'
float8 -> numeric cast keeps only 15 significant digits.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Any

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from tripwire_pipeline.metrics import MetricRow


class ConsistencyError(RuntimeError):
    """Recomputed data disagrees with stored rows under the same mapping version."""


def quarantine(
    conn: psycopg.Connection, source: str, records: list[tuple[str, dict[str, Any]]], run_id: int
) -> int:
    """Insert quarantine records, skipping exact duplicates of earlier runs. Returns new rows."""
    if not records:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """insert into staging.quarantine (source, record, rule_violated, run_id)
               values (%s, %s, %s, %s)
               on conflict (source, rule_violated, (md5(record::text))) do nothing""",
            [(source, Jsonb(_jsonable(rec)), rule, run_id) for rule, rec in records],
        )
        return max(cur.rowcount, 0)


def _jsonable(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if isinstance(v, dt.date) else v for k, v in record.items()}


_FACT_COLUMNS = ["cik", "taxonomy", "tag", "unit", "value", "period_start", "period_end", "fy",
                 "fp", "form", "accession", "filed_date", "frame"]  # fmt: skip


def load_staged_facts(conn: psycopg.Connection, facts: pl.DataFrame, run_id: int) -> int:
    """Append staged facts; facts already present (same key) are left untouched."""
    if facts.height == 0:
        return 0
    rows = facts.select(
        "cik", "taxonomy", "tag", "unit", "value_text", "period_start", "period_end", "fy", "fp",
        "form", "accession", "filed_date", "frame",
    ).iter_rows()  # fmt: skip
    cols = ", ".join(_FACT_COLUMNS)
    with conn.transaction():
        # Dropped explicitly: inside an outer transaction ON COMMIT DROP would never fire.
        conn.execute("drop table if exists tmp_facts")
        conn.execute(
            """create temp table tmp_facts (cik text, taxonomy text, tag text, unit text,
               value numeric, period_start date, period_end date, fy integer, fp text, form text,
               accession text, filed_date date, frame text) on commit drop"""
        )
        with conn.cursor() as cur, cur.copy(f"copy tmp_facts ({cols}) from stdin") as copy:
            for row in rows:
                copy.write_row(row)
        inserted = conn.execute(
            f"""insert into staging.facts ({cols}, run_id)
                select {cols}, %s from tmp_facts
                on conflict (cik, taxonomy, tag, unit, period_start, period_end, accession)
                do nothing""",  # noqa: S608  # fixed column list
            [run_id],
        )
        conn.execute("drop table tmp_facts")
        return max(inserted.rowcount, 0)


def read_staged_facts(conn: psycopg.Connection, cik: str) -> pl.DataFrame:
    rows = conn.execute(
        """select cik, taxonomy, tag, unit, value::text, period_start, period_end, accession,
                  filed_date
           from staging.facts where cik = %s""",
        [cik],
    ).fetchall()
    schema = {"cik": pl.String, "taxonomy": pl.String, "tag": pl.String, "unit": pl.String,
              "value": pl.String, "period_start": pl.Date, "period_end": pl.Date,
              "accession": pl.String, "filed_date": pl.Date}  # fmt: skip
    frame = pl.DataFrame(rows, schema=schema, orient="row")
    return frame.with_columns(pl.col("value").cast(pl.Float64))


def load_metric_rows(
    conn: psycopg.Connection, cik: str, rows: list[MetricRow], mapping_version: str
) -> dict[str, int]:
    """Append new metric rows with the next version numbers; verify existing ones unchanged."""
    existing = conn.execute(
        """select canonical_metric, period_type, period_end, accession, value, version
           from curated.financial_metric where cik = %s and mapping_version = %s""",
        [cik, mapping_version],
    ).fetchall()
    stored: dict[tuple[str, str, dt.date, str], Decimal] = {}
    next_version: dict[tuple[str, str, dt.date], int] = defaultdict(lambda: 1)
    for metric, ptype, end, accession, value, version in existing:
        stored[(metric, ptype, end, accession)] = value
        next_version[(metric, ptype, end)] = max(next_version[(metric, ptype, end)], version + 1)

    new: list[dict[str, Any]] = []
    for row in rows:
        value = _exact(row.value)
        if row.key in stored:
            if stored[row.key] != value:
                raise ConsistencyError(
                    f"{cik} {row.key}: stored {stored[row.key]} != recomputed {value}; "
                    "bump metric_mapping.yaml version to publish a changed mapping"
                )
            continue
        series = (row.metric, row.period_type, row.period_end)
        new.append({
            "cik": cik, "canonical_metric": row.metric, "period_type": row.period_type,
            "period_start": row.period_start, "period_end": row.period_end, "unit": row.unit,
            "value": value, "source_tag": row.source_tag, "is_derived": row.is_derived,
            "derivation": Jsonb(row.derivation) if row.derivation else None,
            "filed_date": row.filed_date, "accession": row.accession,
            "version": next_version[series], "mapping_version": mapping_version,
        })  # fmt: skip
        next_version[series] += 1

    if new:
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                """insert into curated.financial_metric
                     (cik, canonical_metric, period_type, period_start, period_end, unit, value,
                      source_tag, is_derived, derivation, filed_date, accession, version,
                      mapping_version)
                   values (%(cik)s, %(canonical_metric)s, %(period_type)s, %(period_start)s,
                           %(period_end)s, %(unit)s, %(value)s, %(source_tag)s, %(is_derived)s,
                           %(derivation)s, %(filed_date)s, %(accession)s, %(version)s,
                           %(mapping_version)s)""",
                new,
            )
    return {"metric_rows_new": len(new), "metric_rows_unchanged": len(rows) - len(new)}


def _exact(value: float) -> Decimal:
    """Shortest exact decimal for a float (e.g. 1.5 -> Decimal('1.5'), 1e11 -> 100000000000)."""
    d = Decimal(repr(value))
    return d.quantize(Decimal(1)) if d == d.to_integral_value() else d.normalize()


def load_filings(conn: psycopg.Connection, filings: pl.DataFrame, run_id: int) -> int:
    if filings.height == 0:
        return 0
    cols = ["cik", "accession", "form_type", "filed_date", "accepted_at", "report_date",
            "primary_document", "items"]  # fmt: skip
    names = ", ".join(cols)
    with conn.transaction():
        # Dropped explicitly: inside an outer transaction ON COMMIT DROP would never fire.
        conn.execute("drop table if exists tmp_filings")
        conn.execute(
            """create temp table tmp_filings (cik text, accession text, form_type text,
               filed_date date, accepted_at timestamptz, report_date date,
               primary_document text, items text[]) on commit drop"""
        )
        with conn.cursor() as cur, cur.copy(f"copy tmp_filings ({names}) from stdin") as copy:
            copy.set_types(["text", "text", "text", "date", "timestamptz", "date", "text",
                            "text[]"])  # fmt: skip
            for row in filings.select(cols).iter_rows():
                copy.write_row(row)
        inserted = conn.execute(
            f"""insert into staging.filings ({names}, run_id)
                select {names}, %s from tmp_filings on conflict (cik, accession) do nothing""",  # noqa: S608
            [run_id],
        )
        conn.execute("drop table tmp_filings")
        return max(inserted.rowcount, 0)


def load_events(conn: psycopg.Connection, events: list[dict[str, Any]]) -> int:
    if not events:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """insert into curated.filing_event
                 (cik, accession, event_type, filed_date, raw_items, form_type)
               values (%(cik)s, %(accession)s, %(event_type)s, %(filed_date)s, %(raw_items)s,
                       %(form_type)s)
               on conflict (cik, accession, event_type) do nothing""",
            events,
        )
        return max(cur.rowcount, 0)
