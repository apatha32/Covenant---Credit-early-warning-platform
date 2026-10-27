"""Train and evaluate the three distress models on time-based splits.

  altman    - rule baseline: lower Altman Z'' = riskier (no fitting)
  logistic  - median imputation + missing indicators, clipped at train 1st/99th pct, scaled
  lightgbm  - gradient-boosted trees, class-weighted, early-stopped on validation PR-AUC

Every model's raw score is calibrated on the validation split (isotonic), so its probability is
comparable to observed default rates. The test split is used only for the final numbers.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from tripwire_common.config import MLConfig
from tripwire_ml.features import FEATURE_COLUMNS

log = logging.getLogger(__name__)

ALGOS = ("altman", "logistic", "lightgbm")


def split_frame(data: pl.DataFrame, config: MLConfig, name: str) -> pl.DataFrame:
    span = getattr(config.splits, name)
    return data.filter(
        pl.col("label_complete") & pl.col("as_of_date").is_between(span.start, span.end)
    )


def _xy(frame: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    x: np.ndarray = frame.select(FEATURE_COLUMNS).to_numpy().astype(float)
    y: np.ndarray = frame["label"].to_numpy().astype(int)
    return x, y


class Clipper:
    """Clip each feature to its train 1st / 99th percentile (fit on train only)."""

    def fit(self, x: np.ndarray, y: Any = None) -> Clipper:
        self.low_ = np.nanpercentile(x, 1, axis=0)
        self.high_ = np.nanpercentile(x, 99, axis=0)
        return self

    def transform(self, x: np.ndarray) -> np.ndarray:
        clipped: np.ndarray = np.clip(x, self.low_, self.high_)
        return clipped

    def get_params(self, deep: bool = True) -> dict[str, Any]:
        return {}

    def set_params(self, **params: Any) -> Clipper:
        return self


@dataclass
class TrainedModel:
    algo: str
    model: Any
    calibrator: IsotonicRegression
    details: dict[str, Any] = field(default_factory=dict)

    def raw_score(self, x: np.ndarray) -> np.ndarray:
        if self.algo == "altman":
            z = x[:, FEATURE_COLUMNS.index("altman_z2")]
            return -np.where(np.isnan(z), self.details["z_fill"], z)
        if self.algo == "logistic":
            return np.asarray(self.model.decision_function(x))
        return np.asarray(self.model.predict(x, raw_score=True))

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.calibrator.predict(self.raw_score(x)))


def _calibrate(scores: np.ndarray, y: np.ndarray) -> IsotonicRegression:
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    return iso.fit(scores, y)


def train_altman(train: pl.DataFrame, val: pl.DataFrame) -> TrainedModel:
    median = train["altman_z2"].median()
    z_fill = float(median) if isinstance(median, int | float) else 0.0
    model = TrainedModel("altman", None, IsotonicRegression(), {"z_fill": z_fill})
    xv, yv = _xy(val)
    model.calibrator = _calibrate(model.raw_score(xv), yv)
    return model


def train_logistic(train: pl.DataFrame, val: pl.DataFrame, config: MLConfig) -> TrainedModel:
    xt, yt = _xy(train)
    xv, yv = _xy(val)
    best: tuple[float, float, Pipeline] | None = None
    for c in config.logistic_c_grid:
        pipe = Pipeline(
            [
                ("impute", SimpleImputer(strategy="median", add_indicator=True)),
                ("clip", Clipper()),
                ("scale", StandardScaler()),
                ("model", LogisticRegression(C=c, class_weight="balanced", max_iter=3000)),
            ]
        )
        pipe.fit(xt, yt)
        ap = average_precision_score(yv, pipe.decision_function(xv))
        if best is None or ap > best[0]:
            best = (ap, c, pipe)
    assert best is not None
    model = TrainedModel("logistic", best[2], IsotonicRegression(), {"C": best[1]})
    model.calibrator = _calibrate(model.raw_score(xv), yv)
    return model


def train_lightgbm(train: pl.DataFrame, val: pl.DataFrame, config: MLConfig) -> TrainedModel:
    xt, yt = _xy(train)
    xv, yv = _xy(val)
    p = config.lightgbm
    pos = max(int(yt.sum()), 1)
    params = {
        "objective": "binary",
        "metric": "average_precision",
        "verbosity": -1,
        "learning_rate": p.learning_rate,
        "num_leaves": p.num_leaves,
        "min_data_in_leaf": p.min_data_in_leaf,
        "feature_fraction": p.feature_fraction,
        "bagging_fraction": p.bagging_fraction,
        "bagging_freq": p.bagging_freq,
        "lambda_l2": p.lambda_l2,
        "seed": config.random_seed,
        "deterministic": True,
        # Class weighting for the rare positives; calibration undoes the probability skew.
        "scale_pos_weight": min((len(yt) - pos) / pos, 50.0),
    }
    dtrain = lgb.Dataset(xt, yt, feature_name=FEATURE_COLUMNS)
    dval = lgb.Dataset(xv, yv, reference=dtrain)
    booster = lgb.train(
        params,
        dtrain,
        num_boost_round=p.max_rounds,
        valid_sets=[dval],
        callbacks=[lgb.early_stopping(p.early_stopping_rounds, verbose=False)],
    )
    model = TrainedModel(
        "lightgbm",
        booster,
        IsotonicRegression(),
        {"best_iteration": booster.best_iteration, "params": params},
    )
    model.calibrator = _calibrate(model.raw_score(xv), yv)
    return model


def train_all(data: pl.DataFrame, config: MLConfig) -> dict[str, TrainedModel]:
    train, val = split_frame(data, config, "train"), split_frame(data, config, "validation")
    return {
        "altman": train_altman(train, val),
        "logistic": train_logistic(train, val, config),
        "lightgbm": train_lightgbm(train, val, config),
    }


# --- evaluation ---


def tier_by_rank(frame: pl.DataFrame, score: str, config: MLConfig) -> pl.Expr:
    """high = top `tiers.high` of companies on that as-of date; elevated = the next band."""
    pct = pl.col(score).rank(method="ordinal", descending=True).over("as_of_date") / pl.len().over(
        "as_of_date"
    )
    return (
        pl.when(pct <= config.tiers.high)
        .then(pl.lit("high"))
        .when(pct <= config.tiers.elevated)
        .then(pl.lit("elevated"))
        .otherwise(pl.lit("low"))
    )


def evaluate(frame: pl.DataFrame, model: TrainedModel, config: MLConfig) -> dict[str, Any]:
    """Metrics on one split. Ranking metrics are computed within each as-of date."""
    x, y = _xy(frame)
    scored = frame.select("cik", "as_of_date", "label").with_columns(
        score=pl.Series(model.raw_score(x)), prob=pl.Series(model.predict(x))
    )
    pct = pl.col("score").rank(method="ordinal", descending=True).over(
        "as_of_date"
    ) / pl.len().over("as_of_date")
    scored = scored.with_columns(pct=pct)
    positives = int(y.sum())

    def recall_at(k: float) -> float:
        hit = scored.filter((pl.col("pct") <= k) & (pl.col("label") == 1)).height
        return hit / positives if positives else float("nan")

    bins = (
        scored.with_columns(
            bin=(pl.col("prob").rank(method="ordinal") * 10 / pl.len()).ceil().clip(1, 10)
        )
        .group_by("bin")
        .agg(predicted=pl.col("prob").mean(), observed=pl.col("label").mean(), n=pl.len())
        .sort("bin")
    )
    return {
        "rows": len(y),
        "positives": positives,
        "base_rate": positives / len(y) if len(y) else float("nan"),
        "pr_auc": float(average_precision_score(y, scored["score"])) if positives else float("nan"),
        "roc_auc": float(roc_auc_score(y, scored["score"]))
        if 0 < positives < len(y)
        else float("nan"),
        "brier": float(brier_score_loss(y, scored["prob"])),
        "recall_top5": recall_at(0.05),
        "recall_top10": recall_at(0.10),
        "precision_top5": (
            scored.filter((pl.col("pct") <= 0.05) & (pl.col("label") == 1)).height
            / max(scored.filter(pl.col("pct") <= 0.05).height, 1)
        ),
        "calibration": bins.to_dicts(),
    }


def lead_times(
    frame: pl.DataFrame,
    model: TrainedModel,
    cases: pl.DataFrame,
    config: MLConfig,
    window_start: dt.date,
    window_end: dt.date,
    lookback_days: int = 730,
) -> dict[str, Any]:
    """For each case starting in [window_start, window_end]: months between the first as-of
    date (within `lookback_days` before the event) on which the company ranked in the high tier,
    and the event. Events never flagged count as missed."""
    x, _ = _xy(frame)
    scored = frame.select("cik", "as_of_date").with_columns(score=pl.Series(model.raw_score(x)))
    scored = scored.with_columns(tier=tier_by_rank(scored, "score", config))
    events = cases.filter(pl.col("case_start").is_between(window_start, window_end))
    leads: list[float] = []
    missed = 0
    for row in events.iter_rows(named=True):
        start = row["case_start"]
        history = scored.filter(
            (pl.col("cik") == row["cik"])
            & (pl.col("as_of_date") < start)
            & (pl.col("as_of_date") >= start - dt.timedelta(days=lookback_days))
        )
        if history.height == 0:
            continue  # no financials on file before the event (e.g. stopped filing)
        flagged = history.filter(pl.col("tier") == "high").sort("as_of_date")
        if flagged.height == 0:
            missed += 1
            continue
        leads.append((start - flagged["as_of_date"][0]).days / 30.44)
    covered = len(leads) + missed
    return {
        "events_with_history": covered,
        "flagged": len(leads),
        "detection_rate": len(leads) / covered if covered else float("nan"),
        "median_lead_months": float(np.median(leads)) if leads else float("nan"),
    }


def backtest(
    data: pl.DataFrame, config: MLConfig, years: list[int], best_iteration: int, c: float
) -> list[dict[str, Any]]:
    """Rolling origin: for test year Y, train on as-of dates up to the end of Y-2 (so training
    labels end before Y starts), score as-of dates in Y. Fixed hyperparameters."""
    rows = []
    complete = data.filter(pl.col("label_complete"))
    for year in years:
        train = complete.filter(pl.col("as_of_date") <= dt.date(year - 2, 12, 31))
        test = complete.filter(pl.col("as_of_date").dt.year() == year)
        xt, yt = _xy(train)
        xs, ys = _xy(test)
        if ys.sum() == 0 or yt.sum() == 0:
            continue
        z = xs[:, FEATURE_COLUMNS.index("altman_z2")]
        altman = -np.where(np.isnan(z), np.nanmedian(xt[:, FEATURE_COLUMNS.index("altman_z2")]), z)
        logit = Pipeline(
            [
                ("impute", SimpleImputer(strategy="median", add_indicator=True)),
                ("clip", Clipper()),
                ("scale", StandardScaler()),
                ("model", LogisticRegression(C=c, class_weight="balanced", max_iter=3000)),
            ]
        ).fit(xt, yt)
        p = config.lightgbm
        pos = max(int(yt.sum()), 1)
        booster = lgb.train(
            {
                "objective": "binary",
                "verbosity": -1,
                "learning_rate": p.learning_rate,
                "num_leaves": p.num_leaves,
                "min_data_in_leaf": p.min_data_in_leaf,
                "feature_fraction": p.feature_fraction,
                "bagging_fraction": p.bagging_fraction,
                "bagging_freq": p.bagging_freq,
                "lambda_l2": p.lambda_l2,
                "seed": config.random_seed,
                "deterministic": True,
                "scale_pos_weight": min((len(yt) - pos) / pos, 50.0),
            },
            lgb.Dataset(xt, yt),
            num_boost_round=max(best_iteration, 50),
        )
        rows.append(
            {
                "year": year,
                "rows": len(ys),
                "positives": int(ys.sum()),
                "base_rate": float(ys.mean()),
                "altman": float(average_precision_score(ys, altman)),
                "logistic": float(average_precision_score(ys, logit.decision_function(xs))),
                "lightgbm": float(average_precision_score(ys, booster.predict(xs, raw_score=True))),
            }
        )
        log.info("backtest year done", extra=rows[-1])
    return rows


def shap_importance(model: TrainedModel, frame: pl.DataFrame) -> list[tuple[str, float]]:
    """Mean |SHAP| per feature (LightGBM's exact TreeSHAP contributions)."""
    x, _ = _xy(frame)
    contrib = model.model.predict(x, pred_contrib=True)[:, :-1]
    mean_abs = np.abs(contrib).mean(axis=0)
    return sorted(zip(FEATURE_COLUMNS, mean_abs.tolist(), strict=True), key=lambda t: -t[1])
