from __future__ import annotations

import datetime as dt

import pytest
from pipeline_fakes import FY_10K, FY_10KA, Q3_10Q, fact, fiscal_2023_revenue, raw_frame

from tripwire_common.config import MetricMapping, load_domain_pack
from tripwire_common.settings import REPO_ROOT
from tripwire_pipeline.metrics import MetricRow, build_rows, resolve, scale_outliers
from tripwire_pipeline.periods import classify_duration
from tripwire_pipeline.staging import stage_facts

MAPPING = load_domain_pack("finance", REPO_ROOT / "config").metrics
D = dt.date.fromisoformat


def _rows(raw_rows: list[dict[str, object]], mapping: MetricMapping = MAPPING) -> list[MetricRow]:
    staged = stage_facts(raw_frame(raw_rows), mapping).facts
    resolution = resolve(staged, mapping)
    return build_rows(resolution.observations, mapping, resolution.fy_ends)


def _series(rows: list[MetricRow], metric: str, ptype: str, end: str) -> list[MetricRow]:
    return [r for r in rows if (r.metric, r.period_type, r.period_end) == (metric, ptype, D(end))]


# --- periods ---


@pytest.mark.parametrize(
    ("start", "end", "cls"),
    [
        ("2023-01-01", "2023-03-31", "Q"),
        ("2022-12-25", "2023-03-25", "Q"),  # 13-week quarter
        ("2023-01-01", "2023-06-30", "H1"),
        ("2023-01-01", "2023-09-30", "M9"),
        ("2023-01-01", "2023-12-31", "FY"),
        ("2022-12-26", "2023-12-31", "FY"),  # 53-week year
        ("2023-01-01", "2023-02-28", None),  # 2-month transition period
        ("2023-01-01", "2024-06-30", None),
    ],
)
def test_classify_by_actual_length(start: str, end: str, cls: str | None) -> None:
    assert classify_duration(D(start), D(end)) == cls


# --- tag fallback ---


def test_highest_priority_tag_wins_and_is_recorded() -> None:
    rows = _rows([
        fact("SalesRevenueNet", 90, "2023-03-31", "2023-05-05", "0000000001-23-000010",
             start="2023-01-01"),
        fact("Revenues", 100, "2023-03-31", "2023-05-05", "0000000001-23-000010",
             start="2023-01-01"),
    ])  # fmt: skip
    (row,) = _series(rows, "revenue", "Q", "2023-03-31")
    assert row.value == 100 and row.source_tag == "us-gaap:Revenues"


def test_falls_back_to_lower_priority_tag_when_preferred_is_missing() -> None:
    rows = _rows([fact("SalesRevenueNet", 90, "2023-03-31", "2023-05-05", "0000000001-23-000010",
                       start="2023-01-01")])  # fmt: skip
    (row,) = _series(rows, "revenue", "Q", "2023-03-31")
    assert row.value == 90 and row.source_tag == "us-gaap:SalesRevenueNet"


def test_tag_choice_is_per_filing() -> None:
    # Older filing used SalesRevenueNet; a later one re-reports the period with Revenues.
    rows = _rows([
        fact("SalesRevenueNet", 90, "2017-03-31", "2017-05-05", "0000000001-17-000010",
             start="2017-01-01"),
        fact("Revenues", 91, "2017-03-31", "2018-05-05", "0000000001-18-000010",
             start="2017-01-01"),
    ])  # fmt: skip
    versions = _series(rows, "revenue", "Q", "2017-03-31")
    assert [(v.source_tag, v.value) for v in versions] == [
        ("us-gaap:SalesRevenueNet", 90), ("us-gaap:Revenues", 91)]  # fmt: skip


def test_sign_is_applied() -> None:
    capex = MAPPING.metrics["capex"].model_copy(update={"sign": -1})
    flipped = MAPPING.model_copy(update={"metrics": {**MAPPING.metrics, "capex": capex}})
    tag = "PaymentsToAcquirePropertyPlantAndEquipment"
    raw = [fact(tag, 50, "2023-12-31", "2024-02-20", FY_10K, start="2023-01-01", form="10-K")]
    rows = _rows(raw, flipped)
    assert _series(rows, "capex", "FY", "2023-12-31")[0].value == -50


# --- YTD -> quarterly derivation ---


def test_q4_is_derived_from_fy_minus_nine_months() -> None:
    rows = _rows(fiscal_2023_revenue())
    (q4,) = _series(rows, "revenue", "Q", "2023-12-31")
    assert q4.is_derived and q4.value == 460 - 330
    assert q4.period_start == D("2023-10-01")
    # Knowable only once the 10-K is public; lineage names both inputs.
    assert q4.filed_date == D("2024-02-20") and q4.accession == FY_10K
    assert [(c["role"], c["accession"], c["value"]) for c in q4.derivation or []] == [
        ("total", FY_10K, 460), ("minus", Q3_10Q, 330)]  # fmt: skip


def test_reported_quarters_are_not_derived() -> None:
    rows = _rows(fiscal_2023_revenue())
    for end in ("2023-03-31", "2023-06-30", "2023-09-30"):
        assert [r.is_derived for r in _series(rows, "revenue", "Q", end)] == [False]


def test_q2_and_q3_are_derived_when_10qs_report_only_ytd() -> None:
    # Cash flow statements: 10-Qs carry only year-to-date values.
    tag = "NetCashProvidedByUsedInOperatingActivities"
    rows = _rows([
        fact(tag, 10, "2023-03-31", "2023-05-05", "0000000001-23-000010", start="2023-01-01"),
        fact(tag, 25, "2023-06-30", "2023-08-04", "0000000001-23-000020", start="2023-01-01"),
        fact(tag, 45, "2023-09-30", "2023-11-03", "0000000001-23-000030", start="2023-01-01"),
        fact(tag, 70, "2023-12-31", "2024-02-20", FY_10K, start="2023-01-01", form="10-K"),
    ])  # fmt: skip
    quarters = {r.period_end: r.value for r in rows
                if r.metric == "operating_cash_flow" and r.period_type == "Q"}  # fmt: skip
    assert quarters == {D("2023-03-31"): 10, D("2023-06-30"): 15, D("2023-09-30"): 20,
                        D("2023-12-31"): 25}  # fmt: skip


def test_restated_component_creates_a_new_derived_version() -> None:
    rows = _rows(fiscal_2023_revenue(restate_fy=True))
    q4 = _series(rows, "revenue", "Q", "2023-12-31")
    assert [(r.value, r.filed_date, r.accession) for r in q4] == [
        (130, D("2024-02-20"), FY_10K), (120, D("2024-06-01"), FY_10KA)]  # fmt: skip
    fy = _series(rows, "revenue", "FY", "2023-12-31")
    assert [r.value for r in fy] == [460, 450]  # both versions kept


def test_unchanged_re_report_adds_no_derived_version() -> None:
    raw = fiscal_2023_revenue()
    # Next year's 10-K repeats FY2023 = 460 as a comparative.
    raw.append(fact("Revenues", 460, "2023-12-31", "2025-02-20", "0000000001-25-000005",
                    start="2023-01-01", form="10-K"))  # fmt: skip
    rows = _rows(raw)
    assert len(_series(rows, "revenue", "Q", "2023-12-31")) == 1
    assert len(_series(rows, "revenue", "FY", "2023-12-31")) == 2  # reported re-statements kept


def test_direct_report_stops_further_derivation() -> None:
    raw = fiscal_2023_revenue(restate_fy=True)
    # Q4 reported directly (e.g. quarterly table) before the amendment.
    raw.append(fact("Revenues", 131, "2023-12-31", "2024-03-01", "0000000001-24-000010",
                    start="2023-10-01", form="10-K"))  # fmt: skip
    q4 = _series(_rows(raw), "revenue", "Q", "2023-12-31")
    assert [(r.value, r.is_derived) for r in q4] == [(130, True), (131, False)]


# --- instants ---


def test_balance_sheet_dates_are_fy_or_q_by_reporting_period_ends() -> None:
    raw = fiscal_2023_revenue()
    raw += [
        fact("Assets", 1000, "2023-09-30", "2023-11-03", Q3_10Q),
        fact("Assets", 1200, "2023-12-31", "2024-02-20", FY_10K, form="10-K"),
        fact("Assets", 999, "2024-01-31", "2024-02-20", FY_10K, form="10-K"),  # no period ends here
    ]
    rows = _rows(raw)
    assets = {(r.period_type, r.period_end): r.value for r in rows if r.metric == "total_assets"}
    assert assets == {("Q", D("2023-09-30")): 1000, ("FY", D("2023-12-31")): 1200}


# --- scale outliers ---


def _assets(values: list[float]) -> list[dict[str, object]]:
    raw = []
    for i, v in enumerate(values):
        end = f"{2020 + i}-12-31"
        raw.append(fact("Assets", v, end, f"{2021 + i}-02-20", f"0000000001-{21 + i}-000005",
                        form="10-K"))  # fmt: skip
        raw.append(fact("Revenues", 1, end, f"{2021 + i}-02-20", f"0000000001-{21 + i}-000005",
                        start=f"{2020 + i}-01-01", form="10-K"))  # fmt: skip
    return raw


def _outliers(raw: list[dict[str, object]]) -> list[float]:
    resolution = resolve(stage_facts(raw_frame(raw), MAPPING).facts, MAPPING)
    return [o.value for o in scale_outliers(resolution.observations, MAPPING)
            if o.metric == "total_assets"]  # fmt: skip


def test_value_reported_in_thousands_is_flagged() -> None:
    assert _outliers(_assets([290e6, 295e6, 293_500, 300e6])) == [293_500]


def test_noisy_neighbors_are_not_evidence_of_a_scale_slip() -> None:
    # Shell company before a reverse merger: tiny, erratic values, then the real business.
    assert _outliers(_assets([1_000, 10_000, 8.3e6, 1_000, 1.5e6])) == []


def test_metrics_without_scale_check_are_never_flagged() -> None:
    raw = [fact("NetIncomeLoss", v, f"{2020 + i}-12-31", f"{2021 + i}-02-20",
                f"0000000001-{21 + i}-000005", start=f"{2020 + i}-01-01", form="10-K")
           for i, v in enumerate([5e6, 5.1e6, 5_000, 5.2e6])]  # fmt: skip
    resolution = resolve(stage_facts(raw_frame(raw), MAPPING).facts, MAPPING)
    assert scale_outliers(resolution.observations, MAPPING) == []


# --- formula fallbacks ---

LE = "LiabilitiesAndStockholdersEquity"
EQ = "StockholdersEquity"
EQ_NCI = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"


def _balance_sheet(**tags: float) -> list[dict[str, object]]:
    """FY balance sheet at 2023-12-31 with the given tags, plus a revenue fact for period ends."""
    raw = fiscal_2023_revenue()
    raw += [
        fact(tag, v, "2023-12-31", "2024-02-20", FY_10K, form="10-K") for tag, v in tags.items()
    ]
    return raw


def _liabilities(raw: list[dict[str, object]]) -> list[MetricRow]:
    return _series(_rows(raw), "total_liabilities", "FY", "2023-12-31")


def test_reported_total_beats_the_formula() -> None:
    (row,) = _liabilities(_balance_sheet(Liabilities=600, **{LE: 1000, EQ_NCI: 390}))
    assert (row.value, row.source_tag, row.is_derived) == (600, "us-gaap:Liabilities", False)


def test_formula_fills_in_a_missing_total_with_lineage() -> None:
    (row,) = _liabilities(_balance_sheet(**{LE: 1000, EQ_NCI: 390, EQ: 380}))
    assert row.value == 610 and row.is_derived
    assert row.source_tag.startswith("formula: ") and EQ_NCI in row.source_tag
    assert row.derivation == [
        {"role": "plus", "tag": f"us-gaap:{LE}", "value": 1000, "accession": FY_10K},
        {"role": "minus", "tag": f"us-gaap:{EQ_NCI}", "value": 390, "accession": FY_10K},
    ]
    assert row.filed_date == D("2024-02-20")


def test_next_formula_is_used_when_a_component_is_missing() -> None:
    (row,) = _liabilities(_balance_sheet(**{LE: 1000, EQ: 380}))
    assert row.value == 620 and f"us-gaap:{EQ}" in row.source_tag


def test_no_value_when_no_formula_is_complete() -> None:
    assert _liabilities(_balance_sheet(**{LE: 1000})) == []
