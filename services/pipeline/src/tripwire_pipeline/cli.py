"""Pipeline commands (raw -> staging -> curated). Each step is idempotent and logs a run.

    uv run python -m tripwire_pipeline all          # staging, curated, events, coverage
    uv run python -m tripwire_pipeline staging      # raw companyfacts -> staging.facts
    uv run python -m tripwire_pipeline curated      # staging.facts -> curated.financial_metric
    uv run python -m tripwire_pipeline events       # raw submissions -> staging.filings + events
    uv run python -m tripwire_pipeline coverage     # reports/metric_coverage.md

Default scope is the dev sample; pass --cik to restrict.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from dataclasses import dataclass

import psycopg

from tripwire_common.config import DomainPack, load_dev_sample, load_domain_pack
from tripwire_common.db import connect
from tripwire_common.logging import configure_logging
from tripwire_common.raw import latest_cik_path, read_parquet
from tripwire_common.runs import ingest_run
from tripwire_common.settings import REPO_ROOT, get_settings
from tripwire_common.storage import Storage, get_storage
from tripwire_pipeline import load
from tripwire_pipeline.coverage import render_coverage
from tripwire_pipeline.events import extract_events, typed_filings
from tripwire_pipeline.metrics import build_rows, resolve, scale_outliers
from tripwire_pipeline.staging import SOURCE as STAGING_SOURCE
from tripwire_pipeline.staging import stage_facts

log = logging.getLogger(__name__)

COVERAGE_PATH = REPO_ROOT / "reports" / "metric_coverage.md"
CURATED_SOURCE = "pipeline_curated"


@dataclass(frozen=True)
class Context:
    conn: psycopg.Connection
    pack: DomainPack
    storage: Storage
    ciks: list[str]


def cmd_staging(ctx: Context) -> None:
    with ingest_run(ctx.conn, STAGING_SOURCE) as run:
        for cik in ctx.ciks:
            try:
                path = latest_cik_path(ctx.conn, "companyfacts", cik)
                if path is None:
                    run.counts["no_companyfacts"] += 1
                    continue
                staged = stage_facts(read_parquet(ctx.storage, f"{path}/facts.parquet"),
                                     ctx.pack.metrics)  # fmt: skip
                run.counts.update(staged.counts)
                run.counts["facts_inserted"] += load.load_staged_facts(
                    ctx.conn, staged.facts, run.id
                )
                run.counts["quarantined_new"] += load.quarantine(
                    ctx.conn, STAGING_SOURCE, staged.quarantine, run.id
                )
                run.counts["companies"] += 1
            except Exception as exc:
                run.item_failed(cik, exc)


def cmd_curated(ctx: Context) -> None:
    mapping = ctx.pack.metrics
    with ingest_run(ctx.conn, CURATED_SOURCE) as run:
        run.counts["mapping_version"] = mapping.version
        for cik in ctx.ciks:
            try:
                facts = load.read_staged_facts(ctx.conn, cik)
                if facts.height == 0:
                    run.counts["no_staged_facts"] += 1
                    continue
                resolution = resolve(facts, mapping)
                outliers = scale_outliers(resolution.observations, mapping)
                run.counts["quarantined_new"] += load.quarantine(
                    ctx.conn, CURATED_SOURCE,
                    [("scale_outlier", {**o.lineage(), "cik": o.cik, "metric": o.metric})
                     for o in outliers], run.id,
                )  # fmt: skip
                dropped = set(outliers)
                observations = [o for o in resolution.observations if o not in dropped]
                rows = build_rows(observations, mapping, resolution.fy_ends)
                run.counts.update(
                    load.load_metric_rows(ctx.conn, cik, rows, mapping.mapping_version)
                )
                run.counts["observations"] += len(observations)
                run.counts["scale_outliers"] += len(outliers)
                run.counts["unclassified_durations"] += resolution.unclassified
                run.counts["unaligned_instants"] += resolution.unaligned_instants
                run.counts["companies"] += 1
            except Exception as exc:
                run.item_failed(cik, exc)


def cmd_events(ctx: Context) -> None:
    with ingest_run(ctx.conn, "pipeline_events") as run:
        for cik in ctx.ciks:
            try:
                path = latest_cik_path(ctx.conn, "submissions", cik)
                if path is None:
                    run.counts["no_submissions"] += 1
                    continue
                filings = typed_filings(read_parquet(ctx.storage, f"{path}/filings.parquet"))
                run.counts["filings"] += filings.height
                run.counts["filings_inserted"] += load.load_filings(ctx.conn, filings, run.id)
                events = extract_events(filings, ctx.pack.event_codes)
                run.counts["events"] += len(events)
                run.counts["events_inserted"] += load.load_events(ctx.conn, events)
                for event in events:
                    run.counts[f"event_{event['event_type']}"] += 1
                run.counts["companies"] += 1
            except Exception as exc:
                run.item_failed(cik, exc)


def cmd_coverage(ctx: Context) -> None:
    COVERAGE_PATH.parent.mkdir(exist_ok=True)
    COVERAGE_PATH.write_text(render_coverage(ctx.conn, ctx.ciks, ctx.pack.metrics, dt.date.today()))
    log.info("coverage report written", extra={"path": str(COVERAGE_PATH)})


COMMANDS = {"staging": cmd_staging, "curated": cmd_curated, "events": cmd_events,
            "coverage": cmd_coverage}  # fmt: skip


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tripwire_pipeline",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["all", *COMMANDS])
    parser.add_argument(
        "--cik", type=int, nargs="+", help="specific CIKs instead of the dev sample"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("pipeline")
    pack = load_domain_pack()
    ciks = [f"{c:010d}" for c in (args.cik or load_dev_sample(pack).ciks())]
    with connect(autocommit=True) as conn:
        ctx = Context(conn, pack, get_storage(settings), ciks)
        steps = list(COMMANDS) if args.command == "all" else [args.command]
        for step in steps:
            COMMANDS[step](ctx)
