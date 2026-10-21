from __future__ import annotations

from collections.abc import Callable

import polars as pl
import psycopg
import pytest
from sec_fakes import FakeServer, filing, submissions_main

from tripwire_common.config import DevSample, SampleCompany, load_domain_pack
from tripwire_common.settings import REPO_ROOT
from tripwire_ingest import cli
from tripwire_ingest.runs import ingest_run
from tripwire_ingest.sample import has_xbrl_history
from tripwire_ingest.scan import item_hits, primary_event_items, render_report, spot_check
from tripwire_ingest.sec import SubmissionsPayload, parse_submissions

PACK = load_domain_pack("finance", REPO_ROOT / "config")


def _filings(rows: list[dict[str, object]]) -> pl.DataFrame:
    payload = SubmissionsPayload("0000000001", submissions_main(1, "A", rows), [])
    return parse_submissions(payload)[1]


def test_primary_items_come_from_the_domain_pack() -> None:
    assert primary_event_items(PACK) == ["1.03"]


def test_item_hits_match_whole_item_codes_only() -> None:
    filings = _filings([
        filing("0000000001-20-000001", "2020-05-01", "8-K", "1.03,9.01"),
        filing("0000000001-20-000002", "2020-05-02", "8-K", "1.031"),        # not 1.03
        filing("0000000001-20-000003", "2020-05-03", "8-K/A", " 1.03"),      # amendment, spaced
        filing("0000000001-20-000004", "2020-05-04", "10-Q", "1.03"),        # not an 8-K
        filing("0000000001-20-000005", "2020-05-05", "8-K", ""),
    ])  # fmt: skip
    hits = item_hits(filings, ["1.03"])
    assert hits["accessionNumber"].to_list() == ["0000000001-20-000001", "0000000001-20-000003"]


def test_item_hits_order_is_deterministic_for_same_day_filings() -> None:
    rows = [
        filing("0000000001-23-000009", "2023-10-16", "8-K", "1.03"),
        filing("0000000001-23-000002", "2023-10-16", "8-K/A", "1.03"),
    ]
    for ordering in (rows, rows[::-1]):
        hits = item_hits(_filings(ordering), ["1.03"])
        assert hits["accessionNumber"].to_list() == ["0000000001-23-000002", "0000000001-23-000009"]


def test_xbrl_history_requires_an_xbrl_annual_report_by_2015() -> None:
    assert has_xbrl_history(
        _filings([filing("0000000001-15-000001", "2015-03-01", "10-K", xbrl=1)])
    )
    assert not has_xbrl_history(
        _filings([filing("0000000001-16-000001", "2016-03-01", "10-K", xbrl=1)])
    )
    assert not has_xbrl_history(
        _filings([filing("0000000001-15-000001", "2015-03-01", "10-K", xbrl=0)])
    )


@pytest.mark.db
def test_spot_check_flags_data_issues_into_quarantine(
    db: psycopg.Connection, ctx_factory: Callable[[], cli.Context], fake_sec: FakeServer
) -> None:
    hertz = 1657853  # in known_bankruptcies.yaml, petition_year 2020
    fake_sec.add(f"/submissions/CIK{hertz:010d}.json", submissions_main(hertz, "HERTZ", [
        filing("0001657853-20-000050", "2020-05-26", "8-K", "1.03,7.01"),
        filing("0001657853-21-000070", "2021-06-16", "8-K", "1.03"),
    ]))  # fmt: skip
    fake_sec.add("/submissions/CIK0000000001.json", submissions_main(1, "Random Co", [
        filing("0000000001-19-000001", "2019-01-10", "8-K/A", "1.03"),
    ]))  # fmt: skip
    ctx = ctx_factory()
    cli.cmd_submissions(ctx, [hertz, 1])

    sample = DevSample(version=1, generated_on="2026-10-20", seed=1, criteria="test",
                       random=[SampleCompany(cik=1, name="Random Co")],
                       known_bankruptcies=[SampleCompany(cik=hertz, name="Hertz")])  # fmt: skip
    for _ in range(2):  # second run must not duplicate quarantine rows
        with ingest_run(db, "item_scan") as run:
            results = spot_check(db, ctx.storage, sample, PACK, run)

    by_cik = {r.cik: r for r in results}
    assert by_cik["0001657853"].issues == []  # petition + emergence 13 months apart: no issue
    assert set(by_cik["0000000001"].issues) == {"item_hit_outside_known_list",
                                                "item_hit_on_amended_8k"}  # fmt: skip
    rules = db.execute(
        "select rule_violated, count(*) from staging.quarantine where source = 'item_scan' "
        "group by 1 order by 1"
    ).fetchall()
    assert rules == [("item_hit_on_amended_8k", 1), ("item_hit_outside_known_list", 1)]

    report = render_report(results, sample, ["1.03"])
    assert "| 0001657853 | HERTZ | 2020 | 2020-05-26 8-K<br>2021-06-16 8-K | - |" in report


@pytest.mark.db
def test_spot_check_flags_known_bankruptcy_without_event(
    db: psycopg.Connection, ctx_factory: Callable[[], cli.Context], fake_sec: FakeServer
) -> None:
    hertz = 1657853
    fake_sec.add(f"/submissions/CIK{hertz:010d}.json", submissions_main(hertz, "HERTZ", [
        filing("0001657853-20-000050", "2020-05-26", "8-K", "7.01"),
    ]))  # fmt: skip
    ctx = ctx_factory()
    cli.cmd_submissions(ctx, [hertz])
    sample = DevSample(version=1, generated_on="x", seed=1, criteria="t", random=[],
                       known_bankruptcies=[SampleCompany(cik=hertz, name="Hertz")])  # fmt: skip
    with ingest_run(db, "item_scan") as run:
        (result,) = spot_check(db, ctx.storage, sample, PACK, run)
    assert result.issues == ["known_bankruptcy_missing_item_in_petition_year"]
