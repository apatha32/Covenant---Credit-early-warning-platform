"""Phase 6b database side: alert generation rules, serving queue, and the new api views
staying inside the caller's tenant."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest
from rls_helpers import acting_as

pytestmark = pytest.mark.db

CIK = "0000000031"


def _id(conn: psycopg.Connection, query: str, params: list[object] | None = None) -> uuid.UUID:
    row = conn.execute(query, params or []).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _tenant(conn: psycopg.Connection, name: str, *, demo: bool = False,
            added: str = "2024-05-15") -> tuple[uuid.UUID, uuid.UUID]:  # fmt: skip
    tenant = _id(conn, "insert into app.tenant (name, is_demo) values (%s, %s) returning id",
                 [name, demo])  # fmt: skip
    portfolio = _id(conn, "insert into app.portfolio (tenant_id, name) values (%s, 'book') "
                          "returning id", [tenant])  # fmt: skip
    conn.execute(
        """insert into app.portfolio_borrower (tenant_id, portfolio_id, cik, uploaded_name,
             match_status, created_at) values (%s, %s, %s, 'Borrower', 'confirmed', %s)""",
        [tenant, portfolio, CIK, added],
    )
    return tenant, portfolio


@pytest.fixture
def scored(db: psycopg.Connection) -> int:
    """One company: low -> elevated -> high -> high -> elevated -> high over six quarters."""
    model = db.execute(
        """insert into ml.model_version (algo, feature_set_version, train_window, is_active)
           values ('lightgbm', 'v1', '[2011-01-01,2019-01-01)', true) returning id"""
    ).fetchone()
    assert model is not None
    db.execute("insert into curated.company (cik, name) values (%s, 'Example Air')", [CIK])
    history = [("2023-12-31", "low"), ("2024-03-31", "elevated"), ("2024-06-30", "high"),
               ("2024-09-30", "high"), ("2024-12-31", "elevated"),
               ("2025-03-31", "high")]  # fmt: skip
    for as_of, tier in history:
        db.execute(
            """insert into ml.prediction (cik, as_of_date, model_version_id, probability,
                 risk_tier, top_drivers) values (%s, %s, %s, 0.05, %s, '[]')""",
            [CIK, as_of, model[0], tier],
        )
    return int(model[0])


def _alerts(db: psycopg.Connection, tenant: uuid.UUID) -> list[tuple[str, str | None, str]]:
    rows = db.execute(
        """select as_of_date::text, previous_tier, risk_tier from app.alert
           where tenant_id = %s order by as_of_date""",
        [tenant],
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def test_demo_tenant_gets_every_rise_dated_when_scored(db: psycopg.Connection, scored: int) -> None:
    demo, _ = _tenant(db, "Demo", demo=True)
    db.execute("select app.generate_alerts()")
    assert _alerts(db, demo) == [("2024-03-31", "low", "elevated"),
                                 ("2024-06-30", "elevated", "high"),
                                 ("2025-03-31", "elevated", "high")]  # fmt: skip
    triggered = db.execute("select min(triggered_at)::date from app.alert where tenant_id = %s",
                           [demo]).fetchone()  # fmt: skip
    assert triggered == (dt.date(2024, 3, 31),)


def test_lender_gets_baseline_then_changes_after_adding(
    db: psycopg.Connection, scored: int
) -> None:
    # Added 2024-08-01: the latest score then (2024-06-30) is high -> one "when added" alert;
    # the 2024-03-31 rise happened before and is not reported; the 2025-03-31 rise is.
    lender, _ = _tenant(db, "Lender", added="2024-08-01")
    first = db.execute("select app.generate_alerts()").fetchone()
    assert first == (2,)
    assert _alerts(db, lender) == [("2024-06-30", "elevated", "high"),
                                   ("2025-03-31", "elevated", "high")]  # fmt: skip
    reasons = [r[0] for r in db.execute(
        "select reason from app.alert where tenant_id = %s order by as_of_date", [lender]
    )]  # fmt: skip
    assert reasons[0].startswith("High risk when added")
    assert reasons[1].startswith("Risk tier rose from elevated to high")
    assert db.execute("select app.generate_alerts()").fetchone() == (0,)  # idempotent


def test_lender_adding_a_low_risk_company_gets_no_alert(
    db: psycopg.Connection, scored: int
) -> None:
    lender, _ = _tenant(db, "Lender", added="2025-01-15")  # latest then: 2024-12-31 elevated
    db.execute("select app.generate_alerts()")
    assert _alerts(db, lender) == [("2025-03-31", "elevated", "high")]


def test_request_serving_caps_and_skips_excluded(db: psycopg.Connection) -> None:
    db.execute(
        """insert into curated.company (cik, name, is_excluded, exclusion_reason) values
           ('0000000041', 'A', false, null), ('0000000042', 'B', false, null),
           ('0000000043', 'C Bank', true, 'banks'), ('0000000044', 'D', false, null)"""
    )
    db.execute("update ml.serving_limits set max_companies = 2")
    left_out = db.execute(
        "select * from ml.request_serving(%s)",
        [["0000000041", "0000000042", "0000000043", "0000000044", "0000000041"]],
    ).fetchall()
    assert left_out == [("0000000044",)]
    rows = db.execute("select cik, status from ml.served_company").fetchall()
    statuses: dict[str, str] = dict(rows)
    assert statuses == {"0000000041": "queued", "0000000042": "queued", "0000000043": "excluded"}


def test_views_stay_inside_the_callers_tenant(db: psycopg.Connection, scored: int) -> None:
    mine, my_portfolio = _tenant(db, "Mine")
    theirs, _ = _tenant(db, "Theirs")
    demo, _ = _tenant(db, "Demo", demo=True)
    me = _id(db, "insert into auth.users (email) values ('me@x.test') returning id")
    db.execute("insert into app.membership (user_id, tenant_id, role) values (%s, %s, 'analyst')",
               [me, mine])  # fmt: skip
    borrower = _id(db, "select id from app.portfolio_borrower where portfolio_id = %s",
                   [my_portfolio])  # fmt: skip
    db.execute("insert into app.match_review (tenant_id, borrower_id, candidate_ciks) "
               "values (%s, %s, '[]')", [mine, borrower])  # fmt: skip
    db.execute("select app.generate_alerts()")
    visitor = _id(db, "insert into auth.users (is_anonymous) values (true) returning id")

    def tenants_seen(view: str) -> set[uuid.UUID]:
        return {r[0] for r in db.execute(f"select tenant_id from {view}")}

    with acting_as(db, me):
        for view in ("api.portfolios", "api.borrowers", "api.alerts", "api.match_reviews"):
            assert tenants_seen(view) == {mine}, view
        assert db.execute("select id, role from api.tenants").fetchall() == [(mine, "analyst")]
        assert [r[1] for r in db.execute("select * from api.tenant_members(%s)", [mine])] == [
            "me@x.test"]  # fmt: skip
        assert db.execute("select * from api.tenant_members(%s)", [theirs]).fetchall() == []
        row = db.execute("select coverage, risk_tier from api.borrowers").fetchone()
        assert row == ("over_capacity", "high")  # matched, never queued; latest score high
    with acting_as(db, visitor, is_anonymous=True):
        assert tenants_seen("api.alerts") == {demo}
        assert db.execute("select id, role from api.tenants").fetchall() == [(demo, None)]


def test_data_health_reports_without_tenant_data(db: psycopg.Connection) -> None:
    user = _id(db, "insert into auth.users (email) values ('h@x.test') returning id")
    with acting_as(db, user):
        row = db.execute("select api.data_health()").fetchone()
    assert row is not None
    assert set(row[0]) == {"runs", "quarantine", "served", "max_companies", "latest_prediction",
                           "latest_filing", "companies_with_metrics", "text_signals"}  # fmt: skip


def test_no_baseline_alert_on_a_stale_score(db: psycopg.Connection, scored: int) -> None:
    # Latest score 2025-03-31 (high); added more than six months later: the company may have
    # stopped filing, so being "high when added" is not news. Later rises still alert.
    lender, _ = _tenant(db, "Lender", added="2025-12-01")
    db.execute("select app.generate_alerts()")
    assert _alerts(db, lender) == []
