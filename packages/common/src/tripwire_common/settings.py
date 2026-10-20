"""Environment-backed settings. Secrets come only from env vars (or a local .env)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]

_EMAIL_RE = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    tripwire_env: Literal["dev", "ci", "prod"] = "dev"
    tripwire_domain_pack: str = "finance"
    tripwire_config_dir: Path = REPO_ROOT / "config"
    log_level: str = "INFO"

    # Secrets and endpoints are optional at load time so each service only needs
    # what it uses; call `require()` at the point of use.
    sec_user_agent: str | None = None
    fred_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    supabase_url: str | None = None
    supabase_anon_key: SecretStr | None = None
    supabase_service_role_key: SecretStr | None = None
    database_url: SecretStr | None = None

    @field_validator("sec_user_agent")
    @classmethod
    def _user_agent_has_email(cls, value: str | None) -> str | None:
        # SEC asks for a contact email in the User-Agent; without one the IP can be blocked.
        if value is not None and not _EMAIL_RE.search(value):
            raise ValueError("SEC_USER_AGENT must contain a contact email, e.g. 'Name <me@x.com>'")
        return value

    def require(self, name: str) -> str:
        """Return a setting's value, raising ConfigError if it is unset or empty."""
        value = getattr(self, name)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if value is None or value == "":
            raise ConfigError(f"{name.upper()} is not set; see .env.example")
        return str(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
