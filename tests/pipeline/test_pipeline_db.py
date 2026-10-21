"""Phase 2 acceptance: the point-in-time query returns values as known on date T, and every
pipeline step is idempotent. Runs the real commands against a throwaway database."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
import pytest
from pipeline_fakes import FY_10K, fact, fiscal_2023_revenue, raw_frame

from tripwire_common.config import load_domain_pack
from tripwire_common.settings import REPO_ROOT
from tripwire_common.storage import LocalStorage
from tripwire_pipeline import cli
from tripwire_pipeline.load import ConsistencyError, load_metric_rows
from tripwire_pipeline.metrics import MetricRow

pytestmark = pytest.mark.db

CIK = "0000000001"
PACK = load_domain_pack("finance", REPO_ROOT / "config")
D = dt.date.fromisoformat
V = PACK.metrics.mapping_version


def _parquet(frame: pl.DataFrame) -> bytes:
    from io import BytesIO

    buf = BytesIO()
    frame.write_parquet(buf)
    return buf.getvalue()


def _land(db: psycopg.Connection, storage: LocalStorage, raw: list[dict[str, Any]],
          filings: pl.DataFrame | None = None) -> None:  # fmt: skip
    """Put raw files in storage and index them, as the ingest service would."""
    run = db.execute("insert into raw.ingest_runs (source) values ('test') returning id").fetchone()
    assert run is not None
    prefix = f"sec/companyfacts/cik={CIK}/sha256=test{len(raw)}"
    storage.write_bytes(f"{prefix}/facts.parquet", _parquet(raw_frame(raw)))
    db.execute(
        "insert into raw.companyfacts_index (cik, storage_path, checksum, run_id) "
        "values (%s, %s, %s, %s)",
        [CIK, prefix, f"test{len(raw)}", run[0]],
    )
    if filings is not None:
        sub = f"sec/submissions/cik={CIK}/sha256=test"
        storage.write_bytes(f"{sub}/filings.parquet", _parquet(filings))
        db.execute(
            "insert into raw.submissions_index (cik, storage_path, checksum, run_id) "
            "values (%s, %s, 'test', %s)",
            [CIK, sub, run[0]],
        )


@pytest.fixture
def ctx(db: psycopg.Connection, tmp_path: Path) -> cli.Context:
    db.execute("insert into curated.company (cik, name) values (%s, 'Acme')", [CIK])
    return cli.Context(db, PACK, LocalStorage(tmp_path), [CIK])


def _as_of(db: psycopg.Connection, as_of: str, metric: str = "revenue",
           version: str = V) -> dict[tuple[str, dt.date], Decimal]:  # fmt: skip
    rows = db.execute(
        """select period_type, period_end, value
           from curated.financial_metric_as_of(%s, %s, %s) where canonical_metric = %s""",
        [D(as_of), version, CIK, metric],
    ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def _counts(db: psycopg.Connection, source: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "select counts from raw.ingest_runs where source = %s order by id", [source]
    ).fetchall()
    return [r[0] for r in rows]


def test_point_in_time_query_returns_values_as_known_on_each_date(
    db: psycopg.Connection, ctx: cli.Context
) -> None:
    _land(db, ctx.storage, fiscal_2023_revenue(restate_fy=True))  # type: ignore[arg-type]
    cli.cmd_staging(ctx)
    cli.cmd_curated(ctx)

    q1, q2, q3, q4 = (("Q", D(d)) for d in ("2023-03-31", "2023-06-30", "2023-09-30",
                                            "2023-12-31"))  # fmt: skip
    fy = ("FY", D("2023-12-31"))
    # Before anything was filed: nothing is known.
    assert _as_of(db, "2023-05-04") == {}
    # After the Q3 10-Q: three quarters, no FY, no Q4.
    assert _as_of(db, "2023-11-03") == {q1: 100, q2: 110, q3: 120}
    # The day before the 10-K: still no FY / Q4 (leakage check on the derived value).
    assert _as_of(db, "2024-02-19") == {q1: 100, q2: 110, q3: 120}
    # 10-K filed: FY reported, Q4 derived as FY - 9M YTD.
    assert _as_of(db, "2024-02-20") == {q1: 100, q2: 110, q3: 120, q4: 130, fy: 460}
    # 10-K/A restates FY: both FY and the derived Q4 move; the earlier view is unchanged.
    assert _as_of(db, "2024-06-01") == {q1: 100, q2: 110, q3: 120, q4: 120, fy: 450}
    assert _as_of(db, "2024-05-31")[fy] == 460


def test_no_as_of_result_was_filed_after_the_as_of_date(
    db: psycopg.Connection, ctx: cli.Context
) -> None:
    _land(db, ctx.storage, fiscal_2023_revenue(restate_fy=True))  # type: ignore[arg-type]
    cli.cmd_staging(ctx)
    cli.cmd_curated(ctx)
    all_rows = db.execute(
        "select canonical_metric, period_type, period_end, filed_date, version, value "
        "from curated.financial_metric where cik = %s", [CIK]
    ).fetchall()  # fmt: skip
    day = D("2023-04-01")
    while day <= D("2024-07-01"):
        got = db.execute(
            "select canonical_metric, period_type, period_end, filed_date, value "
            "from curated.financial_metric_as_of(%s, %s, %s)", [day, V, CIK]
        ).fetchall()  # fmt: skip
        assert all(r[3] <= day for r in got), f"future-filed value visible on {day}"
        # And it is the latest known version per period (recomputed independently).
        expected: dict[tuple[str, str, dt.date], tuple[dt.date, int, Decimal]] = {}
        for metric, ptype, end, filed, version, value in all_rows:
            key = (metric, ptype, end)
            if filed <= day and (key not in expected or (filed, version) > expected[key][:2]):
                expected[key] = (filed, version, value)
        assert {(r[0], r[1], r[2]): r[4] for r in got} == {k: v[2] for k, v in expected.items()}
        day += dt.timedelta(days=7)


def test_derived_values_carry_lineage(db: psycopg.Connection, ctx: cli.Context) -> None:
    _land(db, ctx.storage, fiscal_2023_revenue())  # type: ignore[arg-type]
    cli.cmd_staging(ctx)
    cli.cmd_curated(ctx)
    row = db.execute(
        """select is_derived, accession, source_tag, derivation from curated.financial_metric
           where cik = %s and period_type = 'Q' and period_end = '2023-12-31'""",
        [CIK],
    ).fetchone()
    assert row is not None
    is_derived, accession, tag, derivation = row
    assert is_derived and accession == FY_10K and tag == "us-gaap:Revenues"
    assert [c["role"] for c in derivation] == ["total", "minus"]


def test_staging_and_curated_reruns_are_idempotent(
    db: psycopg.Connection, ctx: cli.Context
) -> None:
    cad = fact(
        "Revenues",
        1,
        "2023-03-31",
        "2023-05-05",
        "0000000001-23-000010",
        start="2023-01-01",
        unit="CAD",
    )
    raw = [*fiscal_2023_revenue(), cad]
    _land(db, ctx.storage, raw)  # type: ignore[arg-type]
    for _ in range(2):
        cli.cmd_staging(ctx)
        cli.cmd_curated(ctx)

    first, second = _counts(db, "pipeline_staging")
    assert first["facts_inserted"] == 6 and second["facts_inserted"] == 0
    assert first["quarantined_new"] == 1 and second["quarantined_new"] == 0
    c1, c2 = _counts(db, "pipeline_curated")
    # Q1-Q3 + FY reported, Q4 derived; the 6M/9M YTD values are derivation inputs only.
    assert c1["metric_rows_new"] == 5 and c2["metric_rows_new"] == 0
    assert c2["metric_rows_unchanged"] == 5


def test_new_filing_appends_versions_without_touching_old_rows(
    db: psycopg.Connection, ctx: cli.Context
) -> None:
    _land(db, ctx.storage, fiscal_2023_revenue())  # type: ignore[arg-type]
    cli.cmd_staging(ctx)
    cli.cmd_curated(ctx)
    before = db.execute(
        "select id, version, value from curated.financial_metric order by id"
    ).fetchall()

    _land(db, ctx.storage, fiscal_2023_revenue(restate_fy=True))  # type: ignore[arg-type]
    cli.cmd_staging(ctx)
    cli.cmd_curated(ctx)
    after = db.execute(
        "select id, version, value from curated.financial_metric order by id"
    ).fetchall()
    assert after[: len(before)] == before
    new = db.execute(
        "select period_type, version, value, is_derived from curated.financial_metric "
        "where id > %s order by period_type", [before[-1][0]]
    ).fetchall()  # fmt: skip
    assert new == [("FY", 2, 450, False), ("Q", 2, 120, True)]


def test_changed_mapping_without_version_bump_is_refused(
    db: psycopg.Connection, ctx: cli.Context
) -> None:
    _land(db, ctx.storage, fiscal_2023_revenue())  # type: ignore[arg-type]
    cli.cmd_staging(ctx)
    cli.cmd_curated(ctx)
    tampered = MetricRow(
        cik=CIK, metric="revenue", period_type="FY", period_start=D("2023-01-01"),
        period_end=D("2023-12-31"), unit="USD", value=999, source_tag="us-gaap:Revenues",
        is_derived=False, filed_date=D("2024-02-20"), accession=FY_10K,
    )  # fmt: skip
    with pytest.raises(ConsistencyError, match="bump"):
        load_metric_rows(db, CIK, [tampered], V)
    # Under a new mapping version the same period is a separate, independent series.
    bumped = str(int(V) + 1)
    assert load_metric_rows(db, CIK, [tampered], bumped)["metric_rows_new"] == 1
    assert _as_of(db, "2024-12-31", version=bumped) == {("FY", D("2023-12-31")): 999}
    assert _as_of(db, "2024-12-31")[("FY", D("2023-12-31"))] == 460


def test_curated_rows_cannot_be_truncated(db: psycopg.Connection, ctx: cli.Context) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("truncate curated.financial_metric")


def test_events_rerun_is_idempotent(db: psycopg.Connection, ctx: cli.Context) -> None:
    filings = pl.DataFrame({
        "cik": [CIK] * 3,
        "accessionNumber": ["0000000001-23-000001", "0000000001-23-000002", "0000000001-23-000003"],
        "form": ["8-K", "NT 10-Q", "10-Q"], "items": ["1.03,9.01", "", ""],
        "filingDate": ["2023-05-01", "2023-05-16", "2023-05-20"],
        "acceptanceDateTime": ["2023-05-01T16:05:00.000Z"] * 3,
        "reportDate": ["", "", "2023-03-31"], "primaryDocument": ["a.htm", "b.htm", "c.htm"],
    })  # fmt: skip
    _land(db, ctx.storage, fiscal_2023_revenue(), filings)  # type: ignore[arg-type]
    cli.cmd_events(ctx)
    cli.cmd_events(ctx)
    events = db.execute(
        "select event_type, form_type, raw_items from curated.filing_event order by filed_date"
    ).fetchall()
    assert events == [
        ("bankruptcy", "8-K", "1.03,9.01"),
        ("late_filing_quarterly", "NT 10-Q", None),
    ]
    first, second = _counts(db, "pipeline_events")
    assert first["filings_inserted"] == 3 and second["filings_inserted"] == 0
    assert second["events_inserted"] == 0
    items = db.execute("select items from staging.filings where form_type = '8-K'").fetchone()
    assert items == (["1.03", "9.01"],)


def test_coverage_report_renders(db: psycopg.Connection, ctx: cli.Context) -> None:
    from tripwire_pipeline.coverage import render_coverage

    _land(db, ctx.storage, fiscal_2023_revenue())  # type: ignore[arg-type]
    cli.cmd_staging(ctx)
    cli.cmd_curated(ctx)
    report = render_coverage(db, [CIK], PACK.metrics, D("2024-12-31"))
    assert "| revenue | duration | 1/1 (100%)" in report
    assert "Revenues 100%" in report
