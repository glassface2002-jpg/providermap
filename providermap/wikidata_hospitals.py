"""Bulk hospital website lookup via Wikidata (ROADMAP.md stage 4 follow-on).

Unlike ``providermap/website_enrichment.py``'s domain-guessing (a heuristic
with no ground truth to check itself against), Wikidata's "official website"
property (P856) is community-verified data, not a guess. Given that, a match
found here is reported as ``Confidence.HIGH`` - the one place in this
project's organization pipeline that label is used. The residual risk isn't
"is this really the hospital's website" (Wikidata already asserts that) -
it's "does this Wikidata entity actually correspond to this database row",
which is why matching is still exact-normalized-name-only, the same
discipline ``providermap/organization_linking.py`` uses for provider ↔
organization linking.

One bulk SPARQL query fetches every US hospital Wikidata knows a website for
in a single request - both far more efficient and far more accurate than
looking one up per organization. Community data has its own noise (an
occasional P856 value is a deep link or a stale URL rather than a clean
homepage), which this module can't algorithmically detect - "verified by
Wikidata contributors" is a much stronger signal than a guessed domain, not
a guarantee.
"""

from __future__ import annotations

import httpx

from .parser_utils import normalize_org_name

_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Q16917 = hospital, Q30 = United States of America, P856 = official website.
# wdt:P279* (subclass-of, transitive) so specialty hospital subtypes are
# included, not just items typed exactly "hospital".
_QUERY = """
SELECT ?hospitalLabel ?website WHERE {
  ?hospital wdt:P31/wdt:P279* wd:Q16917 .
  ?hospital wdt:P17 wd:Q30 .
  ?hospital wdt:P856 ?website .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""


def _parse_sparql_response(data: dict[str, object]) -> dict[str, str]:
    """Pure parsing of the SPARQL JSON results format - the part worth
    testing offline. Returns ``{normalized_name: website_url}``; entries
    Wikidata is missing a name or website for are skipped, not raised on.
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


def fetch_wikidata_hospital_websites(
    user_agent: str, timeout_seconds: float = 60.0
) -> dict[str, str]:
    """One bulk query for every US hospital Wikidata has a website for.

    Returns ``{normalized_name: website_url}``. Returns an empty dict (never
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
        return {}
    return _parse_sparql_response(data)
