"""FRED: fetch series observations (all ALFRED vintages when configured) as raw frames.

Values stay as FRED's text ('.' marks a missing observation); staging decides what to do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import polars as pl

from tripwire_common.config import FredSourceConfig, MacroSeriesSpec
from tripwire_common.settings import Settings
from tripwire_ingest.http import HttpClient, HttpError
from tripwire_ingest.sec import SourceSchemaError, sha256

# FRED's sentinel range meaning "every vintage ever published".
ALL_VINTAGES = {"realtime_start": "1776-07-04", "realtime_end": "9999-12-31"}

OBSERVATION_SCHEMA = {
    "series_id": pl.String,
    "date": pl.String,
    "value": pl.String,
    "realtime_start": pl.String,
    "realtime_end": pl.String,
}


@dataclass(frozen=True)
class FredPayload:
    series: MacroSeriesSpec
    pages: list[bytes]

    @property
    def series_id(self) -> str:
        return self.series.id

    @property
    def checksum(self) -> str:
        """Hash of the observation content. Without vintages FRED stamps every row with today's
        date as realtime_start/end, so those fields are left out or every day would look new."""
        keys = ["date", "value"]
        if self.series.vintages:
            keys += ["realtime_start", "realtime_end"]
        rows = [[obs.get(k) for k in keys] for page in self.pages
                for obs in json.loads(page)["observations"]]  # fmt: skip
        return sha256(json.dumps(rows).encode())


def fred_client(settings: Settings, config: FredSourceConfig, **kwargs: Any) -> HttpClient:
    settings.require("fred_api_key")
    return HttpClient(config, **kwargs)


def fetch_series(
    client: HttpClient, settings: Settings, config: FredSourceConfig, series: MacroSeriesSpec
) -> FredPayload:
    params: dict[str, str | int] = {
        "series_id": series.id,
        "api_key": settings.require("fred_api_key"),
        "file_type": "json",
        "sort_order": "asc",
        "limit": config.page_size,
    }
    if series.vintages:
        params.update(ALL_VINTAGES)
    pages: list[bytes] = []
    offset = 0
    while True:
        response = client.get(config.observations_url, params={**params, "offset": offset})
        if response.status_code == 404:
            raise HttpError(f"FRED series {series.id} not found", status=404)
        body = json.loads(response.content)
        if "observations" not in body or "count" not in body:
            raise SourceSchemaError(f"FRED {series.id}: unexpected response keys {sorted(body)}")
        pages.append(response.content)
        offset += len(body["observations"])
        if offset >= int(body["count"]) or not body["observations"]:
            return FredPayload(series=series, pages=pages)


def parse_series(payload: FredPayload) -> pl.DataFrame:
    """One row per observation (per vintage). Without vintages the realtime fields are only
    today's date, so they are stored as null to keep the file deterministic."""
    vintages = payload.series.vintages
    rows: list[dict[str, Any]] = []
    for page in payload.pages:
        for obs in json.loads(page)["observations"]:
            rows.append({
                "series_id": payload.series_id,
                "date": obs.get("date"),
                "value": obs.get("value"),
                "realtime_start": obs.get("realtime_start") if vintages else None,
                "realtime_end": obs.get("realtime_end") if vintages else None,
            })  # fmt: skip
    return pl.DataFrame(rows, schema=OBSERVATION_SCHEMA)
