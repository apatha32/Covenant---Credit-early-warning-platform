"""Training dataset: features at quarterly as-of dates, plus labels and the leakage check."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import polars as pl

from tripwire_common.config import Labels
from tripwire_ml.features import FEATURE_COLUMNS, Inputs, features_as_of
from tripwire_ml.labels import Case, label_rows

log = logging.getLogger(__name__)


def build_dataset(
    inputs: Inputs,
    dates: list[dt.date],
    cases: list[Case],
    labels: Labels,
    data_cutoff: dt.date,
) -> pl.DataFrame:
    """Features for every date, labels where the horizon is complete, in-bankruptcy rows dropped.

    `label_complete` is False for dates whose label window extends past the data cutoff; those
    rows can be scored but must not be used for training or evaluation.
    """
    horizon = dt.timedelta(days=round(labels.horizon_months * 365.25 / 12))
    frames = []
    for as_of in dates:
        frame = features_as_of(inputs, as_of)
        frames.append(frame)
        log.info("features built", extra={"as_of": as_of.isoformat(), "rows": frame.height})
    features = pl.concat(frames)
    labelled = label_rows(features.select("cik", "as_of_date"), cases, labels)
    return (
        features.join(labelled, on=["cik", "as_of_date"], how="left")
        .filter(~pl.col("in_bankruptcy"))
        .with_columns(label_complete=pl.col("as_of_date") + horizon <= data_cutoff)
        .drop("in_bankruptcy")
    )


def truncate_inputs(inputs: Inputs, as_of: dt.date) -> Inputs:
    """The same inputs with everything published after `as_of` deleted."""
    return Inputs(
        metrics=inputs.metrics.filter(pl.col("filed_date") <= as_of),
        filings=inputs.filings.filter(pl.col("filed") <= as_of),
        macro=inputs.macro.filter(pl.col("release_date") <= as_of),
        universe=inputs.universe,
    )


@dataclass(frozen=True)
class LeakageResult:
    as_of: dt.date
    rows: int
    mismatched_rows: int
    mismatched_features: list[str]
    max_source_after_as_of: int

    @property
    def passed(self) -> bool:
        return self.mismatched_rows == 0 and self.max_source_after_as_of == 0


def leakage_check(inputs: Inputs, as_of: dt.date) -> LeakageResult:
    """Features computed from all data must equal features computed after deleting every
    later filing, restatement and macro release. Any difference means a feature read the future."""
    full = features_as_of(inputs, as_of)
    truncated = features_as_of(truncate_inputs(inputs, as_of), as_of)
    joined = full.join(truncated, on="cik", how="full", suffix="_t", coalesce=True)
    bad_features = []
    mismatch = pl.lit(False)
    for col in FEATURE_COLUMNS:
        a, b = pl.col(col), pl.col(f"{col}_t")
        differs = ~((a.is_null() & b.is_null()) | (a == b).fill_null(False))
        if joined.select(differs.any()).item():
            bad_features.append(col)
        mismatch = mismatch | differs
    future_sources = full.filter(pl.col("max_source_filed_date") > as_of).height
    return LeakageResult(
        as_of=as_of,
        rows=full.height,
        mismatched_rows=joined.filter(mismatch).height + abs(full.height - truncated.height),
        mismatched_features=bad_features,
        max_source_after_as_of=future_sources,
    )
