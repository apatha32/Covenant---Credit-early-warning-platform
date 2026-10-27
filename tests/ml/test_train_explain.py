from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from tripwire_common.config import load_models_config
from tripwire_common.settings import REPO_ROOT
from tripwire_ml.explain import drivers, fmt
from tripwire_ml.features import FEATURE_COLUMNS, quarter_ends
from tripwire_ml.train import evaluate, split_frame, train_all

ML = load_models_config(REPO_ROOT / "config").ml


def _synthetic(seed: int = 0) -> pl.DataFrame:
    """Quarterly panel where distress depends on leverage and cash runway (plus noise)."""
    rng = np.random.default_rng(seed)
    dates = [d for d in quarter_ends(dt.date(2011, 3, 31), dt.date(2025, 9, 30))]
    rows = []
    for d in dates:
        n = 400
        x = rng.normal(size=(n, len(FEATURE_COLUMNS)))
        risk = (
            2.0 * x[:, FEATURE_COLUMNS.index("liabilities_assets")]
            - 1.5 * x[:, FEATURE_COLUMNS.index("cash_runway_q")]
        )
        label = (risk + rng.normal(scale=1.0, size=n) > 4.5).astype(int)
        x[rng.random(size=x.shape) < 0.1] = np.nan  # some missing values
        frame = pl.DataFrame(x, schema=FEATURE_COLUMNS).with_columns(
            cik=pl.Series([f"{i:010d}" for i in range(n)]),
            as_of_date=pl.lit(d),
            label=pl.Series(label, dtype=pl.Int8),
            label_complete=pl.lit(True),
        )
        rows.append(frame)
    return pl.concat(rows)


def test_models_train_and_lightgbm_learns_the_signal() -> None:
    data = _synthetic()
    models = train_all(data, ML)
    test = split_frame(data, ML, "test")
    results = {algo: evaluate(test, model, ML) for algo, model in models.items()}
    base = results["lightgbm"]["base_rate"]
    assert 0 < base < 0.1
    # The planted signal is not in Altman's inputs, so the learned models must beat it clearly.
    assert results["lightgbm"]["pr_auc"] > 3 * base
    assert results["lightgbm"]["pr_auc"] > results["altman"]["pr_auc"]
    assert results["logistic"]["pr_auc"] > results["altman"]["pr_auc"]
    for r in results.values():
        assert 0 <= r["brier"] <= 1
        assert len(r["calibration"]) == 10


def test_splits_do_not_overlap_and_respect_label_completeness() -> None:
    data = _synthetic().with_columns(label_complete=pl.col("as_of_date") <= dt.date(2024, 12, 31))
    train, val, test = (split_frame(data, ML, s) for s in ("train", "validation", "test"))
    assert train["as_of_date"].max() < val["as_of_date"].min()  # type: ignore[operator]
    assert val["as_of_date"].max() < test["as_of_date"].min()  # type: ignore[operator]
    assert test["label_complete"].all()


@pytest.mark.parametrize(
    ("value", "kind", "text"),
    [(0.031, "pct", "3.1%"), (3.14, "x", "3.1x"), (120.4, "days", "120"), (None, "x", "n/a"),
     (float("nan"), "ratio", "n/a"), (0.5, "ratio", "0.50"), (9.98, "log_usd", "$9.5B")],
)  # fmt: skip
def test_format(value: float | None, kind: str, text: str) -> None:
    assert fmt(value, kind) == text


def test_drivers_are_plain_english_and_only_risk_increasing() -> None:
    contrib = np.zeros(len(FEATURE_COLUMNS))
    contrib[FEATURE_COLUMNS.index("interest_coverage")] = 0.8
    contrib[FEATURE_COLUMNS.index("negative_equity")] = 0.5
    contrib[FEATURE_COLUMNS.index("cash_assets")] = -0.4  # lowers risk: not a driver
    values = {"interest_coverage": 0.8, "negative_equity": 1.0, "cash_assets": 0.3}
    out = drivers(contrib, values, {"interest_coverage": 3.1})
    assert [d["text"] for d in out] == [
        "Interest coverage (EBIT / interest) fell from 3.1x to 0.8x",
        "Book equity is negative",
    ]


def test_tiny_changes_are_stated_as_a_value_not_a_trend() -> None:
    contrib = np.zeros(len(FEATURE_COLUMNS))
    contrib[FEATURE_COLUMNS.index("log_total_assets")] = 0.3
    (d,) = drivers(contrib, {"log_total_assets": 9.98}, {"log_total_assets": 9.97})
    assert d["text"] == "Total assets: $9.5B"
