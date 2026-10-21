from __future__ import annotations

from pathlib import Path

import pytest

from tripwire_common.config import load_entity_resolution_config
from tripwire_common.settings import REPO_ROOT
from tripwire_pipeline.entity.evaluate import evaluate, load_labeled, load_universe_fixture
from tripwire_pipeline.entity.matcher import Company, PriorDecision, Universe, normalize_ticker

CONFIG = load_entity_resolution_config(REPO_ROOT / "config")
FIXTURES = Path(__file__).parent / "fixtures"

COMPANIES = [
    Company("0000000001", "Acme Widgets International, Inc.", ("ACME",)),
    Company("0000000002", "Globex Corporation", ("GLBX",), former_names=("Initech Holdings Inc",)),
    Company("0000000003", "Delta Air Lines, Inc.", ("DAL",)),
    Company("0000000004", "Twin Peaks Corp", ("TPA",)),
    Company("0000000005", "Twin Peaks Corp /DE/", ("TPB",)),  # same normalized name as 4
    Company("0000000006", "Northwind Traders Inc", ("NWT",)),
    Company("0000000007", "Northwind Trading Inc", ("NWX",)),
]
U = Universe(COMPANIES, CONFIG)


# --- normalization ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("aapl", "AAPL"), ("NYSE: aapl", "AAPL"), ("$AAPL", "AAPL"), ("BRK.B", "BRK-B"),
     ("BRK/B", "BRK-B"), ("NASDAQ:MSFT", "MSFT"), ("", ""), (None, "")],
)  # fmt: skip
def test_normalize_ticker(raw: str | None, expected: str) -> None:
    assert normalize_ticker(raw) == expected


def test_abbreviations_expand_before_matching() -> None:
    assert U.normalize("ACME WIDGETS INTL") == U.normalize("Acme Widgets International, Inc.")


def test_input_key_is_stable_across_formatting() -> None:
    assert U.input_key(" Acme Widgets Intl. ", "nyse: acme") == U.input_key(
        "ACME WIDGETS INTERNATIONAL INC", "ACME"
    )


# --- cascade ---


def test_ticker_and_agreeing_name_auto_match() -> None:
    r = U.match("Acme Widgets Intl", "ACME")
    assert (r.status, r.cik, r.method) == ("auto_matched", "0000000001", "ticker_and_name")


def test_ticker_only_auto_matches() -> None:
    r = U.match(None, "glbx")
    assert (r.status, r.cik, r.method) == ("auto_matched", "0000000002", "ticker_only")


def test_ticker_pointing_at_another_company_goes_to_review() -> None:
    r = U.match("Globex Corporation", "ACME")
    assert (r.status, r.cik, r.method) == ("needs_review", None, "ticker_name_conflict")
    assert {c.cik for c in r.candidates} >= {"0000000001", "0000000002"}


def test_unknown_ticker_falls_back_to_name() -> None:
    r = U.match("Globex Corp", "ZZZZ")
    assert (r.status, r.cik, r.method) == ("auto_matched", "0000000002", "exact_name")


def test_former_name_matches_renamed_company() -> None:
    r = U.match("Initech Holdings", None)
    assert (r.status, r.cik, r.method) == ("auto_matched", "0000000002", "exact_former_name")


def test_formerly_pattern_tries_each_name() -> None:
    r = U.match("Some New Name LLC (formerly Initech Holdings Inc.)", None)
    assert (r.cik, r.method) == ("0000000002", "exact_former_name")


def test_typo_is_matched_fuzzily() -> None:
    r = U.match("Acme Widgetts Internationl", None)
    assert (r.status, r.cik, r.method) == ("auto_matched", "0000000001", "fuzzy_name")


def test_spacing_differences_do_not_matter() -> None:
    r = U.match("Delta Airlines", None)
    assert (r.status, r.cik) == ("auto_matched", "0000000003")


def test_name_shared_by_two_companies_goes_to_review() -> None:
    r = U.match("Twin Peaks Corporation", None)
    assert (r.status, r.method) == ("needs_review", "ambiguous_name")
    assert {c.cik for c in r.candidates} >= {"0000000004", "0000000005"}


def test_near_tie_between_companies_is_capped_below_threshold() -> None:
    r = U.match("Northwind Trad", None)  # equally close to "Traders" and "Trading"
    assert r.status == "needs_review"
    assert r.confidence <= CONFIG.fuzzy_ambiguous_cap


def test_unrelated_name_goes_to_review_with_no_cik() -> None:
    r = U.match("Smith Family Dental Group LLC", None)
    assert (r.status, r.cik) == ("needs_review", None)
    assert r.confidence < CONFIG.auto_match_min_confidence


def test_prior_decision_wins() -> None:
    assert U.match("anything", None, PriorDecision("0000000006")).status == "confirmed"
    no_match = U.match("Acme Widgets International", "ACME", PriorDecision(None))
    assert (no_match.status, no_match.cik) == ("unmatched", None)


# --- acceptance (Phase 3): messy 100-row file, never used for tuning ---


@pytest.fixture(scope="module")
def universe() -> Universe:
    return Universe(load_universe_fixture(FIXTURES / "universe.csv.gz"), CONFIG)


def test_holdout_auto_match_precision_is_at_least_90_percent(universe: Universe) -> None:
    ev = evaluate(universe, load_labeled(FIXTURES / "borrowers_holdout.csv"))
    assert len(ev.scored) == 100
    assert ev.precision >= 0.90, [s.row for s in ev.auto if not s.correct_auto]
    assert ev.auto_rate >= 0.70  # and it still does most of the work


def test_holdout_every_low_confidence_row_is_routed_to_review(universe: Universe) -> None:
    ev = evaluate(universe, load_labeled(FIXTURES / "borrowers_holdout.csv"))
    for s in ev.scored:
        if s.result.confidence < CONFIG.auto_match_min_confidence:
            assert s.result.status == "needs_review" and s.result.cik is None, s.row
        else:
            assert s.result.status == "auto_matched"
    # Rows that are not SEC companies, or whose ticker belongs to another company, never auto-match.
    for s in ev.scored:
        if s.row["case"] in ("not_an_sec_company", "wrong_ticker"):
            assert s.result.status == "needs_review", s.row


def test_adversarial_cases_produce_no_wrong_auto_matches(universe: Universe) -> None:
    ev = evaluate(universe, load_labeled(FIXTURES / "borrowers_adversarial.csv"))
    assert [s.row for s in ev.auto if not s.correct_auto] == []
