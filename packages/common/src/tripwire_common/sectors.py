"""SEC SIC code -> industry division (the console's sector filter). Mirrors util.sector_of()."""

from __future__ import annotations

DIVISIONS = [
    (100, 999, "Agriculture"),
    (1000, 1499, "Mining & energy"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transportation & utilities"),
    (5000, 5199, "Wholesale"),
    (5200, 5999, "Retail"),
    (6000, 6799, "Finance"),
    (7000, 8999, "Services"),
]


def sector_of(sic: int | None) -> str:
    if sic is not None:
        for low, high, name in DIVISIONS:
            if low <= sic <= high:
                return name
    return "Other"
