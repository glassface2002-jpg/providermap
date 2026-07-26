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
    # "http" (plain httpx, the default) or "playwright" (real browser
    # rendering - see RenderingConfig). Needed for sites whose bot-management
    # (e.g. Akamai) blocks plain HTTP clients but not a normal browser.
    fetch_mode: str = "http"


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
class RenderingConfig:
    """Only consulted when ``site.fetch_mode == "playwright"``. Kept as its
    own section (rather than folded into ``politeness``) because it's
    optional infrastructure most adapters/sites never need - see
    ``providermap/render.py``.
    """

    headless: bool = True
    browser: str = "chromium"
    wait_after_load_seconds: float = 1.5


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
class OrganizationsConfig:
    """Settings for organization-side adapters (see ``adapters/organizations``).

    Flat rather than nested per-adapter, matching every other single-purpose
    config section here - add a second key when a second organization
    adapter needs one.
    """

    # `None` (the default): auto-fetch CMS's "Hospital General Information"
    # dataset on every `providermap ingest-organizations` run, via CMS's
    # metastore API (see adapters/organizations/cms_hospitals/adapter.py's
    # module docstring) - always the current file, no manual download step.
    # Set to a path to pin a local copy instead (a fixed archived version,
    # or offline testing).
    cms_hospitals_csv_path: str | None = None
    cms_hospitals_fetch_timeout_seconds: float = 30.0

    # Used by `providermap enrich-organizations` (see
    # providermap/website_enrichment.py) - a handful of existence-check
    # requests per organization, spread across unrelated domains, so a
    # separate conservative budget rather than reusing `politeness.*`
    # (which is tuned for one site's directory, not many unrelated ones).
    website_enrichment_requests_per_second: float = 1.0
    website_enrichment_timeout_seconds: float = 8.0

    # `providermap enrich-organizations` queries Wikidata's public SPARQL
    # endpoint once per run (see providermap/wikidata_hospitals.py) before
    # falling back to guessing - a longer timeout since it's a single,
    # heavier bulk query rather than many small ones.
    wikidata_fetch_timeout_seconds: float = 60.0

    # `providermap enrich-organizations` also queries HIFLD's Hospitals
    # dataset once per run (see providermap/hifld_hospitals.py), between
    # Wikidata and guessing - fetched in ~2,000-row pages, so a similarly
    # generous timeout per page request.
    hifld_fetch_timeout_seconds: float = 60.0

    # An organization checked (Wikidata + guessing both tried) and still left
    # with no website isn't retried again until this many days have passed -
    # otherwise every `enrich-organizations` run re-guesses domains for every
    # hospital that has ever come up empty. `<= 0` means "always retry,
    # ignore how recently it was checked" (mirrors `incremental.
    # refresh_older_than_days`'s convention on the provider side).
    website_recheck_after_days: int = 30

    # `providermap enrich-organizations` also queries the VA Facilities API
    # once per run (see providermap/va_facilities.py), between HIFLD and
    # guessing - narrowly scoped to VA/DoD-type organizations. Unlike the
    # other bulk sources, this one requires a credential: register a free
    # sandbox key at https://developer.va.gov/explore/api/va-facilities/
    # sandbox-access and set it here (never commit a real key - config.yaml
    # is gitignored for exactly this reason, see .gitignore). `None` (the
    # default) skips this source entirely rather than erroring - same
    # graceful-skip contract as every other optional enrichment step.
    va_api_key: str | None = None
    va_facilities_base_url: str = "https://sandbox-api.va.gov/services/va_facilities/v1"
    va_facilities_fetch_timeout_seconds: float = 30.0

    # `providermap enrich-organizations` also consults a local OpenStreetMap
    # hospital index (see providermap/osm_hospitals.py), between VA and
    # guessing. Unlike Wikidata/HIFLD/VA, this source never calls a live API
    # during enrich-organizations - Phase 6.0's validation measured the
    # public Overpass API returning 504/429 errors under load on two
    # separate instances, so it isn't something a production run should
    # depend on. Instead, `providermap refresh-osm-cache` does a periodic,
    # explicit, one-time bulk download+parse of a Geofabrik .osm.pbf extract
    # into a small local JSON cache; enrich-organizations only ever reads
    # that cache. `None` (the default) or a missing cache file skips this
    # source entirely - same graceful-skip contract as VA.
    osm_cache_path: str | None = None
    osm_pbf_url: str = "https://download.geofabrik.de/north-america/us-latest.osm.pbf"


@dataclass
class ProvidersConfig:
    """Settings for provider *dataset/API* adapters (see
    ``adapters/providers/base_dataset.py``) - as opposed to
    ``site``/``SiteConfig``, which configures the scraped-directory pipeline.

    Flat rather than nested per-adapter, matching :class:`OrganizationsConfig`'s
    convention - add a second key when a second provider dataset adapter
    needs one.
    """

    # Cigna's public FHIR R4 Provider Directory (Da Vinci PDex Plan-Net) -
    # see adapters/providers/cigna_fhir/adapter.py. No auth required.
    cigna_fhir_base_url: str = "https://fhir.cigna.com/ProviderDirectory/v1"

    # Two-letter state code (e.g. "CT") to scope a `providermap
    # ingest-providers` pilot run to one state via the Plan-Net
    # `location.address-state` search parameter. `None` (the default) means
    # unscoped - never use that for a real run without deliberately opting
    # in, since Cigna's directory is national.
    cigna_fhir_state: str | None = None

    cigna_fhir_page_size: int = 50
    cigna_fhir_fetch_timeout_seconds: float = 30.0

    # NPPES enrichment for Cigna-sourced providers is off by default: the
    # FHIR response already carries NPI/name/taxonomy, and NPPES is the
    # pipeline's main runtime cost driver (see NppesConfig). Opt in to
    # compare against the FHIR-only pilot numbers.
    cigna_fhir_use_nppes: bool = False

    # Durable resume support (see providermap/checkpoint.py) - a dedicated
    # SQLite file, separate from database.path, tracking which fetch phases/
    # batches a provider dataset adapter has already completed. `None` (the
    # default) disables checkpointing entirely - a run behaves exactly as it
    # did before this existed. Set a path (e.g. "database/checkpoints.db")
    # before any run expected to take hours, so it can survive an
    # interruption without re-fetching everything already done.
    checkpoint_path: str | None = None

    # Bounded concurrency for the PractitionerRole batch phase only (see
    # adapters/providers/cigna_fhir/CONCURRENCY_DESIGN.md) - the Location
    # phase and the unscoped PractitionerRole search stay sequential
    # regardless of this setting, since both paginate via an opaque
    # continuation token that can't be parallelized. `1` (the default)
    # reproduces the original fully-sequential behavior exactly - this is
    # also what the "concurrency on vs. off produce identical output"
    # regression test pins as the baseline. Raise it only for a run where
    # the measured, sequential runtime is already known to be a problem.
    cigna_fhir_max_concurrent_requests: int = 1

    # A token-bucket cap on how fast new batch requests are dispatched,
    # independent of cigna_fhir_max_concurrent_requests (which only bounds
    # how many are in flight at once) - see the design doc's "configurable
    # throttling" section for why both knobs are needed.
    cigna_fhir_requests_per_second: float = 5.0

    # Which DiscoveryStrategy CignaFhirAdapter.discover() delegates to (see
    # adapters/providers/cigna_fhir/discovery.py and
    # NATIONAL_DISCOVERY_DESIGN.md). "state_scoped" (the default) reproduces
    # the original, only-ever-existing behavior exactly - a
    # Location-enumeration-then-batch fetch scoped to cigna_fhir_state (or a
    # single unscoped PractitionerRole walk if that's unset). "national" is
    # a memory-bounded, spool-backed strategy (see resource_spool_path
    # below) - implemented, but per the approved design's migration
    # discipline this default does not change until a production-quality
    # national ingestion has completed successfully and its operational
    # characteristics have been verified. See the ADR for why that's a
    # separate, later decision from "is the code correct".
    cigna_fhir_discovery_mode: str = "state_scoped"

    # Only read by the "national" discovery strategy - a dedicated SQLite
    # file, separate from database.path, that every fetched FHIR resource is
    # spooled to instead of an in-memory cache (see
    # providermap/resource_spool.py). `None` (the default) means national
    # mode isn't usable yet - selecting it without a path set raises rather
    # than silently falling back to the unbounded in-memory cache this
    # strategy exists specifically to avoid.
    resource_spool_path: str | None = None

    # National discovery partitions its unscoped PractitionerRole walk into
    # `_lastUpdated` windows, each `cigna_fhir_window_days` wide, covering
    # the last `cigna_fhir_lookback_days`. Both bounds are documented
    # defaults, not measured ones - the calibration probe (a later,
    # separate step) is what right-sizes them against real data. Both ends
    # of the walk stay open regardless of these values (see
    # discovery.py::_generate_lastupdated_windows), so a resource older than
    # the lookback, or newer than "now", is never silently missed.
    cigna_fhir_window_days: int = 30
    cigna_fhir_lookback_days: int = 3650


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
    rendering: RenderingConfig
    investigation: InvestigationConfig
    incremental: IncrementalConfig
    exports: ExportsConfig
    organizations: OrganizationsConfig
    providers: ProvidersConfig


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
        rendering=RenderingConfig(**raw.get("rendering", {})),
        investigation=InvestigationConfig(**raw.get("investigation", {})),
        incremental=IncrementalConfig(**raw.get("incremental", {})),
        exports=ExportsConfig(**raw.get("exports", {})),
        organizations=OrganizationsConfig(**raw.get("organizations", {})),
        providers=ProvidersConfig(**raw.get("providers", {})),
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
