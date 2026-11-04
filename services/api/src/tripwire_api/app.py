"""Covenant API (FastAPI): the write side of the console.

The web app reads through Supabase `api` views under RLS. Everything that changes data, or
needs the Python matcher, comes here: create a workspace, add teammates, upload a borrower
file, decide review items, search companies, dispose alerts. Each request carries the user's
Supabase access token; the API verifies it, checks the user's role in the tenant (in addition
to RLS, which the service-role connection bypasses) and audit-logs the change.

    uv run python -m tripwire_api           # serves on $PORT (default 8000)
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, File, Header, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from tripwire_api.auth import AuthError, Principal, TokenVerifier
from tripwire_api.workspace import (
    ApiError,
    add_member,
    create_portfolio,
    create_tenant,
    delete_borrower,
    dispose_alert,
    generate_alerts,
    request_serving,
    require_role,
    tenant_of,
)
from tripwire_common.config import EntityResolutionConfig, load_entity_resolution_config
from tripwire_common.db import KEEPALIVES
from tripwire_common.settings import Settings, get_settings
from tripwire_pipeline.entity.matcher import Candidate, Universe, normalize_ticker
from tripwire_pipeline.entity.upload import UploadError, decide, load_universe, upload_borrowers

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 1_000_000
UNIVERSE_TTL_SECONDS = 900


class UniverseCache:
    """The SEC company universe for matching, reloaded every UNIVERSE_TTL_SECONDS."""

    def __init__(self, config: EntityResolutionConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._universe: Universe | None = None
        self._loaded = 0.0

    def get(self, conn: psycopg.Connection) -> Universe:
        with self._lock:
            if self._universe is None or time.monotonic() - self._loaded > UNIVERSE_TTL_SECONDS:
                self._universe = load_universe(conn, self.config)
                self._loaded = time.monotonic()
            return self._universe


# --- request bodies ---


class NewTenant(BaseModel):
    name: str = Field(min_length=2, max_length=80)


class NewMember(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["owner", "admin", "analyst", "viewer"] = "analyst"


class NewPortfolio(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class MatchDecision(BaseModel):
    decision: Literal["accept", "reject", "no_match"]
    cik: str | None = Field(default=None, pattern=r"^\d{1,10}$")


class Disposition(BaseModel):
    decision: Literal["agree", "dismiss"]
    reason: str | None = Field(default=None, max_length=1000)


def _candidate(c: Candidate) -> dict[str, Any]:
    return {"cik": c.cik, "name": c.name, "score": round(c.score, 1),
            "matched_name": c.matched_name, "kind": c.kind}  # fmt: skip


def search_companies(universe: Universe, query: str, limit: int) -> list[dict[str, Any]]:
    """Ticker hits first, then the closest names (strict, then looser suggestions)."""
    query = query.strip()
    found: dict[str, Candidate] = {}
    for cik in sorted(universe.by_ticker.get(normalize_ticker(query), set())):
        company = universe.companies[cik]
        found[cik] = Candidate(cik, company.name, 100.0, query.upper(), "ticker")
    norm = universe.normalize(query)
    if norm:
        for c in [*universe.name_candidates(norm), *universe.suggestions(norm)]:
            found.setdefault(c.cik, c)
    ranked = sorted(found.values(), key=lambda c: (c.kind != "ticker", -c.score, c.cik))
    out = []
    for c in ranked[:limit]:
        company = universe.companies[c.cik]
        out.append({**_candidate(c), "tickers": list(company.tickers),
                    "is_excluded": company.is_excluded})  # fmt: skip
    return out


# --- dependencies (state lives on app.state, set by create_app) ---


def principal(request: Request, authorization: Annotated[str | None, Header()] = None) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "sign in first")
    try:
        verify: Callable[[str], Principal] = request.app.state.verify
        return verify(authorization.split(" ", 1)[1].strip())
    except AuthError as exc:
        raise ApiError(401, "your session has expired; sign in again") from exc


def member(user: Annotated[Principal, Depends(principal)]) -> Principal:
    """A signed-in account. Anonymous demo visitors can read the demo but change nothing."""
    if user.is_anonymous:
        raise ApiError(403, "the public demo is read-only; create an account to do this")
    return user


def connection(request: Request) -> Iterator[psycopg.Connection]:
    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as conn:
        yield conn


User = Annotated[Principal, Depends(member)]
Conn = Annotated[psycopg.Connection, Depends(connection)]


def create_app(
    settings: Settings | None = None,
    *,
    verifier: Callable[[str], Principal] | None = None,
    pool: ConnectionPool | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if verifier is None:
        secret = settings.supabase_jwt_secret
        verifier = TokenVerifier(
            settings.require("supabase_url"), secret.get_secret_value() if secret else None
        ).verify
    owns_pool = pool is None
    if pool is None:
        conninfo = make_conninfo(settings.require("database_url"), **KEEPALIVES)
        pool = ConnectionPool(conninfo, min_size=1, max_size=4, open=False,
                              check=ConnectionPool.check_connection)  # fmt: skip
    universes = UniverseCache(load_entity_resolution_config(settings.tripwire_config_dir))
    db_pool = pool

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if owns_pool:
            db_pool.open()
        try:
            yield
        finally:
            if owns_pool:
                db_pool.close()

    app = FastAPI(title="Covenant API", version="0.6.0", lifespan=lifespan)
    app.state.verify = verifier
    app.state.pool = pool
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.api_cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=3600,
    )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(UploadError)
    async def _upload_error(_: Request, exc: UploadError) -> JSONResponse:
        message = str(exc)
        status = 404 if "not found" in message else 400
        return JSONResponse({"error": message}, status_code=status)

    @app.get("/health")
    def health(conn: Conn) -> dict[str, Any]:
        conn.execute("select 1")
        return {"ok": True}

    @app.post("/tenants", status_code=201)
    def new_tenant(body: NewTenant, user: User, conn: Conn) -> dict[str, str]:
        return create_tenant(conn, user.user_id, body.name)

    @app.post("/tenants/{tenant_id}/members", status_code=201)
    def new_member(tenant_id: uuid.UUID, body: NewMember, user: User, conn: Conn) -> dict[str, str]:
        return add_member(conn, tenant_id, user.user_id, body.email, body.role)

    @app.post("/tenants/{tenant_id}/portfolios", status_code=201)
    def new_portfolio(
        tenant_id: uuid.UUID, body: NewPortfolio, user: User, conn: Conn
    ) -> dict[str, str]:
        return create_portfolio(conn, tenant_id, user.user_id, body.name)

    @app.post("/portfolios/{portfolio_id}/upload")
    def upload(
        portfolio_id: uuid.UUID, user: User, conn: Conn, file: Annotated[UploadFile, File()]
    ) -> dict[str, Any]:
        tenant_id = tenant_of(conn, "portfolio", portfolio_id)
        require_role(conn, tenant_id, user.user_id, what="portfolio")
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ApiError(413, "file is larger than 1 MB")
        universe = universes.get(conn)
        summary = upload_borrowers(conn, tenant_id, portfolio_id, data, universe, user.user_id)
        over_capacity = request_serving(conn, portfolio_id)
        alerts = generate_alerts(conn)
        log.info("portfolio upload", extra={"portfolio_id": str(portfolio_id),
                                            "counts": dict(summary.counts)})  # fmt: skip
        return {
            "counts": dict(summary.counts),
            "skipped": [{"row": r, "reason": why} for r, why in summary.skipped],
            "over_capacity": over_capacity,
            "new_alerts": alerts,
        }

    @app.get("/portfolios/{portfolio_id}/matches")
    def matches(portfolio_id: uuid.UUID, user: User, conn: Conn) -> list[dict[str, Any]]:
        tenant_id = tenant_of(conn, "portfolio", portfolio_id)
        require_role(conn, tenant_id, user.user_id, ("owner", "admin", "analyst", "viewer"),
                     what="portfolio")  # fmt: skip
        rows = conn.execute(
            """select r.id, b.id, b.row_number, b.uploaded_name, b.uploaded_ticker,
                      r.candidate_ciks, b.match_confidence, b.match_method
               from app.match_review r join app.portfolio_borrower b on b.id = r.borrower_id
               where b.portfolio_id = %s and r.decision is null
               order by b.row_number nulls last""",
            [portfolio_id],
        ).fetchall()
        keys = ["id", "borrower_id", "row_number", "uploaded_name", "uploaded_ticker",
                "candidates", "confidence", "method"]  # fmt: skip
        return [dict(zip(keys, [str(r[0]), str(r[1]), *r[2:]], strict=True)) for r in rows]

    @app.post("/matches/{review_id}/decision")
    def match_decision(
        review_id: uuid.UUID, body: MatchDecision, user: User, conn: Conn
    ) -> dict[str, Any]:
        tenant_id = tenant_of(conn, "match_review", review_id)
        require_role(conn, tenant_id, user.user_id, what="review item")
        cik = f"{int(body.cik):010d}" if body.cik else None
        updated = decide(conn, tenant_id, review_id, body.decision, user.user_id, cik)
        portfolio = conn.execute(
            """select b.portfolio_id from app.match_review r
               join app.portfolio_borrower b on b.id = r.borrower_id where r.id = %s""",
            [review_id],
        ).fetchone()
        over_capacity = request_serving(conn, portfolio[0]) if portfolio else []
        return {"borrowers_updated": updated, "over_capacity": over_capacity,
                "new_alerts": generate_alerts(conn)}  # fmt: skip

    @app.delete("/borrowers/{borrower_id}", status_code=204)
    def remove_borrower(borrower_id: uuid.UUID, user: User, conn: Conn) -> None:
        delete_borrower(conn, borrower_id, user.user_id)

    @app.get("/companies/search")
    def company_search(
        user: User, conn: Conn, q: Annotated[str, Query(min_length=1, max_length=120)],
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> list[dict[str, Any]]:  # fmt: skip
        return search_companies(universes.get(conn), q, limit)

    @app.post("/alerts/{alert_id}/disposition")
    def alert_disposition(
        alert_id: uuid.UUID, body: Disposition, user: User, conn: Conn
    ) -> dict[str, str]:
        return dispose_alert(conn, alert_id, user.user_id, body.decision, body.reason)

    return app
