from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from tripwire_common.config import load_domain_pack, load_models_config
from tripwire_common.settings import REPO_ROOT, ConfigError, Settings

CONFIG_DIR = REPO_ROOT / "config"


def test_models_config_loads() -> None:
    cfg = load_models_config(CONFIG_DIR)
    assert cfg.llm.extraction_model("going_concern") == cfg.llm.extraction_default


def test_extraction_override_wins(tmp_path: Path) -> None:
    text = (CONFIG_DIR / "models.yaml").read_text()
    text = text.replace("extraction_overrides: {}", "extraction_overrides: {covenant: big-model}")
    (tmp_path / "models.yaml").write_text(text)
    cfg = load_models_config(tmp_path)
    assert cfg.llm.extraction_model("covenant") == "big-model"


def test_embedding_dimensions_match_migration() -> None:
    dims = load_models_config(CONFIG_DIR).embedding.dimensions
    migrations = "".join(p.read_text() for p in (REPO_ROOT / "supabase/migrations").glob("*.sql"))
    assert f"extensions.vector({dims})" in migrations


def test_finance_pack_loads() -> None:
    pack = load_domain_pack("finance", CONFIG_DIR)
    assert pack.event_codes.eight_k_items["1.03"].event_type == "bankruptcy"
    assert pack.event_codes.form_types["NT 10-K"].event_type == "late_filing_annual"
    assert pack.labels.primary_events == ["bankruptcy"]
    assert pack.labels.horizon_months == 12


@pytest.mark.parametrize(
    ("sic", "reason"),
    [
        (6770, "shell_company"),
        (6021, "financial_sector"),
        (6000, "financial_sector"),
        (6799, "financial_sector"),
        (5999, None),
        (6800, None),
        (3571, None),
        (None, None),
    ],
)
def test_sic_exclusions(sic: int | None, reason: str | None) -> None:
    pack = load_domain_pack("finance", CONFIG_DIR)
    assert pack.exclusions.exclusion_reason(sic) == reason


def test_labels_must_reference_known_event_types(tmp_path: Path) -> None:
    pack_dir = tmp_path / "domain" / "finance"
    shutil.copytree(CONFIG_DIR / "domain" / "finance", pack_dir)
    labels = pack_dir / "labels.yaml"
    labels.write_text(labels.read_text().replace("[bankruptcy]", "[meteor_strike]"))
    with pytest.raises(ValidationError, match="meteor_strike"):
        load_domain_pack("finance", tmp_path)


def test_missing_pack_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_domain_pack("energy", tmp_path)


def test_require_raises_for_missing_secret() -> None:
    settings = Settings(_env_file=None, fred_api_key=None)  # type: ignore[call-arg]
    with pytest.raises(ConfigError, match="FRED_API_KEY"):
        settings.require("fred_api_key")


def test_require_returns_secret_value() -> None:
    settings = Settings(_env_file=None, fred_api_key=SecretStr("abc"))  # type: ignore[call-arg]
    assert settings.require("fred_api_key") == "abc"


def test_sec_user_agent_needs_email() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sec_user_agent="Ambarish")  # type: ignore[call-arg]
    ok = Settings(_env_file=None, sec_user_agent="Ambarish <a@b.com>")  # type: ignore[call-arg]
    assert ok.sec_user_agent == "Ambarish <a@b.com>"


def test_no_model_names_hard_coded_in_python() -> None:
    pattern = re.compile(r"claude-(haiku|sonnet|opus|fable)-\d")
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for root in ("packages", "services", "scripts")
        for p in (REPO_ROOT / root).rglob("*.py")
        if pattern.search(p.read_text())
    ]
    assert offenders == [], "model names belong in config/models.yaml"
