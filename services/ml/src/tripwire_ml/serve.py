"""Serving the hosted console (Phase 6a demo, Phase 6b lender portfolios).

    demo-export   (local)  active model + tier cutoffs + the demo companies' point-in-time
                           predictions, plus the SEC company universe (for matching uploads)
                           and all bankruptcy cases -> data/demo_bundle/
    demo-seed     (target) per-company SEC + FRED refresh, then load the bundle: model artifacts
                           to Storage, model version, company universe, demo tenant / portfolio,
                           cases, predictions, demo alerts
    demo-refresh  (weekly) per-company SEC + FRED refresh of every served company, re-score as
                           of today with the active model (features read from curated tables),
                           write predictions and alerts
    serve-new     (hourly) companies newly matched in lender portfolios (ml.served_company
                           'queued'): fetch their data, score them as of today and on past
                           as-of dates since BACKFILL_FROM, write alerts

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

from tripwire_common.config import DomainPack, MLConfig, load_demo_portfolio, load_models_config
from tripwire_common.names import normalize_company_name
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
BACKFILL_FROM = dt.date(2019, 1, 1)  # score history shown for a newly added company


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


def insert_predictions(conn: psycopg.Connection, rows: list[dict[str, Any]], version: int,
                       batch: int = 250) -> int:  # fmt: skip
    """Idempotent insert, in small batches: one huge write over a pooled TLS connection is
    the likeliest thing to be cut off, and a re-run then resumes where it stopped."""
    inserted = 0
    for i in range(0, len(rows), batch):
        with conn.cursor() as cur:
            cur.executemany(
                """insert into ml.prediction (cik, as_of_date, model_version_id, probability,
                                              risk_tier, top_drivers)
                   values (%s, %s, %s, %s, %s, %s) on conflict do nothing""",
                [(r["cik"], r["as_of_date"], version, r["probability"], r["risk_tier"],
                  Jsonb(r["top_drivers"])) for r in rows[i : i + batch]],
            )  # fmt: skip
            inserted += max(cur.rowcount, 0)
    return inserted


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
    # All cases (not only the demo's): lender portfolios can hold any company.
    pl.read_parquet(ctx.root / "labels" / "cases.parquet").write_parquet(out / "cases.parquet")
    _export_universe(ctx, out)
    missing = set(demo.ciks()) - set(mine["cik"])
    if missing:
        raise RuntimeError(f"demo companies without predictions: {sorted(missing)}")
    log.info("demo bundle written", extra={"path": str(out), "model_version": version,
                                           "predictions": len(rows)})  # fmt: skip


UNIVERSE_COLUMNS = ("cik", "name", "tickers", "sic", "sic_description", "state",
                    "fiscal_year_end", "is_excluded", "exclusion_reason")  # fmt: skip
ALIAS_COLUMNS = ("cik", "alias", "alias_normalized", "alias_type", "valid_from", "valid_to")


def _export_universe(ctx: Context, out: Path) -> None:
    """Every SEC company an uploaded row can match: the training universe (all 10-K / 10-Q
    filers in the bulk files, with former names), the curated company table (rows win where
    both have one) and the latest stored SEC ticker map (current tickers, listed companies)."""
    curated = pl.DataFrame(
        ctx.conn.execute(f"select {', '.join(UNIVERSE_COLUMNS)} from curated.company").fetchall(),  # noqa: S608
        schema=list(UNIVERSE_COLUMNS), orient="row", infer_schema_length=None,
    )  # fmt: skip
    bulk = pl.read_parquet(ctx.root / "companies.parquet")
    fye = pl.col("fiscal_year_end")
    valid_fye = pl.when(fye.str.contains(r"^(0[1-9]|1[0-2])[0-3]\d$")).then(fye)
    extra = (
        bulk.filter(~pl.col("cik").is_in(curated["cik"].implode()))
        .with_columns(state=pl.lit(None, pl.String), fiscal_year_end=valid_fye)
        .select(UNIVERSE_COLUMNS)
    )
    maps = sorted((ctx.settings.tripwire_data_dir / "sec_tickers" / "company_tickers")
                  .glob("*/data.parquet"), key=lambda p: p.stat().st_mtime)  # fmt: skip
    empty = pl.DataFrame(schema={"cik": pl.String, "ticker": pl.String, "title": pl.String})
    ticker_map = pl.read_parquet(maps[-1]) if maps else empty
    listed = ticker_map.group_by("cik").agg(map_tickers="ticker", title=pl.col("title").first())
    known = pl.concat([curated["cik"], extra["cik"]])
    only_listed = (
        listed.filter(~pl.col("cik").is_in(known.implode()))
        .select("cik", name="title", tickers="map_tickers")
        .with_columns(is_excluded=pl.lit(False))
    )
    companies = (
        pl.concat([curated.with_columns(pl.col("sic").cast(pl.Int32)),
                   extra.with_columns(pl.col("sic").cast(pl.Int32)), only_listed],
                  how="diagonal_relaxed")
        .join(listed.select("cik", "map_tickers"), on="cik", how="left")
        .join(bulk.select("cik", bulk_tickers="tickers"), on="cik", how="left")
        .with_columns(tickers=pl.when(pl.col("tickers").list.len() > 0).then("tickers")
                      .when(pl.col("map_tickers").list.len() > 0).then("map_tickers")
                      .otherwise(pl.col("bulk_tickers").fill_null([])))
        .select(UNIVERSE_COLUMNS).sort("cik")
    )  # fmt: skip
    companies.write_parquet(out / "company.parquet")

    aliases = ctx.conn.execute(
        f"select {', '.join(ALIAS_COLUMNS)} from curated.company_alias"  # noqa: S608
    ).fetchall()
    rows = {(r[0], r[1], r[3]): r for r in aliases}
    for cik, former in bulk.select("cik", "former_names").iter_rows():
        for name in former or []:
            alias = (cik, name, normalize_company_name(name), "former_name", None, None)
            rows.setdefault((cik, name, "former_name"), alias)
    pl.DataFrame(sorted(rows.values()), schema={
        "cik": pl.String, "alias": pl.String, "alias_normalized": pl.String,
        "alias_type": pl.String, "valid_from": pl.Date, "valid_to": pl.Date,
    }, orient="row").write_parquet(out / "company_alias.parquet")  # fmt: skip


def _load_universe(conn: psycopg.Connection, bundle: Path, batch: int = 500) -> dict[str, int]:
    """Insert companies / aliases the target lacks (rows the pipeline already wrote win)."""
    counts = {}
    for table, columns, conflict in (
        ("company", UNIVERSE_COLUMNS, "(cik)"),
        ("company_alias", ALIAS_COLUMNS, "(cik, alias_type, alias)"),
    ):
        rows = pl.read_parquet(bundle / f"{table}.parquet").select(columns).rows()
        inserted = 0
        for i in range(0, len(rows), batch):
            with conn.cursor() as cur:
                cur.executemany(
                    f"insert into curated.{table} ({', '.join(columns)}) "  # noqa: S608
                    f"values ({', '.join(['%s'] * len(columns))}) "
                    f"on conflict {conflict} do nothing",
                    rows[i : i + batch],
                )
                inserted += max(cur.rowcount, 0)
        counts[f"{table}_inserted"] = inserted
    return counts


# --- data refresh (shared by seed and the scheduled jobs) ---


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


def served_ciks(conn: psycopg.Connection, statuses: tuple[str, ...]) -> list[str]:
    rows = conn.execute(
        "select cik from ml.served_company where status = any(%s) order by cik", [list(statuses)]
    ).fetchall()
    return [r[0] for r in rows]


def sync_queue(conn: psycopg.Connection) -> list[str]:
    """Queue every matched portfolio company that is not served yet (the API queues on upload;
    this catches anything it missed). Returns the CIKs left out for lack of capacity."""
    ciks = [r[0] for r in conn.execute(
        """select distinct cik from app.portfolio_borrower
           where cik is not null and match_status in ('auto_matched', 'confirmed')""")]  # fmt: skip
    if not ciks:
        return []
    return [r[0] for r in conn.execute("select * from ml.request_serving(%s)", [ciks])]


def prepare_demo(
    storage: Storage, pack: DomainPack, command: str, skip_refresh: bool
) -> tuple[dt.datetime, list[str]]:
    """Create the Storage bucket, pick the companies this command serves and refresh their data.
    Runs before the command opens its own database connection: the refresh takes minutes, and
    a pooled connection left idle that long can be closed under it."""
    from tripwire_common.db import connect

    started = dt.datetime.now(dt.UTC)
    if isinstance(storage, SupabaseStorage):
        storage.ensure_bucket()
    if command == "demo-seed":
        ciks = load_demo_portfolio(pack).ciks()
    else:
        with connect(autocommit=True) as conn:
            if command == "serve-new":
                over = sync_queue(conn)
                if over:
                    log.warning("serving capacity reached", extra={"left_out": len(over)})
                ciks = served_ciks(conn, ("queued",))
            else:
                ciks = sorted(set(load_demo_portfolio(pack).ciks())
                              | set(served_ciks(conn, ("scored", "queued"))))  # fmt: skip
    if ciks and not skip_refresh:
        refresh_data(ciks)
    return started, ciks


def _failed_child_runs(conn: psycopg.Connection, since: dt.datetime) -> list[str]:
    rows = conn.execute(
        """select source || ' #' || id || ': ' || status from raw.ingest_runs
           where started_at >= %s and status <> 'succeeded' and status <> 'running'""",
        [since],
    ).fetchall()
    return [r[0] for r in rows]


# --- demo-seed ---


def cmd_demo_seed(ctx: Context) -> None:
    demo = load_demo_portfolio(ctx.pack)
    bundle = bundle_dir(ctx)
    model_row = json.loads((bundle / "model_version.json").read_text())
    version, prefix = int(model_row["id"]), str(model_row["artifact_path"])
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

        run.counts.update(_load_universe(ctx.conn, bundle))
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
        with ctx.conn.cursor() as cur:
            cur.executemany(
                """insert into ml.served_company (cik, status, last_scored_at)
                   values (%s, 'scored', now()) on conflict (cik) do nothing""",
                [(c,) for c in demo.ciks()],
            )
        run.counts.update({"companies": len(demo.companies), "cases": cases.height,
                           "predictions_inserted": insert_predictions(ctx.conn, rows, version),
                           "model_version": version})  # fmt: skip
        run.counts["alerts_created"] = _generate_alerts(ctx.conn)


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


def _generate_alerts(conn: psycopg.Connection) -> int:
    row = conn.execute("select app.generate_alerts()").fetchone()
    return int(row[0]) if row else 0


def _score_dates(
    ctx: Context, inputs: Inputs, model: TrainedModel, cutoffs: dict[str, list[float]],
    dates: list[dt.date],
) -> tuple[list[dict[str, Any]], dict[dt.date, pl.DataFrame]]:  # fmt: skip
    """Score every company in `inputs` on each date (tiers from that date's saved cutoffs, or
    the latest earlier ones). Returns the rows and each date's feature frame."""
    rows: list[dict[str, Any]] = []
    frames = {}
    for as_of in dates:
        key = max((d for d in cutoffs if d <= as_of.isoformat()), default=None)
        if key is None:
            continue
        now = _not_in_bankruptcy(ctx, features_as_of(inputs, as_of))
        if now.is_empty():
            continue
        frames[as_of] = now
        then = features_as_of(inputs, as_of - YEAR)
        year_ago = {(r["cik"], as_of): {f: r[f] for f in FEATURE_COLUMNS}
                    for r in then.iter_rows(named=True)}  # fmt: skip
        x, _ = _xy(now.with_columns(label=pl.lit(0)))
        tiers = pl.Series([_tier(s, cutoffs[key]) for s in model.raw_score(x)], dtype=pl.String)
        rows += score_rows(now, model, tiers, year_ago)
    return rows, frames


def cmd_serve(ctx: Context, started: dt.datetime, ciks: list[str], job: str,
              as_of: dt.date | None = None) -> None:  # fmt: skip
    """Score `ciks` as of `as_of` (default today); companies still queued (never scored) are
    also scored on every earlier as-of date since BACKFILL_FROM, so they get a score history.
    Then update ml.served_company and create alerts. `started`: when prepare_demo began, so
    failed refresh steps since then fail this command too."""
    as_of = as_of or dt.date.today()
    with ingest_run(ctx.conn, job) as run:
        row = ctx.conn.execute(
            "select id, artifact_path from ml.model_version where is_active"
        ).fetchone()
        if row is None:
            raise RuntimeError("no active model; run demo-seed first")
        version, prefix = int(row[0]), str(row[1])
        excluded = {r[0] for r in ctx.conn.execute(
            "select cik from curated.company where cik = any(%s) and is_excluded", [ciks]
        )}  # fmt: skip
        ciks = [c for c in ciks if c not in excluded]
        queued = [c for c in served_ciks(ctx.conn, ("queued",)) if c in set(ciks)]
        run.counts.update({"model_version": version, "companies": len(ciks),
                           "new_companies": len(queued), "excluded": len(excluded)})  # fmt: skip
        rows: list[dict[str, Any]] = []
        if ciks:
            model = load_model(ctx.storage, prefix)
            cutoffs = json.loads(ctx.storage.read_bytes(f"{prefix}/tier_cutoffs.json"))
            today_rows, frames = _score_dates(ctx, served_inputs(ctx, ciks), model, cutoffs,
                                              [as_of])  # fmt: skip
            rows += today_rows
            if queued:
                past = [r[0] for r in ctx.conn.execute(
                    """select distinct p.as_of_date from ml.prediction p
                       join ml.model_version v on v.id = p.model_version_id and v.is_active
                       where p.as_of_date >= %s and p.as_of_date < %s order by 1""",
                    [BACKFILL_FROM, as_of])]  # fmt: skip
                rows += _score_dates(ctx, served_inputs(ctx, queued), model, cutoffs, past)[0]
            inserted = insert_predictions(ctx.conn, rows, version)
            run.counts.update({"scored_today": len(today_rows), "predictions_inserted": inserted})
            if as_of in frames:
                _snapshot_features(ctx.conn, frames[as_of], as_of)
        _update_served(ctx.conn, ciks, excluded, {r["cik"] for r in rows})
        run.counts["alerts_created"] = _generate_alerts(ctx.conn)
        failed = _failed_child_runs(ctx.conn, started)
        if failed:
            raise RuntimeError("refresh steps did not fully succeed: " + "; ".join(failed))


def _snapshot_features(conn: psycopg.Connection, frame: pl.DataFrame, as_of: dt.date) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """insert into ml.feature_snapshot (cik, as_of_date, feature_set_version, features,
                 max_source_filed_date) values (%s, %s, %s, %s, %s) on conflict do nothing""",
            [(r["cik"], as_of, FEATURE_SET_VERSION,
              Jsonb({f: (None if v is None or v != v else v)
                     for f, v in ((f, r[f]) for f in FEATURE_COLUMNS)}),
              r["max_source_filed_date"]) for r in frame.iter_rows(named=True)],
        )  # fmt: skip


def _update_served(conn: psycopg.Connection, ciks: list[str], excluded: set[str],
                   scored: set[str]) -> None:  # fmt: skip
    """Scored now or earlier -> scored; never scored -> no_data; excluded sector -> excluded."""
    has_history = {r[0] for r in conn.execute(
        """select distinct p.cik from ml.prediction p
           join ml.model_version v on v.id = p.model_version_id and v.is_active
           where p.cik = any(%s)""", [ciks])}  # fmt: skip
    for cik in ciks:
        if cik in scored or cik in has_history:
            conn.execute(
                """update ml.served_company set status = 'scored', detail = null,
                     last_scored_at = case when %s then now() else last_scored_at end
                   where cik = %s""",
                [cik in scored, cik],
            )
        else:
            conn.execute(
                """update ml.served_company set status = 'no_data',
                     detail = 'no recent XBRL financial statements to score' where cik = %s""",
                [cik],
            )
    if excluded:
        conn.execute(
            """update ml.served_company set status = 'excluded',
                 detail = 'sector the model does not cover (banks, insurers, funds...)'
               where cik = any(%s)""",
            [sorted(excluded)],
        )
