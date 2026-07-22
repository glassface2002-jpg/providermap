"""Typed data models shared across the pipeline.

Everything that used to travel as an untyped ``dict`` between the fetcher,
classifier, and database layer is defined here instead. The database module
still speaks SQL at its boundary (that's what ``sqlite3.Row`` is for), but the
*public* functions on :class:`~providermap.database.Database` accept and
return these types, not raw dicts, so a typo in a field name is a ``mypy``
error instead of a silent ``None`` at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any


class ProviderType(str, Enum):
    """The seven classification labels a directory record can receive."""

    PHYSICIAN = "Physician"
    ADVANCED_PRACTICE_PROVIDER = "Advanced Practice Provider"
    FACILITY = "Facility"
    CLINIC = "Clinic"
    HOSPITAL = "Hospital"
    ORGANIZATION = "Organization"
    UNKNOWN = "Unknown"

    @property
    def is_individual(self) -> bool:
        """True for the two labels that represent an actual person."""
        return self in (ProviderType.PHYSICIAN, ProviderType.ADVANCED_PRACTICE_PROVIDER)


class Confidence(str, Enum):
    """How much a downstream consumer should trust a derived value."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IdentityConfidence(str, Enum):
    """How much to trust an *identity* fact (an alias or a system-membership
    link) - deliberately a separate enum from :class:`Confidence`, which
    scores *website* trust. "We're sure this facility belongs to Ballad
    Health" and "we're sure this URL is Ballad Health's site" are independent
    claims (see ROADMAP.md's Phase 5.1 architecture notes); collapsing them
    into one enum would hide that distinction.
    """

    HIGH = "high"  # verified ownership/system relationship
    MEDIUM = "medium"  # strong alias or inferred relationship
    LOW = "low"  # weak inference


class ScrapeStatus(str, Enum):
    """State of one URL in the resume ledger."""

    PENDING = "pending"
    DONE = "done"
    ERROR = "error"
    SKIPPED = "skipped"


class UpsertOutcome(str, Enum):
    """What happened when processing one directory record."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    EXCLUDED = "excluded"
    ERROR = "error"


@dataclass(frozen=True)
class NpiPolicy:
    """How strict to be about NPIs.

    See ``config.example.yaml`` for the user-facing documentation of each
    field; this is its typed counterpart. The pipeline is designed to produce
    a usable dataset even with NPPES switched off entirely
    (``require_nppes=False``, the default) - every degradation is recorded on
    the :class:`Provider` it affects rather than silently assumed away.
    """

    checksum: str = "warn"  # "strict" | "warn" | "off"
    require_npi: bool = True
    require_nppes: bool = False

    def __post_init__(self) -> None:
        if self.checksum not in {"strict", "warn", "off"}:
            raise ValueError(f"npi.checksum must be strict|warn|off, got {self.checksum!r}")


@dataclass
class ClassificationResult:
    """The outcome of deciding what a directory record actually is."""

    provider_type: ProviderType
    confidence: Confidence
    basis: str  # e.g. "nppes:NPI-1+credential" - for audit, not display
    reason: str | None = None  # populated when the record will be excluded

    @property
    def is_individual(self) -> bool:
        return self.provider_type.is_individual


@dataclass
class VCardData:
    """Fields extracted from a directory's structured contact-card endpoint."""

    full_name: str | None = None
    organization: str | None = None
    phone: str | None = None
    title: str | None = None
    facility_label: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None


@dataclass
class ProfilePage:
    """Fields extracted from a rendered provider profile page.

    ``display_name``, ``specialty``, and the fields below are populated from
    a site's schema.org JSON-LD when the adapter finds one (see
    :func:`providermap.parser_utils.parse_jsonld_physician`), falling back to
    HTML scraping otherwise. ``location_ids`` is always structural (counted
    from appointment-link ids on the page), never JSON-LD - see the adapter
    docstrings for why the rest is a *fallback* source relative to
    vCard/NPPES/URL.
    """

    display_name: str | None = None
    specialty: str | None = None
    location_ids: list[str] = field(default_factory=list)
    is_error_page: bool = False

    # Sourced from JSON-LD only where a site publishes it; None/empty
    # otherwise. Not derivable from the vCard endpoint or NPPES.
    hospital_affiliation: str | None = None
    accepting_new_patients: bool | None = None
    rating: float | None = None
    rating_count: int | None = None
    languages: list[str] = field(default_factory=list)
    insurance_accepted: list[str] = field(default_factory=list)


@dataclass
class Provider:
    """One individual healthcare provider.

    Mirrors the ``providers`` table. Instances are the unit of exchange
    between the pipeline, the database, and the exporter - nothing downstream
    of classification passes a raw dict.
    """

    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    credentials: str | None = None
    provider_type: ProviderType | None = None
    specialty: str | None = None
    primary_site_of_care: str | None = None
    practice_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    phone: str | None = None
    profile_url: str | None = None
    npi: str | None = None
    npi_valid: bool = True

    location_count: int = 1
    site_confidence: Confidence | None = None
    nppes_enumeration: str | None = None  # "NPI-1" | "NPI-2" | "not_found"
    nppes_name_match: bool | None = None
    taxonomy_code: str | None = None
    taxonomy_desc: str | None = None
    source_notes: str | None = None

    # From JSON-LD where a site publishes it (see ProfilePage); None/empty
    # for adapters or records without it.
    hospital_affiliation: str | None = None
    accepting_new_patients: bool | None = None
    rating: float | None = None
    rating_count: int | None = None
    languages: list[str] = field(default_factory=list)
    insurance_accepted: list[str] = field(default_factory=list)

    # Read-only bookkeeping populated by the database layer on read.
    provider_id: int | None = None
    created_date: str | None = None
    updated_date: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    last_checked: str | None = None
    is_active: bool = True

    def tracked_snapshot(self) -> dict[str, str | None]:
        """The subset of fields that participate in change detection.

        Used to build the content hash and, on a change, the before/after
        pairs recorded in ``provider_changes``.
        """
        return {name: _stringify(getattr(self, name)) for name in TRACKED_FIELDS}


# Kept as a module-level tuple (not a Provider classmethod) so database.py can
# import it without importing the dataclass machinery it doesn't need.
TRACKED_FIELDS: tuple[str, ...] = (
    "full_name",
    "credentials",
    "provider_type",
    "specialty",
    "primary_site_of_care",
    "practice_name",
    "address",
    "city",
    "state",
    "zip",
    "phone",
    "location_count",
    "hospital_affiliation",
    "accepting_new_patients",
    # rating/rating_count deliberately excluded: they fluctuate on their own
    # and would generate constant, meaningless "changed" noise in
    # provider_changes on every refresh. The latest value is still stored,
    # just not diffed.
)


@dataclass
class Organization:
    """One healthcare organization - a hospital, health system, clinic,
    medical group, or facility.

    The foundation for the ``provider -> organization -> location`` model
    ProviderMap is growing into: an :class:`Organization` is the "where"
    that sits between a :class:`Provider` and a physical :class:`Location`
    (e.g. Dr. Smith -> Banner Desert Medical Center -> Mesa, AZ). Mirrors the
    ``organizations`` table.

    Not yet populated by any adapter - the organization ingestion side is
    scaffolding at this stage; see ``ROADMAP.md``. Field names follow the
    same convention as :class:`Provider` (``organization_id``,
    ``created_date``/``updated_date``) rather than a second style.
    """

    name: str | None = None
    normalized_name: str | None = None  # lowercased/stripped, for dedupe + matching
    organization_type: str | None = None  # "hospital" | "health_system" | "clinic" | ...
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    phone: str | None = None
    website: str | None = None
    website_confidence: Confidence | None = None  # trust in a discovered website
    source: str | None = None  # which adapter/dataset this came from (see `sources` table)
    source_id: str | None = None  # that source's own id for this org, if any

    # Read-only bookkeeping populated by the database layer on read.
    organization_id: int | None = None
    created_date: str | None = None
    updated_date: str | None = None


@dataclass
class HealthSystem:
    """A parent health system - Ballad Health, Ascension, Mercy, ... - as a
    first-class entity independent of any one facility (schema v7).

    Belongs to the Organization Identity Resolution layer (see ROADMAP.md's
    Phase 5.1 architecture notes): it answers "what system is this facility
    part of", never "what is this system's verified website". ``primary_domain``
    is an identity fact ("this system is publicly known by this domain"),
    the same pattern :class:`Organization`'s own ``website`` already uses -
    not a claim that's been through the reachability/candidate-ranking
    machinery Website Enrichment owns.
    """

    name: str | None = None
    normalized_name: str | None = None
    primary_domain: str | None = None

    # Read-only bookkeeping populated by the database layer on read.
    system_id: int | None = None
    created_at: str | None = None


@dataclass
class OrganizationAlias:
    """An alternate name for an :class:`Organization` - a rebrand, a
    historical name, or a Wikidata alias (schema v7).

    Purely an identity fact: no website concept anywhere on this dataclass.
    Handles the CMS-name-vs-current-name drift Phase 4 measured (e.g. "Wellmont
    Holston Valley Medical Center" vs. "Holston Valley Medical Center").
    """

    organization_id: int
    alias: str
    normalized_alias: str | None = None
    source: str | None = None  # e.g. "wikidata" | "manual"
    confidence: IdentityConfidence | None = None

    # Read-only bookkeeping populated by the database layer on read.
    id: int | None = None
    created_at: str | None = None


@dataclass
class OrganizationSystemMembership:
    """One period of ``Organization`` belonging to a ``HealthSystem``
    (schema v7).

    Deliberately a separate, historied table rather than a column on
    ``organizations`` - ownership changes over time (Phase 4 found exactly
    this pattern: an independent facility later acquired by a system).
    ``valid_to IS NULL`` marks the currently-active membership; an ownership
    change closes the old row (sets ``valid_to``) and opens a new one rather
    than overwriting history.
    """

    organization_id: int
    system_id: int
    source: str | None = None  # e.g. "manual_seed" | "cms_ownership"
    confidence: IdentityConfidence | None = None
    valid_from: str | None = None
    valid_to: str | None = None  # NULL/None = currently active

    # Read-only bookkeeping populated by the database layer on read.
    membership_id: int | None = None
    created_at: str | None = None


class ResolutionMethod(str, Enum):
    """How a :class:`WebsiteCandidate` was found - the ``resolution_method``
    Phase 5.1's architecture review called for, so a future investigator can
    answer "why did ProviderMap choose this website" without rerunning
    enrichment (schema v8).

    Only ``WIKIDATA``, ``HIFLD``, and ``GUESS`` are produced by any code
    today (Phase 5.3 wires candidate logging into the existing cascade,
    nothing more). The rest of this vocabulary is defined now - it's a
    stable, one-value-per-source-module set that shouldn't need widening
    later - but stays unused until the sources they name (Phase 5.4's VA
    resolver, Phase 5.5's search resolver, and identity-layer-driven
    matching) actually exist.
    """

    WIKIDATA = "wikidata"
    HIFLD = "hifld"
    GUESS = "guess"
    VA_API = "va_api"
    SEARCH_API = "search_api"
    SYSTEM_DOMAIN = "system_domain"
    ALIAS_MATCH = "alias_match"
    MANUAL = "manual"


@dataclass
class WebsiteCandidate:
    """One website URL considered for an :class:`Organization`, whether or
    not it was ultimately promoted (schema v8).

    Website Enrichment owns this table - it never becomes Identity
    Resolution's responsibility. ``system_membership_id`` is the one
    deliberately nullable, one-directional reference to the identity layer
    (see ROADMAP.md's Phase 5.1 architecture notes): a candidate may *cite*
    an :class:`OrganizationSystemMembership` row as its justification, but
    identity resolution has no awareness this table exists.

    Logging every candidate - not just the winner - is what makes a
    rejected HIFLD URL (matched, but failed the reachability check) an
    auditable row instead of a number that vanishes into a counter.
    """

    organization_id: int
    candidate_url: str
    resolution_method: ResolutionMethod | None = None
    reachable: bool | None = None  # None = not checked (e.g. a Wikidata match)
    confidence: Confidence | None = None
    system_membership_id: int | None = None
    promoted: bool = False

    # Read-only bookkeeping populated by the database layer on read.
    candidate_id: int | None = None
    created_at: str | None = None


@dataclass
class Location:
    """A unique care location, deduplicated by normalized address."""

    facility_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    phone: str | None = None
    address_key: str | None = None

    location_id: int | None = None
    first_seen: str | None = None
    last_seen: str | None = None


@dataclass
class ExcludedRecord:
    """A directory record that was deliberately kept out of ``providers``."""

    name: str
    url: str
    classification: str
    reason_excluded: str
    npi: str | None = None
    date_found: str | None = None
    last_seen: str | None = None


@dataclass
class RunRecord:
    """One row in the ``runs`` table - what a single invocation did."""

    run_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    mode: str = "full"  # full | incremental | test | dry-run | discover
    source_used: str | None = None
    urls_discovered: int = 0
    providers_new: int = 0
    providers_changed: int = 0
    providers_unchanged: int = 0
    excluded: int = 0
    errors: int = 0
    notes: str | None = None


@dataclass
class ProviderChange:
    """One field-level change recorded during an incremental refresh."""

    provider_id: int
    run_id: int
    field: str
    old_value: str | None
    new_value: str | None
    changed_at: str
    full_name: str | None = None  # joined in for display, not stored here
    npi: str | None = None


@dataclass
class PipelineTally:
    """Outcome counts from processing a batch of URLs."""

    new: int = 0
    changed: int = 0
    unchanged: int = 0
    excluded: int = 0
    error: int = 0

    def record(self, outcome: UpsertOutcome | str) -> None:
        key = outcome.value if isinstance(outcome, UpsertOutcome) else outcome
        if hasattr(self, key):
            setattr(self, key, getattr(self, key) + 1)

    def as_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)
