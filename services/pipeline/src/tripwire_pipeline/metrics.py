"""Staged XBRL facts -> canonical metric rows (pure functions, no I/O).

Steps, per company:
1. resolve:  for each metric, period and filing, take the highest-priority tag that filing
             reported (tag fallback), apply the sign, and record the tag as lineage.
2. screen:   drop scale outliers (values ~1000x off neighboring periods) for quarantine.
3. build:    every reported quarter / fiscal year / balance-sheet date becomes a row; every
             filing that reports a period again is another version of it (restatements included).
4. derive:   missing quarters come from year-to-date values (Q2 = 6M - Q1, Q3 = 9M - 6M,
             Q4 = FY - 9M), re-derived whenever a component is restated.

Point-in-time rule for derived values: a derived value is only knowable once *both* inputs are
public, so it takes the later input's filed_date and accession, and lists both inputs in
`derivation`. A derived value is never produced for a quarter that is already reported directly
as of that date.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

import polars as pl

from tripwire_common.config import MetricMapping
from tripwire_pipeline.periods import YTD_PART, DurationClass, classify_duration

PeriodType = Literal["Q", "FY"]
ObsClass = Literal["Q", "H1", "M9", "FY", "I"]  # I = instant (balance sheet)

# A scale slip (value reported in thousands as units, or the reverse) is ~1000x off neighbors
# that agree with each other. Noisy neighbors (e.g. a shell company before a reverse merger) are
# not evidence of a slip, so the check is skipped when they spread more than SCALE_MAX_SPREAD.
SCALE_RATIO = (500.0, 1500.0)  # |value / neighbor median|, or its inverse
SCALE_MAX_SPREAD = 10.0  # max(neighbors) / min(neighbors)
SCALE_WINDOW_DAYS = 730
SCALE_MIN_NEIGHBORS = 2


@dataclass(frozen=True)
class Observation:
    """One value for one canonical metric, as reported in one filing."""

    cik: str
    metric: str
    period_class: ObsClass
    period_start: dt.date | None
    period_end: dt.date
    value: float
    source_tag: str  # taxonomy:tag
    accession: str
    filed_date: dt.date
    # Formula fallbacks: (role, taxonomy:tag, value) for each input, all from this filing.
    components: tuple[tuple[str, str, float], ...] = ()

    def lineage(self) -> dict[str, Any]:
        return {
            "accession": self.accession,
            "tag": self.source_tag,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat(),
            "value": self.value,
            "filed_date": self.filed_date.isoformat(),
        }


@dataclass(frozen=True)
class MetricRow:
    """A curated.financial_metric row, before a version number is assigned."""

    cik: str
    metric: str
    period_type: PeriodType
    period_start: dt.date | None
    period_end: dt.date
    unit: str
    value: float
    source_tag: str
    is_derived: bool
    filed_date: dt.date
    accession: str
    derivation: list[dict[str, Any]] | None = None

    @property
    def key(self) -> tuple[str, str, dt.date, str]:
        return (self.metric, self.period_type, self.period_end, self.accession)


@dataclass
class Resolution:
    observations: list[Observation] = field(default_factory=list)
    unclassified: int = 0  # durations of other lengths (e.g. 2-month transition periods)
    unaligned_instants: int = 0  # balance-sheet dates that match no reporting period end
    fy_ends: set[dt.date] = field(default_factory=set)  # fiscal-year end dates seen in the data


def _obs_class(
    kind: str, start: dt.date | None, end: dt.date, period_ends: set[dt.date], result: Resolution
) -> ObsClass | None:
    """Observation class for a fact feeding a metric of this kind, or None to skip it."""
    if kind == "duration":
        if start is None:
            return None
        cls = classify_duration(start, end)
        if cls is None:
            result.unclassified += 1
        return cls
    if start is not None:
        return None
    if end not in period_ends:
        result.unaligned_instants += 1
        return None
    return "I"


def resolve(facts: pl.DataFrame, mapping: MetricMapping) -> Resolution:
    """Staged facts for ONE company -> one observation per (metric, period, filing)."""
    index = mapping.tag_index()
    result = Resolution()
    fy_ends: set[dt.date] = set()
    all_ends: set[dt.date] = set()
    best: dict[tuple[str, ObsClass, dt.date | None, dt.date, str], tuple[int, Observation]] = {}

    rows = facts.select(
        "cik", "taxonomy", "tag", "value", "period_start", "period_end", "accession", "filed_date"
    ).to_dicts()
    # Period ends of every duration fact (any tag) tell us which balance-sheet dates are
    # fiscal-year ends and which are quarter ends.
    for r in rows:
        if r["period_start"] is not None:
            cls = classify_duration(r["period_start"], r["period_end"])
            if cls is not None:
                all_ends.add(r["period_end"])
            if cls == "FY":
                fy_ends.add(r["period_end"])

    for r in rows:
        for metric, priority in index.get((r["taxonomy"], r["tag"]), []):
            spec = mapping.metrics[metric]
            obs_class = _obs_class(spec.kind, r["period_start"], r["period_end"], all_ends, result)
            if obs_class is None:
                continue
            obs = Observation(
                cik=r["cik"], metric=metric, period_class=obs_class,
                period_start=r["period_start"], period_end=r["period_end"],
                value=float(r["value"]) * spec.sign, source_tag=f"{r['taxonomy']}:{r['tag']}",
                accession=r["accession"], filed_date=r["filed_date"],
            )  # fmt: skip
            key = (metric, obs_class, r["period_start"], r["period_end"], r["accession"])
            if key not in best or priority < best[key][0]:
                best[key] = (priority, obs)

    _apply_formulas(rows, mapping, all_ends, best, result)
    result.observations = sorted(
        (obs for _, obs in best.values()),
        key=lambda o: (o.metric, o.period_class, o.period_end, o.filed_date, o.accession),
    )
    result.fy_ends = fy_ends
    return result


def _apply_formulas(
    rows: list[dict[str, Any]],
    mapping: MetricMapping,
    all_ends: set[dt.date],
    best: dict[tuple[str, ObsClass, dt.date | None, dt.date, str], tuple[int, Observation]],
    result: Resolution,
) -> None:
    """Formula fallbacks, only for (metric, period, filing) keys no tag covered."""
    values: dict[tuple[str, dt.date | None, dt.date, str], tuple[float, dt.date, str]] = {}
    for r in rows:
        qualified = f"{r['taxonomy']}:{r['tag']}"
        values[(qualified, r["period_start"], r["period_end"], r["accession"])] = (
            float(r["value"]), r["filed_date"], r["cik"])  # fmt: skip
    for metric, spec in mapping.metrics.items():
        if not spec.formulas:
            continue
        components = {t for f in spec.formulas for t in f.tags()}
        periods = {(start, end, acc) for (tag, start, end, acc) in values if tag in components}
        for start, end, accession in sorted(periods, key=lambda p: (p[1], p[2])):
            obs_class = _obs_class(spec.kind, start, end, all_ends, result)
            if obs_class is None or (metric, obs_class, start, end, accession) in best:
                continue
            key = (metric, obs_class, start, end, accession)
            for i, formula in enumerate(spec.formulas):
                parts = [(role, tag, values.get((tag, start, end, accession)))
                         for role, tags in (("plus", formula.plus), ("minus", formula.minus))
                         for tag in tags]  # fmt: skip
                if any(v is None for _, _, v in parts):
                    continue
                filed, cik = next(v for _, _, v in parts if v is not None)[1:]
                total = sum(v[0] if role == "plus" else -v[0]
                            for role, _, v in parts if v is not None)  # fmt: skip
                best[key] = (len(spec.tags) + i, Observation(
                    cik=cik, metric=metric, period_class=obs_class, period_start=start,
                    period_end=end, value=total * spec.sign,
                    source_tag=f"formula: {formula.describe()}", accession=accession,
                    filed_date=filed,
                    components=tuple((role, tag, v[0]) for role, tag, v in parts if v is not None),
                ))  # fmt: skip
                break


def scale_outliers(observations: list[Observation], mapping: MetricMapping) -> list[Observation]:
    """Observations ~1000x off the median of the same metric's neighboring periods."""
    by_series: dict[tuple[str, ObsClass], list[Observation]] = defaultdict(list)
    for obs in observations:
        if mapping.metrics[obs.metric].scale_check:
            by_series[(obs.metric, obs.period_class)].append(obs)

    flagged: list[Observation] = []
    low, high = SCALE_RATIO
    for series in by_series.values():
        latest: dict[dt.date, Observation] = {}
        for obs in sorted(series, key=lambda o: (o.filed_date, o.accession)):
            latest[obs.period_end] = obs
        for obs in series:
            neighbors = [
                abs(o.value) for end, o in latest.items()
                if end != obs.period_end and abs((end - obs.period_end).days) <= SCALE_WINDOW_DAYS
                and o.value != 0
            ]  # fmt: skip
            if len(neighbors) < SCALE_MIN_NEIGHBORS or obs.value == 0:
                continue
            if max(neighbors) / min(neighbors) > SCALE_MAX_SPREAD:
                continue
            ratio = abs(obs.value) / statistics.median(neighbors)
            if low <= ratio <= high or low <= 1 / ratio <= high:
                flagged.append(obs)
    return flagged


def build_rows(
    observations: list[Observation], mapping: MetricMapping, fy_ends: set[dt.date]
) -> list[MetricRow]:
    """Reported rows plus YTD-derived quarters, for one company."""
    rows: list[MetricRow] = []
    for obs in observations:
        unit = mapping.metrics[obs.metric].unit
        if obs.period_class in ("Q", "FY"):
            period_type: PeriodType = "FY" if obs.period_class == "FY" else "Q"
        elif obs.period_class == "I":
            period_type = "FY" if obs.period_end in fy_ends else "Q"
        else:
            continue  # H1 / M9 YTD values are inputs to derivation only
        rows.append(MetricRow(
            cik=obs.cik, metric=obs.metric, period_type=period_type,
            period_start=obs.period_start, period_end=obs.period_end, unit=unit, value=obs.value,
            source_tag=obs.source_tag, is_derived=bool(obs.components),
            filed_date=obs.filed_date, accession=obs.accession,
            derivation=[{"role": role, "tag": tag, "value": value, "accession": obs.accession}
                        for role, tag, value in obs.components] or None,
        ))  # fmt: skip
    rows += derive_quarters(observations, mapping)
    return dedupe_keys(rows)


def derive_quarters(observations: list[Observation], mapping: MetricMapping) -> list[MetricRow]:
    """Discrete quarters from YTD values, with point-in-time versions."""
    series: dict[tuple[str, ObsClass, dt.date | None, dt.date], list[Observation]] = defaultdict(
        list
    )
    direct_q: dict[tuple[str, dt.date], list[Observation]] = defaultdict(list)
    for obs in observations:
        series[(obs.metric, obs.period_class, obs.period_start, obs.period_end)].append(obs)
        if obs.period_class == "Q":
            direct_q[(obs.metric, obs.period_end)].append(obs)
    for versions in [*series.values(), *direct_q.values()]:
        versions.sort(key=lambda o: (o.filed_date, o.accession))

    derived: list[MetricRow] = []
    for (metric, total_cls, start, end), totals in series.items():
        if total_cls not in YTD_PART or start is None:
            continue
        part_cls: DurationClass = YTD_PART[total_cls]
        for (p_metric, p_cls, p_start, p_end), parts in series.items():
            if (p_metric, p_cls, p_start) != (metric, part_cls, start) or p_end >= end:
                continue
            q_start = p_end + dt.timedelta(days=1)
            if classify_duration(q_start, end) != "Q":
                continue
            derived += _derive_one(
                metric, mapping.metrics[metric].unit, q_start, end, totals, parts,
                direct_q.get((metric, end), []),
            )  # fmt: skip
    return derived


def _latest(versions: list[Observation], as_of: dt.date) -> Observation | None:
    known = [v for v in versions if v.filed_date <= as_of]
    return known[-1] if known else None


def _derive_one(
    metric: str,
    unit: str,
    q_start: dt.date,
    q_end: dt.date,
    totals: list[Observation],
    parts: list[Observation],
    direct: list[Observation],
) -> list[MetricRow]:
    rows: list[MetricRow] = []
    last_value: float | None = None
    for as_of in sorted({o.filed_date for o in [*totals, *parts]}):
        if direct and direct[0].filed_date <= as_of:
            break  # the quarter is reported directly from here on
        total, part = _latest(totals, as_of), _latest(parts, as_of)
        if total is None or part is None:
            continue
        value = total.value - part.value
        if value == last_value:
            continue  # inputs re-reported unchanged: no new version
        newest = max((total, part), key=lambda o: (o.filed_date, o.accession))
        rows.append(MetricRow(
            cik=total.cik, metric=metric, period_type="Q", period_start=q_start, period_end=q_end,
            unit=unit, value=value, source_tag=total.source_tag, is_derived=True,
            filed_date=newest.filed_date, accession=newest.accession,
            derivation=[{"role": "total", **total.lineage()}, {"role": "minus", **part.lineage()}],
        ))  # fmt: skip
        last_value = value
    return rows


def dedupe_keys(rows: list[MetricRow]) -> list[MetricRow]:
    """One row per (metric, period_type, period_end, accession); reported beats derived."""
    chosen: dict[tuple[str, str, dt.date, str], MetricRow] = {}
    for row in sorted(rows, key=lambda r: r.is_derived):
        chosen.setdefault(row.key, row)
    return sorted(chosen.values(), key=lambda r: (r.metric, r.period_type, r.period_end,
                                                  r.filed_date, r.accession))  # fmt: skip
