"""Borrower upload, review queue and decision reuse (database side of entity resolution).

These functions run with service-role access (the API calls them after verifying the user's
JWT), so each one checks that the portfolio / review belongs to the given tenant itself.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb

from tripwire_common.config import EntityResolutionConfig
from tripwire_pipeline.entity.matcher import Company, MatchResult, PriorDecision, Universe

NAME_HEADERS = ("borrower_name", "borrower", "company_name", "company", "name", "obligor",
                "legal_name", "issuer")  # fmt: skip
TICKER_HEADERS = ("ticker", "ticker_symbol", "symbol", "stock_ticker")

Decision = Literal["accept", "reject", "no_match"]


class UploadError(ValueError):
    """The file cannot be used at all (no usable columns, too many rows, wrong tenant...)."""


@dataclass(frozen=True)
class ParsedRow:
    row_number: int  # 1-based data row (header excluded)
    name: str | None
    ticker: str | None


@dataclass
class ParsedFile:
    rows: list[ParsedRow] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (row_number, reason)
    name_column: str | None = None
    ticker_column: str | None = None


def _header_key(header: str) -> str:
    return "_".join(header.strip().lower().replace("-", " ").split())


def parse_borrower_csv(data: bytes, max_rows: int) -> ParsedFile:
    """Find the name / ticker columns by header and read the rows; extra columns are ignored."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")  # Excel exports
    reader = csv.DictReader(io.StringIO(text))
    headers = {_header_key(h): h for h in reader.fieldnames or [] if h}
    parsed = ParsedFile(
        name_column=next((headers[h] for h in NAME_HEADERS if h in headers), None),
        ticker_column=next((headers[h] for h in TICKER_HEADERS if h in headers), None),
    )
    if parsed.name_column is None and parsed.ticker_column is None:
        raise UploadError(
            f"no borrower name or ticker column; expected one of {NAME_HEADERS + TICKER_HEADERS}"
        )
    for i, raw in enumerate(reader, start=1):
        if i > max_rows:
            raise UploadError(f"file has more than {max_rows} rows")
        name = (raw.get(parsed.name_column or "") or "").strip() or None
        ticker = (raw.get(parsed.ticker_column or "") or "").strip() or None
        if name is None and ticker is None:
            parsed.skipped.append((i, "empty row"))
            continue
        parsed.rows.append(ParsedRow(i, name, ticker))
    return parsed


def load_universe(conn: psycopg.Connection, config: EntityResolutionConfig) -> Universe:
    rows = conn.execute(
        """select c.cik, c.name, c.tickers, c.is_excluded,
                  coalesce(array_agg(a.alias order by a.alias)
                           filter (where a.alias_type = 'former_name'), '{}')
           from curated.company c
           left join curated.company_alias a on a.cik = c.cik
           group by c.cik"""
    ).fetchall()
    companies = [Company(cik, name, tuple(tickers), tuple(former), excluded)
                 for cik, name, tickers, excluded, former in rows]  # fmt: skip
    return Universe(companies, config)


def _prior_decisions(conn: psycopg.Connection, tenant_id: uuid.UUID) -> dict[str, PriorDecision]:
    rows = conn.execute(
        "select input_key, cik from app.entity_alias where tenant_id = %s", [tenant_id]
    ).fetchall()
    return {key: PriorDecision(cik) for key, cik in rows}


def _candidates_json(result: MatchResult) -> Jsonb:
    return Jsonb([{"cik": c.cik, "name": c.name, "score": round(c.score, 1),
                   "matched_name": c.matched_name, "kind": c.kind}
                  for c in result.candidates])  # fmt: skip


@dataclass
class UploadSummary:
    portfolio_id: uuid.UUID
    counts: Counter[str] = field(default_factory=Counter)
    skipped: list[tuple[int, str]] = field(default_factory=list)


def upload_borrowers(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    data: bytes,
    universe: Universe,
    actor_user_id: uuid.UUID | None = None,
) -> UploadSummary:
    """Parse, match and store an uploaded borrower file; low-confidence rows go to review."""
    owner = conn.execute("select tenant_id from app.portfolio where id = %s", [portfolio_id])
    row = owner.fetchone()
    if row is None or row[0] != tenant_id:
        raise UploadError("portfolio not found for this tenant")

    parsed = parse_borrower_csv(data, universe.config.max_upload_rows)
    summary = UploadSummary(portfolio_id, skipped=list(parsed.skipped))
    priors = _prior_decisions(conn, tenant_id)
    with conn.transaction():
        for r in parsed.rows:
            key = universe.input_key(r.name, r.ticker)
            result = universe.match(r.name, r.ticker, priors.get(key))
            inserted = conn.execute(
                """insert into app.portfolio_borrower
                     (tenant_id, portfolio_id, cik, uploaded_name, uploaded_ticker, match_status,
                      match_confidence, row_number, input_key, match_method, matched_at)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                   on conflict (portfolio_id, input_key) where input_key is not null do nothing
                   returning id""",
                [tenant_id, portfolio_id, result.cik, r.name, r.ticker, result.status,
                 result.confidence, r.row_number, key, result.method],
            ).fetchone()  # fmt: skip
            if inserted is None:
                summary.skipped.append((r.row_number, "duplicate of an existing borrower"))
                summary.counts["duplicate"] += 1
                continue
            summary.counts[result.status] += 1
            if result.status == "needs_review":
                conn.execute(
                    """insert into app.match_review (tenant_id, borrower_id, candidate_ciks)
                       values (%s, %s, %s)""",
                    [tenant_id, inserted[0], _candidates_json(result)],
                )
        _audit(conn, tenant_id, actor_user_id, "portfolio_upload", "portfolio", portfolio_id,
               {"counts": dict(summary.counts), "skipped": len(summary.skipped)})  # fmt: skip
    return summary


def decide(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    review_id: uuid.UUID,
    decision: Decision,
    user_id: uuid.UUID,
    cik: str | None = None,
) -> int:
    """Record a reviewer's decision. accept/no_match are remembered for the tenant and applied to
    every other borrower of this tenant still waiting on the same input. Returns borrowers updated.

    reject = "none of the suggestions is right" without saying there is no SEC match; the row
    stays out of the watchlist and nothing is remembered.
    """
    if (decision == "accept") != (cik is not None):
        raise UploadError("accept needs a cik; reject / no_match must not have one")
    found = conn.execute(
        """select r.borrower_id, b.input_key from app.match_review r
           join app.portfolio_borrower b on b.id = r.borrower_id
           where r.id = %s and r.tenant_id = %s and r.decision is null""",
        [review_id, tenant_id],
    ).fetchone()
    if found is None:
        raise UploadError("open review not found for this tenant")
    borrower_id, input_key = found
    if (
        cik is not None
        and conn.execute("select 1 from curated.company where cik = %s", [cik]).fetchone() is None
    ):
        raise UploadError(f"unknown company {cik}")

    status = {"accept": "confirmed", "no_match": "unmatched", "reject": "rejected"}[decision]
    with conn.transaction():
        if decision == "reject":
            targets = [borrower_id]
        else:
            conn.execute(
                """insert into app.entity_alias (tenant_id, input_key, cik, decision, decided_by)
                   values (%s, %s, %s, %s, %s)
                   on conflict (tenant_id, input_key) do update
                   set cik = excluded.cik, decision = excluded.decision,
                       decided_by = excluded.decided_by, decided_at = now()""",
                [tenant_id, input_key, cik, decision, user_id],
            )
            # Same input waiting elsewhere in this tenant: resolve it too.
            targets = [r[0] for r in conn.execute(
                """select id from app.portfolio_borrower
                   where tenant_id = %s and input_key = %s
                     and (id = %s or match_status = 'needs_review')""",
                [tenant_id, input_key, borrower_id],
            ).fetchall()]  # fmt: skip
        conn.execute(
            """update app.portfolio_borrower
               set cik = %s, match_status = %s, match_confidence = 1.0,
                   match_method = 'human_decision', matched_at = now()
               where id = any(%s)""",
            [cik, status, targets],
        )
        conn.execute(
            """update app.match_review
               set decision = %s, decided_cik = %s, decided_by = %s, decided_at = now()
               where borrower_id = any(%s) and decision is null""",
            [decision, cik, user_id, targets],
        )
        _audit(conn, tenant_id, user_id, f"match_{decision}", "match_review", review_id,
               {"cik": cik, "borrowers_updated": len(targets)})  # fmt: skip
    return len(targets)


def _audit(
    conn: psycopg.Connection,
    tenant_id: uuid.UUID,
    actor: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    details: dict[str, object],
) -> None:
    conn.execute(
        """insert into app.audit_log (tenant_id, actor_user_id, action, entity_type, entity_id,
                                      details)
           values (%s, %s, %s, %s, %s, %s)""",
        [tenant_id, actor, action, entity_type, str(entity_id), Jsonb(details)],
    )
