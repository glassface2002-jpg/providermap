"""Bulk hospital website lookup via the VA Facilities API (ROADMAP.md
Phase 5.4 - measured worth building in Phase 5.4.0's live validation before
this was written, per this project's "measure before build" discipline).

Phase 5.4.0 found, via three live test queries against the sandbox API,
that every VA Medical Center record carries a direct ``website`` attribute
- the complete, deep-linked official URL (e.g.
``https://www.va.gov/white-river-junction-health-care/locations/
white-river-junction-va-medical-center/``), not just a slug requiring
construction. All three sampled facilities matched exactly what a live
WebSearch spot-check had already found manually. Despite that direct field,
this module still treats a match as no more trustworthy on day one than
HIFLD's: every candidate is reachability-checked before being stored, and
recorded as ``Confidence.MEDIUM``, never HIGH - a federal API earns the same
trust HIFLD did, through the live check, not through being federal.

Matching logic mirrors ``hifld_hospitals.py``/``wikidata_hospitals.py``
exactly: normalized name, optionally disambiguated by US state, with any key
mapping to more than one distinct website excluded entirely rather than
resolved arbitrarily. Unlike those two sources, the VA API has no separate
alt-name field to merge in - just one name per facility.

The API groups many facility types under one endpoint (VA Medical Centers,
Vet Centers, national cemeteries, regional benefit offices, mobile clinics,
...) - only records where ``classification == "VA Medical Center (VAMC)"``
are hospital-equivalent and relevant here; everything else is filtered out
during parsing. Requires a free sandbox API key (see
``config.example.yaml``'s ``organizations.va_api_key`` - registration at
https://developer.va.gov/explore/api/va-facilities/sandbox-access); with no
key configured, this source is skipped entirely, same graceful-skip
contract as every other optional step in ``cmd_enrich_organizations``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .parser_utils import normalize_org_name
from .wikidata_hospitals import _STATE_ABBR

_PAGE_SIZE = 100
_VAMC_CLASSIFICATION = "VA Medical Center (VAMC)"

# Every USPS state/territory abbreviation the API might file a facility
# under - reusing the same values `_STATE_ABBR` already maps state *names*
# to, rather than duplicating a second 50-state list.
_ALL_STATE_CODES: tuple[str, ...] = tuple(sorted(set(_STATE_ABBR.values())))


@dataclass(frozen=True)
class VaFacilityIndex:
    """Lookup structure for VA Facilities API websites - same shape and same
    ambiguity-exclusion discipline as
    :class:`~providermap.hifld_hospitals.HifldHospitalIndex`.

    ``by_name``: normalized facility name -> website, excluding any key that
    maps to more than one distinct website.

    ``by_name_state``: ``(normalized name, USPS state abbreviation)`` ->
    website, disambiguating a name shared by facilities in different states.

    Every website value here has already passed basic cleaning but has
    **not** been reachability-checked - callers must do that themselves
    before trusting/persisting a match (see ``cli.py::cmd_enrich_organizations``).
    """

    by_name: dict[str, str] = field(default_factory=dict)
    by_name_state: dict[tuple[str, str], str] = field(default_factory=dict)


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v or None


def _build_va_index(rows: list[dict[str, object]]) -> VaFacilityIndex:
    """Pure parsing/matching over already-fetched API ``attributes`` dicts -
    the part worth testing offline. Never raises: a row missing a usable
    name/website, or not a VA Medical Center, is simply skipped.
    """
    key_sites: dict[str, set[str]] = {}
    key_state_sites: dict[tuple[str, str], set[str]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("classification") != _VAMC_CLASSIFICATION:
            continue
        website = _clean(row.get("website"))
        if not website:
            continue
        key = normalize_org_name(_clean(row.get("name")))
        if not key:
            continue

        address = row.get("address")
        physical = address.get("physical") if isinstance(address, dict) else None
        state = _clean(physical.get("state")) if isinstance(physical, dict) else None
        state = state.upper() if state else None

        key_sites.setdefault(key, set()).add(website)
        if state:
            key_state_sites.setdefault((key, state), set()).add(website)

    by_name = {k: next(iter(v)) for k, v in key_sites.items() if len(v) == 1}
    by_name_state = {k: next(iter(v)) for k, v in key_state_sites.items() if len(v) == 1}
    return VaFacilityIndex(by_name=by_name, by_name_state=by_name_state)


def fetch_va_facility_index(
    api_key: str | None, base_url: str, user_agent: str, timeout_seconds: float = 30.0
) -> VaFacilityIndex:
    """Fetch every VA Medical Center record, one state/territory at a time
    (the API has no single "all facilities" query), and build a
    :class:`VaFacilityIndex`.

    Returns an empty index immediately, with no network call, if
    ``api_key`` is falsy - this source is entirely optional (see
    ``config.example.yaml``). Returns an empty index (never raises) on any
    network or parse failure too - same discipline as
    ``hifld_hospitals.fetch_hifld_hospital_index``: a federal API is an
    enrichment enhancement, not a dependency the pipeline should ever
    hard-fail on.
    """
    if not api_key:
        return VaFacilityIndex()

    rows: list[dict[str, object]] = []
    try:
        with httpx.Client(
            timeout=timeout_seconds, headers={"apikey": api_key, "User-Agent": user_agent}
        ) as client:
            for state in _ALL_STATE_CODES:
                page = 1
                while True:
                    resp = client.get(
                        f"{base_url}/facilities",
                        params={"state": state, "page": page, "per_page": _PAGE_SIZE},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    facilities = data.get("data") if isinstance(data, dict) else None
                    if not isinstance(facilities, list):
                        break
                    rows.extend(
                        f["attributes"]
                        for f in facilities
                        if isinstance(f, dict) and isinstance(f.get("attributes"), dict)
                    )
                    meta = data.get("meta") if isinstance(data, dict) else None
                    pagination = meta.get("pagination") if isinstance(meta, dict) else None
                    total_pages = (
                        pagination.get("totalPages") if isinstance(pagination, dict) else None
                    )
                    if not isinstance(total_pages, int) or page >= total_pages:
                        break
                    page += 1
    except (httpx.HTTPError, ValueError):
        return VaFacilityIndex()

    return _build_va_index(rows)
