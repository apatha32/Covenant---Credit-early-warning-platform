"""ML commands. Inputs come from the training universe Parquet built by
`python -m tripwire_pipeline training-data`.

    uv run python -m tripwire_ml labels      # classify Item 1.03 8-Ks, build bankruptcy cases
    uv run python -m tripwire_ml dataset     # point-in-time features + labels, quarterly
    uv run python -m tripwire_ml train       # 3 models, evaluation, backtest, model card
    uv run python -m tripwire_ml demo-export # hosted demo bundle (see tripwire_ml.serve)
    uv run python -m tripwire_ml demo-seed   # load it into DATABASE_URL / TRIPWIRE_STORAGE
    uv run python -m tripwire_ml demo-refresh  # weekly: refresh SEC data, re-score as of today
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl
import psycopg

from tripwire_common.config import (
    DomainPack,
    SourcesConfig,
    load_domain_pack,
    load_models_config,
    load_sources_config,
)
from tripwire_common.db import connect
from tripwire_common.logging import configure_logging
from tripwire_common.paths import training_root
from tripwire_common.runs import ingest_run
from tripwire_common.settings import Settings, get_settings
from tripwire_common.storage import Storage, get_storage
from tripwire_ingest.http import HttpClient
from tripwire_ingest.sec import sec_client
from tripwire_ml.dataset import build_dataset, leakage_check
from tripwire_ml.documents import fetch_document, html_to_text
from tripwire_ml.features import FEATURE_SET_VERSION, Inputs, prepare_filings, quarter_ends
from tripwire_ml.labels import Case, TextRules, build_cases, primary_items
from tripwire_ml.macro import load_macro, store_macro

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Context:
    conn: psycopg.Connection
    settings: Settings
    sources: SourcesConfig
    pack: DomainPack
    storage: Storage

    def sec(self) -> HttpClient:
        return sec_client(self.settings, self.sources.sec)

    @property
    def root(self) -> Path:
        return training_root(self.pack.metrics.mapping_version, self.settings)


def cmd_labels(ctx: Context) -> None:
    """Classify every primary-label filing of the universe from its text; write events + cases."""
    rules_config = ctx.pack.labels.text_check
    if rules_config is None:
        raise SystemExit("labels.yaml has no text_check section")
    rules = TextRules(rules_config)
    companies = pl.read_parquet(ctx.root / "companies.parquet")
    names = {r["cik"]: [r["name"], *r["former_names"]] for r in companies.iter_rows(named=True)}
    operating = set(companies["cik"])
    universe = companies.filter(~pl.col("is_excluded")).select("cik")
    items = primary_items(ctx.pack)
    item_match = (
        pl.col("items")
        .fill_null("")
        .str.split(",")
        .list.eval(pl.element().str.strip_chars().is_in(items))
        .list.any()
    )
    candidates = (
        pl.scan_parquet(ctx.root / "filings" / "*.parquet")
        .filter(pl.col("form").is_in(["8-K", "8-K/A"]) & item_match)
        .join(universe.lazy(), on="cik", how="semi")
        .unique(subset=["cik", "accessionNumber"])
        .sort("cik", "filingDate")
        .collect()
    )
    records = []
    with ctx.sec() as client, ingest_run(ctx.conn, "ml_labels", [client]) as run:
        run.counts["candidates"] = candidates.height
        for row in candidates.iter_rows(named=True):
            cik, accession = row["cik"], row["accessionNumber"]
            try:
                body = fetch_document(ctx.conn, ctx.storage, client, ctx.sources.sec, cik,
                                      accession, row["primaryDocument"], run)  # fmt: skip
                filed = pl.Series([row["filingDate"]]).str.to_date()[0]
                if body is None:
                    kind, ends, snippet = "unclear", False, "document missing on EDGAR"
                else:
                    strict = accession[:10] != cik and accession[:10] in operating
                    c = rules.classify(html_to_text(body), names[cik], filed, strict)
                    kind, ends, snippet = c.kind, c.ends_case, c.snippet
            except Exception as exc:
                run.item_failed(f"{cik} {accession}", exc)
                continue
            run.counts[f"kind_{kind}"] += 1
            records.append({"cik": cik, "accession": accession, "filed_date": filed,
                            "form": row["form"], "kind": kind, "ends_case": ends,
                            "snippet": snippet})  # fmt: skip
        events = pl.DataFrame(records, schema_overrides={"filed_date": pl.Date})
        cases = build_cases(events, ctx.pack.labels)
        run.counts["cases"] = len(cases)
        out = ctx.root / "labels"
        out.mkdir(exist_ok=True)
        events.write_parquet(out / "item_events.parquet")
        pl.DataFrame(
            {"cik": [c.cik for c in cases], "case_start": [c.start for c in cases],
             "case_end": [c.end for c in cases], "accession": [c.accession for c in cases]},
            schema={"cik": pl.String, "case_start": pl.Date, "case_end": pl.Date,
                    "accession": pl.String},
        ).write_parquet(out / "cases.parquet")  # fmt: skip


def load_inputs(ctx: Context) -> Inputs:
    companies = pl.read_parquet(ctx.root / "companies.parquet")
    return Inputs(
        metrics=pl.scan_parquet(ctx.root / "metrics" / "*.parquet"),
        filings=prepare_filings(pl.scan_parquet(ctx.root / "filings" / "*.parquet")),
        macro=load_macro(ctx.conn, ctx.storage, ctx.pack.macro),
        universe=frozenset(companies.filter(~pl.col("is_excluded"))["cik"]),
    )


def data_cutoff(ctx: Context) -> dt.date:
    """When the bulk data was fetched: nothing later is known to the training set."""
    row = ctx.conn.execute(
        """select max(fetched_at)::date from raw.source_files
           where source = 'sec_bulk' and source_key = 'submissions'"""
    ).fetchone()
    if row is None or row[0] is None:
        raise SystemExit("no bulk submissions landed")
    return row[0]  # type: ignore[no-any-return]


def read_cases(ctx: Context) -> list[Case]:
    frame = pl.read_parquet(ctx.root / "labels" / "cases.parquet")
    return [Case(r["cik"], r["case_start"], r["case_end"], r["accession"])
            for r in frame.iter_rows(named=True)]  # fmt: skip


def cmd_dataset(ctx: Context) -> None:
    ml = load_models_config().ml
    cutoff = data_cutoff(ctx)
    dates = quarter_ends(ml.first_as_of, cutoff)
    with ingest_run(ctx.conn, "ml_dataset") as run:
        inputs = load_inputs(ctx)
        store_macro(ctx.conn, inputs.macro)
        data = build_dataset(inputs, dates, read_cases(ctx), ctx.pack.labels, cutoff)
        out = ctx.root / "datasets" / f"feature_set={FEATURE_SET_VERSION}"
        out.mkdir(parents=True, exist_ok=True)
        data.write_parquet(out / "dataset.parquet")
        run.counts.update({
            "as_of_dates": len(dates), "rows": data.height,
            "labelled_rows": data.filter(pl.col("label_complete")).height,
            "positive_rows": int(data.filter(pl.col("label_complete"))["label"].sum()),
            "companies": data["cik"].n_unique(),
        })  # fmt: skip
        for as_of in (dates[len(dates) // 5], dates[len(dates) // 2], dates[-6]):
            result = leakage_check(inputs, as_of)
            run.counts[f"leakage_{as_of.isoformat()}_mismatched"] = result.mismatched_rows
            if not result.passed:
                raise RuntimeError(f"leakage check failed on {as_of}: {result}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tripwire_ml",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command", choices=["labels", "dataset", "train", "demo-export", "demo-seed",
                            "demo-refresh"],
    )  # fmt: skip
    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help="demo-seed / demo-refresh: do not re-fetch SEC and FRED data",
    )
    parser.add_argument("--as-of", type=dt.date.fromisoformat,
                        help="demo-refresh: score as of this date (default: today)")  # fmt: skip
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging("ml")
    with connect(autocommit=True) as conn:
        ctx = Context(conn, settings, load_sources_config(), load_domain_pack(),
                      get_storage(settings))  # fmt: skip
        if args.command == "labels":
            cmd_labels(ctx)
        elif args.command == "dataset":
            cmd_dataset(ctx)
        elif args.command == "train":
            from tripwire_ml.report import cmd_train

            cmd_train(ctx)
        elif args.command == "demo-export":
            from tripwire_ml.serve import cmd_demo_export

            cmd_demo_export(ctx)
        elif args.command == "demo-seed":
            from tripwire_ml.serve import cmd_demo_seed

            cmd_demo_seed(ctx, args.skip_refresh)
        elif args.command == "demo-refresh":
            from tripwire_ml.serve import cmd_demo_refresh

            cmd_demo_refresh(ctx, args.as_of, args.skip_refresh)
