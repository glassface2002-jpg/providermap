"""Organization identity resolution (ROADMAP.md Phase 5.2).

Answers "what organization is this, really, and what is it part of" -
aliases and parent-health-system membership - kept deliberately separate
from website enrichment (``website_enrichment.py``, ``wikidata_hospitals.py``,
``hifld_hospitals.py``), which only ever *consumes* these facts (a nullable
``system_membership_id`` reference, once ``website_candidates`` ships in
Phase 5.3) and never owns them. Nothing in this module knows about URLs,
domains, reachability, or website confidence - see ROADMAP.md's Phase 5.1
architecture notes for the full boundary rationale and Phase 4's own
findings for why this layer exists at all (the residual missing-website
population turned out to be an entity-resolution problem, not a website
one: CMS's on-file name is frequently a facility whose real site lives on a
*parent system's* domain, e.g. "Wellmont Holston Valley Medical Center" ->
Ballad Health -> balladhealth.org).

This module intentionally does not import anything from
``website_enrichment.py`` (or vice versa) - the boundary test proposed in
Phase 5.1 is that identity-resolution code never depends on a URL/domain
concept.

Two backfill sources, both explicitly approved for Phase 5.2:

1. A small, hand-curated seed list of health systems named in Phase 4's own
   token-clustering finding (:data:`HEALTH_SYSTEM_SEEDS`), with
   ``primary_domain`` values live-verified during this phase's own research
   (mirroring the "verify before recommending" discipline used throughout
   this project) - not yet linked to any organization (see the module-level
   note in :func:`seed_health_systems` for why membership backfill is
   deliberately *not* attempted here).
2. Wikidata's alias data (``skos:altLabel``), grouped per-item rather than
   flattened - unlike ``wikidata_hospitals.py``'s website-focused index
   (which only needs "does this name resolve to a website", so it can
   safely flatten name -> URL), alias backfill needs "what are *all* the
   names for the *same* item", which requires grouping by the item's own
   URI. This module runs its own bulk SPARQL query for that reason, rather
   than reusing ``wikidata_hospitals.py``'s already-flattened index.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import httpx

from .database import Database
from .models import IdentityConfidence, Organization
from .parser_utils import normalize_org_name
from .wikidata_hospitals import _STATE_ABBR  # noqa: F401 - reused, not duplicated

_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Same hospital-typing/US-only shape as wikidata_hospitals.py's query,
# including its `wdt:P856 ?website` requirement - measured live: dropping
# that requirement (to reach every US hospital-typed item regardless of
# whether it has a recorded website) roughly doubles the candidate set and
# reliably 502s against Wikidata's query service, while the website-scoped
# version is the exact shape already proven to succeed in production. The
# website *value* itself isn't used here (alias backfill only needs the
# item's other names), but keeping the item scoped to "has a website"
# keeps this query's cost profile identical to the one already running
# successfully. `?hospital` itself is projected (unlike the website-focused
# query) so rows can be grouped by the actual entity, not approximated by
# label text alone.
_ALIAS_QUERY = """
SELECT ?hospital ?hospitalLabel ?altLabel ?stateLabel WHERE {
  ?hospital wdt:P31/wdt:P279* wd:Q16917 .
  ?hospital wdt:P17 wd:Q30 .
  ?hospital wdt:P856 ?website .
  OPTIONAL { ?hospital skos:altLabel ?altLabel . FILTER(LANG(?altLabel) = "en") }
  OPTIONAL { ?hospital wdt:P131* ?state . ?state wdt:P31 wd:Q35657 . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

# Manually curated from Phase 4's health-system name-token clustering
# (ROADMAP.md). Domains were live-verified via web search during Phase 5.2's
# own implementation, the same "verify before recommending" discipline used
# throughout this project - not an unverified guess sitting in the identity
# layer (see the Phase 5.1 architecture notes on `health_systems.primary_domain`).
# This is a bootstrap, not comprehensive coverage - expected to grow
# incrementally, not all at once.
HEALTH_SYSTEM_SEEDS: tuple[tuple[str, str], ...] = (
    ("Ascension", "ascension.org"),
    ("Mercy", "mercy.net"),
    ("Baylor Scott & White Health", "bswhealth.com"),
    ("Ballad Health", "balladhealth.org"),
    ("Brown University Health", "brownhealth.org"),
    ("TGH North", "tghnorth.org"),
)


def seed_health_systems(db: Database) -> int:
    """Seed :data:`HEALTH_SYSTEM_SEEDS` into ``health_systems``. Idempotent -
    see :meth:`Database.get_or_create_health_system`. Returns the number of
    seed rows processed (not necessarily newly created - re-running this is
    always safe).

    Deliberately does **not** attempt to link any organization to these
    systems via name-token matching (``organization_system_membership``
    stays unpopulated by this function). A literal substring match (e.g. any
    organization whose name contains "Mercy") is exactly the kind of guess
    Phase 5.1's ambiguity-preservation rule forbids: `wikidata_hospitals.py`'s
    own module docstring measured "Mercy Hospital" as a name naming 13
    distinct real hospitals, so token-matching "Mercy" against CMS names
    would silently produce false positives at real, measured scale. System
    *membership* backfill needs disambiguating signals (state, address, an
    authoritative ownership record) that this phase's scope explicitly
    excludes as "resolver logic" - it is deferred, not skipped.
    """
    for name, domain in HEALTH_SYSTEM_SEEDS:
        normalized = normalize_org_name(name)
        assert normalized is not None  # every seed name is a non-empty literal
        db.get_or_create_health_system(name=name, normalized_name=normalized, primary_domain=domain)
    return len(HEALTH_SYSTEM_SEEDS)


@dataclass(frozen=True)
class WikidataAliasIndex:
    """Lookup structure for Wikidata's per-item name sets.

    ``by_name``/``by_name_state`` map a normalized name-or-alias to an index
    into ``item_names`` - but *only* for keys that resolve to exactly one
    Wikidata item, mirroring :class:`~providermap.wikidata_hospitals.WikidataHospitalIndex`'s
    "never guess" discipline: a key naming more than one distinct item is
    excluded from both structures entirely rather than resolved arbitrarily.

    ``item_names`` holds, per item, every name recorded for that item as
    ``{normalized_name: original_text}`` (primary label + every English
    altLabel) - this is what makes alias backfill possible: once an
    organization is matched to an item via one name, every *other* name in
    that item's set is a candidate alias for it. Keeping the original text
    (not just the normalized key) is what lets a stored
    :class:`~providermap.models.OrganizationAlias` show a readable alias
    rather than an all-lowercase normalized string.
    """

    by_name: dict[str, int] = field(default_factory=dict)
    by_name_state: dict[tuple[str, str], int] = field(default_factory=dict)
    item_names: tuple[dict[str, str], ...] = ()


def _binding_value(row: dict[str, object], key: str) -> str | None:
    binding = row.get(key)
    value = binding.get("value") if isinstance(binding, dict) else None
    return value if isinstance(value, str) else None


def _build_alias_index(data: dict[str, object]) -> WikidataAliasIndex:
    """Pure parsing/grouping - the part worth testing offline. Groups rows by
    the ``?hospital`` entity URI (not by label text), then applies the same
    ambiguous-key exclusion already proven in ``wikidata_hospitals.py`` and
    ``hifld_hospitals.py``. Never raises - degrades to an empty index on any
    malformed shape, same discipline as those modules.
    """
    outer = data.get("results")
    bindings = outer.get("bindings", []) if isinstance(outer, dict) else []
    if not isinstance(bindings, list):
        return WikidataAliasIndex()

    # item URI -> ({normalized_name: original_text}, set of resolved state abbreviations)
    item_data: dict[str, tuple[dict[str, str], set[str]]] = {}
    item_order: list[str] = []

    for row in bindings:
        if not isinstance(row, dict):
            continue
        item = _binding_value(row, "hospital")
        label = _binding_value(row, "hospitalLabel")
        alt = _binding_value(row, "altLabel")
        state_label = _binding_value(row, "stateLabel")
        if not item:
            continue

        if item not in item_data:
            item_data[item] = ({}, set())
            item_order.append(item)
        names, states = item_data[item]

        for raw_name in (label, alt):
            if raw_name:
                key = normalize_org_name(raw_name)
                if key:
                    names.setdefault(key, raw_name)

        abbr = _STATE_ABBR.get(state_label) if state_label else None
        if abbr:
            states.add(abbr)

    item_names: list[dict[str, str]] = []
    item_state: list[str | None] = []
    for item in item_order:
        names, states = item_data[item]
        if not names:
            continue
        item_names.append(names)
        # Only trust a single, unambiguously-resolved state for this item -
        # P131*'s transitive climb can occasionally resolve more than one
        # state for one item; treating that as "unknown" rather than picking
        # one is the same "never guess" discipline as everything else here.
        item_state.append(next(iter(states)) if len(states) == 1 else None)

    key_items: dict[str, set[int]] = defaultdict(set)
    key_state_items: dict[tuple[str, str], set[int]] = defaultdict(set)
    for idx, names in enumerate(item_names):
        state = item_state[idx]
        for key in names:
            key_items[key].add(idx)
            if state:
                key_state_items[(key, state)].add(idx)

    by_name = {k: next(iter(v)) for k, v in key_items.items() if len(v) == 1}
    by_name_state = {k: next(iter(v)) for k, v in key_state_items.items() if len(v) == 1}
    return WikidataAliasIndex(
        by_name=by_name, by_name_state=by_name_state, item_names=tuple(item_names)
    )


def fetch_wikidata_alias_index(
    user_agent: str, timeout_seconds: float = 60.0
) -> WikidataAliasIndex:
    """One bulk query for every US hospital Wikidata has a name for, grouped
    per-item for alias backfill (see module docstring for why this is a
    separate query from ``wikidata_hospitals.py``'s). Returns an empty index
    (never raises) on any network or parse failure - alias backfill is a
    best-effort enhancement, not a hard dependency.
    """
    try:
        resp = httpx.get(
            _SPARQL_ENDPOINT,
            params={"query": _ALIAS_QUERY, "format": "json"},
            headers={"User-Agent": user_agent, "Accept": "application/sparql-results+json"},
            timeout=timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return WikidataAliasIndex()
    return _build_alias_index(data)


def backfill_organization_aliases(
    db: Database, index: WikidataAliasIndex, limit: int | None = None
) -> dict[str, int]:
    """Match every organization in ``db`` against ``index`` by normalized
    name (+ state, when available), and record every *other* name in the
    matched item's name set as an :class:`~providermap.models.OrganizationAlias`.

    An organization whose own name doesn't resolve to any item, or resolves
    ambiguously (excluded from ``index`` already, per its own "never guess"
    discipline), is simply skipped - never guessed at. Idempotent via
    :meth:`Database.add_organization_alias`.

    Returns counts: ``organizations_checked``, ``organizations_matched``,
    ``aliases_added`` - the measurement report this phase's verification
    discipline calls for.
    """
    orgs_checked = 0
    orgs_matched = 0
    aliases_added = 0

    orgs = list(db.organizations_by_normalized_name().values())
    if limit is not None:
        orgs = orgs[:limit]

    for org in orgs:
        orgs_checked += 1
        key = org.normalized_name
        if not key or org.organization_id is None:
            continue

        item_idx = None
        if org.state:
            item_idx = index.by_name_state.get((key, org.state))
        if item_idx is None:
            item_idx = index.by_name.get(key)
        if item_idx is None:
            continue

        orgs_matched += 1
        other_names = {norm: raw for norm, raw in index.item_names[item_idx].items() if norm != key}
        for normalized_alias, raw_alias in other_names.items():
            db.add_organization_alias(
                organization_id=org.organization_id,
                alias=raw_alias,
                normalized_alias=normalized_alias,
                source="wikidata",
                confidence=IdentityConfidence.HIGH,
            )
            aliases_added += 1

    return {
        "organizations_checked": orgs_checked,
        "organizations_matched": orgs_matched,
        "aliases_added": aliases_added,
    }


def get_candidate_keys(org: Organization, db: Database) -> list[str]:
    """Every normalized name key worth trying when matching ``org`` against
    a bulk source's index (Wikidata, HIFLD, VA, or any future source),
    in priority order.

    This is the single place cross-source matching's key-generation policy
    lives (ROADMAP.md Phase 5.5) - a caller never hardcodes "own name plus
    which alias tier" itself; it just iterates whatever this returns. Today
    that policy is "the organization's own normalized name, then its
    IdentityConfidence.HIGH aliases" (currently populated only by
    :func:`backfill_organization_aliases`'s Wikidata pass - see
    ``organization_aliases``), each state-then-name lookup stopping at the
    first hit. MEDIUM/LOW-confidence aliases are deliberately excluded for
    now: a lower-trust alias is a plausible source of a wrong cross-match in
    some other source's index, and broadening this is a separate, measured
    decision, not a default. Widening the confidence gate, adding a second
    alias source, or reordering keys are all future changes that belong
    here, not in any caller.

    Own name is always first (never displaced by an alias) and never
    duplicated if an alias happens to equal it or another alias verbatim.
    Returns just the organization's own key (or an empty list, if the
    organization has no derivable name at all) when it has no HIGH-confidence
    aliases - callers see no behavior difference from before this function
    existed in that case.
    """
    keys: list[str] = []
    own_key = org.normalized_name or normalize_org_name(org.name)
    if own_key:
        keys.append(own_key)

    if org.organization_id is not None:
        for alias in db.get_aliases_for_organization(org.organization_id):
            if (
                alias.confidence == IdentityConfidence.HIGH
                and alias.normalized_alias
                and alias.normalized_alias not in keys
            ):
                keys.append(alias.normalized_alias)

    return keys
