"""Typed loaders for YAML config: model settings and the active domain pack."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


class DomainPack(_Strict):
    name: str
    event_codes: EventCodes
    exclusions: Exclusions
    labels: Labels

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
        event_codes=EventCodes.model_validate(_read_yaml(pack_dir / "event_codes.yaml")),
        exclusions=Exclusions.model_validate(_read_yaml(pack_dir / "exclusions.yaml")),
        labels=Labels.model_validate(_read_yaml(pack_dir / "labels.yaml")),
    )
