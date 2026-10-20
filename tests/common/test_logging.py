from __future__ import annotations

import json
import logging

import pytest

from tripwire_common.logging import configure_logging


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
