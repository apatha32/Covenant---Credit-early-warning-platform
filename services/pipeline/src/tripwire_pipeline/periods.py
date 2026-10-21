"""Classify XBRL periods by their actual length.

SEC's `fy`/`fp` fields describe the *filing* a fact came from, not the fact's own period (a 10-K
also reports prior-year comparatives with the current fy/fp), so they cannot be used to tell a
quarter from a year-to-date value. The period's length in days can. The ranges allow for
52/53-week fiscal years and quarters of 12-14 weeks.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

DurationClass = Literal["Q", "H1", "M9", "FY"]

# Inclusive day-count ranges.
DURATION_DAYS: dict[DurationClass, tuple[int, int]] = {
    "Q": (80, 100),
    "H1": (172, 193),
    "M9": (263, 285),
    "FY": (350, 380),
}

# Year-to-date total -> the shorter YTD period that, subtracted from it, leaves one quarter.
YTD_PART: dict[DurationClass, DurationClass] = {"H1": "Q", "M9": "H1", "FY": "M9"}


def days(start: dt.date, end: dt.date) -> int:
    return (end - start).days + 1


def classify_duration(start: dt.date, end: dt.date) -> DurationClass | None:
    """'Q', 'H1' (6M YTD), 'M9' (9M YTD), 'FY', or None for any other length."""
    n = days(start, end)
    for cls, (low, high) in DURATION_DAYS.items():
        if low <= n <= high:
            return cls
    return None
