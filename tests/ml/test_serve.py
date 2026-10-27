from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import polars as pl
from sklearn.isotonic import IsotonicRegression

from tripwire_common.config import load_demo_portfolio, load_domain_pack, load_models_config
from tripwire_common.settings import REPO_ROOT
from tripwire_ml.features import FEATURE_COLUMNS
from tripwire_ml.serve import _tier, tier_cutoffs
from tripwire_ml.train import TrainedModel

ML = load_models_config(REPO_ROOT / "config").ml


class _Booster:
    """Raw score = the first feature."""

    def predict(self, x: np.ndarray, raw_score: bool = False, **_: Any) -> np.ndarray:
        return np.asarray(x[:, 0])


def test_tier_cutoffs_match_rank_tiers() -> None:
    n = 200
    data = pl.DataFrame(np.arange(n * len(FEATURE_COLUMNS), dtype=float).reshape(n, -1)[:, :1]
                        .repeat(len(FEATURE_COLUMNS), axis=1), schema=FEATURE_COLUMNS)  # fmt: skip
    data = data.with_columns(as_of_date=pl.lit(dt.date(2024, 6, 30)))
    model = TrainedModel("lightgbm", _Booster(), IsotonicRegression())
    cutoffs = tier_cutoffs(data, model, ML)["2024-06-30"]
    scores = sorted(data[FEATURE_COLUMNS[0]].to_list(), reverse=True)
    tiers = [_tier(s, cutoffs) for s in scores]
    assert tiers.count("high") == int(n * ML.tiers.high)
    assert tiers.count("high") + tiers.count("elevated") == int(n * ML.tiers.elevated)


def test_demo_portfolio_config() -> None:
    demo = load_demo_portfolio(load_domain_pack())
    ciks = demo.ciks()
    assert len(ciks) == len(set(ciks)) == 50
    assert f"{demo.story.cik:010d}" in ciks
    groups = [c.group for c in demo.companies]
    assert groups.count("bankruptcy") == groups.count("healthy") == 25
