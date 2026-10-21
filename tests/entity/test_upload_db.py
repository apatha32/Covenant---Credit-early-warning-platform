"""Upload flow, review queue and decision reuse against a throwaway database."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from tripwire_common.config import load_entity_resolution_config
from tripwire_common.settings import REPO_ROOT
from tripwire_pipeline.entity.upload import (
    UploadError,
    decide,
    load_universe,
    parse_borrower_csv,
    upload_borrowers,
)

CONFIG = load_entity_resolution_config(REPO_ROOT / "config")

GLOBEX_WITH_ACME_TICKER = b"name,ticker\nGlobex Corporation,ACME\n"

CSV = b"""Company Name,Ticker,Notes
Acme Widgets Intl,ACME,matches by ticker + name
Globex Corp,,exact name
Smith Family Dental Group LLC,,not an SEC company
,,empty row
Globex Corporation,ACME,ticker belongs to another company
"""


def _id(conn: psycopg.Connection, sql: str, params: list[object]) -> uuid.UUID:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


@pytest.fixture
def setup(db: psycopg.Connection) -> dict[str, uuid.UUID]:
    db.execute(
        """insert into curated.company (cik, name, tickers) values
           ('0000000001', 'Acme Widgets International, Inc.', '{ACME}'),
           ('0000000002', 'Globex Corporation', '{GLBX}')"""
    )
    user = _id(db, "insert into auth.users (email) values ('a@x.test') returning id", [])
    tenant = _id(db, "insert into app.tenant (name) values ('A') returning id", [])
    other = _id(db, "insert into app.tenant (name) values ('B') returning id", [])
    new_portfolio = "insert into app.portfolio (tenant_id, name) values (%s, %s) returning id"
    portfolio = _id(db, new_portfolio, [tenant, "Book"])
    second = _id(db, new_portfolio, [tenant, "Book 2"])
    return {"user": user, "tenant": tenant, "other": other, "portfolio": portfolio,
            "second": second}  # fmt: skip


def _borrowers(db: psycopg.Connection, portfolio: uuid.UUID) -> dict[str, tuple[object, ...]]:
    rows = db.execute(
        """select coalesce(uploaded_name, ''), match_status, cik, match_method, row_number
           from app.portfolio_borrower where portfolio_id = %s""",
        [portfolio],
    ).fetchall()
    return {r[0]: tuple(r[1:]) for r in rows}


def test_parse_finds_columns_by_header_and_skips_empty_rows() -> None:
    parsed = parse_borrower_csv(CSV, max_rows=100)
    assert (parsed.name_column, parsed.ticker_column) == ("Company Name", "Ticker")
    assert [r.row_number for r in parsed.rows] == [1, 2, 3, 5]
    assert parsed.skipped == [(4, "empty row")]


def test_parse_rejects_files_without_usable_columns() -> None:
    with pytest.raises(UploadError, match="no borrower name or ticker column"):
        parse_borrower_csv(b"foo,bar\n1,2\n", max_rows=100)
    with pytest.raises(UploadError, match="more than 2 rows"):
        parse_borrower_csv(b"name\na\nb\nc\n", max_rows=2)


def test_parse_handles_excel_bom_and_latin1() -> None:
    assert parse_borrower_csv(b"\xef\xbb\xbfName\nAcme\n", 10).rows[0].name == "Acme"
    assert parse_borrower_csv("Name\nSociété Générale\n".encode("latin-1"),
                              10).rows[0].name == "Société Générale"  # fmt: skip


@pytest.mark.db
def test_upload_matches_rows_and_queues_low_confidence_ones(
    db: psycopg.Connection, setup: dict[str, uuid.UUID]
) -> None:
    universe = load_universe(db, CONFIG)
    summary = upload_borrowers(
        db, setup["tenant"], setup["portfolio"], CSV, universe, setup["user"]
    )

    assert dict(summary.counts) == {"auto_matched": 2, "needs_review": 2}
    rows = _borrowers(db, setup["portfolio"])
    assert rows["Acme Widgets Intl"] == ("auto_matched", "0000000001", "ticker_and_name", 1)
    assert rows["Globex Corp"] == ("auto_matched", "0000000002", "exact_name", 2)
    assert rows["Smith Family Dental Group LLC"][:2] == ("needs_review", None)
    assert rows["Globex Corporation"][:3] == ("needs_review", None, "ticker_name_conflict")
    reviews = db.execute(
        "select candidate_ciks from app.match_review where tenant_id = %s", [setup["tenant"]]
    ).fetchall()
    assert len(reviews) == 2
    conflict = next(r[0] for r in reviews if len(r[0]) >= 2)
    assert {c["cik"] for c in conflict} >= {"0000000001", "0000000002"}
    audit = db.execute("select action, details from app.audit_log").fetchone()
    assert audit is not None and audit[0] == "portfolio_upload"


@pytest.mark.db
def test_reupload_is_idempotent(db: psycopg.Connection, setup: dict[str, uuid.UUID]) -> None:
    universe = load_universe(db, CONFIG)
    upload_borrowers(db, setup["tenant"], setup["portfolio"], CSV, universe)
    again = upload_borrowers(db, setup["tenant"], setup["portfolio"], CSV, universe)
    assert dict(again.counts) == {"duplicate": 4}
    count = db.execute("select count(*) from app.portfolio_borrower").fetchone()
    reviews = db.execute("select count(*) from app.match_review").fetchone()
    assert (count, reviews) == ((4,), (2,))


@pytest.mark.db
def test_upload_refuses_another_tenants_portfolio(
    db: psycopg.Connection, setup: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(UploadError, match="portfolio not found"):
        upload_borrowers(db, setup["other"], setup["portfolio"], CSV, load_universe(db, CONFIG))


def _review_for(db: psycopg.Connection, name: str) -> uuid.UUID:
    return _id(
        db,
        """select r.id from app.match_review r join app.portfolio_borrower b on b.id = r.borrower_id
           where b.uploaded_name = %s and r.decision is null limit 1""",
        [name],
    )


@pytest.mark.db
def test_decision_is_remembered_and_reused(
    db: psycopg.Connection, setup: dict[str, uuid.UUID]
) -> None:
    universe = load_universe(db, CONFIG)
    upload_borrowers(db, setup["tenant"], setup["portfolio"], CSV, universe)
    review = _review_for(db, "Globex Corporation")

    updated = decide(db, setup["tenant"], review, "accept", setup["user"], cik="0000000002")
    assert updated == 1
    assert _borrowers(db, setup["portfolio"])["Globex Corporation"][:3] == (
        "confirmed", "0000000002", "human_decision")  # fmt: skip

    # Same input in another portfolio of the same tenant: resolved from the stored decision.
    upload_borrowers(db, setup["tenant"], setup["second"], GLOBEX_WITH_ACME_TICKER, universe)
    assert _borrowers(db, setup["second"])["Globex Corporation"][:3] == (
        "confirmed", "0000000002", "prior_decision")  # fmt: skip


@pytest.mark.db
def test_decision_resolves_other_pending_rows_with_the_same_input(
    db: psycopg.Connection, setup: dict[str, uuid.UUID]
) -> None:
    universe = load_universe(db, CONFIG)
    upload_borrowers(db, setup["tenant"], setup["portfolio"], CSV, universe)
    upload_borrowers(db, setup["tenant"], setup["second"], CSV, universe)
    review = _review_for(db, "Smith Family Dental Group LLC")

    assert decide(db, setup["tenant"], review, "no_match", setup["user"]) == 2
    for portfolio in (setup["portfolio"], setup["second"]):
        assert _borrowers(db, portfolio)["Smith Family Dental Group LLC"][:2] == ("unmatched", None)
    open_reviews = db.execute("select count(*) from app.match_review where decision is null")
    assert open_reviews.fetchone() == (2,)  # only the two ticker-conflict rows remain


@pytest.mark.db
def test_decisions_are_not_shared_across_tenants(
    db: psycopg.Connection, setup: dict[str, uuid.UUID]
) -> None:
    universe = load_universe(db, CONFIG)
    upload_borrowers(db, setup["tenant"], setup["portfolio"], CSV, universe)
    decide(db, setup["tenant"], _review_for(db, "Globex Corporation"), "accept", setup["user"],
           cik="0000000002")  # fmt: skip
    other_book = _id(
        db,
        "insert into app.portfolio (tenant_id, name) values (%s, 'X') returning id",
        [setup["other"]],
    )
    upload_borrowers(db, setup["other"], other_book, GLOBEX_WITH_ACME_TICKER, universe)
    assert _borrowers(db, other_book)["Globex Corporation"][0] == "needs_review"


@pytest.mark.db
def test_decide_validates_input(db: psycopg.Connection, setup: dict[str, uuid.UUID]) -> None:
    upload_borrowers(db, setup["tenant"], setup["portfolio"], CSV, load_universe(db, CONFIG))
    review = _review_for(db, "Globex Corporation")
    with pytest.raises(UploadError, match="needs a cik"):
        decide(db, setup["tenant"], review, "accept", setup["user"])
    with pytest.raises(UploadError, match="unknown company"):
        decide(db, setup["tenant"], review, "accept", setup["user"], cik="0000009999")
    with pytest.raises(UploadError, match="open review not found"):
        decide(db, setup["other"], review, "no_match", setup["user"])
    decide(db, setup["tenant"], review, "reject", setup["user"])
    with pytest.raises(UploadError, match="open review not found"):
        decide(db, setup["tenant"], review, "reject", setup["user"])  # already decided
