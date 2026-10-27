"""Build the training universe from SEC bulk zips (submissions.zip, companyfacts.zip).

The zips stay in raw storage as the immutable source; this module streams them one company at a
time (they are 6 GB and 19 GB unzipped) and writes Parquet under the training directory:

  companies.parquet        one row per operating filer (has 10-K / 10-Q filings)
  filings/part-*.parquet   10-K, 10-Q, 8-K and NT filings of those companies (SEC field names)
  metrics/part-*.parquet   canonical metrics, built by the same Phase 2 code as the served path

Metric rows keep only versions whose value changed: an identical re-report (a comparative column
in a later filing) carries no new point-in-time information. The first report and every
restatement are kept, each with its filed_date and accession.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import polars as pl

from tripwire_common.config import DomainPack, MetricMapping
from tripwire_ingest.sec import FILING_COLUMNS, RawPayload, cik10, parse_companyfacts
from tripwire_pipeline.metrics import MetricRow, build_rows, resolve, scale_outliers
from tripwire_pipeline.staging import stage_facts

log = logging.getLogger(__name__)

REPORT_FORMS = ("10-K", "10-K/A", "10-K405", "10-KT", "10-KT/A", "10-Q", "10-Q/A")
KEPT_FORMS = (*REPORT_FORMS, "8-K", "8-K/A", "NT 10-K", "NT 10-Q", "NT 10-K/A", "NT 10-Q/A")
_MAIN = re.compile(r"^CIK(\d{10})\.json$")
_PAGE = re.compile(r"^CIK(\d{10})-submissions-\d+\.json$")
FILING_FIELDS = ["accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form",
                 "items", "primaryDocument", "isXBRL"]  # fmt: skip
METRIC_SCHEMA = {
    "cik": pl.String, "metric": pl.String, "period_type": pl.String, "period_start": pl.Date,
    "period_end": pl.Date, "value": pl.Float64, "source_tag": pl.String, "is_derived": pl.Boolean,
    "filed_date": pl.Date, "accession": pl.String, "version": pl.Int32,
}  # fmt: skip


# --- submissions ---


def _filings_frame(cik: str, columns: dict[str, list[Any]]) -> pl.DataFrame:
    n = len(columns.get("form", []))
    data = {"cik": [cik] * n}
    for field in FILING_FIELDS:
        data[field] = columns.get(field, [None] * n)
    schema = {"cik": pl.String} | {f: FILING_COLUMNS[f] for f in FILING_FIELDS}
    frame = pl.DataFrame(data, schema=schema)
    return frame.filter(pl.col("form").is_in(KEPT_FORMS))


def build_submissions(
    zip_path: Path, out_dir: Path, pack: DomainPack, batch: int = 5000
) -> Counter[str]:
    """companies.parquet + filings/part-*.parquet for every company that files 10-K / 10-Q."""
    counts: Counter[str] = Counter()
    (out_dir / "filings").mkdir(parents=True, exist_ok=True)
    companies: list[dict[str, Any]] = []
    pending: list[pl.DataFrame] = []
    pages: dict[str, list[str]] = {}
    part = 0

    def flush() -> None:
        nonlocal part, pending
        if pending:
            pl.concat(pending).write_parquet(out_dir / "filings" / f"part-{part:05d}.parquet")
            part += 1
            pending = []

    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if (m := _PAGE.match(info.filename)) is not None:
                pages.setdefault(m.group(1), []).append(info.filename)
                continue
            if (m := _MAIN.match(info.filename)) is None:
                continue
            counts["entities"] += 1
            body = zf.read(info)
            # Cheap pre-filter: most entities (people, funds) never file a 10-K or 10-Q.
            if b'"10-K' not in body and b'"10-Q' not in body:
                continue
            data = json.loads(body)
            cik = cik10(data["cik"])
            filings = _filings_frame(cik, data.get("filings", {}).get("recent", {}))
            if filings.filter(pl.col("form").is_in(REPORT_FORMS)).height == 0:
                continue
            counts["operating_filers"] += 1
            sic = int(data["sic"]) if str(data.get("sic") or "").isdigit() else None
            companies.append({
                "cik": cik, "name": data.get("name"), "entity_type": data.get("entityType"),
                "sic": sic, "sic_description": data.get("sicDescription"),
                "fiscal_year_end": data.get("fiscalYearEnd"),
                "tickers": [str(t) for t in data.get("tickers") or []],
                "former_names": [f["name"] for f in data.get("formerNames") or [] if f.get("name")],
                "exclusion_reason": pack.exclusions.exclusion_reason(sic),
                "has_pages": cik in pages or bool(data["filings"].get("files")),
            })  # fmt: skip
            pending.append(filings)
            if len(pending) >= batch:
                flush()
        flush()
        # Older filings live in separate page files; add them for the companies we kept.
        kept = {c["cik"] for c in companies}
        for cik, names in pages.items():
            if cik not in kept:
                continue
            for name in names:
                pending.append(_filings_frame(cik, json.loads(zf.read(name))))
                counts["pages"] += 1
        flush()

    frame = pl.DataFrame(companies, schema_overrides={"sic": pl.Int32})
    frame.with_columns(is_excluded=pl.col("exclusion_reason").is_not_null()).write_parquet(
        out_dir / "companies.parquet"
    )
    counts["excluded"] = frame.filter(pl.col("exclusion_reason").is_not_null()).height
    counts["filing_parts"] = part
    return counts


# --- companyfacts -> canonical metrics ---

_MAPPING: MetricMapping | None = None


def _init_worker(mapping: MetricMapping) -> None:
    global _MAPPING
    _MAPPING = mapping


def collapse_unchanged(rows: list[MetricRow]) -> list[MetricRow]:
    """Drop re-reports whose value equals the previously known version of the same period."""
    kept: list[MetricRow] = []
    last: dict[tuple[str, str, Any], float] = {}
    for row in sorted(rows, key=lambda r: (r.metric, r.period_type, r.period_end, r.filed_date,
                                           r.accession)):  # fmt: skip
        key = (row.metric, row.period_type, row.period_end)
        if key in last and last[key] == row.value:
            continue
        last[key] = row.value
        kept.append(row)
    return kept


def company_metrics(cik: str, body: bytes) -> tuple[pl.DataFrame, Counter[str]]:
    """One company's companyfacts JSON -> canonical metric rows (Phase 2 logic, unchanged)."""
    assert _MAPPING is not None, "worker not initialised"
    counts: Counter[str] = Counter()
    facts, _ = parse_companyfacts(RawPayload(cik, body))
    staged = stage_facts(facts, _MAPPING)
    for rule, _ in staged.quarantine:
        counts[f"quarantined_{rule}"] += 1
    resolution = resolve(staged.facts, _MAPPING)
    outliers = set(scale_outliers(resolution.observations, _MAPPING))
    counts["scale_outliers"] += len(outliers)
    observations = [o for o in resolution.observations if o not in outliers]
    rows = collapse_unchanged(build_rows(observations, _MAPPING, resolution.fy_ends))
    counts["metric_rows"] += len(rows)
    version: Counter[tuple[str, str, Any]] = Counter()
    records = []
    for r in rows:
        version[(r.metric, r.period_type, r.period_end)] += 1
        records.append({
            "cik": cik, "metric": r.metric, "period_type": r.period_type,
            "period_start": r.period_start, "period_end": r.period_end, "value": r.value,
            "source_tag": r.source_tag, "is_derived": r.is_derived, "filed_date": r.filed_date,
            "accession": r.accession, "version": version[(r.metric, r.period_type, r.period_end)],
        })  # fmt: skip
    return pl.DataFrame(records, schema=METRIC_SCHEMA), counts


def _safe_company_metrics(
    item: tuple[str, bytes],
) -> tuple[str, pl.DataFrame | None, Counter[str], str | None]:
    cik, body = item
    try:
        frame, counts = company_metrics(cik, body)
        return cik, frame, counts, None
    except Exception as exc:  # one malformed company must not stop the build
        return cik, None, Counter(), f"{type(exc).__name__}: {exc}"


def _iter_companyfacts(zip_path: Path, ciks: set[str]) -> Iterator[tuple[str, bytes]]:
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            m = _MAIN.match(info.filename)
            if m and m.group(1) in ciks:
                yield m.group(1), zf.read(info)


def build_metrics(
    zip_path: Path,
    out_dir: Path,
    ciks: set[str],
    mapping: MetricMapping,
    workers: int = 6,
    batch: int = 200,
) -> tuple[Counter[str], list[tuple[str, str]]]:
    """metrics/part-*.parquet for the given companies. Returns (counts, [(cik, error)])."""
    metrics_dir = out_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    errors: list[tuple[str, str]] = []
    part = 0
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(mapping,)) as pool:  # fmt: skip
        items = _iter_companyfacts(zip_path, ciks)
        while True:
            chunk = [item for _, item in zip(range(batch), items, strict=False)]
            if not chunk:
                break
            frames = []
            for cik, frame, c, error in pool.map(_safe_company_metrics, chunk):
                counts.update(c)
                if error:
                    errors.append((cik, error))
                    counts["failed"] += 1
                elif frame is not None:
                    counts["companies"] += 1
                    frames.append(frame)
            if frames:
                pl.concat(frames).write_parquet(metrics_dir / f"part-{part:05d}.parquet")
                part += 1
            log.info(
                "metrics batch written", extra={"part": part, "companies": counts["companies"]}
            )
    return counts, errors
