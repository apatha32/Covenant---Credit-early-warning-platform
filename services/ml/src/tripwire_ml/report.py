"""`python -m tripwire_ml train`: train, evaluate, register, score, and write the model card."""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import math
from typing import TYPE_CHECKING, Any

import joblib
import polars as pl
from psycopg.types.json import Jsonb

from tripwire_common.config import load_dev_sample, load_models_config
from tripwire_common.runs import ingest_run
from tripwire_common.settings import REPO_ROOT
from tripwire_ml.explain import drivers
from tripwire_ml.features import FEATURE_COLUMNS, FEATURE_SET_VERSION, FEATURES
from tripwire_ml.train import (
    ALGOS,
    TrainedModel,
    _xy,
    backtest,
    evaluate,
    lead_times,
    shap_importance,
    split_frame,
    tier_by_rank,
    train_all,
)

if TYPE_CHECKING:
    from tripwire_ml.cli import Context

log = logging.getLogger(__name__)

REPORT_PATH = REPO_ROOT / "reports" / "model_eval.md"


def _clean(value: Any) -> Any:
    """JSON-safe (NaN -> None) for jsonb columns."""
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _save_artifacts(ctx: Context, version_id: int, model: TrainedModel) -> str:
    prefix = f"models/{version_id}"
    if model.algo == "lightgbm":
        ctx.storage.write_bytes(f"{prefix}/model.txt", model.model.model_to_string().encode())
    elif model.algo == "logistic":
        buf = io.BytesIO()
        joblib.dump(model.model, buf)
        ctx.storage.write_bytes(f"{prefix}/model.joblib", buf.getvalue())
    buf = io.BytesIO()
    joblib.dump(model.calibrator, buf)
    ctx.storage.write_bytes(f"{prefix}/calibrator.joblib", buf.getvalue())
    ctx.storage.write_bytes(
        f"{prefix}/meta.json",
        json.dumps(
            {
                "algo": model.algo,
                "feature_set_version": FEATURE_SET_VERSION,
                "features": FEATURE_COLUMNS,
                "details": _clean(model.details),
            },
            default=str,
        ).encode(),
    )
    return prefix


def _register(
    ctx: Context, models: dict[str, TrainedModel], metrics: dict[str, Any], train_window: str
) -> dict[str, int]:
    """Insert one ml.model_version per algorithm; promote LightGBM if it beats the active model
    on test PR-AUC without worse calibration (Brier)."""
    ids: dict[str, int] = {}
    for algo, model in models.items():
        row = ctx.conn.execute(
            """insert into ml.model_version (algo, feature_set_version, train_window, metrics)
               values (%s, %s, %s::daterange, %s) returning id""",
            [algo, FEATURE_SET_VERSION, train_window, Jsonb(_clean(metrics[algo]))],
        ).fetchone()
        assert row is not None
        ids[algo] = int(row[0])
        path = _save_artifacts(ctx, ids[algo], model)
        ctx.conn.execute(
            "update ml.model_version set artifact_path = %s, model_card_path = %s where id = %s",
            [path, f"{path}/model_card.md", ids[algo]],
        )
    candidate = metrics["lightgbm"]["test"]
    active = ctx.conn.execute("select id, metrics from ml.model_version where is_active").fetchone()
    promote = active is None or (
        candidate["pr_auc"] > (active[1]["test"]["pr_auc"] or 0)
        and candidate["brier"] <= (active[1]["test"]["brier"] or 1)
    )
    if promote:
        with ctx.conn.transaction():
            ctx.conn.execute("update ml.model_version set is_active = false where is_active")
            ctx.conn.execute(
                "update ml.model_version set is_active = true where id = %s", [ids["lightgbm"]]
            )
    ids["promoted"] = int(promote)
    return ids


def _score_served(ctx: Context, data: pl.DataFrame, model: TrainedModel, version_id: int) -> int:
    """Predictions + feature snapshots for the served companies (dev sample for now), every
    as-of date. Tiers are ranked against the whole universe on each date."""
    ml = load_models_config().ml
    x, _ = _xy(data.with_columns(label=pl.lit(0)))
    contrib = model.model.predict(x, pred_contrib=True)[:, :-1]
    scored = data.select(
        "cik", "as_of_date", "max_source_filed_date", *FEATURE_COLUMNS
    ).with_columns(
        score=pl.Series(model.raw_score(x)),
        probability=pl.Series(model.predict(x)),
        row=pl.int_range(pl.len()),
    )
    scored = scored.with_columns(risk_tier=tier_by_rank(scored, "score", ml))
    served = {f"{c:010d}" for c in load_dev_sample(ctx.pack).ciks()}
    known = {
        r[0]
        for r in ctx.conn.execute(
            "select cik from curated.company where cik = any(%s)", [list(served)]
        )
    }
    mine = scored.filter(pl.col("cik").is_in(list(known)))
    by_key = {(r["cik"], r["as_of_date"]): r for r in mine.iter_rows(named=True)}
    predictions, snapshots = [], []
    for r in mine.iter_rows(named=True):
        values = {f: r[f] for f in FEATURE_COLUMNS}
        prev = by_key.get((r["cik"], r["as_of_date"] - dt.timedelta(days=365)))
        if prev is None:  # quarter ends do not always land exactly 365 days apart
            prev = by_key.get((r["cik"], r["as_of_date"] - dt.timedelta(days=366)))
        year_ago = {f: prev[f] for f in FEATURE_COLUMNS} if prev else None
        top = drivers(contrib[r["row"]], values, year_ago)
        predictions.append(
            (
                r["cik"],
                r["as_of_date"],
                version_id,
                float(r["probability"]),
                r["risk_tier"],
                Jsonb(top),
            )
        )
        snapshots.append(
            (
                r["cik"],
                r["as_of_date"],
                FEATURE_SET_VERSION,
                Jsonb(_clean(values)),
                r["max_source_filed_date"],
            )
        )
    with ctx.conn.transaction(), ctx.conn.cursor() as cur:
        cur.executemany(
            """insert into ml.prediction (cik, as_of_date, model_version_id, probability,
                                          risk_tier, top_drivers)
               values (%s, %s, %s, %s, %s, %s) on conflict do nothing""",
            predictions,
        )
        cur.executemany(
            """insert into ml.feature_snapshot (cik, as_of_date, feature_set_version, features,
                                                max_source_filed_date)
               values (%s, %s, %s, %s, %s) on conflict do nothing""",
            snapshots,
        )
    return len(predictions)


def cmd_train(ctx: Context) -> None:
    from tripwire_ml.cli import data_cutoff

    ml = load_models_config().ml
    data = pl.read_parquet(
        ctx.root / "datasets" / f"feature_set={FEATURE_SET_VERSION}" / "dataset.parquet"
    )
    cutoff = data_cutoff(ctx)
    cases = pl.read_parquet(ctx.root / "labels" / "cases.parquet")
    events = pl.read_parquet(ctx.root / "labels" / "item_events.parquet")
    with ingest_run(ctx.conn, "ml_train") as run:
        models = train_all(data, ml)
        splits = {name: split_frame(data, ml, name) for name in ("train", "validation", "test")}
        metrics: dict[str, Any] = {}
        test_rows = data.filter(pl.col("as_of_date") >= ml.splits.test.start)
        lead_start = ml.splits.test.start + dt.timedelta(days=366)
        for algo, model in models.items():
            metrics[algo] = {
                name: evaluate(frame, model, ml)
                for name, frame in splits.items()
                if name != "train"
            }
            metrics[algo]["lead_time"] = lead_times(test_rows, model, cases, ml, lead_start, cutoff)
            metrics[algo]["details"] = _clean(
                {k: v for k, v in model.details.items() if k != "params"}
            )
        importance = shap_importance(models["lightgbm"], splits["test"])
        years = list(range(2015, 2025))
        bt = backtest(
            data,
            ml,
            years,
            models["lightgbm"].details["best_iteration"],
            models["logistic"].details["C"],
        )
        window = f"[{ml.splits.train.start},{ml.splits.train.end}]"
        ids = _register(ctx, models, metrics, window)
        run.counts.update({f"model_version_{k}": v for k, v in ids.items()})
        run.counts["predictions"] = _score_served(ctx, data, models["lightgbm"], ids["lightgbm"])
        report = render(data, splits, metrics, importance, bt, events, cases, ids, cutoff, ml)
        REPORT_PATH.write_text(report)
        ctx.storage.write_bytes(f"models/{ids['lightgbm']}/model_card.md", report.encode())
        for algo in ALGOS:
            run.counts[f"test_pr_auc_{algo}_x1000"] = round(metrics[algo]["test"]["pr_auc"] * 1000)
    log.info("model card written", extra={"path": str(REPORT_PATH)})


def _f(v: Any, digits: int = 3) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:.{digits}f}"


def _pct(v: Any) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:.1f}%"


def render(
    data: pl.DataFrame,
    splits: dict[str, pl.DataFrame],
    metrics: dict[str, Any],
    importance: list[tuple[str, float]],
    bt: list[dict[str, Any]],
    events: pl.DataFrame,
    cases: pl.DataFrame,
    ids: dict[str, int],
    cutoff: dt.date,
    ml: Any,
) -> str:
    t = {algo: metrics[algo]["test"] for algo in ALGOS}
    names = {
        "altman": "Altman Z'' (rule)",
        "logistic": "Logistic regression",
        "lightgbm": "LightGBM",
    }
    lines = [
        "# Model card: 12-month distress model (feature set v1)",
        "",
        f"Generated by `python -m tripwire_ml train` on {dt.date.today()}. Data as of {cutoff}. "
        f"Model versions: LightGBM {ids['lightgbm']} ({'active' if ids['promoted'] else 'not promoted'}), "
        f"logistic {ids['logistic']}, Altman {ids['altman']}.",
        "",
        "## Task",
        "",
        "For each SEC filer at each quarter end T: will the company start a bankruptcy case "
        "(8-K Item 1.03, text-checked) within (T, T + 12 months]? Features use only data filed "
        "on or before T. Companies already in bankruptcy at T are excluded. Banks, insurers, REITs, "
        "funds and shells (SIC 6000-6799) are excluded.",
        "",
        "## Data",
        "",
        "| Split | As-of dates | Rows | Companies | Positives | Base rate |",
        "|---|---|---|---|---|---|",
    ]
    for name, frame in splits.items():
        span = getattr(ml.splits, name)
        pos = int(frame["label"].sum())
        lines.append(
            f"| {name} | {span.start} to {span.end} | {frame.height:,} | "
            f"{frame['cik'].n_unique():,} | {pos} | {_pct(pos / max(frame.height, 1))} |"
        )
    lines += [
        "",
        "Label windows of different splits never overlap (as-of dates in 2019 and early 2021 are "
        "left out). The test split was used only for these numbers, never for tuning.",
        "",
        "## Results on the test split",
        "",
        "| Model | PR-AUC | ROC-AUC | Recall @ top 5% | Recall @ top 10% | Precision @ top 5% "
        "| Brier | Median lead (months) | Bankruptcies flagged |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for algo in ALGOS:
        m, lead = t[algo], metrics[algo]["lead_time"]
        lines.append(
            f"| {names[algo]} | **{_f(m['pr_auc'])}** | {_f(m['roc_auc'])} | {_pct(m['recall_top5'])} "
            f"| {_pct(m['recall_top10'])} | {_pct(m['precision_top5'])} | {_f(m['brier'], 4)} "
            f"| {_f(lead['median_lead_months'], 1)} | {lead['flagged']}/{lead['events_with_history']} |"
        )
    lift = (
        t["lightgbm"]["pr_auc"] / t["altman"]["pr_auc"] if t["altman"]["pr_auc"] else float("nan")
    )
    lines += [
        "",
        f"Base rate on test: {_pct(t['lightgbm']['base_rate'])} (a random ranking scores PR-AUC "
        f"≈ {_f(t['lightgbm']['base_rate'])}). LightGBM PR-AUC is {_f(lift, 1)}x the Altman baseline.",
        "",
        "Recall @ top k%: share of test bankruptcies whose company ranked in the riskiest k% "
        "on that quarter end. Lead time: for bankruptcies starting from "
        f"{ml.splits.test.start + dt.timedelta(days=366)} (so a year of test-period history "
        "exists), months between the first quarter end on which the company was in the high "
        f"tier (top {ml.tiers.high:.0%}) and the filing; looks back at most 24 months.",
        "",
        "## Calibration (LightGBM, test)",
        "",
        "| Decile | Mean predicted | Observed rate | Rows |",
        "|---|---|---|---|",
        *[
            f"| {int(b['bin'])} | {_pct(b['predicted'])} | {_pct(b['observed'])} | {b['n']:,} |"
            for b in t["lightgbm"]["calibration"]
        ],
        "",
        "## Rolling-origin backtest (PR-AUC by test year)",
        "",
        "Train on as-of dates up to the end of Y-2, test on as-of dates in Y, fixed hyperparameters.",
        "",
        "| Year | Rows | Positives | Base rate | Altman | Logistic | LightGBM |",
        "|---|---|---|---|---|---|---|",
        *[
            f"| {r['year']} | {r['rows']:,} | {r['positives']} | {_pct(r['base_rate'])} | "
            f"{_f(r['altman'])} | {_f(r['logistic'])} | {_f(r['lightgbm'])} |"
            for r in bt
        ],
        "",
        "## Top drivers (mean |SHAP|, LightGBM, test)",
        "",
        "| Feature | Mean |SHAP| |",
        "|---|---|",
        *[f"| {FEATURES[name][0]} | {value:.3f} |" for name, value in importance[:15]],
        "",
        "## Label quality",
        "",
    ]
    kinds = events.group_by("kind").agg(n=pl.len()).sort("n", descending=True)
    starts = cases.join(
        events.select("cik", "accession", "kind"), on=["cik", "accession"], how="left"
    )
    start_kinds = starts.group_by("kind").agg(n=pl.len()).sort("n", descending=True)
    lines += [
        f"{events.height:,} Item 1.03 8-Ks of in-scope companies were classified from their text "
        f"into {cases.height:,} bankruptcy cases.",
        "",
        "| Filing class | Filings |",
        "|---|---|",
        *[f"| {r['kind']} | {r['n']:,} |" for r in kinds.iter_rows(named=True)],
        "",
        "| Case started by | Cases |",
        "|---|---|",
        *[f"| {r['kind']} | {r['n']:,} |" for r in start_kinds.iter_rows(named=True)],
        "",
        "Hand review of a fresh random sample (not used for rule changes): 18 of 20 `petition` "
        "filings were the filer's own bankruptcy (errors: a parent filed without this registrant); "
        "about half of `unclear` filings were new insolvencies. Estimated label precision ≈ 88%. "
        "Bankruptcies never reported on an Item 1.03 8-K are missing (recall is not measured).",
        "",
        "## Leakage check",
        "",
        "Features are recomputed after deleting every filing, restatement and macro release dated "
        "after the as-of date; the output must be identical. It runs in the unit tests (synthetic "
        "data, including a planted leak that must be caught) and on real data during every "
        "dataset build (results in `raw.ingest_runs`, source `ml_dataset`).",
        "",
        "## Known limitations",
        "",
        "- Book values only: no market prices, so Altman Z'' (book-equity variant) is the baseline.",
        "- Universe is SEC XBRL filers since 2009; dominated by small caps.",
        "- Labels rely on Item 1.03 8-Ks; companies that stop filing before bankruptcy may be missed.",
        "- Text-derived risk signals (Phase 5) are not in feature set v1.",
        "- Probabilities are calibrated on 2020, an unusual year; check calibration drift (Phase 7).",
    ]
    return "\n".join(lines) + "\n"
