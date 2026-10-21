from __future__ import annotations

import httpx
import pytest
from sec_fakes import FakeServer

from tripwire_common.config import HttpSourceConfig, load_sources_config
from tripwire_common.settings import REPO_ROOT, ConfigError, Settings
from tripwire_ingest.http import HttpClient, HttpError
from tripwire_ingest.ratelimit import RateMeter, TokenBucket
from tripwire_ingest.sec import sec_client


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _config(**overrides: float) -> HttpSourceConfig:
    values = {"rate_per_sec": 8.0, "burst": 1.0, "max_retries": 3, "timeout_seconds": 5.0}
    return HttpSourceConfig.model_validate({**values, **overrides})


def _client(server: FakeServer, clock: FakeClock, **overrides: float) -> HttpClient:
    return HttpClient(_config(**overrides), transport=server.transport, clock=clock,
                      sleep=clock.sleep, headers={"User-Agent": "T <t@t.com>"})  # fmt: skip


# --- rate limiting ---


def test_token_bucket_spaces_requests_at_the_rate() -> None:
    clock = FakeClock()
    bucket = TokenBucket(8, 1, clock=clock, sleep=clock.sleep)
    for _ in range(17):
        bucket.acquire()
    assert clock.now == pytest.approx(16 / 8)  # first is free, then one every 1/8 s


def test_token_bucket_allows_burst_then_throttles() -> None:
    clock = FakeClock()
    bucket = TokenBucket(2, 3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        bucket.acquire()
    assert clock.now == 0
    bucket.acquire()
    assert clock.now == pytest.approx(0.5)


def test_sec_config_never_exceeds_ten_per_second() -> None:
    sec = load_sources_config(REPO_ROOT / "config").sec
    clock = FakeClock()
    bucket = TokenBucket(sec.rate_per_sec, sec.burst, clock=clock, sleep=clock.sleep)
    meter = RateMeter(clock=clock)
    for _ in range(200):
        bucket.acquire()
        meter.record()
    assert meter.max_per_second <= 10


def test_sec_config_rejects_unsafe_rate() -> None:
    raw = load_sources_config(REPO_ROOT / "config").sec.model_dump()
    with pytest.raises(ValueError, match="10 requests/sec"):
        type(load_sources_config(REPO_ROOT / "config").sec).model_validate(
            {**raw, "rate_per_sec": 10, "burst": 1}
        )


def test_retries_count_against_the_rate_limit(fake_sec: FakeServer) -> None:
    clock = FakeClock()
    fake_sec.add("/x", status=503)
    fake_sec.add("/x", body=b"ok")
    client = _client(fake_sec, clock)
    client.backoff_base = 0  # isolate rate-limit waits from backoff
    client.get("https://h/x")
    assert client.stats.requests == 2
    assert client.stats.wait_seconds == pytest.approx(1 / 8)


# --- retries ---


def test_retries_5xx_with_exponential_backoff(fake_sec: FakeServer) -> None:
    clock = FakeClock()
    for status in (503, 502):
        fake_sec.add("/x", status=status)
    fake_sec.add("/x", body=b"ok")
    client = _client(fake_sec, clock, rate_per_sec=1000)
    assert client.get("https://h/x").content == b"ok"
    assert client.stats.retries == 2
    backoffs = [s for s in clock.sleeps if s >= 1]
    assert 1 <= backoffs[0] <= 1.5 and 2 <= backoffs[1] <= 3


def test_honors_retry_after_on_429(fake_sec: FakeServer) -> None:
    clock = FakeClock()
    fake_sec.add("/x", status=429, headers={"Retry-After": "7"})
    fake_sec.add("/x", body=b"ok")
    client = _client(fake_sec, clock, rate_per_sec=1000)
    client.get("https://h/x")
    assert 7 in clock.sleeps


def test_retries_transport_errors(fake_sec: FakeServer) -> None:
    clock = FakeClock()
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, content=b"ok")

    fake_sec.handler = flaky
    assert _client(fake_sec, clock, rate_per_sec=1000).get("https://h/x").content == b"ok"


def test_gives_up_after_max_retries(fake_sec: FakeServer) -> None:
    clock = FakeClock()
    fake_sec.add("/x", status=500)
    client = _client(fake_sec, clock, rate_per_sec=1000, max_retries=2)
    with pytest.raises(HttpError, match="after 3 attempts"):
        client.get("https://h/x")
    assert client.stats.requests == 3


def test_404_is_returned_not_retried(fake_sec: FakeServer) -> None:
    clock = FakeClock()
    client = _client(fake_sec, clock)
    assert client.get("https://h/missing").status_code == 404
    assert client.stats.requests == 1


def test_403_fails_fast(fake_sec: FakeServer) -> None:
    clock = FakeClock()
    fake_sec.add("/x", body=b"Undeclared Automated Tool", status=403)
    client = _client(fake_sec, clock)
    with pytest.raises(HttpError, match="403"):
        client.get("https://h/x")
    assert client.stats.requests == 1


def test_api_key_is_redacted_from_errors(fake_sec: FakeServer) -> None:
    clock = FakeClock()
    fake_sec.add("/x", status=400, body=b"bad")
    with pytest.raises(HttpError) as info:
        _client(fake_sec, clock).get("https://h/x", params={"api_key": "s3cret", "q": "1"})
    assert "s3cret" not in str(info.value)
    assert "REDACTED" in str(info.value)


# --- SEC client ---


def test_sec_client_sends_user_agent(fake_sec: FakeServer) -> None:
    settings = Settings(_env_file=None, sec_user_agent="Ambarish <a@b.com>")  # type: ignore[call-arg]
    fake_sec.add("/x", body=b"ok")
    sec = load_sources_config(REPO_ROOT / "config").sec
    with sec_client(settings, sec, transport=fake_sec.transport) as client:
        client.get("https://data.sec.gov/x")
    assert fake_sec.requests[0].headers["User-Agent"] == "Ambarish <a@b.com>"


def test_sec_client_requires_user_agent() -> None:
    settings = Settings(_env_file=None, sec_user_agent=None)  # type: ignore[call-arg]
    with pytest.raises(ConfigError, match="SEC_USER_AGENT"):
        sec_client(settings, load_sources_config(REPO_ROOT / "config").sec)
