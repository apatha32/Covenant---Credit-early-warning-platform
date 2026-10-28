"""Serving the demo portfolio (Phase 6a).

    demo-export   (local)  active model + tier cutoffs + the demo companies' point-in-time
                           predictions and bankruptcy cases -> data/demo_bundle/
    demo-seed     (target) per-company SEC + FRED refresh, then load the bundle: model artifacts
                           to Storage, model version, demo tenant / portfolio, cases, predictions
    demo-refresh  (weekly) per-company SEC + FRED refresh, re-score as of today with the active
                           model (features read from curated tables), write predictions

The target is whatever DATABASE_URL / TRIPWIRE_STORAGE point at, so the same commands run
locally and in GitHub Actions.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import joblib
import lightgbm as lgb
import numpy as np
import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from tripwire_common.config import MLConfig, load_demo_portfolio, load_models_config
from tripwire_common.runs import ingest_run
from tripwire_common.storage import LocalStorage, Storage, SupabaseStorage
from tripwire_ml.explain import drivers
from tripwire_ml.features import (
    FEATURE_COLUMNS,
    FEATURE_SET_VERSION,
    Inputs,
    features_as_of,
    prepare_filings,
)
from tripwire_ml.labels import Case, label_rows
from tripwire_ml.macro import load_macro
from tripwire_ml.train import TrainedModel, _xy, tier_by_rank

if TYPE_CHECKING:
    from tripwire_ml.cli import Context

log = logging.getLogger(__name__)

YEAR = dt.timedelta(days=365)


def bundle_dir(ctx: Context) -> Path:
    return ctx.settings.tripwire_data_dir / "demo_bundle"


# --- model artifacts ---


def load_model(storage: Storage, prefix: str) -> TrainedModel:
    meta = json.loads(storage.read_bytes(f"{prefix}/meta.json"))
    if meta["algo"] != "lightgbm" or meta["features"] != FEATURE_COLUMNS:
        raise RuntimeError(f"{prefix} is not a LightGBM model on feature set {FEATURE_SET_VERSION}")
    booster = lgb.Booster(model_str=storage.read_bytes(f"{prefix}/model.txt").decode())
    calibrator = joblib.load(io.BytesIO(storage.read_bytes(f"{prefix}/calibrator.joblib")))
    return TrainedModel("lightgbm", booster, calibrator, meta.get("details") or {})


def tier_cutoffs(data: pl.DataFrame, model: TrainedModel, ml: MLConfig) -> dict[str, list[float]]:
    """Raw-score thresholds of the high / elevated tiers on each as-of date (whole universe).
    Scoring a handful of companies on a new date reuses the latest date's thresholds."""
    x, _ = _xy(data.with_columns(label=pl.lit(0)))
    scores = data.select("as_of_date").with_columns(score=pl.Series(model.raw_score(x)))
    out = {}
    for (as_of,), frame in scores.group_by("as_of_date"):
        s = np.sort(frame["score"].to_numpy())[::-1]
        out[as_of.isoformat()] = [
            float(s[max(int(len(s) * ml.tiers.high) - 1, 0)]),
            float(s[max(int(len(s) * ml.tiers.elevated) - 1, 0)]),
        ]
    return dict(sorted(out.items()))


def _tier(score: float, cutoffs: list[float]) -> str:
    return "high" if score >= cutoffs[0] else "elevated" if score >= cutoffs[1] else "low"


# --- scoring ---


def score_rows(
    frame: pl.DataFrame,
    model: TrainedModel,
    tiers: pl.Series,
    year_ago: dict[tuple[str, dt.date], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Probability, tier and plain-English drivers for each feature row."""
    x, _ = _xy(frame.with_columns(label=pl.lit(0)))
    contrib = model.model.predict(x, pred_contrib=True)[:, :-1]
    probability = model.predict(x)
    out = []
    for i, r in enumerate(
        frame.select("cik", "as_of_date", *FEATURE_COLUMNS).iter_rows(named=True)
    ):
        values = {f: r[f] for f in FEATURE_COLUMNS}
        out.append({
            "cik": r["cik"], "as_of_date": r["as_of_date"], "probability": float(probability[i]),
            "risk_tier": tiers[i],
            "top_drivers": drivers(contrib[i], values, year_ago.get((r["cik"], r["as_of_date"]))),
        })  # fmt: skip
    return out


def _year_ago_index(frame: pl.DataFrame) -> dict[tuple[str, dt.date], dict[str, Any]]:
    """(cik, as_of) -> that company's features a year earlier (365 or 366 days)."""
    rows = {(r["cik"], r["as_of_date"]): r for r in
            frame.select("cik", "as_of_date", *FEATURE_COLUMNS).iter_rows(named=True)}  # fmt: skip
    out = {}
    for cik, as_of in rows:
        prev = rows.get((cik, as_of - YEAR)) or rows.get((cik, as_of - YEAR - dt.timedelta(days=1)))
        if prev:
            out[(cik, as_of)] = {f: prev[f] for f in FEATURE_COLUMNS}
    return out


def insert_predictions(conn: psycopg.Connection, rows: list[dict[str, Any]], version: int) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            """insert into ml.prediction (cik, as_of_date, model_version_id, probability,
                                          risk_tier, top_drivers)
               values (%s, %s, %s, %s, %s, %s) on conflict do nothing""",
            [(r["cik"], r["as_of_date"], version, r["probability"], r["risk_tier"],
              Jsonb(r["top_drivers"])) for r in rows],
        )  # fmt: skip
        return max(cur.rowcount, 0)


# --- demo-export ---


def cmd_demo_export(ctx: Context) -> None:
    """Everything the hosted demo needs from local training results, as plain files."""
    ml = load_models_config().ml
    demo = load_demo_portfolio(ctx.pack)
    out = bundle_dir(ctx)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    row = ctx.conn.execute(
        """select id, algo, feature_set_version, train_window::text, metrics, artifact_path
           from ml.model_version where is_active"""
    ).fetchone()
    if row is None:
        raise SystemExit("no active model; run `python -m tripwire_ml train`")
    version, prefix = int(row[0]), str(row[5])
    model_row = dict(zip(["id", "algo", "feature_set_version", "train_window", "metrics",
                          "artifact_path"], row, strict=True))  # fmt: skip
    (out / "model_version.json").write_text(json.dumps(model_row, default=str))
    for name in ("model.txt", "calibrator.joblib", "meta.json"):
        target = out / prefix / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(ctx.storage.read_bytes(f"{prefix}/{name}"))
    model = load_model(ctx.storage, prefix)

    data = pl.read_parquet(ctx.root / "datasets" / f"feature_set={FEATURE_SET_VERSION}"
                           / "dataset.parquet")  # fmt: skip
    cutoffs = tier_cutoffs(data, model, ml)
    (out / prefix / "tier_cutoffs.json").write_text(json.dumps(cutoffs))

    x, _ = _xy(data.with_columns(label=pl.lit(0)))
    ranked = data.select("cik", "as_of_date").with_columns(score=pl.Series(model.raw_score(x)))
    ranked = ranked.with_columns(risk_tier=tier_by_rank(ranked, "score", ml))
    mine = data.join(ranked.select("cik", "as_of_date", "risk_tier"), on=["cik", "as_of_date"])
    mine = mine.filter(pl.col("cik").is_in(demo.ciks())).sort("cik", "as_of_date")
    rows = score_rows(mine, model, mine["risk_tier"], _year_ago_index(mine))
    pl.DataFrame(
        [{**r, "top_drivers": json.dumps(r["top_drivers"])} for r in rows],
        schema={"cik": pl.String, "as_of_date": pl.Date, "probability": pl.Float64,
                "risk_tier": pl.String, "top_drivers": pl.String},
    ).write_parquet(out / "predictions.parquet")  # fmt: skip
    cases = pl.read_parquet(ctx.root / "labels" / "cases.parquet")
    cases.filter(pl.col("cik").is_in(demo.ciks())).write_parquet(out / "cases.parquet")
    missing = set(demo.ciks()) - set(mine["cik"])
    if missing:
        raise RuntimeError(f"demo companies without predictions: {sorted(missing)}")
    log.info("demo bundle written", extra={"path": str(out), "model_version": version,
                                           "predictions": len(rows)})  # fmt: skip


# --- data refresh (shared by seed and weekly refresh) ---


def refresh_data(ciks: list[str]) -> None:
    """Per-company SEC API (not bulk) + FRED, then raw -> staging -> curated. Each step logs its
    own raw.ingest_runs row."""
    from tripwire_ingest.cli import main as ingest
    from tripwire_pipeline.cli import main as pipeline

    ints = [str(int(c)) for c in ciks]
    ingest(["submissions", "--cik", *ints])
    ingest(["companyfacts", "--cik", *ints])
    ingest(["fred"])
    for step in ("staging", "curated", "events"):  # not "all": coverage rewrites a repo report
        pipeline([step, "--cik", *ints])


def _ensure_bucket(storage: Storage) -> None:
    if isinstance(storage, SupabaseStorage):
        storage.ensure_bucket()


def _failed_child_runs(conn: psycopg.Connection, since: dt.datetime) -> list[str]:
    rows = conn.execute(
        """select source || ' #' || id || ': ' || status from raw.ingest_runs
           where started_at >= %s and status <> 'succeeded' and status <> 'running'""",
        [since],
    ).fetchall()
    return [r[0] for r in rows]


# --- demo-seed ---


def cmd_demo_seed(ctx: Context, skip_refresh: bool = False) -> None:
    demo = load_demo_portfolio(ctx.pack)
    bundle = bundle_dir(ctx)
    model_row = json.loads((bundle / "model_version.json").read_text())
    version, prefix = int(model_row["id"]), str(model_row["artifact_path"])
    _ensure_bucket(ctx.storage)
    if not skip_refresh:
        refresh_data(demo.ciks())
    with ingest_run(ctx.conn, "ml_demo_seed") as run:
        source = LocalStorage(bundle)
        for name in ("model.txt", "calibrator.joblib", "meta.json", "tier_cutoffs.json"):
            ctx.storage.write_bytes(f"{prefix}/{name}", source.read_bytes(f"{prefix}/{name}"))
        with ctx.conn.transaction():
            exists = ctx.conn.execute("select 1 from ml.model_version where id = %s",
                                      [version]).fetchone()  # fmt: skip
            if not exists:
                ctx.conn.execute(
                    """insert into ml.model_version (id, algo, feature_set_version, train_window,
                         metrics, artifact_path, model_card_path)
                       overriding system value values (%s, %s, %s, %s::daterange, %s, %s, %s)""",
                    [version, model_row["algo"], model_row["feature_set_version"],
                     model_row["train_window"], Jsonb(model_row["metrics"]), prefix,
                     "reports/model_eval.md"],
                )  # fmt: skip
                ctx.conn.execute(
                    "select setval(pg_get_serial_sequence('ml.model_version', 'id'), "
                    "greatest((select max(id) from ml.model_version), 1))"
                )
            ctx.conn.execute("update ml.model_version set is_active = false where is_active "
                             "and id <> %s", [version])  # fmt: skip
            ctx.conn.execute("update ml.model_version set is_active = true where id = %s",
                             [version])  # fmt: skip

        known = {r[0] for r in ctx.conn.execute(
            "select cik from curated.company where cik = any(%s)", [demo.ciks()])}  # fmt: skip
        missing = set(demo.ciks()) - known
        if missing:
            raise RuntimeError(f"demo companies not in curated.company: {sorted(missing)}")
        with ctx.conn.transaction():
            tenant = _demo_tenant(ctx.conn, demo.tenant, demo.portfolio)
            portfolio_id, tenant_id = tenant
            for c in demo.companies:
                ctx.conn.execute(
                    """insert into app.portfolio_borrower (tenant_id, portfolio_id, cik,
                         uploaded_name, match_status, match_confidence)
                       select %s, %s, %s, %s, 'confirmed', 1
                       where not exists (select 1 from app.portfolio_borrower
                                         where portfolio_id = %s and cik = %s)""",
                    [tenant_id, portfolio_id, f"{c.cik:010d}", c.name, portfolio_id,
                     f"{c.cik:010d}"],
                )  # fmt: skip
        label_version = ctx.pack.labels.text_check.version if ctx.pack.labels.text_check else ""
        cases = pl.read_parquet(bundle / "cases.parquet")
        with ctx.conn.cursor() as cur:
            cur.executemany(
                """insert into curated.bankruptcy_case (cik, case_start, case_end, accession,
                     label_version) values (%s, %s, %s, %s, %s) on conflict do nothing""",
                [(r["cik"], r["case_start"], r["case_end"], r["accession"], str(label_version))
                 for r in cases.iter_rows(named=True)],
            )  # fmt: skip
        predictions = pl.read_parquet(bundle / "predictions.parquet")
        rows = [{**r, "top_drivers": json.loads(r["top_drivers"])}
                for r in predictions.iter_rows(named=True)]  # fmt: skip
        run.counts.update({"companies": len(demo.companies), "cases": cases.height,
                           "predictions_inserted": insert_predictions(ctx.conn, rows, version),
                           "model_version": version})  # fmt: skip


def _id_or_insert(conn: psycopg.Connection, find: str, insert: str, params: list[Any]) -> Any:
    row = conn.execute(find, params).fetchone() or conn.execute(insert, params).fetchone()
    assert row is not None
    return row[0]


def _demo_tenant(conn: psycopg.Connection, tenant: str, portfolio: str) -> tuple[Any, Any]:
    tenant_id = _id_or_insert(
        conn,
        "select id from app.tenant where is_demo and name = %s",
        "insert into app.tenant (name, is_demo) values (%s, true) returning id",
        [tenant],
    )
    portfolio_id = _id_or_insert(
        conn,
        "select id from app.portfolio where tenant_id = %s and name = %s",
        "insert into app.portfolio (tenant_id, name) values (%s, %s) returning id",
        [tenant_id, portfolio],
    )
    return portfolio_id, tenant_id


# --- demo-refresh ---


def served_inputs(ctx: Context, ciks: list[str]) -> Inputs:
    """The same inputs features_as_of reads in training, from curated / staging tables."""
    metrics = pl.read_database(
        """select cik::text, canonical_metric as metric, period_type, period_start, period_end,
                  value::float8 as value, filed_date, version
           from curated.financial_metric where mapping_version = %(v)s and cik = any(%(c)s)""",
        ctx.conn, execute_options={"params": {"v": ctx.pack.metrics.mapping_version,
                                                  "c": ciks}},
        schema_overrides={"period_start": pl.Date, "period_end": pl.Date, "filed_date": pl.Date,
                          "value": pl.Float64, "version": pl.Int64},
        infer_schema_length=None,
    )  # fmt: skip
    filings = pl.read_database(
        """select cik::text, accession as "accessionNumber", form_type as form,
                  filed_date::text as "filingDate", coalesce(report_date::text, '') as "reportDate",
                  array_to_string(items, ',') as items
           from staging.filings where cik = any(%(c)s)""",
        ctx.conn, execute_options={"params": {"c": ciks}}, infer_schema_length=None,
    )  # fmt: skip
    return Inputs(metrics.lazy(), prepare_filings(filings.lazy()),
                  load_macro(ctx.conn, ctx.storage, ctx.pack.macro), frozenset(ciks))  # fmt: skip


def _not_in_bankruptcy(ctx: Context, frame: pl.DataFrame) -> pl.DataFrame:
    """Drop companies inside a bankruptcy case on the as-of date (training excludes them)."""
    cases = [Case(r[0], r[1], r[2], r[3]) for r in ctx.conn.execute(
        "select cik, case_start, case_end, accession from curated.bankruptcy_case")]  # fmt: skip
    flags = label_rows(frame.select("cik", "as_of_date"), cases, ctx.pack.labels)
    keep = flags.filter(~pl.col("in_bankruptcy")).select("cik", "as_of_date")
    return frame.join(keep, on=["cik", "as_of_date"], how="semi")


def cmd_demo_refresh(
    ctx: Context, as_of: dt.date | None = None, skip_refresh: bool = False
) -> None:
    demo = load_demo_portfolio(ctx.pack)
    as_of = as_of or dt.date.today()
    started = dt.datetime.now(dt.UTC)
    _ensure_bucket(ctx.storage)
    if not skip_refresh:
        refresh_data(demo.ciks())
    with ingest_run(ctx.conn, "ml_demo_refresh") as run:
        row = ctx.conn.execute(
            "select id, artifact_path from ml.model_version where is_active"
        ).fetchone()
        if row is None:
            raise RuntimeError("no active model; run demo-seed first")
        version, prefix = int(row[0]), str(row[1])
        model = load_model(ctx.storage, prefix)
        cutoffs = json.loads(ctx.storage.read_bytes(f"{prefix}/tier_cutoffs.json"))
        latest = cutoffs[max(d for d in cutoffs if d <= as_of.isoformat())]
        inputs = served_inputs(ctx, demo.ciks())
        now = _not_in_bankruptcy(ctx, features_as_of(inputs, as_of))
        then = features_as_of(inputs, as_of - YEAR)
        year_ago = {(r["cik"], as_of): {f: r[f] for f in FEATURE_COLUMNS}
                    for r in then.iter_rows(named=True)}  # fmt: skip
        x, _ = _xy(now.with_columns(label=pl.lit(0)))
        tiers = pl.Series([_tier(s, latest) for s in model.raw_score(x)], dtype=pl.String)
        rows = score_rows(now, model, tiers, year_ago)
        run.counts.update({
            "model_version": version, "companies": len(demo.companies), "scored": len(rows),
            "not_scored_inactive_or_stale": len(demo.companies) - len(rows),
            "predictions_inserted": insert_predictions(ctx.conn, rows, version),
        })  # fmt: skip
        with ctx.conn.cursor() as cur:
            cur.executemany(
                """insert into ml.feature_snapshot (cik, as_of_date, feature_set_version, features,
                     max_source_filed_date) values (%s, %s, %s, %s, %s) on conflict do nothing""",
                [(r["cik"], as_of, FEATURE_SET_VERSION,
                  Jsonb({f: (None if v is None or v != v else v)
                         for f, v in ((f, r[f]) for f in FEATURE_COLUMNS)}),
                  r["max_source_filed_date"]) for r in now.iter_rows(named=True)],
            )  # fmt: skip
        failed = _failed_child_runs(ctx.conn, started)
        if failed:
            raise RuntimeError("refresh steps did not fully succeed: " + "; ".join(failed))
