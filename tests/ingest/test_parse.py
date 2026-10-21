from __future__ import annotations

import json

import polars as pl
import pytest
from sec_fakes import columns, companyfacts, filing, submissions_main

from tripwire_common.config import MacroSeriesSpec
from tripwire_ingest.fred import FredPayload, parse_series
from tripwire_ingest.sec import (
    RawPayload,
    SourceSchemaError,
    SubmissionsPayload,
    accession_nodash,
    archive_cik,
    cik10,
    parse_companyfacts,
    parse_submissions,
    parse_tickers,
)


def test_identifier_formats() -> None:
    assert cik10(320193) == "0000320193"
    assert cik10("0000320193") == "0000320193"
    assert archive_cik("0000320193") == "320193"
    assert accession_nodash("0000320193-23-000106") == "000032019323000106"


def test_submissions_merge_recent_and_paginated_files() -> None:
    main = submissions_main(
        20520, "Frontier", [filing("0000020520-20-000010", "2020-04-15", "8-K", "1.03,9.01")],
        tickers=["FYBR"], pages=["CIK0000020520-submissions-001.json"],
        former_names=[{"name": "FRONTIER COMMUNICATIONS CORP", "from": "2008-05-08T00:00:00.000Z",
                       "to": "2021-05-03T00:00:00.000Z"}],
    )  # fmt: skip
    page = json.dumps(columns([filing("0000020520-10-000001", "2010-02-01", "10-K", xbrl=1)]))
    payload = SubmissionsPayload("0000020520", main, [("CIK0000020520-submissions-001.json",
                                                       page.encode())])  # fmt: skip
    company, filings = parse_submissions(payload)

    assert company["name"][0] == "Frontier"
    assert company["tickers"][0].to_list() == ["FYBR"]
    assert "FRONTIER COMMUNICATIONS CORP" in company["former_names_json"][0]
    assert filings.height == 2
    assert set(filings["source_file"]) == {"recent", "CIK0000020520-submissions-001.json"}
    assert filings.filter(pl.col("form") == "8-K")["items"][0] == "1.03,9.01"


def test_older_pages_missing_optional_columns_are_null_filled() -> None:
    main = submissions_main(1, "A", [], pages=["p1.json"])
    old = columns([filing("0000000001-05-000001", "2005-01-01", "10-K")])
    del old["isInlineXBRL"], old["core_type"]
    payload = SubmissionsPayload("0000000001", main, [("p1.json", json.dumps(old).encode())])
    _, filings = parse_submissions(payload)
    assert filings["isInlineXBRL"].to_list() == [None]


def test_missing_required_filing_column_stops_the_run() -> None:
    main = json.loads(
        submissions_main(1, "A", [filing("0000000001-20-000001", "2020-01-01", "8-K")])
    )
    del main["filings"]["recent"]["items"]
    with pytest.raises(SourceSchemaError, match="items"):
        parse_submissions(SubmissionsPayload("0000000001", json.dumps(main).encode(), []))


def test_unexpected_filing_column_is_kept_not_dropped() -> None:
    main = json.loads(
        submissions_main(1, "A", [filing("0000000001-20-000001", "2020-01-01", "8-K")])
    )
    main["filings"]["recent"]["newField"] = [42]
    _, filings = parse_submissions(SubmissionsPayload("0000000001", json.dumps(main).encode(), []))
    assert filings["newField"].to_list() == ["42"]


def test_cik_mismatch_is_rejected() -> None:
    with pytest.raises(SourceSchemaError, match="returned cik"):
        parse_submissions(SubmissionsPayload("0000000002", submissions_main(1, "A", []), []))


def test_companyfacts_keeps_exact_values_and_period_types() -> None:
    body = companyfacts(1, {
        "Revenues": {"USD": [
            {"start": "2023-01-01", "end": "2023-12-31", "val": 394328000000, "accn":
             "0000000001-24-000001", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2024-02-01",
             "frame": "CY2023"}]},
        "EarningsPerShareBasic": {"USD/shares": [
            {"start": "2023-01-01", "end": "2023-12-31", "val": 6.13,
             "accn": "0000000001-24-000001",
             "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2024-02-01"}]},
        "Assets": {"USD": [
            {"end": "2023-12-31", "val": 0.1, "accn": "0000000001-24-000001", "fy": 2023,
             "fp": "FY", "form": "10-K", "filed": "2024-02-01"}]},
    })  # fmt: skip
    facts, tags = parse_companyfacts(RawPayload("0000000001", body))
    by_tag = {r["tag"]: r for r in facts.to_dicts()}
    assert by_tag["Revenues"]["val"] == "394328000000"
    assert by_tag["EarningsPerShareBasic"]["val"] == "6.13"
    assert by_tag["EarningsPerShareBasic"]["unit"] == "USD/shares"
    assert by_tag["Assets"]["start"] is None  # instant fact
    assert by_tag["Assets"]["val"] == "0.1"  # no float artifacts
    assert by_tag["Revenues"]["fy"] == 2023
    assert tags.height == 3


def test_companyfacts_fact_missing_filed_date_stops_the_run() -> None:
    fact = {"end": "2023-12-31", "val": 1, "accn": "0000000001-24-000001", "form": "10-K"}
    body = companyfacts(1, {"Assets": {"USD": [fact]}})
    with pytest.raises(SourceSchemaError, match="filed"):
        parse_companyfacts(RawPayload("0000000001", body))


def test_tickers_parse() -> None:
    raw = json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                      "1": {"cik_str": 1, "ticker": "X-A", "title": "X"}}).encode()  # fmt: skip
    frame = parse_tickers(raw)
    assert frame.to_dicts()[0] == {"cik": "0000320193", "ticker": "AAPL", "title": "Apple Inc."}


def _fred_page(rows: list[dict[str, str]]) -> bytes:
    return json.dumps({"count": len(rows), "observations": rows}).encode()


def test_fred_checksum_ignores_todays_realtime_stamp_without_vintages() -> None:
    spec = MacroSeriesSpec(id="DGS10", vintages=False, release_lag_days=1, description="x")
    day1 = _fred_page([{"date": "2024-01-02", "value": "3.9", "realtime_start": "2026-10-19",
                        "realtime_end": "2026-10-19"}])  # fmt: skip
    day2 = day1.replace(b"2026-10-19", b"2026-10-20")
    assert FredPayload(spec, [day1]).checksum == FredPayload(spec, [day2]).checksum
    assert parse_series(FredPayload(spec, [day1]))["realtime_start"].to_list() == [None]


def test_fred_checksum_tracks_vintages_when_enabled() -> None:
    spec = MacroSeriesSpec(id="UNRATE", vintages=True, description="x")
    v1 = _fred_page([{"date": "2024-01-01", "value": "3.7", "realtime_start": "2024-02-02",
                      "realtime_end": "9999-12-31"}])  # fmt: skip
    v2 = v1.replace(b"2024-02-02", b"2024-03-08")
    assert FredPayload(spec, [v1]).checksum != FredPayload(spec, [v2]).checksum
    assert parse_series(FredPayload(spec, [v1]))["realtime_start"].to_list() == ["2024-02-02"]
