"""Phase 0 acceptance: tenant A cannot read or modify tenant B's rows.

Tests run as the `authenticated` role with Supabase-style JWT claims, exactly how PostgREST
and supabase-js reach the database. Seed data is inserted as superuser (bypasses RLS).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import sql
from rls_helpers import acting_as, denied

pytestmark = pytest.mark.db

CIK = "0000320193"

# Every app table and the column holding its tenant.
APP_TABLES = {
    "tenant": "id",
    "membership": "tenant_id",
    "portfolio": "tenant_id",
    "portfolio_borrower": "tenant_id",
    "match_review": "tenant_id",
    "alert": "tenant_id",
    "disposition": "tenant_id",
    "audit_log": "tenant_id",
    "entity_alias": "tenant_id",
}


@dataclass(frozen=True)
class TenantRows:
    tenant_id: uuid.UUID
    portfolio_id: uuid.UUID
    borrower_id: uuid.UUID
    alert_id: uuid.UUID


@dataclass(frozen=True)
class World:
    a: TenantRows
    b: TenantRows
    alice: uuid.UUID  # analyst in A
    ada: uuid.UUID  # admin in A
    victor: uuid.UUID  # viewer in A
    bob: uuid.UUID  # owner in B
    nobody: uuid.UUID  # authenticated, no membership


def _user(conn: psycopg.Connection, email: str) -> uuid.UUID:
    row = conn.execute(
        "insert into auth.users (email) values (%s) returning id", [email]
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _one(conn: psycopg.Connection, query: str, params: list[object]) -> uuid.UUID:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _seed_tenant(conn: psycopg.Connection, name: str, member: uuid.UUID) -> TenantRows:
    tenant_id = _one(conn, "insert into app.tenant (name) values (%s) returning id", [name])
    portfolio_id = _one(
        conn,
        "insert into app.portfolio (tenant_id, name, created_by) values (%s, %s, %s) returning id",
        [tenant_id, f"{name} book", member],
    )
    borrower_id = _one(
        conn,
        """insert into app.portfolio_borrower (tenant_id, portfolio_id, cik, uploaded_name,
             match_status) values (%s, %s, %s, 'Apple', 'confirmed') returning id""",
        [tenant_id, portfolio_id, CIK],
    )
    conn.execute(
        """insert into app.match_review (tenant_id, borrower_id, candidate_ciks)
           values (%s, %s, '[]')""",
        [tenant_id, borrower_id],
    )
    alert_id = _one(
        conn,
        "insert into app.alert (tenant_id, cik, reason) values (%s, %s, 'test') returning id",
        [tenant_id, CIK],
    )
    conn.execute(
        """insert into app.disposition (tenant_id, alert_id, user_id, decision)
           values (%s, %s, %s, 'agree')""",
        [tenant_id, alert_id, member],
    )
    conn.execute(
        "insert into app.audit_log (tenant_id, actor_user_id, action) values (%s, %s, 'seed')",
        [tenant_id, member],
    )
    conn.execute(
        """insert into app.entity_alias (tenant_id, input_key, cik, decision, decided_by)
           values (%s, 'apple|', %s, 'accept', %s)""",
        [tenant_id, CIK, member],
    )
    return TenantRows(tenant_id, portfolio_id, borrower_id, alert_id)


def _member(conn: psycopg.Connection, user: uuid.UUID, tenant: uuid.UUID, role: str) -> None:
    conn.execute(
        "insert into app.membership (user_id, tenant_id, role) values (%s, %s, %s)",
        [user, tenant, role],
    )


@pytest.fixture
def world(db: psycopg.Connection) -> World:
    db.execute(
        "insert into curated.company (cik, name, tickers) values (%s, 'Apple Inc.', '{AAPL}')",
        [CIK],
    )
    alice, ada, victor = _user(db, "alice@a.test"), _user(db, "ada@a.test"), _user(db, "v@a.test")
    bob, nobody = _user(db, "bob@b.test"), _user(db, "nobody@x.test")
    a = _seed_tenant(db, "Tenant A", alice)
    b = _seed_tenant(db, "Tenant B", bob)
    _member(db, alice, a.tenant_id, "analyst")
    _member(db, ada, a.tenant_id, "admin")
    _member(db, victor, a.tenant_id, "viewer")
    _member(db, bob, b.tenant_id, "owner")
    return World(a, b, alice, ada, victor, bob, nobody)


def _visible_tenants(conn: psycopg.Connection, table: str, column: str) -> list[uuid.UUID]:
    query = sql.SQL("select {} from app.{}").format(sql.Identifier(column), sql.Identifier(table))
    return [uuid.UUID(str(r[0])) for r in conn.execute(query).fetchall()]


# --- reads ---


@pytest.mark.parametrize(("table", "column"), APP_TABLES.items())
def test_tenant_a_reads_only_tenant_a_rows(
    db: psycopg.Connection, world: World, table: str, column: str
) -> None:
    # alice is an analyst and cannot read audit_log; ada (admin, same tenant) can.
    reader = world.ada if table == "audit_log" else world.alice
    with acting_as(db, reader):
        tenants = _visible_tenants(db, table, column)
    assert tenants, f"tenant A should see its own app.{table} rows"
    assert set(tenants) == {world.a.tenant_id}


@pytest.mark.parametrize(("table", "column"), APP_TABLES.items())
def test_tenant_b_reads_only_tenant_b_rows(
    db: psycopg.Connection, world: World, table: str, column: str
) -> None:
    with acting_as(db, world.bob):
        tenants = _visible_tenants(db, table, column)
    assert set(tenants) == {world.b.tenant_id}


@pytest.mark.parametrize(("table", "column"), APP_TABLES.items())
def test_user_without_membership_sees_nothing(
    db: psycopg.Connection, world: World, table: str, column: str
) -> None:
    with acting_as(db, world.nobody):
        assert _visible_tenants(db, table, column) == []


def test_lookup_by_id_of_other_tenants_row_returns_nothing(
    db: psycopg.Connection, world: World
) -> None:
    with acting_as(db, world.alice):
        rows = db.execute("select * from app.portfolio where id = %s", [world.b.portfolio_id])
        assert rows.fetchall() == []


@pytest.mark.parametrize("table", APP_TABLES)
def test_anon_has_no_access(db: psycopg.Connection, world: World, table: str) -> None:
    with acting_as(db, None, role="anon"), denied(db):
        db.execute(sql.SQL("select 1 from app.{}").format(sql.Identifier(table)))


@pytest.mark.parametrize(
    "table",
    [
        "raw.ingest_runs",
        "raw.source_files",
        "staging.facts",
        "staging.filings",
        "curated.macro_series",
        "curated.text_signal",
        "ml.feature_snapshot",
        "ml.label",
    ],
)
def test_authenticated_cannot_read_pipeline_tables(
    db: psycopg.Connection, world: World, table: str
) -> None:
    schema, name = table.split(".")
    with acting_as(db, world.alice), denied(db):
        db.execute(
            sql.SQL("select 1 from {}.{}").format(sql.Identifier(schema), sql.Identifier(name))
        )


@pytest.mark.parametrize(
    "table",
    ["curated.company", "curated.financial_metric", "curated.filing_event",
     "curated.bankruptcy_case", "ml.prediction"],
)  # fmt: skip
def test_shared_data_is_limited_to_own_portfolio_companies(
    db: psycopg.Connection, world: World, table: str
) -> None:
    other = "0000000002"  # a company no tenant holds
    db.execute("insert into curated.company (cik, name) values (%s, 'Other Co')", [other])
    _seed_company_data(db, CIK)
    _seed_company_data(db, other)
    query = sql.SQL("select distinct cik from {}").format(sql.SQL(table))
    with acting_as(db, world.alice):
        assert [r[0] for r in db.execute(query)] == [CIK]
    with acting_as(db, world.nobody):
        assert db.execute(query).fetchall() == []


def _seed_company_data(conn: psycopg.Connection, cik: str) -> None:
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
           values (%s, '2024-06-01', %s, 'v6')""",
        [cik, accession],
    )
    model = conn.execute(
        """insert into ml.model_version (algo, feature_set_version, train_window)
           values ('lightgbm', 'v1', '[2011-01-01,2019-01-01)') returning id"""
    ).fetchone()
    assert model is not None
    conn.execute(
        """insert into ml.prediction (cik, as_of_date, model_version_id, probability, risk_tier)
           values (%s, '2024-03-31', %s, 0.1, 'high')""",
        [cik, model[0]],
    )


# --- writes ---


def test_cannot_insert_into_other_tenant(db: psycopg.Connection, world: World) -> None:
    with acting_as(db, world.alice), denied(db):
        db.execute(
            "insert into app.portfolio (tenant_id, name) values (%s, 'sneaky')",
            [world.b.tenant_id],
        )


def test_cannot_update_or_delete_other_tenants_rows(db: psycopg.Connection, world: World) -> None:
    with acting_as(db, world.alice):
        updated = db.execute(
            "update app.portfolio set name = 'pwned' where id = %s", [world.b.portfolio_id]
        )
        assert updated.rowcount == 0
        deleted = db.execute(
            "delete from app.portfolio_borrower where tenant_id = %s", [world.b.tenant_id]
        )
        assert deleted.rowcount == 0
        alerts = db.execute(
            "update app.alert set status = 'closed' where id = %s", [world.b.alert_id]
        )
        assert alerts.rowcount == 0

    row = db.execute("select name from app.portfolio where id = %s", [world.b.portfolio_id])
    assert row.fetchone() == ("Tenant B book",)
    count = db.execute(
        "select count(*) from app.portfolio_borrower where tenant_id = %s", [world.b.tenant_id]
    )
    assert count.fetchone() == (1,)


def test_cannot_attach_own_row_to_other_tenants_parent(
    db: psycopg.Connection, world: World
) -> None:
    # tenant_id says A, but the portfolio belongs to B: the composite FK rejects it.
    with (
        acting_as(db, world.alice),
        pytest.raises(psycopg.errors.ForeignKeyViolation),
        db.transaction(),
    ):
        db.execute(
            """insert into app.portfolio_borrower (tenant_id, portfolio_id, uploaded_name)
               values (%s, %s, 'x')""",
            [world.a.tenant_id, world.b.portfolio_id],
        )


def test_cannot_disposition_other_tenants_alert(db: psycopg.Connection, world: World) -> None:
    with acting_as(db, world.alice), denied(db):
        db.execute(
            "insert into app.disposition (tenant_id, alert_id, decision) values (%s, %s, 'agree')",
            [world.b.tenant_id, world.b.alert_id],
        )


def test_analyst_can_write_own_tenant(db: psycopg.Connection, world: World) -> None:
    with acting_as(db, world.alice):
        db.execute(
            "insert into app.portfolio (tenant_id, name) values (%s, 'new')", [world.a.tenant_id]
        )
        db.execute("update app.alert set status = 'acknowledged' where id = %s", [world.a.alert_id])
        db.execute(
            "insert into app.disposition (tenant_id, alert_id, decision, reason) "
            "values (%s, %s, 'dismiss', 'known issue')",
            [world.a.tenant_id, world.a.alert_id],
        )
        row = db.execute(
            "select user_id from app.disposition where reason = 'known issue'"
        ).fetchone()
    assert row is not None and uuid.UUID(str(row[0])) == world.alice


def test_viewer_is_read_only(db: psycopg.Connection, world: World) -> None:
    with acting_as(db, world.victor):
        assert _visible_tenants(db, "portfolio", "tenant_id") == [world.a.tenant_id]
        updated = db.execute(
            "update app.alert set status = 'closed' where id = %s", [world.a.alert_id]
        )
        assert updated.rowcount == 0
    with acting_as(db, world.victor), denied(db):
        db.execute(
            "insert into app.portfolio (tenant_id, name) values (%s, 'v')", [world.a.tenant_id]
        )


def test_disposition_cannot_impersonate_another_user(db: psycopg.Connection, world: World) -> None:
    with acting_as(db, world.alice), denied(db):
        db.execute(
            "insert into app.disposition (tenant_id, alert_id, user_id, decision) "
            "values (%s, %s, %s, 'agree')",
            [world.a.tenant_id, world.a.alert_id, world.victor],
        )


def test_alert_users_may_change_status_only(db: psycopg.Connection, world: World) -> None:
    with acting_as(db, world.alice), denied(db):
        db.execute("update app.alert set reason = 'edited' where id = %s", [world.a.alert_id])


def test_cannot_move_row_to_another_tenant(db: psycopg.Connection, world: World) -> None:
    # tenant_id is not in the column-level UPDATE grant.
    with acting_as(db, world.alice), denied(db):
        db.execute(
            "update app.portfolio set tenant_id = %s where id = %s",
            [world.b.tenant_id, world.a.portfolio_id],
        )


def test_admin_cannot_grant_owner_but_can_add_viewer(db: psycopg.Connection, world: World) -> None:
    with acting_as(db, world.ada), denied(db):
        db.execute(
            "insert into app.membership (user_id, tenant_id, role) values (%s, %s, 'owner')",
            [world.nobody, world.a.tenant_id],
        )
    with acting_as(db, world.ada):
        db.execute(
            "insert into app.membership (user_id, tenant_id, role) values (%s, %s, 'viewer')",
            [world.nobody, world.a.tenant_id],
        )


def test_admin_cannot_add_members_to_other_tenant(db: psycopg.Connection, world: World) -> None:
    with acting_as(db, world.ada), denied(db):
        db.execute(
            "insert into app.membership (user_id, tenant_id, role) values (%s, %s, 'viewer')",
            [world.ada, world.b.tenant_id],
        )


def test_analyst_cannot_manage_membership(db: psycopg.Connection, world: World) -> None:
    with acting_as(db, world.alice), denied(db):
        db.execute(
            "insert into app.membership (user_id, tenant_id, role) values (%s, %s, 'viewer')",
            [world.nobody, world.a.tenant_id],
        )


def test_cannot_record_match_decision_for_other_tenant_or_as_someone_else(
    db: psycopg.Connection, world: World
) -> None:
    insert = """insert into app.entity_alias (tenant_id, input_key, cik, decision, decided_by)
                values (%s, 'x|', %s, 'accept', %s)"""
    with acting_as(db, world.alice), denied(db):
        db.execute(insert, [world.b.tenant_id, CIK, world.alice])
    with acting_as(db, world.alice), denied(db):
        db.execute(insert, [world.a.tenant_id, CIK, world.victor])
    with acting_as(db, world.victor), denied(db):  # viewers cannot decide
        db.execute(insert, [world.a.tenant_id, CIK, world.victor])
    with acting_as(db, world.alice):
        db.execute(insert, [world.a.tenant_id, CIK, world.alice])
