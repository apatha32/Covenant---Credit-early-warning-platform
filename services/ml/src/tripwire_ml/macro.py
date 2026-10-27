"""FRED macro series, point in time.

release_date is when a value became public: the ALFRED vintage date for revised series, or
observation date + release_lag_days for unrevised daily market series (see macro_series.yaml).
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import psycopg

from tripwire_common.config import MacroSeries
from tripwire_common.raw import read_parquet
from tripwire_common.storage import Storage

SCHEMA = {"series_id": pl.String, "observation_date": pl.Date, "release_date": pl.Date,
          "value": pl.Float64}  # fmt: skip


def load_macro(conn: psycopg.Connection, storage: Storage, macro: MacroSeries) -> pl.DataFrame:
    """Latest landed raw file per configured series -> (series_id, observation_date,
    release_date, value). FRED's '.' (missing) rows are dropped."""
    frames = []
    for spec in macro.series:
        row = conn.execute(
            """select storage_path from raw.source_files where source = 'fred' and source_key = %s
               order by fetched_at desc, id desc limit 1""",
            [spec.id],
        ).fetchone()
        if row is None:
            raise RuntimeError(f"FRED series {spec.id} not ingested; run `tripwire_ingest fred`")
        raw = read_parquet(storage, str(row[0]))
        frame = raw.with_columns(
            observation_date=pl.col("date").str.to_date(),
            value=pl.col("value").cast(pl.Float64, strict=False),
        ).filter(pl.col("value").is_not_null())
        if spec.vintages:
            frame = frame.with_columns(release_date=pl.col("realtime_start").str.to_date())
        else:
            frame = frame.with_columns(
                release_date=pl.col("observation_date") + dt.timedelta(days=spec.release_lag_days)
            )
        columns = [c for c in SCHEMA if c != "series_id"]
        frames.append(frame.select(pl.lit(spec.id).alias("series_id"), *columns))
    return pl.concat(frames).select(SCHEMA.keys()).cast(SCHEMA)  # type: ignore[arg-type]


def store_macro(conn: psycopg.Connection, macro: pl.DataFrame) -> int:
    """Append to curated.macro_series (idempotent)."""
    with conn.cursor() as cur:
        cur.executemany(
            """insert into curated.macro_series (series_id, observation_date, release_date, value)
               values (%s, %s, %s, %s) on conflict do nothing""",
            macro.select("series_id", "observation_date", "release_date", "value").rows(),
        )
        return max(cur.rowcount, 0)


def known_as_of(macro: pl.DataFrame, as_of: dt.date) -> pl.DataFrame:
    """Each observation's latest value released on or before `as_of`."""
    return (
        macro.filter(pl.col("release_date") <= as_of)
        .sort("release_date")
        .group_by("series_id", "observation_date")
        .agg(pl.col("value").last())
        .sort("series_id", "observation_date")
    )


def _value(known: pl.DataFrame, series: str, on_or_before: dt.date) -> float | None:
    rows = known.filter(
        (pl.col("series_id") == series) & (pl.col("observation_date") <= on_or_before)
    )
    return float(rows["value"][-1]) if rows.height else None


def macro_features(macro: pl.DataFrame, as_of: dt.date) -> dict[str, float | None]:
    """Macro features for one as-of date, from values known on that date only."""
    known = known_as_of(macro, as_of)
    year_ago = as_of - dt.timedelta(days=365)
    quarter_ago = as_of - dt.timedelta(days=91)
    out: dict[str, float | None] = {}

    def level(series: str) -> float | None:
        return _value(known, series, as_of)

    def change(series: str, since: dt.date) -> float | None:
        now, then = level(series), _value(known, series, since)
        return None if now is None or then is None else now - then

    out["macro_baa10y"] = level("BAA10Y")
    out["macro_baa10y_chg_3m"] = change("BAA10Y", quarter_ago)
    out["macro_dgs10"] = level("DGS10")
    out["macro_dgs10_chg_12m"] = change("DGS10", year_ago)
    out["macro_fedfunds"] = level("FEDFUNDS")
    out["macro_fedfunds_chg_12m"] = change("FEDFUNDS", year_ago)
    out["macro_unrate"] = level("UNRATE")
    out["macro_unrate_chg_12m"] = change("UNRATE", year_ago)
    indpro_now, indpro_then = level("INDPRO"), _value(known, "INDPRO", year_ago)
    out["macro_indpro_yoy"] = indpro_now / indpro_then - 1 if indpro_now and indpro_then else None
    return out
