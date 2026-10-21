"""Primary-label event scan (8-K Item 1.03 in the finance pack) and its spot-check.

Labels are derived only from this scan, never from a hand-kept list. The spot-check compares
scan results with the known bankruptcies and records anything surprising in staging.quarantine
as a data issue for a human to review. Nothing is fixed silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from tripwire_common.config import DevSample, DomainPack
from tripwire_common.raw import latest_cik_path, read_parquet
from tripwire_common.runs import RunLog
from tripwire_common.storage import Storage
from tripwire_ingest.sec import cik10

CURRENT_REPORT_FORMS = ("8-K", "8-K/A")


def primary_event_items(pack: DomainPack) -> list[str]:
    """8-K item codes whose event type is a primary label (e.g. ['1.03'])."""
    primary = set(pack.labels.primary_events)
    return sorted(code for code, ev in pack.event_codes.eight_k_items.items()
                  if ev.event_type in primary)  # fmt: skip


def item_hits(filings: pl.DataFrame, items: list[str]) -> pl.DataFrame:
    """8-K / 8-K/A filings whose comma-separated `items` include any of `items`."""
    parsed = pl.col("items").fill_null("").str.split(",").list.eval(pl.element().str.strip_chars())
    return (
        filings.filter(
            pl.col("form").is_in(CURRENT_REPORT_FORMS)
            & parsed.list.eval(pl.element().is_in(items)).list.any()
        )
        .select("cik", "accessionNumber", "filingDate", "form", "items")
        .unique(subset=["cik", "accessionNumber"])
        # Accession breaks same-day ties, so repeated scans produce identical records.
        .sort("cik", "filingDate", "accessionNumber")
    )


@dataclass
class CompanyResult:
    cik: str
    name: str
    group: str  # known_bankruptcy | random
    petition_year: int | None
    is_excluded: bool
    hits: list[dict[str, str]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _load_filings(conn: psycopg.Connection, storage: Storage, cik: str) -> pl.DataFrame | None:
    path = latest_cik_path(conn, "submissions", cik)
    if path is None:
        return None
    return read_parquet(storage, f"{path}/filings.parquet")


def spot_check(
    conn: psycopg.Connection, storage: Storage, sample: DevSample, pack: DomainPack, run: RunLog
) -> list[CompanyResult]:
    items = primary_event_items(pack)
    known = {k.cik: k for k in pack.known_bankruptcies.companies}
    results: list[CompanyResult] = []
    for cik_int in sample.ciks():
        cik = cik10(cik_int)
        row = conn.execute(
            "select name, is_excluded from curated.company where cik = %s", [cik]
        ).fetchone()
        filings = _load_filings(conn, storage, cik)
        if row is None or filings is None:
            run.item_failed(cik, RuntimeError("submissions not ingested; run `submissions` first"))
            continue
        k = known.get(cik_int)
        result = CompanyResult(
            cik=cik, name=str(row[0]), group="known_bankruptcy" if k else "random",
            petition_year=k.petition_year if k else None, is_excluded=bool(row[1]),
        )  # fmt: skip
        result.hits = item_hits(filings, items).to_dicts()
        run.counts["companies_scanned"] += 1
        run.counts["item_hits"] += len(result.hits)

        years = {int(h["filingDate"][:4]) for h in result.hits}
        if k and k.petition_year not in years:
            result.issues.append("known_bankruptcy_missing_item_in_petition_year")
        if k and result.is_excluded:
            result.issues.append("known_bankruptcy_excluded_by_sector_rule")
        if not k and result.hits:
            result.issues.append("item_hit_outside_known_list")
        if any(h["form"] == "8-K/A" for h in result.hits):
            result.issues.append("item_hit_on_amended_8k")
        for issue in result.issues:
            _quarantine(conn, run, issue, result)
        results.append(result)
    return results


def _quarantine(conn: psycopg.Connection, run: RunLog, rule: str, result: CompanyResult) -> None:
    record = {"cik": result.cik, "name": result.name, "group": result.group,
              "petition_year": result.petition_year, "hits": result.hits}  # fmt: skip
    # Re-running the scan must not pile up duplicates of the same issue.
    inserted = conn.execute(
        """insert into staging.quarantine (source, record, rule_violated, run_id)
           select 'item_scan', %s, %s, %s
           where not exists (select 1 from staging.quarantine
                             where source = 'item_scan' and rule_violated = %s and record = %s)""",
        [Jsonb(record), rule, run.id, rule, Jsonb(record)],
    )
    run.counts[f"issue_{rule}"] += 1
    run.counts["quarantined_new"] += inserted.rowcount


def render_report(results: list[CompanyResult], sample: DevSample, items: list[str]) -> str:
    known = [r for r in results if r.group == "known_bankruptcy"]
    random_hits = [r for r in results if r.group == "random" and r.hits]
    found = [r for r in known if "known_bankruptcy_missing_item_in_petition_year" not in r.issues]
    lines = [
        f"# Item {', '.join(items)} scan: dev sample spot-check",
        "",
        "Generated by `python -m tripwire_ingest scan`. Data issues found here are also written",
        "to `staging.quarantine` (source `item_scan`). Nothing was corrected automatically.",
        "",
        f"- Dev sample: {len(sample.random)} random filers + {len(sample.known_bankruptcies)} "
        f"known bankruptcies (generated {sample.generated_on}, seed {sample.seed})",
        f"- Companies scanned: {len(results)}",
        f"- Known bankruptcies with an Item {'/'.join(items)} filing in the petition year: "
        f"{len(found)} / {len(known)}",
        f"- Random-sample companies with any hit: {len(random_hits)}",
        "",
        "## Known bankruptcies",
        "",
        "| CIK | Name (current, per SEC) | Petition year | Hits (date, form) | Issues |",
        "|---|---|---|---|---|",
    ]
    for r in known:
        lines.append(_row(r))
    lines += ["", "## Random-sample companies with hits (need review)", ""]
    if random_hits:
        lines += ["| CIK | Name | - | Hits | Issues |", "|---|---|---|---|---|"]
        lines += [_row(r) for r in random_hits]
    else:
        lines.append("None.")
    lines += [
        "",
        "## What this means for labels",
        "",
        "- One bankruptcy usually produces several Item 1.03 8-Ks (petition, then plan "
        "confirmation or emergence), and some companies go bankrupt more than once.",
        "- Filing dates alone cannot separate the two: Hertz emerged 13 months after its "
        "petition, while Spirit filed a second petition about 9 months after its first. "
        "Telling petitions from emergence filings needs the 8-K text (open question for "
        "Phase 4 labels).",
        "- As-of dates between a petition and emergence must not be scored as 'healthy'; "
        "the company is already in bankruptcy.",
    ]
    return "\n".join(lines) + "\n"


def _row(r: CompanyResult) -> str:
    hits = "<br>".join(f"{h['filingDate']} {h['form']}" for h in r.hits) or "none"
    name = r.name + (" *(excluded)*" if r.is_excluded else "")
    issues = ", ".join(r.issues) or "-"
    return f"| {r.cik} | {name} | {r.petition_year or '-'} | {hits} | {issues} |"
