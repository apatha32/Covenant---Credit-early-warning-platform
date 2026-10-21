"""Ingest commands. Every command is idempotent and writes one raw.ingest_runs row per source.

uv run python -m tripwire_ingest dev            # everything below, in order
uv run python -m tripwire_ingest tickers
uv run python -m tripwire_ingest build-sample   # once; writes <pack>/dev_sample.yaml
uv run python -m tripwire_ingest submissions [--cik 320193 ...]   (default: dev sample)
uv run python -m tripwire_ingest companyfacts [--cik ...]
uv run python -m tripwire_ingest fred
uv run python -m tripwire_ingest scan           # Item 1.03 spot-check report
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass

import httpx
import polars as pl
import psycopg

from tripwire_common.config import (
    DomainPack,
    SourcesConfig,
    dev_sample_path,
    load_dev_sample,
    load_domain_pack,
    load_sources_config,
)
from tripwire_common.db import connect
from tripwire_common.logging import configure_logging
from tripwire_common.runs import ingest_run
from tripwire_common.settings import REPO_ROOT, Settings, get_settings
from tripwire_common.storage import Storage, get_storage
from tripwire_ingest.company import upsert_from_submissions, upsert_from_tickers
from tripwire_ingest.fred import fetch_series, fred_client
from tripwire_ingest.http import HttpClient
from tripwire_ingest.landing import (
    land_companyfacts,
    land_fred_series,
    land_submissions,
    land_tickers,
)
from tripwire_ingest.sample import build_dev_sample, write_dev_sample
from tripwire_ingest.scan import primary_event_items, render_report, spot_check
from tripwire_ingest.sec import (
    cik10,
    fetch_companyfacts,
    fetch_submissions,
    fetch_tickers,
    sec_client,
)

log = logging.getLogger(__name__)

REPORT_PATH = REPO_ROOT / "reports" / "item_103_spotcheck.md"


@dataclass(frozen=True)
class Context:
    conn: psycopg.Connection
    settings: Settings
    sources: SourcesConfig
    pack: DomainPack
    storage: Storage
    # Tests inject fake transports; production uses the network.
    sec_transport: httpx.BaseTransport | None = None
    fred_transport: httpx.BaseTransport | None = None

    def sec(self) -> HttpClient:
        return sec_client(self.settings, self.sources.sec, transport=self.sec_transport)

    def fred(self) -> HttpClient:
        return fred_client(self.settings, self.sources.fred, transport=self.fred_transport)


def cmd_tickers(ctx: Context) -> pl.DataFrame:
    with (
        ctx.sec() as client,
        ingest_run(ctx.conn, "sec_tickers", [client]) as run,
    ):
        _, frame = land_tickers(ctx.conn, ctx.storage, fetch_tickers(client, ctx.sources.sec), run)
        run.counts["companies"] = upsert_from_tickers(ctx.conn, frame)
    return frame


def cmd_build_sample(ctx: Context, n: int, seed: int, force: bool) -> None:
    path = dev_sample_path(ctx.pack)
    if path.exists() and not force:
        raise SystemExit(f"{path} exists; pass --force to regenerate the dev sample")
    tickers = cmd_tickers(ctx)
    with (
        ctx.sec() as client,
        ingest_run(ctx.conn, "sec_submissions", [client]) as run,
    ):
        sample = build_dev_sample(
            ctx.conn, ctx.storage, client, ctx.sources.sec, ctx.pack, tickers, run, n=n, seed=seed
        )
    write_dev_sample(sample, path)
    log.info("dev sample written", extra={"path": str(path), "companies": len(sample.ciks())})


def _target_ciks(ctx: Context, ciks: list[int] | None) -> list[str]:
    return [cik10(c) for c in (ciks or load_dev_sample(ctx.pack).ciks())]


def cmd_submissions(ctx: Context, ciks: list[int] | None) -> None:
    targets = _target_ciks(ctx, ciks)
    with (
        ctx.sec() as client,
        ingest_run(ctx.conn, "sec_submissions", [client]) as run,
    ):
        run.counts["companies_requested"] = len(targets)
        for cik in targets:
            try:
                payload = fetch_submissions(client, ctx.sources.sec, cik)
                if payload is None:
                    run.counts["not_found"] += 1
                    continue
                _, company, _ = land_submissions(ctx.conn, ctx.storage, payload, run)
                upsert_from_submissions(ctx.conn, company, ctx.pack)
            except Exception as exc:
                run.item_failed(cik, exc)


def cmd_companyfacts(ctx: Context, ciks: list[int] | None) -> None:
    targets = _target_ciks(ctx, ciks)
    with (
        ctx.sec() as client,
        ingest_run(ctx.conn, "sec_companyfacts", [client]) as run,
    ):
        run.counts["companies_requested"] = len(targets)
        for cik in targets:
            try:
                payload = fetch_companyfacts(client, ctx.sources.sec, cik)
                if payload is None:
                    run.counts["no_xbrl_data"] += 1
                    continue
                land_companyfacts(ctx.conn, ctx.storage, payload, run)
            except Exception as exc:
                run.item_failed(cik, exc)


def cmd_fred(ctx: Context) -> None:
    with (
        ctx.fred() as client,
        ingest_run(ctx.conn, "fred", [client]) as run,
    ):
        run.counts["series_requested"] = len(ctx.pack.macro.series)
        for series in ctx.pack.macro.series:
            try:
                payload = fetch_series(client, ctx.settings, ctx.sources.fred, series)
                land_fred_series(ctx.conn, ctx.storage, payload, run)
            except Exception as exc:
                run.item_failed(series.id, exc)


def cmd_scan(ctx: Context) -> None:
    sample = load_dev_sample(ctx.pack)
    with ingest_run(ctx.conn, "item_scan") as run:
        results = spot_check(ctx.conn, ctx.storage, sample, ctx.pack, run)
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(render_report(results, sample, primary_event_items(ctx.pack)))
    log.info("spot-check report written", extra={"path": str(REPORT_PATH)})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tripwire_ingest",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "dev", help="tickers, sample (if missing), submissions, companyfacts, fred, scan"
    )
    sub.add_parser("tickers", help="land the SEC ticker map and load curated.company")
    sample = sub.add_parser("build-sample", help="choose the dev sample (once)")
    sample.add_argument("--n", type=int, default=180, help="random companies to draw")
    sample.add_argument("--seed", type=int, default=42)
    sample.add_argument("--force", action="store_true", help="overwrite an existing dev sample")
    for name in ("submissions", "companyfacts"):
        p = sub.add_parser(name, help=f"land SEC {name} (default: the dev sample)")
        p.add_argument("--cik", type=int, nargs="+", help="specific CIKs instead of the dev sample")
    sub.add_parser("fred", help="land the domain pack's FRED series")
    sub.add_parser("scan", help="scan for primary-label 8-K items and write the spot-check report")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("ingest")
    with connect(autocommit=True) as conn:
        ctx = Context(
            conn, settings, load_sources_config(), load_domain_pack(), get_storage(settings)
        )
        if args.command == "dev":
            if not dev_sample_path(ctx.pack).exists():
                cmd_build_sample(ctx, n=180, seed=42, force=False)
            else:
                cmd_tickers(ctx)
            cmd_submissions(ctx, None)
            cmd_companyfacts(ctx, None)
            cmd_fred(ctx)
            cmd_scan(ctx)
        elif args.command == "tickers":
            cmd_tickers(ctx)
        elif args.command == "build-sample":
            cmd_build_sample(ctx, args.n, args.seed, args.force)
        elif args.command == "submissions":
            cmd_submissions(ctx, args.cik)
        elif args.command == "companyfacts":
            cmd_companyfacts(ctx, args.cik)
        elif args.command == "fred":
            cmd_fred(ctx)
        elif args.command == "scan":
            cmd_scan(ctx)
