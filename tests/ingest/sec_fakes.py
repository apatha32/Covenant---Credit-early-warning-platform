"""Fake SEC/FRED server and payload builders shaped like the real responses."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

FILING_KEYS = [
    "accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "act", "form",
    "fileNumber", "filmNumber", "items", "core_type", "size", "isXBRL", "isInlineXBRL",
    "isXBRLNumeric", "primaryDocument", "primaryDocDescription",
]  # fmt: skip


def filing(accession: str, date: str, form: str, items: str = "", xbrl: int = 0) -> dict[str, Any]:
    return {
        "accessionNumber": accession, "filingDate": date, "reportDate": date,
        "acceptanceDateTime": f"{date}T16:05:00.000Z", "act": "34", "form": form,
        "fileNumber": "001-00001", "filmNumber": "1", "items": items, "core_type": form,
        "size": 1000, "isXBRL": xbrl, "isInlineXBRL": 0, "isXBRLNumeric": xbrl,
        "primaryDocument": "doc.htm", "primaryDocDescription": form,
    }  # fmt: skip


def columns(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
    return {k: [r[k] for r in rows] for k in FILING_KEYS}


def submissions_main(
    cik: int,
    name: str,
    recent: list[dict[str, Any]],
    *,
    sic: str = "3571",
    tickers: list[str] | None = None,
    former_names: list[dict[str, str]] | None = None,
    pages: list[str] | None = None,
) -> bytes:
    body = {
        "cik": str(cik), "entityType": "operating", "sic": sic, "sicDescription": "Computers",
        "name": name, "tickers": tickers or [], "exchanges": ["NYSE"] * len(tickers or []),
        "fiscalYearEnd": "1231", "stateOfIncorporation": "DE", "formerNames": former_names or [],
        "filings": {"recent": columns(recent),
                    "files": [{"name": p, "filingCount": 1} for p in pages or []]},
    }  # fmt: skip
    return json.dumps(body).encode()


def companyfacts(cik: int, facts: dict[str, dict[str, list[dict[str, Any]]]]) -> bytes:
    """facts: {tag: {unit: [fact, ...]}} under us-gaap."""
    concepts = {tag: {"label": tag, "description": f"{tag} desc", "units": units}
                for tag, units in facts.items()}  # fmt: skip
    return json.dumps({"cik": cik, "entityName": "X", "facts": {"us-gaap": concepts}}).encode()


class FakeServer:
    """Serves fixed responses by URL path (query string ignored unless registered with it)."""

    def __init__(self) -> None:
        self.routes: dict[str, list[httpx.Response]] = {}
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] | None = None

    def add(
        self, path: str, body: bytes = b"", status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        """Queue a response; the last queued one repeats once the queue is exhausted."""
        self.routes.setdefault(path, []).append(httpx.Response(status, content=body,
                                                               headers=headers))  # fmt: skip

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler:
            return self.handler(request)
        queue = self.routes.get(request.url.path)
        if not queue:
            return httpx.Response(404, content=b"not found")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._respond)
