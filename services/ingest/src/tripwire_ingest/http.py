"""Rate-limited HTTP client with retries and exponential backoff.

Every request first takes a token from the bucket, including retries, so the per-second limit
holds no matter how many requests fail.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from tripwire_common.config import HttpSourceConfig
from tripwire_ingest.ratelimit import RateMeter, TokenBucket

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Query parameters that must never appear in logs or error messages.
_SECRET_PARAMS = ("api_key",)


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class HttpStats:
    requests: int = 0
    retries: int = 0
    wait_seconds: float = 0.0


def redact(url: httpx.URL) -> str:
    for param in _SECRET_PARAMS:
        if param in url.params:
            url = url.copy_set_param(param, "REDACTED")
    return str(url)


class HttpClient:
    def __init__(
        self,
        config: HttpSourceConfig,
        *,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
    ) -> None:
        self.max_retries = config.max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self.bucket = TokenBucket(config.rate_per_sec, config.burst, clock=clock, sleep=sleep)
        self.meter = RateMeter(clock=clock)
        self.stats = HttpStats()
        self._client = httpx.Client(
            headers=dict(headers or {}),
            timeout=config.timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), self.backoff_cap)
        delay = self.backoff_base * (2**attempt)
        return float(min(delay + random.uniform(0, delay / 2), self.backoff_cap))  # noqa: S311

    def get(self, url: str, params: Mapping[str, str | int] | None = None) -> httpx.Response:
        """GET with retries. Returns 2xx and 404 responses; raises HttpError otherwise."""
        request_url = self._client.build_request("GET", url, params=params).url
        safe_url = redact(request_url)
        for attempt in range(self.max_retries + 1):
            self.stats.wait_seconds += self.bucket.acquire()
            self.meter.record()
            self.stats.requests += 1
            response: httpx.Response | None = None
            try:
                response = self._client.get(request_url)
            except httpx.TransportError as exc:
                reason = f"{type(exc).__name__}"
            else:
                if response.status_code < 400 or response.status_code == 404:
                    return response
                if response.status_code not in RETRY_STATUSES:
                    raise HttpError(
                        f"GET {safe_url} -> {response.status_code}: {response.text[:300]}",
                        status=response.status_code,
                    )
                reason = f"HTTP {response.status_code}"
            if attempt == self.max_retries:
                raise HttpError(
                    f"GET {safe_url} failed after {attempt + 1} attempts ({reason})",
                    status=response.status_code if response is not None else None,
                )
            delay = self._backoff(attempt, response)
            self.stats.retries += 1
            log.warning(
                "retrying request",
                extra={"url": safe_url, "reason": reason, "attempt": attempt + 1, "delay": delay},
            )
            self._sleep(delay)
        raise AssertionError("unreachable")
