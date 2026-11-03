from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from tripwire_llm import cli


class FakeBatches:
    """create() fails with a connection error, but the server had accepted the batch."""

    def __init__(self) -> None:
        now = dt.datetime.now(dt.UTC)
        self.listed = [
            SimpleNamespace(id="old", created_at=now - dt.timedelta(seconds=20),
                            request_counts={"succeeded": 2}),
        ]  # fmt: skip

    def create(self, requests: list[Any]) -> Any:
        created = dt.datetime.now(dt.UTC)
        counts = {"processing": len(requests)}
        self.listed.insert(0, SimpleNamespace(id="new", created_at=created, request_counts=counts))
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))

    def list(self, limit: int) -> list[Any]:
        return self.listed


def test_lost_response_recovers_the_new_batch_not_an_older_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    client: Any = SimpleNamespace(messages=SimpleNamespace(batches=FakeBatches()))
    assert cli._create_batch(client, ["a", "b"], known={"old"}) == "new"
