"""Filesystem locations shared by services."""

from __future__ import annotations

from pathlib import Path

from tripwire_common.settings import Settings, get_settings


def training_root(mapping_version: str, settings: Settings | None = None) -> Path:
    """Training universe (Parquet) for one metric-mapping version."""
    settings = settings or get_settings()
    return settings.tripwire_data_dir / "training" / f"mapping_version={mapping_version}"
