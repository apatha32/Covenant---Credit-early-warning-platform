"""Load the company dimension (curated.company + curated.company_alias) from SEC data.

curated.company holds each company's current attributes, so it is upserted. Aliases (tickers
and former names) are only ever added, which keeps old names matchable after renames.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import polars as pl
import psycopg

from tripwire_common.config import DomainPack
from tripwire_common.names import normalize_company_name

_MMDD = re.compile(r"^(0[1-9]|1[0-2])[0-3][0-9]$")

_UPSERT_COMPANY = """
insert into curated.company as c
  (cik, name, tickers, sic, sic_description, state, fiscal_year_end, is_excluded, exclusion_reason)
values (%(cik)s, %(name)s, %(tickers)s, %(sic)s, %(sic_description)s, %(state)s,
        %(fiscal_year_end)s, %(is_excluded)s, %(exclusion_reason)s)
on conflict (cik) do update set
  name = excluded.name, tickers = excluded.tickers, sic = excluded.sic,
  sic_description = excluded.sic_description, state = excluded.state,
  fiscal_year_end = excluded.fiscal_year_end, is_excluded = excluded.is_excluded,
  exclusion_reason = excluded.exclusion_reason, updated_at = now()
where (c.name, c.tickers, c.sic, c.sic_description, c.state, c.fiscal_year_end,
       c.is_excluded, c.exclusion_reason)
  is distinct from
      (excluded.name, excluded.tickers, excluded.sic, excluded.sic_description, excluded.state,
       excluded.fiscal_year_end, excluded.is_excluded, excluded.exclusion_reason)
"""

_INSERT_ALIAS = """
insert into curated.company_alias (cik, alias, alias_normalized, alias_type, valid_from, valid_to)
values (%(cik)s, %(alias)s, %(alias_normalized)s, %(alias_type)s, %(valid_from)s, %(valid_to)s)
on conflict (cik, alias_type, alias) do nothing
"""


def _alias(
    cik: str,
    alias: str,
    alias_type: str,
    valid_from: dt.date | None = None,
    valid_to: dt.date | None = None,
) -> dict[str, Any]:
    return {"cik": cik, "alias": alias, "alias_normalized": normalize_company_name(alias),
            "alias_type": alias_type, "valid_from": valid_from, "valid_to": valid_to}  # fmt: skip


def _ticker_alias(cik: str, ticker: str) -> dict[str, Any]:
    alias = _alias(cik, ticker, "ticker")
    alias["alias_normalized"] = ticker.lower()  # tickers match exactly, not as names
    return alias


def _date(value: Any) -> dt.date | None:
    return dt.date.fromisoformat(str(value)[:10]) if value else None


def upsert_from_submissions(
    conn: psycopg.Connection, company: pl.DataFrame, pack: DomainPack
) -> None:
    row = company.row(0, named=True)
    sic = int(row["sic"]) if row["sic"] else None
    reason = pack.exclusions.exclusion_reason(sic)
    fye = (
        row["fiscal_year_end"]
        if row["fiscal_year_end"] and _MMDD.match(row["fiscal_year_end"])
        else None
    )
    params = {
        "cik": row["cik"],
        "name": row["name"],
        "tickers": row["tickers"],
        "sic": sic,
        "sic_description": row["sic_description"],
        "state": row["state_of_incorporation"] or None,
        "fiscal_year_end": fye,
        "is_excluded": reason is not None,
        "exclusion_reason": reason,
    }
    aliases = [_ticker_alias(row["cik"], t) for t in row["tickers"]] + [
        _alias(row["cik"], f["name"], "former_name", _date(f.get("from")), _date(f.get("to")))
        for f in json.loads(row["former_names_json"])
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_UPSERT_COMPANY, params)
        if aliases:
            cur.executemany(_INSERT_ALIAS, aliases)


def upsert_from_tickers(conn: psycopg.Connection, tickers: pl.DataFrame) -> int:
    """Create companies the ticker map knows about and refresh their ticker lists.

    Names from the ticker map never overwrite names from submissions (the richer source).
    Returns the number of distinct CIKs.
    """
    grouped = (
        tickers.group_by("cik", maintain_order=True)
        .agg(pl.col("title").first().alias("name"), pl.col("ticker").alias("tickers"))
        .sort("cik")
    )
    companies = grouped.to_dicts()
    aliases = [_ticker_alias(r["cik"], t) for r in companies for t in r["tickers"]]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """insert into curated.company as c (cik, name, tickers)
               values (%(cik)s, %(name)s, %(tickers)s)
               on conflict (cik) do update set tickers = excluded.tickers, updated_at = now()
               where c.tickers is distinct from excluded.tickers""",
            companies,
        )
        cur.executemany(_INSERT_ALIAS, aliases)
    return len(companies)
