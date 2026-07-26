"""Live capability probe harness for Cigna's FHIR Provider Directory (Phase
E, Stage 3 - see ``adapters/providers/cigna_fhir/evidence/PHASE_E_CAPABILITY_PROBES.md``).

Every test here reproduces one of the three live findings that drove the
national discovery design, and asserts the *documented* (sometimes quirky)
behavior still holds. This is a regression guard against Cigna's API
changing under us, not a correctness test of ProviderMap's own code - if one
of these ever fails, it means an assumption baked into
``NATIONAL_DISCOVERY_DESIGN.md`` or the adapter's URL builders needs
re-review, not that anything in this repo broke.

**Excluded from the default `pytest` run** (see ``pyproject.toml``'s
``addopts = "-m 'not live'"``) - these are real, read-only GETs against a
third-party production server, run deliberately, not on every test
invocation. Run explicitly:

    pytest -m live tests/test_cigna_fhir_live_capability_probes.py

No assertions here are about ProviderMap's own correctness (that's what the
rest of the suite, all offline, is for) - only about whether the external
server still behaves the way the design assumes.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.live

_BASE_URL = "https://fhir.cigna.com/ProviderDirectory/v1"
_HEADERS = {"User-Agent": "ProviderMapBot/1.0 (+contact: glassface2002@gmail.com)"}
_TIMEOUT = 45.0


def _get(url: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    return resp.status_code, resp.json()


def _resource_types(bundle: dict) -> dict[str, int]:  # type: ignore[type-arg]
    counts: dict[str, int] = {}
    for entry in bundle.get("entry", []) or []:
        rt = (entry.get("resource") or {}).get("resourceType")
        counts[rt] = counts.get(rt, 0) + 1
    return counts


class TestFinding1UnscopedPractitionerRoleReturnsCompleteGraph:
    """See PHASE_E_CAPABILITY_PROBES.md finding 1 - this is what makes the
    national strategy's spool stage correct without a follow-up fetch."""

    def test_all_three_includes_return_200_with_locations_present(self) -> None:
        url = (
            f"{_BASE_URL}/PractitionerRole?_count=5"
            f"&_include=PractitionerRole:practitioner"
            f"&_include=PractitionerRole:location"
            f"&_include=PractitionerRole:organization"
        )
        status, bundle = _get(url)
        assert status == 200
        counts = _resource_types(bundle)

        assert counts.get("PractitionerRole", 0) == 5, (
            "Cigna's PractitionerRole search no longer honors _count=5 for "
            "an unscoped, fully-included query - re-check _national_window_url."
        )
        # The whole point of finding 1: this combination genuinely resolves
        # Location resources, not just Practitioner/Organization.
        assert counts.get("Location", 0) > 0, (
            "Cigna's PractitionerRole search no longer includes Location "
            "resources for this _include combination - NationalDiscovery's "
            "core assumption (this query returns the complete graph) no "
            "longer holds and needs re-review."
        )


class TestFinding2LocationFilterPlusLocationIncludeIsSilentlyEmpty:
    """See PHASE_E_CAPABILITY_PROBES.md finding 2 - the server defect that
    blocks fixing state-scoped discovery's truncation by simply adding the
    missing include. If this test ever fails because Cigna *fixed* it, that
    would be good news worth acting on: state-scoped discovery could then
    stop dropping out-of-state locations (see adapter.py's
    _practitioner_role_by_locations_url docstring and Risk R1)."""

    @pytest.fixture()
    def ri_location_ids(self) -> list[str]:
        status, bundle = _get(f"{_BASE_URL}/Location?address-state=RI&_count=10")
        assert status == 200
        ids = [
            entry["resource"]["id"]
            for entry in bundle.get("entry", []) or []
            if entry.get("resource", {}).get("resourceType") == "Location"
        ]
        if not ids:
            pytest.skip("No RI locations returned right now - cannot exercise this probe")
        return ids

    def test_location_filter_without_location_include_returns_data(
        self, ri_location_ids: list[str]
    ) -> None:
        ids = ",".join(ri_location_ids)
        url = (
            f"{_BASE_URL}/PractitionerRole?location={ids}"
            f"&_include=PractitionerRole:practitioner"
            f"&_include=PractitionerRole:organization"
            f"&_count=50"
        )
        status, bundle = _get(url)
        assert status == 200
        # Not asserting non-empty here - whether these specific locations
        # currently have practitioners is a data fact, not an API contract.
        # The contrast with the next test is the actual finding.

    def test_adding_location_include_to_a_location_filtered_query_still_returns_empty(
        self, ri_location_ids: list[str]
    ) -> None:
        ids = ",".join(ri_location_ids)
        url = (
            f"{_BASE_URL}/PractitionerRole?location={ids}"
            f"&_include=PractitionerRole:practitioner"
            f"&_include=PractitionerRole:organization"
            f"&_include=PractitionerRole:location"
            f"&_count=50"
        )
        status, bundle = _get(url)
        assert status == 200
        counts = _resource_types(bundle)
        assert counts == {}, (
            "Cigna's server no longer silently empties a location=-filtered "
            "PractitionerRole search when a location _include is added - "
            "this was a documented server defect (finding 2), and its "
            "disappearance means state-scoped discovery's truncation "
            "(adapter.py's _practitioner_role_by_locations_url) could "
            "potentially be fixed directly. Re-review "
            "NATIONAL_DISCOVERY_DESIGN.md's Risk R1 if this ever fails."
        )


class TestFinding3LocationIdCommaListReturnsAtMostOne:
    """See PHASE_E_CAPABILITY_PROBES.md finding 3 - confirms
    Location?_id=<comma-list> still doesn't support an OR-list the way
    PractitionerRole.location does. Informational: no current code path
    relies on this (reference-completion by _id was ruled out, not built),
    but a change here would be worth knowing about for future work."""

    @pytest.fixture()
    def location_ids_from_a_real_role(self) -> list[str]:
        status, bundle = _get(
            f"{_BASE_URL}/PractitionerRole?_count=5&_include=PractitionerRole:location"
        )
        assert status == 200
        ids = [
            entry["resource"]["id"]
            for entry in bundle.get("entry", []) or []
            if entry.get("resource", {}).get("resourceType") == "Location"
        ]
        if len(ids) < 2:
            pytest.skip("Fewer than 2 Location ids available right now - cannot exercise this probe")
        return ids

    def test_single_id_resolves(self, location_ids_from_a_real_role: list[str]) -> None:
        status, bundle = _get(f"{_BASE_URL}/Location?_id={location_ids_from_a_real_role[0]}")
        assert status == 200
        assert _resource_types(bundle).get("Location", 0) == 1

    def test_comma_joined_ids_still_return_at_most_one(
        self, location_ids_from_a_real_role: list[str]
    ) -> None:
        ids = ",".join(location_ids_from_a_real_role[:3])
        status, bundle = _get(f"{_BASE_URL}/Location?_id={ids}&_count=50")
        assert status == 200
        returned = _resource_types(bundle).get("Location", 0)
        assert returned <= 1, (
            f"Cigna's Location?_id= now honors comma-joined OR-lists "
            f"(returned {returned} for 3 requested) - this was previously "
            f"truncated to at most 1 (finding 3). If this changed, batched "
            f"reference-completion for state-scoped discovery may now be "
            f"viable and worth reconsidering - see "
            f"NATIONAL_DISCOVERY_DESIGN.md's comparison table."
        )
