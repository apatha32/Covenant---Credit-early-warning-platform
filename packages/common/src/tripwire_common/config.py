"""Typed loaders for YAML config: model settings and the active domain pack."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tripwire_common.settings import ConfigError, get_settings


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- config/models.yaml ---


class LLMConfig(_Strict):
    extraction_default: str
    extraction_overrides: dict[str, str] = Field(default_factory=dict)
    qa: str

    def extraction_model(self, signal_type: str) -> str:
        return self.extraction_overrides.get(signal_type, self.extraction_default)


class EmbeddingConfig(_Strict):
    provider: str
    model: str
    dimensions: int = Field(gt=0)


class MLConfig(_Strict):
    random_seed: int
    primary_metric: str


class ModelsConfig(_Strict):
    version: int
    llm: LLMConfig
    embedding: EmbeddingConfig
    ml: MLConfig


# --- config/domain/<pack>/ ---


class EventCode(_Strict):
    event_type: str
    description: str


class EventCodes(_Strict):
    version: int
    eight_k_items: dict[str, EventCode]
    form_types: dict[str, EventCode]

    def event_types(self) -> set[str]:
        codes = [*self.eight_k_items.values(), *self.form_types.values()]
        return {c.event_type for c in codes}


class SicCode(_Strict):
    code: int
    reason: str
    description: str


class SicRange(_Strict):
    min: int
    max: int
    reason: str
    description: str

    @model_validator(mode="after")
    def _ordered(self) -> SicRange:
        if self.min > self.max:
            raise ValueError(f"SIC range min {self.min} > max {self.max}")
        return self


class Exclusions(_Strict):
    version: int
    sic_codes: list[SicCode] = Field(default_factory=list)
    sic_ranges: list[SicRange] = Field(default_factory=list)

    def exclusion_reason(self, sic: int | None) -> str | None:
        """Reason a company with this SIC is excluded, or None if it is in scope."""
        if sic is None:
            return None
        for code in self.sic_codes:
            if code.code == sic:
                return code.reason
        for rng in self.sic_ranges:
            if rng.min <= sic <= rng.max:
                return rng.reason
        return None


class SecondaryEvent(_Strict):
    event_type: str
    enabled: bool = False


class SecondaryTextSignal(_Strict):
    signal_type: str
    enabled: bool = False
    min_confidence: float = Field(ge=0, le=1)


class Labels(_Strict):
    version: int
    horizon_months: int = Field(gt=0)
    primary_events: list[str] = Field(min_length=1)
    secondary_events: list[SecondaryEvent] = Field(default_factory=list)
    secondary_text_signals: list[SecondaryTextSignal] = Field(default_factory=list)


class MacroSeriesSpec(_Strict):
    id: str
    vintages: bool
    release_lag_days: int = Field(default=0, ge=0)
    description: str

    @model_validator(mode="after")
    def _lag_only_without_vintages(self) -> MacroSeriesSpec:
        if self.vintages and self.release_lag_days:
            raise ValueError(f"{self.id}: release_lag_days applies only when vintages is false")
        return self


class MacroSeries(_Strict):
    version: int
    series: list[MacroSeriesSpec] = Field(min_length=1)


class KnownBankruptcy(_Strict):
    cik: int
    name: str
    petition_year: int


class KnownBankruptcies(_Strict):
    version: int
    companies: list[KnownBankruptcy]


class MetricFormula(_Strict):
    """Fallback when no tag is reported: sum(plus) - sum(minus), all from the same filing and
    period. Every component must be present, or the formula is not used."""

    plus: list[str] = Field(min_length=1)
    minus: list[str] = Field(default_factory=list)

    def tags(self) -> list[str]:
        return [*self.plus, *self.minus]

    def describe(self) -> str:
        return (
            " - ".join([" + ".join(self.plus), *self.minus])
            if self.minus
            else " + ".join(self.plus)
        )


class MetricSpec(_Strict):
    kind: Literal["duration", "instant"]
    unit: str
    sign: Literal[1, -1] = 1
    # Quarantine values ~1000x off the company's neighboring periods (likely scale errors).
    # Only for metrics whose size is stable; net income or cash can legitimately swing by 1000x.
    scale_check: bool = False
    tags: list[str] = Field(min_length=1)
    formulas: list[MetricFormula] = Field(default_factory=list)

    @model_validator(mode="after")
    def _qualified_unique_tags(self) -> MetricSpec:
        for tag in [*self.tags, *(t for f in self.formulas for t in f.tags())]:
            if tag.count(":") != 1:
                raise ValueError(f"tag {tag!r} must be 'taxonomy:Tag'")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError(f"duplicate tags in {self.tags}")
        return self

    def priority(self, taxonomy: str, tag: str) -> int | None:
        """Position of taxonomy:tag in this metric's list (0 = preferred), or None."""
        try:
            return self.tags.index(f"{taxonomy}:{tag}")
        except ValueError:
            return None


class MetricMapping(_Strict):
    version: int
    metrics: dict[str, MetricSpec] = Field(min_length=1)

    @property
    def mapping_version(self) -> str:
        return str(self.version)

    def staged_tags(self) -> dict[tuple[str, str], str]:
        """(taxonomy, tag) -> expected unit, for every tag a metric or formula reads."""
        units: dict[tuple[str, str], str] = {}
        for spec in self.metrics.values():
            for qualified in [*spec.tags, *(t for f in spec.formulas for t in f.tags())]:
                taxonomy, tag = qualified.split(":")
                if units.setdefault((taxonomy, tag), spec.unit) != spec.unit:
                    raise ValueError(f"{qualified} is used with two different units")
        return units

    def tag_index(self) -> dict[tuple[str, str], list[tuple[str, int]]]:
        """(taxonomy, tag) -> [(metric, priority), ...]. A tag may feed more than one metric."""
        index: dict[tuple[str, str], list[tuple[str, int]]] = {}
        for name, spec in self.metrics.items():
            for priority, qualified in enumerate(spec.tags):
                taxonomy, tag = qualified.split(":")
                index.setdefault((taxonomy, tag), []).append((name, priority))
        return index


class DomainPack(_Strict):
    name: str
    directory: Path
    event_codes: EventCodes
    exclusions: Exclusions
    labels: Labels
    macro: MacroSeries
    known_bankruptcies: KnownBankruptcies
    metrics: MetricMapping

    @model_validator(mode="after")
    def _label_events_exist(self) -> DomainPack:
        known = self.event_codes.event_types()
        used = set(self.labels.primary_events) | {
            e.event_type for e in self.labels.secondary_events
        }
        unknown = used - known
        if unknown:
            raise ValueError(f"labels.yaml uses event types not in event_codes.yaml: {unknown}")
        return self


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ConfigError(f"config file is not a mapping: {path}")
    return data


def load_models_config(config_dir: Path | None = None) -> ModelsConfig:
    config_dir = config_dir or get_settings().tripwire_config_dir
    return ModelsConfig.model_validate(_read_yaml(config_dir / "models.yaml"))


def load_domain_pack(name: str | None = None, config_dir: Path | None = None) -> DomainPack:
    settings = get_settings()
    name = name or settings.tripwire_domain_pack
    pack_dir = (config_dir or settings.tripwire_config_dir) / "domain" / name
    return DomainPack(
        name=name,
        directory=pack_dir,
        event_codes=EventCodes.model_validate(_read_yaml(pack_dir / "event_codes.yaml")),
        exclusions=Exclusions.model_validate(_read_yaml(pack_dir / "exclusions.yaml")),
        labels=Labels.model_validate(_read_yaml(pack_dir / "labels.yaml")),
        macro=MacroSeries.model_validate(_read_yaml(pack_dir / "macro_series.yaml")),
        known_bankruptcies=KnownBankruptcies.model_validate(
            _read_yaml(pack_dir / "known_bankruptcies.yaml")
        ),
        metrics=MetricMapping.model_validate(_read_yaml(pack_dir / "metric_mapping.yaml")),
    )


# --- config/sources.yaml ---


class HttpSourceConfig(_Strict):
    rate_per_sec: float = Field(gt=0)
    burst: float = Field(default=1, ge=1)
    max_retries: int = Field(ge=0)
    timeout_seconds: float = Field(gt=0)


class SecSourceConfig(HttpSourceConfig):
    tickers_url: str
    submissions_url: str
    submissions_page_url: str
    companyfacts_url: str
    bulk_submissions_url: str
    bulk_companyfacts_url: str

    @model_validator(mode="after")
    def _within_sec_fair_access(self) -> SecSourceConfig:
        # Worst case in any one-second window is burst + rate requests.
        if self.rate_per_sec + self.burst > 10:
            raise ValueError(
                "SEC allows at most 10 requests/sec: rate_per_sec + burst must be <= 10"
            )
        return self


class FredSourceConfig(HttpSourceConfig):
    observations_url: str
    page_size: int = Field(gt=0, le=100000)


class SourcesConfig(_Strict):
    version: int
    sec: SecSourceConfig
    fred: FredSourceConfig


def load_sources_config(config_dir: Path | None = None) -> SourcesConfig:
    config_dir = config_dir or get_settings().tripwire_config_dir
    return SourcesConfig.model_validate(_read_yaml(config_dir / "sources.yaml"))


# --- <pack>/dev_sample.yaml (generated by `tripwire_ingest build-sample`) ---


class SampleCompany(_Strict):
    cik: int
    name: str


class DevSample(_Strict):
    version: int
    generated_on: str
    seed: int
    criteria: str
    random: list[SampleCompany]
    known_bankruptcies: list[SampleCompany]

    def ciks(self) -> list[int]:
        return sorted({c.cik for c in [*self.random, *self.known_bankruptcies]})


def dev_sample_path(pack: DomainPack) -> Path:
    return pack.directory / "dev_sample.yaml"


def load_dev_sample(pack: DomainPack) -> DevSample:
    return DevSample.model_validate(_read_yaml(dev_sample_path(pack)))
