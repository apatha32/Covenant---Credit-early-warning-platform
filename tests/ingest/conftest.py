from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr
from sec_fakes import FakeServer

from tripwire_common.config import load_domain_pack, load_sources_config
from tripwire_common.settings import REPO_ROOT, Settings
from tripwire_common.storage import LocalStorage
from tripwire_ingest.cli import Context


@pytest.fixture
def fake_sec() -> FakeServer:
    return FakeServer()


@pytest.fixture
def fake_fred() -> FakeServer:
    return FakeServer()


@pytest.fixture
def ctx_factory(
    db: psycopg.Connection, tmp_path: Path, fake_sec: FakeServer, fake_fred: FakeServer
) -> Callable[[], Context]:
    settings = Settings(_env_file=None, sec_user_agent="Test <t@example.com>",  # type: ignore[call-arg]
                        fred_api_key=SecretStr("k"))  # fmt: skip
    sources = load_sources_config(REPO_ROOT / "config")
    pack = load_domain_pack("finance", REPO_ROOT / "config")
    storage = LocalStorage(tmp_path / "data")

    def make() -> Context:
        return Context(db, settings, sources, pack, storage, fake_sec.transport,
                       fake_fred.transport)  # fmt: skip

    return make
