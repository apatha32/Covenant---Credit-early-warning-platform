from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from tripwire_common.logging import configure_logging


@pytest.fixture(autouse=True)
def _restore_root_handlers() -> Iterator[None]:
    """configure_logging points the root logger at capsys's stream; undo that afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_logs_are_single_line_json_with_extras(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("ingest", level="INFO")
    logging.getLogger("tripwire.test").info("fetched", extra={"cik": "0000320193", "rows": 3})
    logging.getLogger("tripwire.test").debug("hidden")

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["service"] == "ingest"
    assert record["level"] == "INFO"
    assert record["message"] == "fetched"
    assert record["cik"] == "0000320193"
    assert record["rows"] == 3


def test_exceptions_are_captured(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api", level="INFO")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("tripwire.test").exception("failed")
    record = json.loads(capsys.readouterr().out.strip())
    assert "ValueError: boom" in record["exc_info"]


def test_httpx_request_urls_are_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("ingest", level="DEBUG")
    logging.getLogger("httpx").info("HTTP Request: GET https://x/?api_key=secret")
    assert "secret" not in capsys.readouterr().out
