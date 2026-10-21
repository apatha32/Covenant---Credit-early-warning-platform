"""Filings -> staging.filings rows and curated.filing_event rows (event codes from the pack)."""

from __future__ import annotations

from typing import Any

import polars as pl

from tripwire_common.config import EventCodes

CURRENT_REPORT_FORMS = ("8-K", "8-K/A")


def typed_filings(raw: pl.DataFrame) -> pl.DataFrame:
    """raw: one company's filings.parquet -> columns of staging.filings."""
    return (
        raw.select(
            pl.col("cik"),
            pl.col("accessionNumber").alias("accession"),
            pl.col("form").alias("form_type"),
            pl.col("filingDate").str.to_date("%Y-%m-%d").alias("filed_date"),
            pl.col("acceptanceDateTime")
            .str.to_datetime("%Y-%m-%dT%H:%M:%S%.fZ", time_zone="UTC", strict=False)
            .alias("accepted_at"),
            pl.col("reportDate").str.to_date("%Y-%m-%d", strict=False).alias("report_date"),
            pl.col("primaryDocument").replace("", None).alias("primary_document"),
            pl.col("items")
            .fill_null("")
            .str.split(",")
            .list.eval(pl.element().str.strip_chars())
            .list.eval(pl.element().filter(pl.element() != ""))
            .alias("items"),
        )
        .unique(subset=["cik", "accession"], keep="first", maintain_order=True)
        .sort("filed_date", "accession")
    )


def extract_events(filings: pl.DataFrame, codes: EventCodes) -> list[dict[str, Any]]:
    """One event per (filing, event type): 8-K items and whole-form events like NT 10-K."""
    events: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in filings.iter_rows(named=True):
        types: list[str] = []
        if row["form_type"] in codes.form_types:
            types.append(codes.form_types[row["form_type"]].event_type)
        if row["form_type"] in CURRENT_REPORT_FORMS:
            types += [codes.eight_k_items[i].event_type for i in row["items"]
                      if i in codes.eight_k_items]  # fmt: skip
        for event_type in types:
            events.setdefault((row["cik"], row["accession"], event_type), {
                "cik": row["cik"], "accession": row["accession"], "event_type": event_type,
                "filed_date": row["filed_date"], "raw_items": ",".join(row["items"]) or None,
                "form_type": row["form_type"],
            })  # fmt: skip
    return list(events.values())
