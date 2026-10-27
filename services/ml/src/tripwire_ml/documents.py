"""Fetch filing documents from EDGAR Archives into raw storage (once each) and extract text."""

from __future__ import annotations

import html
import re

import psycopg

from tripwire_common.config import SecSourceConfig
from tripwire_common.runs import RunLog
from tripwire_common.storage import Storage
from tripwire_ingest.http import HttpClient
from tripwire_ingest.sec import accession_nodash, archive_cik, sha256

_TAG = re.compile(r"<[^>]+>")
_HIDDEN = re.compile(r"<(script|style|ix:header)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_SPACE = re.compile(r"\s+")


def document_key(cik: str, accession: str, document: str) -> str:
    return f"sec/documents/cik={cik}/{accession}/{document}"


def fetch_document(
    conn: psycopg.Connection,
    storage: Storage,
    client: HttpClient,
    config: SecSourceConfig,
    cik: str,
    accession: str,
    document: str,
    run: RunLog,
) -> bytes | None:
    """Raw document bytes, from storage if already fetched. None if EDGAR has no such file."""
    key = document_key(cik, accession, document)
    if storage.exists(key):
        run.counts["documents_cached"] += 1
        return storage.read_bytes(key)
    url = config.document_url.format(archive_cik=archive_cik(cik),
                                      accession_nodash=accession_nodash(accession),
                                      document=document)  # fmt: skip
    response = client.get(url)
    if response.status_code == 404:
        run.counts["documents_missing"] += 1
        return None
    body = response.content
    storage.write_bytes(key, body)
    conn.execute(
        """insert into raw.source_files (source, source_key, storage_path, checksum, run_id)
           values ('sec_document', %s, %s, %s, %s)
           on conflict (source, source_key, checksum) do nothing""",
        [f"{accession}/{document}", key, sha256(body), run.id],
    )
    run.counts["documents_fetched"] += 1
    return body


def html_to_text(raw: bytes) -> str:
    """Plain text from an EDGAR HTML / text filing (inline XBRL tags removed)."""
    text = raw.decode("utf-8", errors="replace")
    text = _HIDDEN.sub(" ", text)
    text = _TAG.sub(" ", text)
    return _SPACE.sub(" ", html.unescape(text)).strip()
