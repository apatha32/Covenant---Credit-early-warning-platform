"""Run the API: `uv run python -m tripwire_api` (port from $PORT, default 8000)."""

from __future__ import annotations

import os

import uvicorn

from tripwire_api.app import create_app
from tripwire_common.logging import configure_logging


def main() -> None:
    configure_logging("api")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),  # noqa: S104
                proxy_headers=True, log_config=None)  # fmt: skip


if __name__ == "__main__":
    main()
