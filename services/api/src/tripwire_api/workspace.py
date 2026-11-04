"""Workspace actions the API performs for a verified user (service-role connection).

Every function checks the user's role in the target tenant itself; RLS does not apply to the
service role. Each change is written to app.audit_log.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

WRITE_ROLES = ("owner", "admin", "analyst")
MAX_OWNED_TENANTS = 5

Role = Literal["owner", "admin", "analyst", "viewer"]


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def not_found(what: str) -> ApiError:
    return ApiError(404, f"{what} not found")


def role_in(conn: psycopg.Connection, tenant_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
    row = conn.execute(
        "select role from app.membership where tenant_id = %s and user_id = %s",
        [tenant_id, user_id],
    ).fetchone()
    return str(row[0]) if row else None


def require_role(
    conn: psycopg.Connection, tenant_id: uuid.UUID, user_id: uuid.UUID,
    roles: tuple[str, ...] = WRITE_ROLES, what: str = "workspace",
) -> str:  # fmt: skip
    """The user's role, if it is one of `roles`. Non-members get 404 (nothing to see)."""
    role = role_in(conn, tenant_id, user_id)
    if role is None:
        raise not_found(what)
    if role not in roles:
        raise ApiError(403, f"your role ({role}) cannot do this")
    return role


def audit(
    conn: psycopg.Connection, tenant_id: uuid.UUID, actor: uuid.UUID, action: str,
    entity_type: str, entity_id: object, details: dict[str, Any] | None = None,
) -> None:  # fmt: skip
    conn.execute(
        """insert into app.audit_log (tenant_id, actor_user_id, action, entity_type, entity_id,
                                      details) values (%s, %s, %s, %s, %s, %s)""",
        [tenant_id, actor, action, entity_type, str(entity_id), Jsonb(details or {})],
    )


def tenant_of(conn: psycopg.Connection, table: str, entity_id: uuid.UUID) -> uuid.UUID:
    """tenant_id of a row in an app table (fixed table names only)."""
    assert table in {"portfolio", "portfolio_borrower", "match_review", "alert"}
    row = conn.execute(
        f"select tenant_id from app.{table} where id = %s",  # noqa: S608
        [entity_id],
    ).fetchone()
    if row is None:
        raise not_found(table.replace("_", " "))
    return uuid.UUID(str(row[0]))


def create_tenant(conn: psycopg.Connection, user_id: uuid.UUID, name: str) -> dict[str, str]:
    """A new workspace owned by the user, with one empty portfolio."""
    name = name.strip()
    if not 2 <= len(name) <= 80:
        raise ApiError(400, "workspace name must be 2-80 characters")
    owned = conn.execute(
        "select count(*) from app.membership where user_id = %s and role = 'owner'", [user_id]
    ).fetchone()
    if owned and owned[0] >= MAX_OWNED_TENANTS:
        raise ApiError(400, f"you already own {MAX_OWNED_TENANTS} workspaces")
    with conn.transaction():
        tenant = conn.execute(
            "insert into app.tenant (name) values (%s) returning id", [name]
        ).fetchone()
        assert tenant is not None
        conn.execute(
            "insert into app.membership (user_id, tenant_id, role) values (%s, %s, 'owner')",
            [user_id, tenant[0]],
        )
        portfolio = conn.execute(
            """insert into app.portfolio (tenant_id, name, created_by) values (%s, 'Main book', %s)
               returning id""",
            [tenant[0], user_id],
        ).fetchone()
        assert portfolio is not None
        audit(conn, tenant[0], user_id, "tenant_create", "tenant", tenant[0], {"name": name})
    return {"tenant_id": str(tenant[0]), "portfolio_id": str(portfolio[0])}


def add_member(
    conn: psycopg.Connection, tenant_id: uuid.UUID, actor: uuid.UUID, email: str, role: Role
) -> dict[str, str]:
    """Add an existing account to the workspace. Only owners may add owners."""
    actor_role = require_role(conn, tenant_id, actor, ("owner", "admin"))
    if role == "owner" and actor_role != "owner":
        raise ApiError(403, "only an owner can add another owner")
    user = conn.execute(
        "select id from auth.users where lower(email) = lower(%s) and not is_anonymous",
        [email.strip()],
    ).fetchone()
    if user is None:
        raise ApiError(404, "no account with that email; ask them to sign up first")
    with conn.transaction():
        added = conn.execute(
            """insert into app.membership (user_id, tenant_id, role) values (%s, %s, %s)
               on conflict do nothing returning user_id""",
            [user[0], tenant_id, role],
        ).fetchone()
        if added is None:
            raise ApiError(409, "already a member")
        audit(conn, tenant_id, actor, "member_add", "membership", user[0], {"role": role})
    return {"user_id": str(user[0]), "role": role}


def create_portfolio(
    conn: psycopg.Connection, tenant_id: uuid.UUID, actor: uuid.UUID, name: str
) -> dict[str, str]:
    require_role(conn, tenant_id, actor)
    name = name.strip()
    if not 1 <= len(name) <= 80:
        raise ApiError(400, "portfolio name must be 1-80 characters")
    with conn.transaction():
        row = conn.execute(
            """insert into app.portfolio (tenant_id, name, created_by) values (%s, %s, %s)
               on conflict (tenant_id, name) do nothing returning id""",
            [tenant_id, name, actor],
        ).fetchone()
        if row is None:
            raise ApiError(409, "a portfolio with that name exists")
        audit(conn, tenant_id, actor, "portfolio_create", "portfolio", row[0], {"name": name})
    return {"portfolio_id": str(row[0])}


def delete_borrower(conn: psycopg.Connection, borrower_id: uuid.UUID, actor: uuid.UUID) -> None:
    tenant_id = tenant_of(conn, "portfolio_borrower", borrower_id)
    require_role(conn, tenant_id, actor, what="portfolio borrower")
    with conn.transaction():
        conn.execute("delete from app.portfolio_borrower where id = %s", [borrower_id])
        audit(conn, tenant_id, actor, "borrower_delete", "portfolio_borrower", borrower_id)


def request_serving(conn: psycopg.Connection, portfolio_id: uuid.UUID) -> list[str]:
    """Queue this portfolio's matched companies for scoring; returns CIKs over capacity."""
    ciks = [r[0] for r in conn.execute(
        """select distinct cik from app.portfolio_borrower
           where portfolio_id = %s and cik is not null
             and match_status in ('auto_matched', 'confirmed')""",
        [portfolio_id],
    ).fetchall()]  # fmt: skip
    if not ciks:
        return []
    rows = conn.execute("select * from ml.request_serving(%s)", [ciks]).fetchall()
    return [r[0] for r in rows]


def generate_alerts(conn: psycopg.Connection) -> int:
    row = conn.execute("select app.generate_alerts()").fetchone()
    return int(row[0]) if row else 0


def dispose_alert(
    conn: psycopg.Connection, alert_id: uuid.UUID, actor: uuid.UUID,
    decision: Literal["agree", "dismiss"], reason: str | None,
) -> dict[str, str]:  # fmt: skip
    """Agree (the alert is a real concern) or dismiss it with a reason. Both close the alert's
    open state: agree -> acknowledged, dismiss -> closed."""
    tenant_id = tenant_of(conn, "alert", alert_id)
    require_role(conn, tenant_id, actor, what="alert")
    reason = (reason or "").strip() or None
    if decision == "dismiss" and reason is None:
        raise ApiError(400, "say why you dismiss the alert")
    status = "acknowledged" if decision == "agree" else "closed"
    with conn.transaction():
        conn.execute(
            """insert into app.disposition (tenant_id, alert_id, user_id, decision, reason)
               values (%s, %s, %s, %s, %s)""",
            [tenant_id, alert_id, actor, decision, reason],
        )
        conn.execute("update app.alert set status = %s where id = %s", [status, alert_id])
        audit(conn, tenant_id, actor, f"alert_{decision}", "alert", alert_id,
              {"reason": reason})  # fmt: skip
    return {"status": status}
