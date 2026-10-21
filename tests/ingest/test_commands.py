"""Phase 1 acceptance, end to end against fake SEC/FRED servers and a real (throwaway) database:
re-running is idempotent, the run log shows counts, and the rate limit holds."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from sec_fakes import FakeServer, columns, companyfacts, filing, submissions_main

from tripwire_common.config import load_domain_pack
from tripwire_common.raw import latest_cik_path
from tripwire_common.settings import REPO_ROOT
from tripwire_ingest import cli
from tripwire_ingest.cli import Context

pytestmark = pytest.mark.db

CIK, CIK_STR = 1, "0000000001"
SUBS = f"/submissions/CIK{CIK_STR}.json"
PAGE = f"/submissions/CIK{CIK_STR}-submissions-001.json"
FACTS = f"/api/xbrl/companyfacts/CIK{CIK_STR}.json"


def _main(extra_filings: int = 0, sic: str = "3571") -> bytes:
    recent = [filing("0000000001-24-000001", "2024-02-01", "10-K", xbrl=1)]
    recent += [filing(f"0000000001-24-{i:06d}", "2024-05-01", "8-K", "2.02") for i in
               range(10, 10 + extra_filings)]  # fmt: skip
    return submissions_main(
        CIK, "Acme Corp", recent, sic=sic, tickers=["ACME"], pages=[PAGE.rsplit("/", 1)[1]],
        former_names=[{"name": "Acme Widgets Inc /DE/", "from": "2001-01-01T00:00:00.000Z",
                       "to": "2015-06-30T00:00:00.000Z"}],
    )  # fmt: skip


def _page() -> bytes:
    return json.dumps(
        columns([filing("0000000001-10-000001", "2010-03-01", "10-K", xbrl=1)])
    ).encode()


def _facts(value: int = 100) -> bytes:
    return companyfacts(CIK, {"Revenues": {"USD": [
        {"start": "2023-01-01", "end": "2023-12-31", "val": value, "accn": "0000000001-24-000001",
         "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2024-02-01"}]}})  # fmt: skip


def _runs(db: psycopg.Connection, source: str) -> list[tuple[str, dict[str, Any], str | None]]:
    rows = db.execute(
        "select status, counts, error from raw.ingest_runs where source = %s order by id", [source]
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _files(tmp_path: Path) -> list[Path]:
    return sorted(p for p in (tmp_path / "data").rglob("*") if p.is_file())


def test_submissions_rerun_is_idempotent(
    db: psycopg.Connection, ctx_factory: Callable[[], Context], fake_sec: FakeServer, tmp_path: Path
) -> None:
    fake_sec.add(SUBS, _main())
    fake_sec.add(PAGE, _page())

    cli.cmd_submissions(ctx_factory(), [CIK])
    files_after_first = _files(tmp_path)
    cli.cmd_submissions(ctx_factory(), [CIK])

    assert _files(tmp_path) == files_after_first
    assert len(files_after_first) == 2  # company.parquet + filings.parquet
    index = db.execute("select count(*) from raw.submissions_index where cik = %s", [CIK_STR])
    assert index.fetchone() == (1,)

    (s1, c1, _), (s2, c2, _) = _runs(db, "sec_submissions")
    assert (s1, s2) == ("succeeded", "succeeded")
    assert c1["landed_new"] == 1 and c1["filings_rows"] == 2 and c1["pages_fetched"] == 1
    assert c2["unchanged"] == 1 and "landed_new" not in c2
    assert c1["http_requests"] == 2  # main + one page
    assert c1["max_requests_per_second"] <= 10


def test_submissions_load_company_and_aliases(
    db: psycopg.Connection, ctx_factory: Callable[[], Context], fake_sec: FakeServer
) -> None:
    fake_sec.add(SUBS, _main())
    fake_sec.add(PAGE, _page())
    cli.cmd_submissions(ctx_factory(), [CIK])
    cli.cmd_submissions(ctx_factory(), [CIK])

    company = db.execute(
        "select name, tickers, sic, fiscal_year_end, is_excluded from curated.company "
        "where cik = %s",
        [CIK_STR],
    ).fetchone()
    assert company == ("Acme Corp", ["ACME"], 3571, "1231", False)
    aliases = db.execute(
        "select alias_type, alias, alias_normalized from curated.company_alias where cik = %s "
        "order by alias_type",
        [CIK_STR],
    ).fetchall()
    assert aliases == [("former_name", "Acme Widgets Inc /DE/", "acme widgets"),
                       ("ticker", "ACME", "acme")]  # fmt: skip


def test_changed_payload_lands_new_version_and_keeps_old(
    db: psycopg.Connection, ctx_factory: Callable[[], Context], fake_sec: FakeServer, tmp_path: Path
) -> None:
    fake_sec.add(SUBS, _main())
    fake_sec.add(SUBS, _main(extra_filings=1))  # second fetch sees a new 8-K
    fake_sec.add(PAGE, _page())

    cli.cmd_submissions(ctx_factory(), [CIK])
    first_path = latest_cik_path(db, "submissions", CIK_STR)
    cli.cmd_submissions(ctx_factory(), [CIK])
    second_path = latest_cik_path(db, "submissions", CIK_STR)

    assert first_path != second_path
    assert len(_files(tmp_path)) == 4  # both versions kept
    _, (_, counts, _) = _runs(db, "sec_submissions")
    assert counts["landed_new"] == 1


def test_sector_exclusion_is_flagged_not_dropped(
    db: psycopg.Connection, ctx_factory: Callable[[], Context], fake_sec: FakeServer
) -> None:
    fake_sec.add(SUBS, _main(sic="6770"))
    fake_sec.add(PAGE, _page())
    cli.cmd_submissions(ctx_factory(), [CIK])
    row = db.execute(
        "select is_excluded, exclusion_reason from curated.company where cik = %s", [CIK_STR]
    ).fetchone()
    assert row == (True, "shell_company")


def test_one_failing_company_makes_the_run_partial(
    db: psycopg.Connection, ctx_factory: Callable[[], Context], fake_sec: FakeServer
) -> None:
    fake_sec.add(SUBS, _main())
    fake_sec.add(PAGE, _page())
    fake_sec.add("/submissions/CIK0000000002.json", b"blocked", status=403)

    cli.cmd_submissions(ctx_factory(), [CIK, 2])

    ((status, counts, error),) = _runs(db, "sec_submissions")
    assert status == "partial"
    assert counts["landed_new"] == 1 and counts["failed"] == 1
    assert error is not None and "0000000002" in error


def test_unknown_cik_counts_as_not_found(
    db: psycopg.Connection, ctx_factory: Callable[[], Context]
) -> None:
    cli.cmd_submissions(ctx_factory(), [999])
    ((status, counts, _),) = _runs(db, "sec_submissions")
    assert status == "succeeded" and counts["not_found"] == 1


def test_companyfacts_rerun_is_idempotent_and_404_means_no_xbrl(
    db: psycopg.Connection, ctx_factory: Callable[[], Context], fake_sec: FakeServer
) -> None:
    fake_sec.add(FACTS, _facts())
    cli.cmd_companyfacts(ctx_factory(), [CIK, 2])  # CIK 2 has no XBRL data -> 404
    cli.cmd_companyfacts(ctx_factory(), [CIK, 2])

    (_, c1, _), (_, c2, _) = _runs(db, "sec_companyfacts")
    assert c1["landed_new"] == 1 and c1["facts_rows"] == 1 and c1["no_xbrl_data"] == 1
    assert c2["unchanged"] == 1 and c2["no_xbrl_data"] == 1
    assert db.execute("select count(*) from raw.companyfacts_index").fetchone() == (1,)


def test_tickers_create_companies_once(
    db: psycopg.Connection, ctx_factory: Callable[[], Context], fake_sec: FakeServer
) -> None:
    fake_sec.add("/files/company_tickers.json", json.dumps({
        "0": {"cik_str": 1, "ticker": "ACME", "title": "Acme Corp"},
        "1": {"cik_str": 1, "ticker": "ACME-B", "title": "Acme Corp"},
        "2": {"cik_str": 3, "ticker": "ZED", "title": "Zed Inc"},
    }).encode())  # fmt: skip
    cli.cmd_tickers(ctx_factory())
    cli.cmd_tickers(ctx_factory())

    rows = db.execute("select cik, tickers from curated.company order by cik").fetchall()
    assert rows == [("0000000001", ["ACME", "ACME-B"]), ("0000000003", ["ZED"])]
    (_, c1, _), (_, c2, _) = _runs(db, "sec_tickers")
    assert c1["landed_new"] == 1 and c1["companies"] == 2
    assert c2["unchanged"] == 1


def test_fred_rerun_is_idempotent(
    db: psycopg.Connection, ctx_factory: Callable[[], Context], fake_fred: FakeServer
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["api_key"] == "k"
        vintages = "realtime_start" in params
        obs = [{"date": "2024-01-01", "value": "3.7",
                "realtime_start": "2024-02-02" if vintages else "2026-10-20",
                "realtime_end": "9999-12-31" if vintages else "2026-10-20"}]  # fmt: skip
        return httpx.Response(200, json={"count": 1, "observations": obs})

    fake_fred.handler = respond
    cli.cmd_fred(ctx_factory())
    cli.cmd_fred(ctx_factory())

    n_series = len(load_domain_pack("finance", REPO_ROOT / "config").macro.series)
    (s1, c1, _), (_, c2, _) = _runs(db, "fred")
    assert s1 == "succeeded"
    assert c1["landed_new"] == n_series and c2["unchanged"] == n_series
    # Only series configured for vintages ask FRED for every vintage.
    asked_vintages = {r.url.params["series_id"] for r in fake_fred.requests
                      if "realtime_start" in r.url.params}  # fmt: skip
    assert asked_vintages == {"FEDFUNDS", "UNRATE", "INDPRO"}
