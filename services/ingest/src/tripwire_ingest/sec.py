"""SEC EDGAR: fetch raw payloads and flatten them into Parquet-ready frames.

Parsing here is deliberately minimal (raw layer): SEC field names are kept, values are kept as
SEC sent them (XBRL values as their exact JSON text), and nothing is filtered or deduplicated.
If SEC's payload shape changes, SourceSchemaError stops the run rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import polars as pl

from tripwire_common.config import SecSourceConfig
from tripwire_common.settings import Settings
from tripwire_ingest.http import HttpClient

log = logging.getLogger(__name__)


class SourceSchemaError(RuntimeError):
    """The source returned data in a shape this code does not expect."""


# --- identifiers (SPEC 3.3) ---


def cik10(cik: int | str) -> str:
    """Zero-padded 10-digit CIK, as used in data.sec.gov URLs and our database."""
    return f"{int(cik):010d}"


def archive_cik(cik: int | str) -> str:
    """Unpadded CIK, as used in EDGAR Archives paths."""
    return str(int(cik))


def accession_nodash(accession: str) -> str:
    return accession.replace("-", "")


def sec_client(settings: Settings, config: SecSourceConfig, **kwargs: Any) -> HttpClient:
    """HTTP client with SEC's required User-Agent. Refuses to start without one."""
    headers = {"User-Agent": settings.require("sec_user_agent"), "Accept-Encoding": "gzip, deflate"}
    return HttpClient(config, headers=headers, **kwargs)


def sha256(*parts: bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))  # length-prefix so part boundaries count
        digest.update(part)
    return digest.hexdigest()


def frame_to_parquet(df: pl.DataFrame) -> bytes:
    """Deterministic Parquet bytes (same frame -> same bytes), so re-landing is a no-op."""
    from io import BytesIO

    buffer = BytesIO()
    df.write_parquet(buffer, compression="zstd", statistics=False)
    return buffer.getvalue()


# --- ticker map ---


def fetch_tickers(client: HttpClient, config: SecSourceConfig) -> bytes:
    return _ok(client, config.tickers_url)


def parse_tickers(raw: bytes) -> pl.DataFrame:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise SourceSchemaError("company_tickers.json: expected an object of rows")
    rows = list(data.values())
    _require_keys(rows[:1], {"cik_str", "ticker", "title"}, "company_tickers.json")
    return pl.DataFrame(
        {
            "cik": [cik10(r["cik_str"]) for r in rows],
            "ticker": [str(r["ticker"]) for r in rows],
            "title": [str(r["title"]) for r in rows],
        },
        schema={"cik": pl.String, "ticker": pl.String, "title": pl.String},
    )


# --- submissions ---

# Filing columns SEC returns in filings.recent and in each filings.files page.
FILING_COLUMNS: dict[str, type[pl.DataType]] = {
    "accessionNumber": pl.String,
    "filingDate": pl.String,
    "reportDate": pl.String,
    "acceptanceDateTime": pl.String,
    "act": pl.String,
    "form": pl.String,
    "fileNumber": pl.String,
    "filmNumber": pl.String,
    "items": pl.String,
    "core_type": pl.String,
    "size": pl.Int64,
    "isXBRL": pl.Int64,
    "isInlineXBRL": pl.Int64,
    "isXBRLNumeric": pl.Int64,
    "primaryDocument": pl.String,
    "primaryDocDescription": pl.String,
}
REQUIRED_FILING_COLUMNS = {"accessionNumber", "filingDate", "form", "items", "primaryDocument"}
REQUIRED_COMPANY_KEYS = {"cik", "name", "sic", "tickers", "formerNames", "filings"}


@dataclass(frozen=True)
class SubmissionsPayload:
    cik: str
    main: bytes
    pages: list[tuple[str, bytes]]  # (file name, bytes) for each filings.files entry

    @property
    def checksum(self) -> str:
        return sha256(self.main, *(b for _, b in self.pages))


def fetch_submissions(
    client: HttpClient, config: SecSourceConfig, cik: int | str
) -> SubmissionsPayload | None:
    """Main submissions JSON plus every paginated older-filings file. None if SEC has no CIK."""
    response = client.get(config.submissions_url.format(cik10=cik10(cik)))
    if response.status_code == 404:
        return None
    main = response.content
    files = json.loads(main).get("filings", {}).get("files", [])
    pages = [
        (f["name"], _ok(client, config.submissions_page_url.format(name=f["name"]))) for f in files
    ]
    return SubmissionsPayload(cik=cik10(cik), main=main, pages=pages)


def parse_submissions(payload: SubmissionsPayload) -> tuple[pl.DataFrame, pl.DataFrame]:
    """-> (company: one row, filings: one row per filing across recent + all pages)."""
    data = json.loads(payload.main)
    _require_keys([data], REQUIRED_COMPANY_KEYS, f"submissions CIK{payload.cik}")
    if cik10(data["cik"]) != payload.cik:
        raise SourceSchemaError(f"submissions for CIK{payload.cik} returned cik {data['cik']}")

    meta = {k: v for k, v in data.items() if k != "filings"}
    company = pl.DataFrame(
        {
            "cik": [payload.cik],
            "name": [str(data["name"])],
            "entity_type": [data.get("entityType")],
            "sic": [str(data["sic"]) if data.get("sic") else None],
            "sic_description": [data.get("sicDescription")],
            "fiscal_year_end": [data.get("fiscalYearEnd")],
            "state_of_incorporation": [data.get("stateOfIncorporation")],
            "tickers": [[str(t) for t in data.get("tickers") or []]],
            "exchanges": [[str(x) if x else "" for x in data.get("exchanges") or []]],
            "former_names_json": [json.dumps(data.get("formerNames") or [])],
            "payload_json": [json.dumps(meta, sort_keys=True)],
        },
        schema_overrides={"tickers": pl.List(pl.String), "exchanges": pl.List(pl.String)},
    )

    frames = [_filings_frame(data["filings"]["recent"], payload.cik, "recent")]
    for name, raw in payload.pages:
        frames.append(_filings_frame(json.loads(raw), payload.cik, name))
    filings = pl.concat(frames, how="diagonal_relaxed")
    return company, filings


def _filings_frame(columns: dict[str, list[Any]], cik: str, source_file: str) -> pl.DataFrame:
    missing = REQUIRED_FILING_COLUMNS - columns.keys()
    if missing:
        raise SourceSchemaError(
            f"CIK{cik} {source_file}: filings missing columns {sorted(missing)}"
        )
    lengths = {len(v) for v in columns.values()}
    if len(lengths) > 1:
        raise SourceSchemaError(f"CIK{cik} {source_file}: filing columns have unequal lengths")
    n = lengths.pop() if lengths else 0
    unknown = columns.keys() - FILING_COLUMNS.keys()
    if unknown:
        # Kept (as text) rather than dropped, and logged so the spec can be updated.
        log.warning("unexpected filing columns", extra={"cik": cik, "columns": sorted(unknown)})
    data: dict[str, list[Any]] = {"cik": [cik] * n, "source_file": [source_file] * n}
    schema: dict[str, Any] = {"cik": pl.String, "source_file": pl.String}
    for col, dtype in FILING_COLUMNS.items():
        data[col] = columns.get(col, [None] * n)
        schema[col] = dtype
    for col in sorted(unknown):
        data[col] = [None if v is None else str(v) for v in columns[col]]
        schema[col] = pl.String
    return pl.DataFrame(data, schema=schema)


# --- companyfacts ---

FACT_KEYS_REQUIRED = {"end", "val", "accn", "form", "filed"}


@dataclass(frozen=True)
class RawPayload:
    cik: str
    body: bytes

    @property
    def checksum(self) -> str:
        return sha256(self.body)


def fetch_companyfacts(
    client: HttpClient, config: SecSourceConfig, cik: int | str
) -> RawPayload | None:
    """None when the company has no XBRL financial data (SEC returns 404)."""
    response = client.get(config.companyfacts_url.format(cik10=cik10(cik)))
    if response.status_code == 404:
        return None
    return RawPayload(cik=cik10(cik), body=response.content)


def parse_companyfacts(payload: RawPayload) -> tuple[pl.DataFrame, pl.DataFrame]:
    """-> (facts: one row per reported value, tags: one row per taxonomy/tag with labels).

    `val` keeps SEC's exact JSON text (no float rounding); staging parses it to numeric.
    """
    data = json.loads(payload.body, parse_float=str, parse_int=str)
    if "facts" not in data:
        raise SourceSchemaError(f"companyfacts CIK{payload.cik}: no 'facts' key")
    if cik10(data.get("cik", 0)) != payload.cik:
        raise SourceSchemaError(f"companyfacts for CIK{payload.cik} returned cik {data.get('cik')}")

    cols: dict[str, list[Any]] = {
        k: []
        for k in ("taxonomy", "tag", "unit", "start", "end", "val", "accn", "fy", "fp", "form",
                  "filed", "frame")
    }  # fmt: skip
    tags: dict[str, list[Any]] = {"taxonomy": [], "tag": [], "label": [], "description": []}
    for taxonomy, concepts in data["facts"].items():
        for tag, concept in concepts.items():
            tags["taxonomy"].append(taxonomy)
            tags["tag"].append(tag)
            tags["label"].append(concept.get("label"))
            tags["description"].append(concept.get("description"))
            for unit, facts in concept.get("units", {}).items():
                for fact in facts:
                    missing = FACT_KEYS_REQUIRED - fact.keys()
                    if missing:
                        raise SourceSchemaError(
                            f"CIK{payload.cik} {taxonomy}:{tag}: fact missing {sorted(missing)}"
                        )
                    cols["taxonomy"].append(taxonomy)
                    cols["tag"].append(tag)
                    cols["unit"].append(unit)
                    for key in ("start", "end", "accn", "fp", "form", "filed", "frame"):
                        cols[key].append(fact.get(key))
                    cols["val"].append(str(fact["val"]))
                    cols["fy"].append(fact.get("fy"))
    n = len(cols["tag"])
    facts_df = pl.DataFrame(
        {"cik": [payload.cik] * n, **cols},
        schema={"cik": pl.String, **{k: pl.String for k in cols}},
    ).with_columns(pl.col("fy").cast(pl.Int64))
    tags_df = pl.DataFrame(tags, schema={k: pl.String for k in tags})
    return facts_df, tags_df


# --- helpers ---


def _ok(client: HttpClient, url: str) -> bytes:
    response = client.get(url)
    if response.status_code == 404:
        raise SourceSchemaError(f"expected resource is missing (404): {url}")
    return response.content


def _require_keys(rows: list[dict[str, Any]], keys: set[str], what: str) -> None:
    for row in rows:
        missing = keys - row.keys()
        if missing:
            raise SourceSchemaError(f"{what}: missing keys {sorted(missing)}")
