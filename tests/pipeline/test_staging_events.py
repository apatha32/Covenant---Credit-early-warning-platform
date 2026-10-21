from __future__ import annotations

import datetime as dt

import polars as pl
from pipeline_fakes import fact, raw_frame

from tripwire_common.config import load_domain_pack
from tripwire_common.settings import REPO_ROOT
from tripwire_pipeline.events import extract_events, typed_filings
from tripwire_pipeline.staging import stage_facts

PACK = load_domain_pack("finance", REPO_ROOT / "config")
ACC = "0000000001-23-000010"


def _stage(rows: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    return stage_facts(raw_frame(rows), PACK.metrics)


def _rules(staged) -> list[str]:  # type: ignore[no-untyped-def]
    return sorted(rule for rule, _ in staged.quarantine)


def test_only_mapped_tags_are_staged() -> None:
    staged = _stage([
        fact("Revenues", 100, "2023-03-31", "2023-05-05", ACC, start="2023-01-01"),
        fact("SomeCustomTag", 1, "2023-03-31", "2023-05-05", ACC, start="2023-01-01"),
        fact("EntityPublicFloat", 1, "2023-03-31", "2023-05-05", ACC, taxonomy="dei"),
    ])  # fmt: skip
    assert staged.facts["tag"].to_list() == ["Revenues"]
    assert staged.quarantine == []


def test_values_and_dates_are_typed_and_exact_text_kept() -> None:
    staged = _stage([fact("Revenues", "394328000000", "2023-03-31", "2023-05-05", ACC,
                          start="2023-01-01")])  # fmt: skip
    row = staged.facts.row(0, named=True)
    assert row["value"] == 394328000000.0 and row["value_text"] == "394328000000"
    assert row["period_start"] == dt.date(2023, 1, 1) and row["filed_date"] == dt.date(2023, 5, 5)


def test_non_usd_unit_is_quarantined() -> None:
    staged = _stage([fact("Revenues", 100, "2023-03-31", "2023-05-05", ACC, start="2023-01-01",
                          unit="CAD")])  # fmt: skip
    assert staged.facts.height == 0 and _rules(staged) == ["unexpected_unit"]


def test_filed_before_period_end_is_quarantined() -> None:
    staged = _stage([fact("Assets", 5, "2023-03-31", "2023-03-01", ACC)])
    assert _rules(staged) == ["filed_before_period_end"]


def test_bad_values_and_dates_are_quarantined() -> None:
    staged = _stage([
        fact("Assets", "abc", "2023-03-31", "2023-05-05", ACC),
        fact("Assets", 5, "2023-13-45", "2023-05-05", ACC),
        fact("Revenues", 5, "2023-03-31", "2023-05-05", ACC, start="2023-06-01"),
    ])  # fmt: skip
    assert _rules(staged) == ["period_start_after_end", "unparseable_date", "unparseable_value"]
    assert staged.facts.height == 0


def test_identical_duplicates_collapse_conflicting_ones_are_quarantined() -> None:
    staged = _stage([
        fact("Assets", 5, "2023-03-31", "2023-05-05", ACC, frame="CY2023Q1I"),
        fact("Assets", 5, "2023-03-31", "2023-05-05", ACC),
        fact("Liabilities", 7, "2023-03-31", "2023-05-05", ACC),
        fact("Liabilities", 8, "2023-03-31", "2023-05-05", ACC),
    ])  # fmt: skip
    assert staged.facts.select("tag", "value").rows() == [("Assets", 5.0)]
    assert _rules(staged) == ["conflicting_duplicate_fact"] * 2
    assert staged.counts["duplicate_rows_collapsed"] == 1


def _filings(rows: list[tuple[str, str, str]]) -> pl.DataFrame:
    """(accession, form, items) -> raw filings frame."""
    return pl.DataFrame({
        "cik": ["0000000001"] * len(rows),
        "accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows],
        "items": [r[2] for r in rows], "filingDate": ["2023-05-01"] * len(rows),
        "acceptanceDateTime": ["2023-05-01T16:05:00.000Z"] * len(rows),
        "reportDate": [""] * len(rows), "primaryDocument": ["d.htm"] * len(rows),
    })  # fmt: skip


def test_events_from_8k_items_and_nt_forms() -> None:
    filings = typed_filings(_filings([
        ("0000000001-23-000001", "8-K", "1.03,9.01"),
        ("0000000001-23-000002", "8-K/A", "4.02"),
        ("0000000001-23-000003", "NT 10-K", ""),
        ("0000000001-23-000004", "10-Q", "1.03"),       # items only count on 8-Ks
        ("0000000001-23-000005", "8-K", "5.02, 2.02"),
    ]))  # fmt: skip
    events = {(e["accession"][-1], e["event_type"], e["form_type"])
              for e in extract_events(filings, PACK.event_codes)}  # fmt: skip
    assert events == {
        ("1", "bankruptcy", "8-K"),
        ("2", "non_reliance", "8-K/A"),
        ("3", "late_filing_annual", "NT 10-K"),
        ("5", "exec_departure", "8-K"),
    }


def test_typed_filings_parse_items_and_timestamps() -> None:
    row = typed_filings(_filings([("0000000001-23-000001", "8-K", "1.03, 9.01")])).row(
        0, named=True
    )
    assert row["items"] == ["1.03", "9.01"]
    assert row["accepted_at"].isoformat() == "2023-05-01T16:05:00+00:00"
    assert row["report_date"] is None
