"""raw.ingest_runs bookkeeping: every ingest and pipeline run records its counts and outcome."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb


class _RequestStats(Protocol):
    requests: int
    retries: int


class _RateMeter(Protocol):
    max_per_second: int


class MeteredClient(Protocol):
    """Anything that counts its requests (e.g. tripwire_ingest.http.HttpClient)."""

    @property
    def stats(self) -> _RequestStats: ...
    @property
    def meter(self) -> _RateMeter: ...


log = logging.getLogger(__name__)

MAX_RECORDED_ERRORS = 50


@dataclass
class RunLog:
    id: int
    source: str
    counts: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)

    def item_failed(self, item: str, exc: BaseException) -> None:
        """One item failed; the run continues and ends as 'partial'."""
        self.counts["failed"] += 1
        message = f"{item}: {type(exc).__name__}: {exc}"
        if len(self.errors) < MAX_RECORDED_ERRORS:
            self.errors.append(message)
        log.error("item failed", extra={"run_id": self.id, "source": self.source, "item": item,
                                        "error": message})  # fmt: skip


@contextmanager
def ingest_run(
    conn: psycopg.Connection, source: str, clients: Sequence[MeteredClient] = ()
) -> Iterator[RunLog]:
    """Open a run row, yield its log, and close it as succeeded / partial / failed.

    `conn` should be in autocommit mode so the run row is visible while the run is in progress.
    """
    row = conn.execute(
        "insert into raw.ingest_runs (source) values (%s) returning id", [source]
    ).fetchone()
    assert row is not None
    run = RunLog(id=int(row[0]), source=source)
    log.info("run started", extra={"run_id": run.id, "source": source})
    status, error = "succeeded", None
    try:
        yield run
    except BaseException as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if status == "succeeded" and run.errors:
            status, error = "partial", "\n".join(run.errors)
        counts: dict[str, int] = dict(run.counts)
        if clients:
            counts["http_requests"] = sum(c.stats.requests for c in clients)
            counts["http_retries"] = sum(c.stats.retries for c in clients)
            # Evidence that the rate limit held: most requests seen in any one-second window.
            counts["max_requests_per_second"] = max(c.meter.max_per_second for c in clients)
        conn.execute(
            """update raw.ingest_runs
               set finished_at = now(), status = %s, counts = %s, error = %s where id = %s""",
            [status, Jsonb(counts), error, run.id],
        )
        log.info("run finished", extra={"run_id": run.id, "source": source, "status": status,
                                         "counts": counts})  # fmt: skip
