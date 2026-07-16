"""Typed configuration.

Every tunable in this project lives in ``config.yaml`` (copy it from
``config.example.yaml``) - nothing is hardcoded in the Python source. This
module's job is to load that YAML into typed, dot-accessible dataclasses
instead of passing a raw ``dict`` around, so a typo like ``cfg["politness"]``
becomes an ``AttributeError`` at the call site instead of a silent ``None``
three modules away.

One value can also come from the environment: ``PROVIDERMAP_CONTACT_EMAIL``
overrides ``politeness.user_agent``'s contact address if set. That exists so
a contributor's personal email address never has to be the value committed in
``config.yaml`` - see ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import NpiPolicy


@dataclass
class SiteConfig:
    base_url: str
    listing_path: str = "/doctors"
    force_source: str | None = None
    page_param: str = "page"
    page_start: int = 0
    vcard_path: str = ""


@dataclass
class PolitenessConfig:
    requests_per_second: float = 0.5
    max_concurrent: int = 2
    timeout_seconds: int = 30
    user_agent: str = ""
    abort_after_consecutive_errors: int = 25
    respect_robots_txt: bool = True


@dataclass
class RetryConfig:
    max_attempts: int = 4
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 60.0
    retry_on_status: list[int] = field(default_factory=lambda: [429, 500, 502, 503, 504])


@dataclass
class NppesConfig:
    enabled: bool = True
    api_url: str = "https://npiregistry.cms.hhs.gov/api/"
    version: str = "2.1"
    requests_per_second: float = 2.0
    timeout_seconds: int = 20
    trip_after_consecutive_failures: int = 20


@dataclass
class DatabaseConfig:
    path: str = "database/providermap.db"


@dataclass
class CacheConfig:
    enabled: bool = True
    dir: str = ".cache"
    ttl_days: int = 30


@dataclass
class LoggingConfig:
    dir: str = "logs"
    level: str = "INFO"


@dataclass
class InvestigationConfig:
    sample_size: int = 750


@dataclass
class IncrementalConfig:
    refresh_older_than_days: int = 30
    deactivate_missing: bool = True


@dataclass
class ExportsConfig:
    dir: str = "exports/output"


@dataclass
class Config:
    """The full, typed configuration tree, mirroring ``config.yaml``."""

    site: SiteConfig
    politeness: PolitenessConfig
    retries: RetryConfig
    nppes: NppesConfig
    npi: NpiPolicy
    database: DatabaseConfig
    cache: CacheConfig
    logging: LoggingConfig
    investigation: InvestigationConfig
    incremental: IncrementalConfig
    exports: ExportsConfig


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load and validate ``config.yaml`` (or the given path) into a :class:`Config`.

    Raises ``FileNotFoundError`` with a hint toward ``config.example.yaml``
    if the file is missing, rather than a bare traceback.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yaml to {path.name} and "
            f"edit politeness.user_agent before running."
        )
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    cfg = Config(
        site=SiteConfig(**raw.get("site", {})),
        politeness=PolitenessConfig(**raw.get("politeness", {})),
        retries=RetryConfig(**raw.get("retries", {})),
        nppes=NppesConfig(**raw.get("nppes", {})),
        npi=NpiPolicy(**raw.get("npi", {})),
        database=DatabaseConfig(**raw.get("database", {})),
        cache=CacheConfig(**raw.get("cache", {})),
        logging=LoggingConfig(**raw.get("logging", {})),
        investigation=InvestigationConfig(**raw.get("investigation", {})),
        incremental=IncrementalConfig(**raw.get("incremental", {})),
        exports=ExportsConfig(**raw.get("exports", {})),
    )
    _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg: Config) -> None:
    """Apply the small set of settings that may come from the environment.

    Kept to exactly one variable on purpose: config.yaml is the source of
    truth for everything else, and a sprawling set of env overrides would
    undermine that. This one exists specifically so a contact email need not
    be committed to a public fork.
    """
    email = os.environ.get("PROVIDERMAP_CONTACT_EMAIL")
    if email:
        cfg.politeness.user_agent = (
            f"ProviderMapBot/1.0 (+contact: {email})"
            if "{contact}" not in cfg.politeness.user_agent
            else cfg.politeness.user_agent.replace("{contact}", email)
        )
