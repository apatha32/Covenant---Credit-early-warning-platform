"""Phase 4 acceptance: the leakage test. Features must not change when everything published
after the as-of date is deleted, and a deliberately leaky feature must be caught."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from tripwire_ml import features as features_module
from tripwire_ml.dataset import leakage_check, truncate_inputs
from tripwire_ml.features import Inputs, features_as_of, prepare_filings

D = dt.date.fromisoformat
CIK = "0000000001"
AS_OF = D("2020-09-30")


def _metric(metric: str, ptype: str, end: str, value: float, filed: str, version: int = 1,
            start: str | None = None) -> dict[str, object]:  # fmt: skip
    return {"cik": CIK, "metric": metric, "period_type": ptype,
            "period_start": D(start) if start else None, "period_end": D(end), "value": value,
            "filed_date": D(filed), "version": version}  # fmt: skip


def _inputs() -> Inputs:
    rows = []
    quarters = [
        ("2019-03-31", "2019-05-10"),
        ("2019-06-30", "2019-08-09"),
        ("2019-09-30", "2019-11-08"),
        ("2019-12-31", "2020-02-28"),
        ("2020-03-31", "2020-05-08"),
        ("2020-06-30", "2020-08-07"),
        ("2020-09-30", "2020-11-06"),
    ]  # fmt: skip  (the last one is filed after AS_OF)
    for i, (end, filed) in enumerate(quarters):
        rows.append(_metric("revenue", "Q", end, 100 + 10 * i, filed))
        rows.append(_metric("total_assets", "Q", end, 1000 - 20 * i, filed))
        rows.append(_metric("cash", "Q", end, 200 - 25 * i, filed))
        rows.append(_metric("current_assets", "Q", end, 400, filed))
        rows.append(_metric("current_liabilities", "Q", end, 300 + 10 * i, filed))
        rows.append(_metric("operating_income", "Q", end, 5 - i, filed))
        rows.append(_metric("interest_expense", "Q", end, 4, filed))
    # A restatement of Q2 2020 revenue, filed after the as-of date: must be invisible.
    rows.append(_metric("revenue", "Q", "2020-06-30", 9999, "2021-02-01", version=2))
    metrics = pl.DataFrame(rows)
    filings = pl.DataFrame({
        "cik": [CIK] * 6,
        "accessionNumber": [f"0000000001-20-00000{i}" for i in range(6)],
        "filingDate": ["2020-05-08", "2020-08-07", "2020-03-02", "2020-08-20", "2020-11-06",
                       "2020-10-15"],
        "reportDate": ["2020-03-31", "2020-06-30", "", "", "2020-09-30", ""],
        "form": ["10-Q", "10-Q", "NT 10-K", "8-K", "10-Q", "8-K"],
        "items": ["", "", "", "5.02", "", "4.01"],  # the 4.01 auditor change is after AS_OF
    })  # fmt: skip
    macro = pl.DataFrame({
        "series_id": ["UNRATE"] * 3,
        "observation_date": [D("2020-08-01"), D("2020-09-01"), D("2020-09-01")],
        "release_date": [D("2020-09-04"), D("2020-10-02"), D("2020-11-06")],  # Sept: not out yet
        "value": [8.4, 7.9, 7.8],
    })  # fmt: skip
    return Inputs(metrics.lazy(), prepare_filings(filings.lazy()), macro, frozenset({CIK}))


def test_features_are_identical_after_deleting_the_future() -> None:
    result = leakage_check(_inputs(), AS_OF)
    assert result.rows == 1
    assert result.passed, result


def test_a_planted_leak_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make latest_known ignore the as-of date (reads restatements filed later): the check
    must fail. Guards against a leakage test that can never fail."""

    def leaky(metrics: pl.LazyFrame, as_of: dt.date) -> pl.DataFrame:
        return (
            metrics.sort("filed_date", "version")
            .group_by("cik", "metric", "period_type", "period_end")
            .agg(pl.col("value").last(), pl.col("filed_date").last())
            .collect()
        )

    monkeypatch.setattr(features_module, "latest_known", leaky)
    result = leakage_check(_inputs(), AS_OF)
    assert not result.passed
    assert "revenue_slope_4q" in result.mismatched_features or result.max_source_after_as_of > 0


def test_feature_values_use_only_known_data() -> None:
    row = features_as_of(_inputs(), AS_OF).row(0, named=True)
    # TTM revenue = Q3'19..Q2'20 as known on Sept 30 (the 9999 restatement is not yet filed).
    assert row["log_revenue_ttm"] == pytest.approx(__import__("math").log10(120 + 130 + 140 + 150))
    assert row["revenue_yoy"] is None  # needs 8 contiguous quarters; only 6 are known
    assert row["late_filings_24m"] == 1  # the NT 10-K
    assert row["auditor_changes_24m"] == 0  # filed after the as-of date
    assert row["exec_departures_12m"] == 1
    assert row["macro_unrate"] == 8.4  # September's rate was released in October
    assert row["filing_lag_days"] == (D("2020-08-07") - D("2020-06-30")).days
    assert row["max_source_filed_date"] <= AS_OF


def test_truncation_removes_only_later_data() -> None:
    inputs = _inputs()
    cut = truncate_inputs(inputs, AS_OF)
    assert cut.metrics.select(pl.col("filed_date").max()).collect().item() <= AS_OF
    assert cut.macro["release_date"].max() <= AS_OF  # type: ignore[operator]
