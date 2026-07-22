"""Bulk hospital website lookup via HIFLD's "Hospitals" dataset (ROADMAP.md
Phase 3 - measured coverage-worthy in Phase 2's live evaluation before this
was built, per this project's "measure before build" discipline).

Unlike Wikidata's ``P856`` (continuously community-verified - see
``wikidata_hospitals.py``), HIFLD (Homeland Infrastructure Foundation-Level
Data, a DHS/HHS-curated federal aggregation) is periodically refreshed and
its ``WEBSITE`` field is measurably stale: a live pull found 55.8% of
non-empty ``WEBSITE`` values were last validated (``VAL_DATE``) in 2013-2014,
over a decade ago, and at least one confirmed-dead URL
(``http://www.alaweb.com/ tgarske``, a personal-page-style link, not a
hospital site) survived in the current data. HIFLD data is therefore never
treated as verified on its own - every candidate is run through the same
live reachability check (``website_enrichment.http_domain_checker``) the
domain-guessing fallback already uses, and a match is recorded as
``Confidence.MEDIUM`` (matching that fallback's discipline), never HIGH.

Matching logic mirrors ``wikidata_hospitals.py`` exactly: normalized name (or
``ALT_NAME``) optionally disambiguated by US state, with any key that maps to
more than one distinct website excluded entirely rather than resolved
arbitrarily - collisions are real here too (a live pull found the same kind
of same-name-different-hospital ambiguity Wikidata has).

The current mirror queried is a live ArcGIS Online republish of HIFLD's
"Hospitals" feature layer (verified reachable and current as of this
writing; HIFLD's own primary site, hifld-geoplatform.hub.arcgis.com, is a
JS-rendered SPA with no stable direct-download endpoint to auto-fetch from,
the same practical reason ``cms_hospitals``'s adapter goes through CMS's
metastore API rather than scraping a landing page).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .parser_utils import normalize_org_name

_FEATURE_SERVER_QUERY_URL = (
    "https://services7.arcgis.com/JEwYeAy2cc8qOe3o/arcgis/rest/services/"
    "hifld_hospitals/FeatureServer/0/query"
)

_PAGE_SIZE = 2000

# HIFLD represents an unknown value as the literal string "NOT AVAILABLE" in
# several columns (the same convention CMS's own Hospital General
# Information dataset uses - see cms_hospitals/adapter.py's `_clean`) - a
# few other common placeholder spellings are guarded defensively too.
_PLACEHOLDERS = {"not available", "no website", "n/a", "na", "none", "-", ""}


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if v and v.lower() not in _PLACEHOLDERS else None


@dataclass(frozen=True)
class HifldHospitalIndex:
    """Lookup structure for HIFLD hospital websites - same shape and same
    ambiguity-exclusion discipline as :class:`~providermap.wikidata_hospitals.WikidataHospitalIndex`.

    ``by_name``: normalized hospital name-or-alt-name -> website, excluding
    any key that maps to more than one distinct website.

    ``by_name_state``: ``(normalized name-or-alt-name, USPS state
    abbreviation)`` -> website, disambiguating a name shared by hospitals in
    different states. Also excludes any ``(name, state)`` pair mapping to
    more than one distinct website.

    Every website value here has already passed placeholder-filtering
    (``_clean``) but has **not** been reachability-checked - callers must do
    that themselves before trusting/persisting a match (see
    ``cli.py::cmd_enrich_organizations``).
    """

    by_name: dict[str, str] = field(default_factory=dict)
    by_name_state: dict[tuple[str, str], str] = field(default_factory=dict)


def _build_hospital_index(rows: list[dict[str, object]]) -> HifldHospitalIndex:
    """Pure parsing/matching over already-fetched ArcGIS ``attributes``
    dicts - the part worth testing offline. Never raises: a row missing a
    usable name or website is simply skipped.
    """
    key_sites: dict[str, set[str]] = {}
    key_state_sites: dict[tuple[str, str], set[str]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        website = _clean(row.get("WEBSITE"))
        if not website:
            continue
        name = normalize_org_name(_clean(row.get("NAME")))
        alt = normalize_org_name(_clean(row.get("ALT_NAME")))
        state = _clean(row.get("STATE"))
        state = state.upper() if state else None

        for key in filter(None, {name, alt}):
            key_sites.setdefault(key, set()).add(website)
            if state:
                key_state_sites.setdefault((key, state), set()).add(website)

    by_name = {k: next(iter(v)) for k, v in key_sites.items() if len(v) == 1}
    by_name_state = {k: next(iter(v)) for k, v in key_state_sites.items() if len(v) == 1}
    return HifldHospitalIndex(by_name=by_name, by_name_state=by_name_state)


def fetch_hifld_hospital_index(
    user_agent: str, timeout_seconds: float = 60.0
) -> HifldHospitalIndex:
    """Fetch every row of HIFLD's Hospitals dataset (paginated - the ArcGIS
    REST API caps a single query at :data:`_PAGE_SIZE` records) and build a
    :class:`HifldHospitalIndex`.

    Returns an empty index (never raises) on any network or parse failure -
    same discipline as ``wikidata_hospitals.fetch_wikidata_hospital_websites``:
    HIFLD is an enrichment enhancement, not a dependency the pipeline should
    ever hard-fail on.
    """
    rows: list[dict[str, object]] = []
    offset = 0
    try:
        with httpx.Client(timeout=timeout_seconds, headers={"User-Agent": user_agent}) as client:
            while True:
                resp = client.get(
                    _FEATURE_SERVER_QUERY_URL,
                    params={
                        "where": "1=1",
                        "outFields": "NAME,ALT_NAME,STATE,WEBSITE",
                        "f": "json",
                        "resultOffset": offset,
                        "resultRecordCount": _PAGE_SIZE,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                features = data.get("features") if isinstance(data, dict) else None
                if not isinstance(features, list) or not features:
                    break
                rows.extend(
                    f["attributes"]
                    for f in features
                    if isinstance(f, dict) and isinstance(f.get("attributes"), dict)
                )
                if len(features) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
    except (httpx.HTTPError, ValueError):
        return HifldHospitalIndex()

    return _build_hospital_index(rows)
