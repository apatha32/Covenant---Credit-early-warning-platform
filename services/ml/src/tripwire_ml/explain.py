"""Plain-English drivers from LightGBM TreeSHAP contributions."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from tripwire_ml.features import FEATURE_COLUMNS, FEATURES


def fmt(value: float | None, kind: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if kind == "log_usd":  # stored as log10(dollars)
        dollars = 10**value
        for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if dollars >= size:
                return f"${dollars / size:.1f}{unit}"
        return f"${dollars:.0f}"
    if kind == "pct":
        return f"{value * 100:.1f}%"
    if kind == "x":
        return f"{value:.1f}x"
    if kind in ("days", "count"):
        return f"{value:.0f}"
    return f"{value:.2f}"


def _moved(now: float, then: float, kind: str) -> bool:
    """Worth saying 'rose / fell'? Not for changes under 5% (or under 1 for counts / days)."""
    if kind == "log_usd":
        now, then = 10**now, 10**then
    if kind in ("count", "days"):
        return abs(now - then) >= 1
    return abs(now - then) > 0.05 * max(abs(then), 1e-9)


def drivers(
    contributions: np.ndarray,
    values: Mapping[str, float | None],
    year_ago: Mapping[str, float | None] | None = None,
    k: int = 5,
) -> list[dict[str, Any]]:
    """Top-k features pushing risk up, as sentences: 'Interest coverage fell from 3.1x to 0.8x'."""
    order = np.argsort(-contributions)
    out: list[dict[str, Any]] = []
    for i in order:
        if len(out) == k or contributions[i] <= 0:
            break
        name = FEATURE_COLUMNS[i]
        label, kind = FEATURES[name]
        now = values.get(name)
        then = (year_ago or {}).get(name)
        if kind == "flag":
            if not now:
                continue
            text = label
        elif (
            then is not None
            and now is not None
            and not math.isnan(then)
            and _moved(now, then, kind)
        ):
            verb = "rose" if now > then else "fell"
            text = f"{label} {verb} from {fmt(then, kind)} to {fmt(now, kind)}"
        elif now is None or (isinstance(now, float) and math.isnan(now)):
            text = f"{label}: not reported"
        else:
            text = f"{label}: {fmt(now, kind)}"
        out.append({"feature": name, "contribution": round(float(contributions[i]), 4),
                    "text": text})  # fmt: skip
    return out
