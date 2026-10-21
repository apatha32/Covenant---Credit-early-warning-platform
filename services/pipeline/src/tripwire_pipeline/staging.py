"""Raw companyfacts Parquet -> typed staging facts, with quarantine rules.

Only facts whose tag feeds a canonical metric are staged (staging.facts is a Postgres table; the
full raw set stays in Parquet). Every rule violation becomes a quarantine record; nothing is
dropped silently. The output must then satisfy the STAGED_FACTS Pandera contract, and a
violation there is a bug in this module, so it raises instead of quarantining.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandera.polars as pa
import polars as pl

from tripwire_common.config import MetricMapping

SOURCE = "pipeline_staging"

STAGED_FACTS = pa.DataFrameSchema(
    {
        "cik": pa.Column(pl.String, pa.Check.str_matches(r"^\d{10}$")),
        "taxonomy": pa.Column(pl.String),
        "tag": pa.Column(pl.String),
        "unit": pa.Column(pl.String),
        "value": pa.Column(pl.Float64),
        "value_text": pa.Column(pl.String),
        "period_start": pa.Column(pl.Date, nullable=True),
        "period_end": pa.Column(pl.Date),
        "fy": pa.Column(pl.Int64, nullable=True),
        "fp": pa.Column(pl.String, nullable=True),
        "form": pa.Column(pl.String),
        "accession": pa.Column(pl.String, pa.Check.str_matches(r"^\d{10}-\d{2}-\d{6}$")),
        "filed_date": pa.Column(pl.Date),
        "frame": pa.Column(pl.String, nullable=True),
    },
    checks=[
        pa.Check(
            lambda d: d.lazyframe.select(
                pl.col("period_start").is_null() | (pl.col("period_start") <= pl.col("period_end"))
            ),
            name="period_start_not_after_end",
        ),
        pa.Check(
            lambda d: d.lazyframe.select(pl.col("filed_date") >= pl.col("period_end")),
            name="filed_not_before_period_end",
        ),
    ],
    unique=["cik", "taxonomy", "tag", "unit", "period_start", "period_end", "accession"],
    strict=True,
)

KEY = ["cik", "taxonomy", "tag", "unit", "period_start", "period_end", "accession"]


@dataclass
class Staged:
    facts: pl.DataFrame
    quarantine: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (rule, record)
    counts: dict[str, int] = field(default_factory=dict)


def _date(col: str) -> pl.Expr:
    return pl.col(col).str.to_date("%Y-%m-%d", strict=False)


def stage_facts(raw: pl.DataFrame, mapping: MetricMapping) -> Staged:
    """raw: one company's facts.parquet (all values as SEC text)."""
    units = mapping.staged_tags()
    wanted = pl.DataFrame(
        {"taxonomy": [k[0] for k in units], "tag": [k[1] for k in units],
         "expected_unit": list(units.values())},
        schema={"taxonomy": pl.String, "tag": pl.String, "expected_unit": pl.String},
    )  # fmt: skip
    mapped = raw.join(wanted, on=["taxonomy", "tag"], how="inner")
    typed = mapped.with_columns(
        value=pl.col("val").cast(pl.Float64, strict=False),
        value_text=pl.col("val"),
        period_start=_date("start"),
        period_end=_date("end"),
        filed_date=_date("filed"),
        accession=pl.col("accn"),
    )

    staged = Staged(facts=typed, counts={"raw_rows": raw.height, "mapped_rows": mapped.height})
    rules: list[tuple[str, pl.Expr]] = [
        ("unparseable_value", pl.col("value").is_null() | ~pl.col("value").is_finite()),
        ("unparseable_date", pl.col("period_end").is_null() | pl.col("filed_date").is_null()
         | (pl.col("start").is_not_null() & pl.col("period_start").is_null())),
        ("unexpected_unit", pl.col("unit") != pl.col("expected_unit")),
        ("period_start_after_end", pl.col("period_start") > pl.col("period_end")),
        ("filed_before_period_end", pl.col("filed_date") < pl.col("period_end")),
    ]  # fmt: skip
    remaining = typed
    for rule, bad in rules:
        hits = remaining.filter(bad.fill_null(False))
        _quarantine(staged, rule, hits)
        remaining = remaining.filter(~bad.fill_null(False))

    # Same fact key reported twice in one filing: identical values are harmless duplicates
    # (SEC repeats facts under different `frame`s); different values cannot be resolved.
    conflicts = remaining.group_by(KEY).agg(pl.col("value").n_unique().alias("n_values"))
    conflicts = conflicts.filter(pl.col("n_values") > 1).select(KEY)
    bad_rows = remaining.join(conflicts, on=KEY, how="semi", nulls_equal=True)
    _quarantine(staged, "conflicting_duplicate_fact", bad_rows)
    remaining = remaining.join(conflicts, on=KEY, how="anti", nulls_equal=True)
    deduped = remaining.sort("frame", nulls_last=True).unique(subset=KEY, keep="first",
                                                              maintain_order=True)  # fmt: skip
    staged.counts["duplicate_rows_collapsed"] = remaining.height - deduped.height

    staged.facts = STAGED_FACTS.validate(
        deduped.select(list(STAGED_FACTS.columns)).sort(KEY, nulls_last=True)
    )
    staged.counts["staged_rows"] = staged.facts.height
    return staged


def _quarantine(staged: Staged, rule: str, rows: pl.DataFrame) -> None:
    if rows.height == 0:
        return
    original = ["cik", "taxonomy", "tag", "unit", "start", "end", "val", "accn", "fy", "fp",
                "form", "filed", "frame"]  # fmt: skip
    for record in rows.select(original).to_dicts():
        staged.quarantine.append((rule, record))
    staged.counts[f"quarantined_{rule}"] = staged.counts.get(f"quarantined_{rule}", 0) + rows.height
