"""Phase 6b API: auth, tenant scope, upload -> review -> serving queue, alert dispositions.

Runs the real FastAPI app against a throwaway database; tokens are mapped to users by a fake
verifier (token verification itself is tested in test_auth.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from tripwire_api.app import create_app
from tripwire_api.auth import AuthError, Principal
from tripwire_common.settings import Settings

pytestmark = pytest.mark.db

SPIRIT, ACME, GLOBEX, BANK = "0001498710", "0000000001", "0000000002", "0000000003"


class _Pool:
    """Hands every request the test's connection, inside a savepoint (rolled back on error)."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        with self.conn.transaction():
            yield self.conn


class World:
    def __init__(self, db: psycopg.Connection) -> None:
        self.db = db
        self.users: dict[str, Principal] = {}
        for name in ("owner", "analyst", "viewer", "stranger"):
            row = db.execute("insert into auth.users (email) values (%s) returning id",
                             [f"{name}@bank.test"]).fetchone()  # fmt: skip
            assert row is not None
            self.users[name] = Principal(row[0], f"{name}@bank.test", False)
        visitor = db.execute(
            "insert into auth.users (is_anonymous) values (true) returning id"
        ).fetchone()
        assert visitor is not None
        self.users["visitor"] = Principal(visitor[0], None, True)

        def verify(token: str) -> Principal:
            if token not in self.users:
                raise AuthError("bad token")
            return self.users[token]

        settings = Settings(supabase_url="http://localhost")
        self.client = TestClient(create_app(settings, verifier=verify, pool=_Pool(db)))  # type: ignore[arg-type]

    def call(self, method: str, path: str, user: str | None, **kw: Any) -> Any:
        headers = {"Authorization": f"Bearer {user}"} if user else {}
        return self.client.request(method, path, headers=headers, **kw)


@pytest.fixture
def world(db: psycopg.Connection) -> World:
    db.execute(
        """insert into curated.company (cik, name, tickers, is_excluded, exclusion_reason) values
           (%s, 'Spirit Airlines, Inc.', '{SAVE}', false, null),
           (%s, 'Acme Widgets International, Inc.', '{ACME}', false, null),
           (%s, 'Globex Corporation', '{GLBX}', false, null),
           (%s, 'First Example Bancorp', '{FEXB}', true, 'banks (SIC 6021)')""",
        [SPIRIT, ACME, GLOBEX, BANK],
    )
    return World(db)


def _workspace(w: World) -> tuple[str, str]:
    r = w.call("POST", "/tenants", "owner", json={"name": "Northwind Credit"})
    assert r.status_code == 201, r.text
    body = r.json()
    for name, role in (("analyst", "analyst"), ("viewer", "viewer")):
        added = w.call("POST", f"/tenants/{body['tenant_id']}/members", "owner",
                       json={"email": f"{name}@bank.test", "role": role})  # fmt: skip
        assert added.status_code == 201, added.text
    return body["tenant_id"], body["portfolio_id"]


CSV = b"""Borrower Name,Ticker
Spirit Airlines,SAVE
Acme Widgets Intl,ACME
Globex Corp,
First Example Bancorp,FEXB
Acme Widget International,GLBX
"""


def _upload(w: World, portfolio: str, user: str = "analyst", data: bytes = CSV) -> Any:
    return w.call("POST", f"/portfolios/{portfolio}/upload", user,
                  files={"file": ("book.csv", data, "text/csv")})  # fmt: skip


def test_health(world: World) -> None:
    assert world.call("GET", "/health", None).json() == {"ok": True}


def test_writes_need_a_signed_in_account(world: World) -> None:
    assert world.call("POST", "/tenants", None, json={"name": "X Co"}).status_code == 401
    assert world.call("POST", "/tenants", "forged", json={"name": "X Co"}).status_code == 401
    r = world.call("POST", "/tenants", "visitor", json={"name": "X Co"})
    assert r.status_code == 403 and "read-only" in r.json()["error"]


def test_create_workspace_makes_owner_and_portfolio(world: World) -> None:
    tenant, portfolio = _workspace(world)
    roles: dict[str, str] = dict(world.db.execute(
        """select u.email, m.role from app.membership m join auth.users u on u.id = m.user_id
           where m.tenant_id = %s""", [tenant]).fetchall())  # fmt: skip
    assert roles == {"owner@bank.test": "owner", "analyst@bank.test": "analyst",
                     "viewer@bank.test": "viewer"}  # fmt: skip
    row = world.db.execute("select tenant_id::text from app.portfolio where id = %s",
                           [portfolio]).fetchone()  # fmt: skip
    assert row == (tenant,)
    actions = {r[0] for r in world.db.execute(
        "select action from app.audit_log where tenant_id = %s", [tenant])}  # fmt: skip
    assert {"tenant_create", "member_add"} <= actions


def test_member_rules(world: World) -> None:
    tenant, _ = _workspace(world)
    path = f"/tenants/{tenant}/members"
    assert world.call("POST", path, "analyst",
                      json={"email": "stranger@bank.test"}).status_code == 403  # fmt: skip
    assert world.call("POST", path, "stranger",
                      json={"email": "stranger@bank.test"}).status_code == 404  # fmt: skip
    r = world.call("POST", path, "owner", json={"email": "nobody@bank.test"})
    assert r.status_code == 404 and "sign up first" in r.json()["error"]
    assert world.call("POST", path, "owner",
                      json={"email": "analyst@bank.test"}).status_code == 409  # fmt: skip


def test_upload_matches_queues_serving_and_routes_to_review(world: World) -> None:
    _, portfolio = _workspace(world)
    r = _upload(world, portfolio)
    assert r.status_code == 200, r.text
    counts = r.json()["counts"]
    assert counts["auto_matched"] == 4 and counts["needs_review"] == 1
    rows = world.db.execute("select cik, status from ml.served_company").fetchall()
    served: dict[str, str] = dict(rows)
    assert served == {SPIRIT: "queued", ACME: "queued", GLOBEX: "queued", BANK: "excluded"}

    reviews = world.call("GET", f"/portfolios/{portfolio}/matches", "viewer").json()
    assert [x["uploaded_name"] for x in reviews] == ["Acme Widget International"]
    review = reviews[0]
    assert {c["cik"] for c in review["candidates"]} >= {ACME, GLOBEX}

    # viewers read but cannot decide; a stranger cannot even see it
    path = f"/matches/{review['id']}/decision"
    assert world.call("POST", path, "viewer", json={"decision": "reject"}).status_code == 403
    assert world.call("POST", path, "stranger", json={"decision": "reject"}).status_code == 404
    r = world.call("POST", path, "analyst", json={"decision": "accept", "cik": "1"})
    assert r.status_code == 200, r.text
    assert r.json()["borrowers_updated"] == 1
    assert world.call("GET", f"/portfolios/{portfolio}/matches", "viewer").json() == []

    # re-upload: nothing duplicated
    again = _upload(world, portfolio).json()
    assert again["counts"] == {"duplicate": 5}


def test_upload_is_tenant_scoped(world: World) -> None:
    _, portfolio = _workspace(world)
    assert _upload(world, portfolio, "stranger").status_code == 404
    assert _upload(world, portfolio, "viewer").status_code == 403
    assert _upload(world, portfolio, "visitor").status_code == 403
    assert _upload(world, str(uuid.uuid4()), "owner").status_code == 404
    bad = _upload(world, portfolio, data=b"foo,bar\n1,2\n")
    assert bad.status_code == 400 and "no borrower name" in bad.json()["error"]


def test_serving_capacity(world: World) -> None:
    world.db.execute("update ml.serving_limits set max_companies = 2")
    _, portfolio = _workspace(world)
    r = _upload(world, portfolio).json()
    assert r["over_capacity"] == [SPIRIT]  # queued in CIK order: 1, 2 fit; the bank takes no room
    coverage: dict[str, str | None] = dict(world.db.execute(
        """select uploaded_name, coverage from api.borrowers where portfolio_id = %s""",
        [portfolio]).fetchall())  # fmt: skip
    assert coverage["Spirit Airlines"] == "over_capacity"
    assert coverage["First Example Bancorp"] == "excluded"
    assert coverage["Acme Widget International"] is None  # waiting for review


def test_company_search(world: World) -> None:
    hits = world.call("GET", "/companies/search?q=SAVE", "analyst").json()
    assert hits[0]["cik"] == SPIRIT and hits[0]["kind"] == "ticker"
    hits = world.call("GET", "/companies/search?q=globex", "analyst").json()
    assert hits[0]["cik"] == GLOBEX
    assert world.call("GET", "/companies/search?q=globex", "visitor").status_code == 403


def test_delete_borrower(world: World) -> None:
    _, portfolio = _workspace(world)
    _upload(world, portfolio)
    row = world.db.execute("select id from app.portfolio_borrower where cik = %s",
                           [SPIRIT]).fetchone()  # fmt: skip
    assert row is not None
    assert world.call("DELETE", f"/borrowers/{row[0]}", "viewer").status_code == 403
    assert world.call("DELETE", f"/borrowers/{row[0]}", "analyst").status_code == 204
    assert world.db.execute("select 1 from app.portfolio_borrower where id = %s",
                            [row[0]]).fetchone() is None  # fmt: skip


def test_alert_disposition(world: World) -> None:
    tenant, _ = _workspace(world)
    row = world.db.execute(
        "insert into app.alert (tenant_id, cik, reason) values (%s, %s, 'x') returning id",
        [tenant, SPIRIT],
    ).fetchone()
    assert row is not None
    path = f"/alerts/{row[0]}/disposition"
    r = world.call("POST", path, "analyst", json={"decision": "dismiss"})
    assert r.status_code == 400 and "why" in r.json()["error"]
    assert world.call("POST", path, "viewer", json={"decision": "agree"}).status_code == 403
    assert world.call("POST", path, "stranger", json={"decision": "agree"}).status_code == 404
    r = world.call("POST", path, "analyst", json={"decision": "dismiss", "reason": "secured"})
    assert r.json() == {"status": "closed"}
    got = world.db.execute(
        """select a.status, d.decision, d.reason from app.alert a
           join app.disposition d on d.alert_id = a.id where a.id = %s""", [row[0]]
    ).fetchone()  # fmt: skip
    assert got == ("closed", "dismiss", "secured")
