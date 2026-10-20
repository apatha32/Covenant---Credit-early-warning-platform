"""Structural checks on the migrated schema: these fail when a new table forgets RLS,
tenant scoping, or leaks a grant."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

pytestmark = pytest.mark.db

OUR_SCHEMAS = ("util", "raw", "staging", "curated", "ml", "app")
PIPELINE_SCHEMAS = ("raw", "staging", "curated", "ml")


def test_all_schemas_exist(db: psycopg.Connection) -> None:
    rows = db.execute(
        "select nspname from pg_namespace where nspname = any(%s)", [list(OUR_SCHEMAS)]
    ).fetchall()
    assert {r[0] for r in rows} == set(OUR_SCHEMAS)


def test_every_table_has_rls_enabled(db: psycopg.Connection) -> None:
    rows = db.execute(
        """select n.nspname || '.' || c.relname
           from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where c.relkind in ('r', 'p') and n.nspname = any(%s) and not c.relrowsecurity""",
        [list(OUR_SCHEMAS)],
    ).fetchall()
    assert rows == [], f"tables without RLS: {[r[0] for r in rows]}"


def test_every_app_table_is_tenant_scoped_with_policies(db: psycopg.Connection) -> None:
    tables = db.execute("select tablename from pg_tables where schemaname = 'app'").fetchall()
    assert tables
    for (table,) in tables:
        tenant_col = "id" if table == "tenant" else "tenant_id"
        has_col = db.execute(
            """select 1 from information_schema.columns
               where table_schema = 'app' and table_name = %s and column_name = %s""",
            [table, tenant_col],
        ).fetchone()
        assert has_col, f"app.{table} has no {tenant_col} column"
        policies = db.execute(
            "select count(*) from pg_policies where schemaname = 'app' and tablename = %s",
            [table],
        ).fetchone()
        assert policies and policies[0] > 0, f"app.{table} has no RLS policies"


def test_anon_has_no_table_privileges(db: psycopg.Connection) -> None:
    rows = db.execute(
        """select table_schema || '.' || table_name, privilege_type
           from information_schema.role_table_grants
           where grantee in ('anon', 'PUBLIC') and table_schema = any(%s)""",
        [list(OUR_SCHEMAS)],
    ).fetchall()
    assert rows == []


def test_authenticated_has_no_pipeline_table_privileges(db: psycopg.Connection) -> None:
    rows = db.execute(
        """select table_schema || '.' || table_name, privilege_type
           from information_schema.role_table_grants
           where grantee = 'authenticated' and table_schema = any(%s)""",
        [list(PIPELINE_SCHEMAS)],
    ).fetchall()
    assert rows == []


def test_security_definer_functions_pin_search_path(db: psycopg.Connection) -> None:
    rows = db.execute(
        """select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = any(%s) and p.prosecdef
             and not exists (select 1 from unnest(coalesce(p.proconfig, '{}')) c
                             where c like 'search_path=%%')""",
        [list(OUR_SCHEMAS)],
    ).fetchall()
    assert rows == []


def test_no_function_executable_by_public(db: psycopg.Connection) -> None:
    rows = db.execute(
        """select n.nspname || '.' || p.proname
           from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = any(%s) and has_function_privilege('anon', p.oid, 'execute')""",
        [list(OUR_SCHEMAS)],
    ).fetchall()
    assert rows == []


# --- history guards ---


def _seed_company(db: psycopg.Connection) -> None:
    db.execute("insert into curated.company (cik, name) values ('0000000001', 'Acme')")


def test_financial_metric_is_append_only(db: psycopg.Connection) -> None:
    _seed_company(db)
    db.execute(
        """insert into curated.financial_metric
             (cik, canonical_metric, period_type, period_end, unit, value, source_tag,
              filed_date, accession, version)
           values ('0000000001', 'revenue', 'FY', '2023-12-31', 'USD', 100,
                   'us-gaap:Revenues', '2024-02-15', '0000000001-24-000001', 1)"""
    )
    for stmt in (
        "update curated.financial_metric set value = 200",
        "delete from curated.financial_metric",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(stmt)


def test_restatement_is_a_new_version(db: psycopg.Connection) -> None:
    _seed_company(db)
    insert = """insert into curated.financial_metric
             (cik, canonical_metric, period_type, period_end, unit, value, source_tag,
              filed_date, accession, version)
           values ('0000000001', 'revenue', 'FY', '2023-12-31', 'USD', %s,
                   'us-gaap:Revenues', %s, %s, %s)"""
    db.execute(insert, [100, "2024-02-15", "0000000001-24-000001", 1])
    db.execute(insert, [90, "2024-08-01", "0000000001-24-000050", 2])
    # Reusing a version number for the same period is rejected.
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute(insert, [95, "2024-09-01", "0000000001-24-000060", 2])


def test_derived_metric_requires_lineage(db: psycopg.Connection) -> None:
    _seed_company(db)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            """insert into curated.financial_metric
                 (cik, canonical_metric, period_type, period_end, unit, value, source_tag,
                  is_derived, filed_date, accession, version)
               values ('0000000001', 'revenue', 'Q', '2023-12-31', 'USD', 25,
                       'us-gaap:Revenues', true, '2024-02-15', '0000000001-24-000001', 1)"""
        )


def test_feature_snapshot_rejects_future_filed_inputs(db: psycopg.Connection) -> None:
    _seed_company(db)
    insert = """insert into ml.feature_snapshot
                  (cik, as_of_date, feature_set_version, features, max_source_filed_date)
                values ('0000000001', %s, 'v1', '{}', %s)"""
    db.execute(insert, [dt.date(2024, 3, 31), dt.date(2024, 3, 31)])
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(insert, [dt.date(2024, 6, 30), dt.date(2024, 7, 1)])


def test_identifier_formats_are_enforced(db: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("insert into curated.company (cik, name) values ('320193', 'unpadded')")
    _seed_company(db)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            """insert into curated.filing_event (cik, accession, event_type, filed_date)
               values ('0000000001', '000000000124000001', 'bankruptcy', '2024-01-01')"""
        )


def test_positive_label_requires_source_accession(db: psycopg.Connection) -> None:
    _seed_company(db)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            """insert into ml.label (cik, as_of_date, horizon_months, label_version, label)
               values ('0000000001', '2024-03-31', 12, '1', 1)"""
        )


def test_only_one_active_model(db: psycopg.Connection) -> None:
    insert = """insert into ml.model_version (algo, feature_set_version, train_window, is_active)
                values ('lightgbm', 'v1', '[2010-01-01,2020-01-01)', true)"""
    db.execute(insert)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute(insert)
