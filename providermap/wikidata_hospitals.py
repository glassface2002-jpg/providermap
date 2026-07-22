"""Bulk hospital website lookup via Wikidata (ROADMAP.md stage 4 follow-on).

Unlike ``providermap/website_enrichment.py``'s domain-guessing (a heuristic
with no ground truth to check itself against), Wikidata's "official website"
property (P856) is community-verified data, not a guess. Given that, a match
found here is reported as ``Confidence.HIGH`` - the one place in this
project's organization pipeline that label is used. The residual risk isn't
"is this really the hospital's website" (Wikidata already asserts that) -
it's "does this Wikidata entity actually correspond to this database row".

Matching against a CMS organization is by normalized name (primary label or
alias), optionally disambiguated by US state - see :class:`WikidataHospitalIndex`.
Exact-primary-label-only matching (the original design) measurably missed a
lot of real coverage: measured live against this project's actual 5,432-org
database, 0 of the 2,368 orgs with no website at all, and 0 of the 1,793
LOW/MEDIUM-confidence guessed ones, matched Wikidata's primary label (every
primary-label match had already been claimed by earlier enrichment runs).
Wikidata's ``skos:altLabel`` aliases (nicknames/abbreviations editors have
recorded, e.g. "MGH" for Massachusetts General Hospital) recover real
additional matches the primary label alone does not.

Aliases introduce a real, measured ambiguity risk though: of 4,170 distinct
normalized label/alias keys in a live pull, 184 (4.4%) name more than one
distinct real hospital with different websites (e.g. "Mercy Hospital" names
13 different actual hospitals; "Deaconess Hospital" names 4). A name-only
lookup that picked one of those arbitrarily would be a silent false positive.
This module handles that two ways: (1) any key mapping to more than one
distinct website across all of Wikidata's data is dropped from the plain
name index entirely - never guessed at - and (2) a second index keyed by
``(normalized_name, state)`` disambiguates same-named hospitals in different
states, since a CMS organization always carries its own ``state``. Measured
live: for every case in the real org database where a state-keyed match
existed, it agreed 100% with the (looser) name-only match - the two never
disagreed - which is why the name-only index is still kept as a fallback for
organizations whose state doesn't resolve to a Wikidata item, not dropped
entirely.

One bulk SPARQL query fetches every US hospital Wikidata knows a website for,
along with its aliases and administrative-territory chain, in a single
request - far more efficient than looking one up per organization.
Community data has its own noise (an occasional P856 value is a deep link or
a stale URL rather than a clean homepage), which this module can't
algorithmically detect - "verified by Wikidata contributors" is a much
stronger signal than a guessed domain, not a guarantee.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import httpx

from .parser_utils import normalize_org_name

_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Q16917 = hospital, Q30 = United States of America, P856 = official website,
# Q35657 = U.S. state. wdt:P279* (subclass-of, transitive) so specialty
# hospital subtypes are included, not just items typed exactly "hospital".
# altLabel is OPTIONAL and language-filtered to English aliases only. The
# state climb is also OPTIONAL and transitive (wdt:P131*) since many hospital
# items link directly to a city/county, not a state - climbing the
# administrative-territory chain until something is itself instance-of a
# U.S. state resolves through that hierarchy without a second query.
_QUERY = """
SELECT ?hospitalLabel ?altLabel ?stateLabel ?website WHERE {
  ?hospital wdt:P31/wdt:P279* wd:Q16917 .
  ?hospital wdt:P17 wd:Q30 .
  ?hospital wdt:P856 ?website .
  OPTIONAL { ?hospital skos:altLabel ?altLabel . FILTER(LANG(?altLabel) = "en") }
  OPTIONAL { ?hospital wdt:P131* ?state . ?state wdt:P31 wd:Q35657 . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

# Wikidata's label service returns a state's full English name (e.g.
# "Massachusetts"), but this project's Organization.state (from CMS) is the
# USPS two-letter abbreviation - this is the one place that conversion needs
# to happen. A couple of state labels come back disambiguated
# (e.g. "Georgia (U.S. state)", to distinguish from the country) - both forms
# are mapped defensively.
_STATE_ABBR: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Georgia (U.S. state)": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "Washington (state)": "WA", "West Virginia": "WV", "Wisconsin": "WI",
    "Wyoming": "WY", "District of Columbia": "DC", "Puerto Rico": "PR",
    "Guam": "GU", "American Samoa": "AS", "United States Virgin Islands": "VI",
    "Northern Mariana Islands": "MP",
}  # fmt: skip


@dataclass(frozen=True)
class WikidataHospitalIndex:
    """Lookup structure for Wikidata-verified hospital websites.

    ``by_name``: normalized hospital name-or-alias -> website, but *only*
    for keys that map to exactly one distinct website across all of
    Wikidata's US hospital data - an ambiguous key (see module docstring) is
    excluded here entirely rather than resolved arbitrarily.

    ``by_name_state``: ``(normalized name-or-alias, USPS state abbreviation)``
    -> website - a second, disambiguating index for hospitals whose plain
    name collides with another hospital in a different state. Also excludes
    any ``(name, state)`` pair that itself maps to more than one distinct
    website (a same-state, same-name collision - rare, but handled the same
    "never guess" way).

    A caller should check ``by_name_state`` first (when it knows the
    organization's state) and fall back to ``by_name`` - see
    ``cli.py::cmd_enrich_organizations``.
    """

    by_name: dict[str, str] = field(default_factory=dict)
    by_name_state: dict[tuple[str, str], str] = field(default_factory=dict)


def _parse_sparql_response(data: dict[str, object]) -> dict[str, str]:
    """Pure parsing of the SPARQL JSON results format - the part worth
    testing offline. Returns ``{normalized_name: website_url}`` from each
    row's primary ``hospitalLabel`` only (ignores ``altLabel``/``stateLabel``
    bindings if present - this is the simple, original-behavior parse still
    used for standalone testing); entries Wikidata is missing a name or
    website for are skipped, not raised on.
    """
    results: dict[str, str] = {}
    outer = data.get("results")
    bindings = outer.get("bindings", []) if isinstance(outer, dict) else []
    if not isinstance(bindings, list):
        return {}

    for row in bindings:
        if not isinstance(row, dict):
            continue
        label_binding = row.get("hospitalLabel")
        website_binding = row.get("website")
        label = label_binding.get("value") if isinstance(label_binding, dict) else None
        website = website_binding.get("value") if isinstance(website_binding, dict) else None
        key = normalize_org_name(label) if isinstance(label, str) else None
        if key and isinstance(website, str):
            results[key] = website
    return results


def _binding_value(row: dict[str, object], key: str) -> str | None:
    binding = row.get(key)
    value = binding.get("value") if isinstance(binding, dict) else None
    return value if isinstance(value, str) else None


def _build_hospital_index(data: dict[str, object]) -> WikidataHospitalIndex:
    """Full parse: primary label + aliases + state-disambiguation, with
    ambiguous keys excluded rather than resolved arbitrarily (see
    :class:`WikidataHospitalIndex`). Never raises - degrades to empty indexes
    on any malformed shape, same discipline as :func:`_parse_sparql_response`.
    """
    outer = data.get("results")
    bindings = outer.get("bindings", []) if isinstance(outer, dict) else []
    if not isinstance(bindings, list):
        return WikidataHospitalIndex()

    key_sites: dict[str, set[str]] = defaultdict(set)
    key_state_sites: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row in bindings:
        if not isinstance(row, dict):
            continue
        label = _binding_value(row, "hospitalLabel")
        alt = _binding_value(row, "altLabel")
        state_label = _binding_value(row, "stateLabel")
        website = _binding_value(row, "website")
        if not website:
            continue

        keys = {normalize_org_name(label) for label in (label, alt) if label}
        keys.discard(None)
        if not keys:
            continue

        abbr = _STATE_ABBR.get(state_label) if state_label else None
        for key in keys:
            key_sites[key].add(website)
            if abbr:
                key_state_sites[(key, abbr)].add(website)

    by_name = {k: next(iter(v)) for k, v in key_sites.items() if len(v) == 1}
    by_name_state = {k: next(iter(v)) for k, v in key_state_sites.items() if len(v) == 1}
    return WikidataHospitalIndex(by_name=by_name, by_name_state=by_name_state)


def fetch_wikidata_hospital_websites(
    user_agent: str, timeout_seconds: float = 60.0
) -> WikidataHospitalIndex:
    """One bulk query for every US hospital Wikidata has a website for.

    Returns a :class:`WikidataHospitalIndex`. Returns an empty index (never
    raises) on any network or parse failure - Wikidata is an enhancement to
    website discovery, not a dependency the pipeline should ever hard-fail
    on; a failed lookup here just means every organization falls through to
    the domain-guessing fallback instead.
    """
    try:
        resp = httpx.get(
            _SPARQL_ENDPOINT,
            params={"query": _QUERY, "format": "json"},
            headers={"User-Agent": user_agent, "Accept": "application/sparql-results+json"},
            timeout=timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return WikidataHospitalIndex()
    return _build_hospital_index(data)
