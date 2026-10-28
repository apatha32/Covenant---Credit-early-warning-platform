"""Phase 6a acceptance: anonymous visitors (Supabase anonymous sign-in) read the demo tenant
and nothing else, and cannot write anything anywhere."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import sql
from rls_helpers import acting_as, denied

pytestmark = pytest.mark.db

DEMO_CIKS = ["0000000011", "0000000012"]
OTHER_CIK = "0000000021"
ACCESSION = "0000000011-24-000001"


@dataclass(frozen=True)
class DemoWorld:
    demo_tenant: uuid.UUID
    demo_portfolio: uuid.UUID
    other_tenant: uuid.UUID
    other_portfolio: uuid.UUID
    visitor: uuid.UUID  # anonymous sign-in
    member: uuid.UUID  # owner of the other tenant
    stranger: uuid.UUID  # signed-in (not anonymous), no membership


def _id(conn: psycopg.Connection, query: str, params: list[object] | None = None) -> uuid.UUID:
    row = conn.execute(query, params or []).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _company_with_data(conn: psycopg.Connection, cik: str, model_id: int) -> None:
    conn.execute("insert into curated.company (cik, name, sic) values (%s, %s, 3721)",
                 [cik, f"Company {cik}"])  # fmt: skip
    accession = f"{cik}-24-000001"
    conn.execute(
        """insert into curated.financial_metric (cik, canonical_metric, period_type, period_end,
             unit, value, source_tag, filed_date, accession, version, mapping_version)
           values (%s, 'cash', 'Q', '2024-03-31', 'USD', 1, 'us-gaap:Cash', '2024-05-01', %s, 1,
                   '2')""",
        [cik, accession],
    )
    conn.execute(
        """insert into curated.filing_event (cik, accession, event_type, filed_date, form_type)
           values (%s, %s, 'auditor_change', '2024-05-01', '8-K')""",
        [cik, accession],
    )
    conn.execute(
        """insert into curated.bankruptcy_case (cik, case_start, accession, label_version)
           values (%s, '2024-11-18', %s, 'v6')""",
        [cik, accession],
    )
    for as_of, p in (("2024-03-31", 0.02), ("2024-06-30", 0.08)):
        conn.execute(
            """insert into ml.prediction (cik, as_of_date, model_version_id, probability,
                 risk_tier, top_drivers)
               values (%s, %s, %s, %s, 'high', '[{"text": "Cash / total assets fell"}]')""",
            [cik, as_of, model_id, p],
        )


def _portfolio(conn: psycopg.Connection, tenant: uuid.UUID, ciks: list[str]) -> uuid.UUID:
    portfolio = _id(conn, "insert into app.portfolio (tenant_id, name) values (%s, 'book') "
                          "returning id", [tenant])  # fmt: skip
    for cik in ciks:
        conn.execute(
            """insert into app.portfolio_borrower (tenant_id, portfolio_id, cik, uploaded_name,
                 match_status) values (%s, %s, %s, %s, 'confirmed')""",
            [tenant, portfolio, cik, f"Borrower {cik}"],
        )
    return portfolio


@pytest.fixture
def demo(db: psycopg.Connection) -> DemoWorld:
    model = db.execute(
        """insert into ml.model_version (algo, feature_set_version, train_window, is_active)
           values ('lightgbm', 'v1', '[2011-01-01,2019-01-01)', true) returning id"""
    ).fetchone()
    assert model is not None
    for cik in [*DEMO_CIKS, OTHER_CIK]:
        _company_with_data(db, cik, model[0])
    demo_tenant = _id(db, "insert into app.tenant (name, is_demo) values ('Demo', true) "
                          "returning id")  # fmt: skip
    other_tenant = _id(db, "insert into app.tenant (name) values ('Lender B') returning id")
    demo_portfolio = _portfolio(db, demo_tenant, DEMO_CIKS)
    other_portfolio = _portfolio(db, other_tenant, [OTHER_CIK])
    visitor = _id(db, "insert into auth.users (email) values (null) returning id")
    member = _id(db, "insert into auth.users (email) values ('b@b.test') returning id")
    stranger = _id(db, "insert into auth.users (email) values ('s@x.test') returning id")
    db.execute("insert into app.membership (user_id, tenant_id, role) values (%s, %s, 'owner')",
               [member, other_tenant])  # fmt: skip
    alert = _id(db, "insert into app.alert (tenant_id, cik, reason) values (%s, %s, 'x') "
                    "returning id", [demo_tenant, DEMO_CIKS[0]])  # fmt: skip
    db.execute("insert into app.alert (tenant_id, cik, reason) values (%s, %s, 'x')",
               [other_tenant, OTHER_CIK])  # fmt: skip
    db.execute("insert into app.disposition (tenant_id, alert_id, user_id, decision) "
               "values (%s, %s, %s, 'agree')", [demo_tenant, alert, member])  # fmt: skip
    return DemoWorld(demo_tenant, demo_portfolio, other_tenant, other_portfolio, visitor,
                     member, stranger)  # fmt: skip


def _as_visitor(db: psycopg.Connection, w: DemoWorld):  # type: ignore[no-untyped-def]
    return acting_as(db, w.visitor, is_anonymous=True)


def _ciks(db: psycopg.Connection, relation: str) -> set[str]:
    query = sql.SQL("select distinct cik from {}").format(sql.SQL(relation))
    return {r[0] for r in db.execute(query)}


# --- reads ---

API_VIEWS = ["api.watchlist", "api.company", "api.score_history", "api.metric_history",
             "api.filing_events"]  # fmt: skip


@pytest.mark.parametrize("view", API_VIEWS)
def test_visitor_reads_demo_companies_only(
    db: psycopg.Connection, demo: DemoWorld, view: str
) -> None:
    with _as_visitor(db, demo):
        assert _ciks(db, view) == set(DEMO_CIKS)


def test_visitor_sees_demo_tenant_and_portfolio_only(
    db: psycopg.Connection, demo: DemoWorld
) -> None:
    with _as_visitor(db, demo):
        assert [r[0] for r in db.execute("select id from app.tenant")] == [demo.demo_tenant]
        assert [r[0] for r in db.execute("select id from app.portfolio")] == [demo.demo_portfolio]
        tenants = {r[0] for r in db.execute("select tenant_id from api.watchlist")}
        assert tenants == {demo.demo_tenant}
        assert (
            db.execute(
                "select 1 from app.portfolio where id = %s", [demo.other_portfolio]
            ).fetchall()
            == []
        )
        assert db.execute("select 1 from app.alert where tenant_id = %s",
                          [demo.other_tenant]).fetchall() == []  # fmt: skip


def test_watchlist_rows(db: psycopg.Connection, demo: DemoWorld) -> None:
    with _as_visitor(db, demo):
        row = db.execute(
            """select name, sector, top_driver, sparkline, next_case_start from api.watchlist
               where cik = %s and as_of_date = '2024-06-30'""",
            [DEMO_CIKS[0]],
        ).fetchone()
    assert row is not None
    name, sector, driver, sparkline, next_case = row
    assert (name, sector, driver) == (f"Borrower {DEMO_CIKS[0]}", "Manufacturing",
                                      "Cash / total assets fell")  # fmt: skip
    assert [float(x) for x in sparkline] == [0.02, 0.08]
    assert str(next_case) == "2024-11-18"


def test_signed_in_user_without_membership_does_not_see_demo(
    db: psycopg.Connection, demo: DemoWorld
) -> None:
    with acting_as(db, demo.stranger, is_anonymous=False):
        assert db.execute("select 1 from api.watchlist").fetchall() == []
        assert db.execute("select 1 from app.tenant").fetchall() == []


def test_other_tenant_does_not_see_demo_or_gain_it(db: psycopg.Connection, demo: DemoWorld) -> None:
    with acting_as(db, demo.member):
        assert _ciks(db, "api.watchlist") == {OTHER_CIK}


@pytest.mark.parametrize("relation", [*API_VIEWS, "api.as_of_dates", "app.tenant"])
def test_anon_key_without_sign_in_reads_nothing(
    db: psycopg.Connection, demo: DemoWorld, relation: str
) -> None:
    with acting_as(db, None, role="anon"), denied(db):
        db.execute(sql.SQL("select 1 from {}").format(sql.SQL(relation)))


# --- writes: every one must fail ---


def _blocked(db: psycopg.Connection, statement: str, params: list[object]) -> None:
    """Denied outright, or (RLS-filtered update / delete) affects no rows."""
    try:
        with db.transaction():
            cur = db.execute(statement, params)
            assert cur.rowcount == 0, f"visitor changed {cur.rowcount} rows: {statement}"
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.FeatureNotSupported,
            psycopg.errors.ObjectNotInPrerequisiteState):  # fmt: skip
        pass


def _app_tables(db: psycopg.Connection) -> list[str]:
    return [r[0] for r in db.execute(
        "select tablename from pg_tables where schemaname = 'app' order by 1")]  # fmt: skip


def test_visitor_cannot_insert_anything(db: psycopg.Connection, demo: DemoWorld) -> None:
    t, p, v = demo.demo_tenant, demo.demo_portfolio, demo.visitor
    inserts: list[tuple[str, list[object]]] = [
        ("insert into app.tenant (name) values ('mine')", []),
        ("insert into app.membership (user_id, tenant_id, role) values (%s, %s, 'owner')", [v, t]),
        ("insert into app.membership (user_id, tenant_id, role) values (%s, %s, 'viewer')", [v, t]),
        ("insert into app.portfolio (tenant_id, name) values (%s, 'x')", [t]),
        ("insert into app.portfolio_borrower (tenant_id, portfolio_id, uploaded_name) "
         "values (%s, %s, 'x')", [t, p]),
        ("insert into app.match_review (tenant_id, borrower_id, candidate_ciks) "
         "select tenant_id, id, '[]' from app.portfolio_borrower limit 1", []),
        ("insert into app.alert (tenant_id, cik, reason) values (%s, %s, 'x')", [t, DEMO_CIKS[0]]),
        ("insert into app.disposition (tenant_id, alert_id, decision) "
         "select tenant_id, id, 'agree' from app.alert limit 1", []),
        ("insert into app.audit_log (tenant_id, action) values (%s, 'x')", [t]),
        ("insert into app.entity_alias (tenant_id, input_key, decision, decided_by) "
         "values (%s, 'x|', 'no_match', %s)", [t, v]),
        ("insert into curated.company (cik, name) values ('0000000099', 'x')", []),
        ("insert into ml.prediction (cik, as_of_date, model_version_id, probability, risk_tier) "
         "select cik, '2030-01-01', model_version_id, 1, 'high' from ml.prediction limit 1", []),
        ("insert into curated.bankruptcy_case (cik, case_start, accession, label_version) "
         "values (%s, '2030-01-01', %s, 'x')", [DEMO_CIKS[0], ACCESSION]),
    ]  # fmt: skip
    covered = {s.split()[2].split(".")[1] for s, _ in inserts if s.split()[2].startswith("app.")}
    assert covered == set(_app_tables(db)), "add an insert case for every app table"
    for statement, params in inserts:
        with _as_visitor(db, demo), denied(db):
            db.execute(statement, params)


def test_visitor_cannot_update_or_delete_anything(db: psycopg.Connection, demo: DemoWorld) -> None:
    tables = [f"app.{t}" for t in _app_tables(db)] + [
        "curated.company", "curated.financial_metric", "curated.filing_event",
        "curated.bankruptcy_case", "ml.prediction", "ml.model_version", *API_VIEWS,
    ]  # fmt: skip
    before = _snapshot(db)
    for table in tables:
        column = db.execute(
            """select a.attname from pg_attribute a where a.attrelid = %s::regclass
               and a.attnum > 0 and not a.attisdropped
               and a.attname not in ('id') order by a.attnum limit 1""",
            [table],
        ).fetchone()
        assert column is not None
        col = sql.Identifier(column[0])
        with _as_visitor(db, demo):
            _blocked(db, sql.SQL("update {} set {} = {}").format(sql.SQL(table), col, col)
                     .as_string(db), [])  # fmt: skip
            _blocked(db, f"delete from {table}", [])
    assert _snapshot(db) == before


def _snapshot(db: psycopg.Connection) -> dict[str, int]:
    tables = ["app.tenant", "app.portfolio", "app.portfolio_borrower", "app.alert",
              "app.disposition", "curated.company", "ml.prediction"]  # fmt: skip
    out = {}
    for t in tables:
        row = db.execute(f"select count(*) from {t}").fetchone()
        assert row is not None
        out[t] = int(row[0])
    return out


def test_anonymous_flag_alone_blocks_writes(db: psycopg.Connection, demo: DemoWorld) -> None:
    """Even an anonymous session holding an owner membership (it never should) cannot write:
    the restrictive policies do not depend on the membership checks."""
    db.execute("insert into app.membership (user_id, tenant_id, role) values (%s, %s, 'owner')",
               [demo.visitor, demo.demo_tenant])  # fmt: skip
    insert = "insert into app.portfolio (tenant_id, name) values (%s, 'x')"
    with _as_visitor(db, demo), denied(db):
        db.execute(insert, [demo.demo_tenant])
    with acting_as(db, demo.visitor, is_anonymous=False):  # same user, signed in for real
        db.execute(insert, [demo.demo_tenant])
