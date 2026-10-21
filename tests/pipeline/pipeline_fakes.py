"""Builders for raw companyfacts frames (all values as SEC text, like the raw Parquet)."""

from __future__ import annotations

from typing import Any

import polars as pl

RAW_COLUMNS = ["cik", "taxonomy", "tag", "unit", "start", "end", "val", "accn", "fy", "fp",
               "form", "filed", "frame"]  # fmt: skip


def fact(
    tag: str,
    val: str | float,
    end: str,
    filed: str,
    accn: str,
    *,
    start: str | None = None,
    unit: str = "USD",
    taxonomy: str = "us-gaap",
    form: str = "10-Q",
    cik: str = "0000000001",
    frame: str | None = None,
) -> dict[str, Any]:
    return {"cik": cik, "taxonomy": taxonomy, "tag": tag, "unit": unit, "start": start,
            "end": end, "val": str(val), "accn": accn, "fy": None, "fp": None, "form": form,
            "filed": filed, "frame": frame}  # fmt: skip


def raw_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    schema = {c: pl.String for c in RAW_COLUMNS} | {"fy": pl.Int64}
    return pl.DataFrame(rows, schema=schema)


# Accession numbers for a fictional company's filings in fiscal 2023 (calendar year).
Q1_10Q = "0000000001-23-000010"  # filed 2023-05-05
Q2_10Q = "0000000001-23-000020"  # filed 2023-08-04
Q3_10Q = "0000000001-23-000030"  # filed 2023-11-03
FY_10K = "0000000001-24-000005"  # filed 2024-02-20
FY_10KA = "0000000001-24-000040"  # filed 2024-06-01 (amendment restating FY revenue)


def fiscal_2023_revenue(tag: str = "Revenues", restate_fy: bool = False) -> list[dict[str, Any]]:
    """Q1 reported; 10-Qs report Q2/Q3 discretely and YTD; the 10-K reports only FY."""
    rows = [
        fact(tag, 100, "2023-03-31", "2023-05-05", Q1_10Q, start="2023-01-01"),
        fact(tag, 110, "2023-06-30", "2023-08-04", Q2_10Q, start="2023-04-01"),
        fact(tag, 210, "2023-06-30", "2023-08-04", Q2_10Q, start="2023-01-01"),
        fact(tag, 120, "2023-09-30", "2023-11-03", Q3_10Q, start="2023-07-01"),
        fact(tag, 330, "2023-09-30", "2023-11-03", Q3_10Q, start="2023-01-01"),
        fact(tag, 460, "2023-12-31", "2024-02-20", FY_10K, start="2023-01-01", form="10-K"),
    ]
    if restate_fy:
        rows.append(fact(tag, 450, "2023-12-31", "2024-06-01", FY_10KA, start="2023-01-01",
                         form="10-K/A"))  # fmt: skip
    return rows
