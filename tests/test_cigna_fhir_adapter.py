"""Tests for the Cigna FHIR provider dataset adapter - the first concrete
:class:`~adapters.providers.base_dataset.ProviderDatasetAdapter`.

Every NPI referenced here (``JANE_DOE_NPI``, ``ORG_1_NPI``,
``MALFORMED_PAGE_NPI``) has a valid CMS Luhn check digit - see
``adapters/providers/cigna_fhir/fixtures.py``'s module docstring.
"""

from __future__ import annotations

import asyncio
import gc
import weakref
from typing import Any

import httpx
import pytest

import adapters.providers.cigna_fhir.adapter as cigna_adapter_module
from adapters.providers.base_dataset import NormalizedProvider
from adapters.providers.cigna_fhir.adapter import (
    CignaFhirAdapter,
    _location_name_is_generated_address,
    _location_search_url,
    _practitioner_role_by_location_url,
    _unscoped_search_url,
)
from adapters.providers.cigna_fhir.fixtures import (
    JANE_DOE_NPI,
    MALFORMED_PAGE,
    MISSING_NPI_PAGE,
    ORG_1_NPI,
    PAGE_1,
    PAGE_2,
    PAGE_2_URL,
    STATE_LOCATION_BUNDLE,
    STATE_ROLE_BUNDLE,
    STATE_SCOPED_PRACTITIONER_NPI,
    make_fetcher,
)
from providermap.checkpoint import CheckpointStore
from providermap.config import Config
from providermap.models import Location, Organization, Provider


def _adapter_with_pages(config: Config, pages: dict) -> CignaFhirAdapter:  # type: ignore[type-arg]
    return CignaFhirAdapter(config, page_fetcher=make_fetcher(pages))


# A location with no PractitionerRoles - what the live server returns for
# the majority of enumerated Locations. Needed now that discovery issues one
# request per location rather than one per 50.
_EMPTY_BUNDLE: dict = {"resourceType": "Bundle", "type": "searchset", "link": [], "entry": []}


class TestRequiresProfileUrlCapability:
    """A provider *dataset* source has no profile page at all - this
    capability flag (not an adapter-name conditional in the validator) is
    how ``cmd_ingest_providers`` knows to build a policy that doesn't
    require one. See ``providermap.models.NpiPolicy.require_profile_url``."""

    def test_cigna_fhir_does_not_require_profile_url(self, config: Config) -> None:
        adapter = CignaFhirAdapter(config)
        assert adapter.requires_profile_url is False

    def test_base_class_default_is_false(self) -> None:
        from adapters.providers.base_dataset import ProviderDatasetAdapter

        assert ProviderDatasetAdapter.requires_profile_url is False


@pytest.fixture()
def initial_url(config: Config) -> str:
    # config.example.yaml's cigna_fhir_state defaults to null - all fixtures
    # in this file exercise the unscoped discovery path. The state-scoped
    # (Location -> PractitionerRole) path is exercised live in the pilot,
    # not offline, since it depends on real Location ids from Cigna.
    pc = config.providers
    return _unscoped_search_url(pc.cigna_fhir_base_url, pc.cigna_fhir_page_size)


class TestDiscoverPagination:
    """`discover()` must not trust FHIR page boundaries: Jane Doe's two
    PractitionerRoles, and the Location/Organization each references, are
    split across PAGE_1/PAGE_2 on purpose (see fixtures.py)."""

    def test_follows_next_link_and_aggregates_across_pages(
        self, config: Config, initial_url: str
    ) -> None:
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        records = list(adapter.discover())
        assert len(records) == 1
        record = records[0]
        assert record["practitioner"]["id"] == "practitioner-jane-doe"
        assert {r["id"] for r in record["roles"]} == {"role-1", "role-2"}
        assert set(record["locations"]) == {"location-1", "location-2"}
        assert set(record["organizations"]) == {"org-1", "org-2"}

    def test_tracks_request_count(self, config: Config, initial_url: str) -> None:
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        list(adapter.discover())
        assert adapter.last_request_count == 2

    def test_no_duplicate_practitioners_across_pages(
        self, config: Config, initial_url: str
    ) -> None:
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        records = list(adapter.discover())
        ids = [r["practitioner"]["id"] for r in records]
        assert len(ids) == len(set(ids))


class TestOneLocationPerRequest:
    """Regression guard for a silent data-loss defect.

    The state-scoped flow used to comma-join up to ``page_size`` (50)
    location ids into a single ``PractitionerRole?location=`` request, on
    the documented assumption that a comma-joined list acts as a FHIR OR
    filter. Measured live, this server honors only ONE id from such a list
    and silently discards the rest, returning HTTP 200 with a
    normal-looking bundle - so ~49 of every 50 locations were never really
    queried, and no counter, log line, or error surfaced it.

    These tests pin the corrected contract at the two places it can break:
    the URL builder must never emit a multi-id filter, and discovery must
    issue one request per location. See
    ``_practitioner_role_by_location_url``'s docstring for the measurement.
    """

    def test_url_builder_emits_exactly_one_location_id(self, config: Config) -> None:
        url = _practitioner_role_by_location_url(config.providers.cigna_fhir_base_url, "loc-a", 50)
        assert "location=loc-a&" in url or url.endswith("location=loc-a")
        assert "," not in url.split("location=")[1].split("&")[0], (
            "the location filter must carry exactly one id - a comma-joined "
            "list silently drops all but one on this server"
        )

    def test_discovery_issues_one_request_per_location_not_one_batch(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        requested: list[str] = []

        def tracking_fetch(url: str) -> dict:  # type: ignore[type-arg]
            requested.append(url)
            return {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            }[url]

        adapter = CignaFhirAdapter(config, page_fetcher=tracking_fetch)
        list(adapter.discover())

        role_requests = [u for u in requested if "PractitionerRole" in u]
        assert len(role_requests) == 2, (
            f"expected one PractitionerRole request per enumerated location "
            f"(2 locations -> 2 requests), got {len(role_requests)}: {role_requests}"
        )
        # Both locations must actually be asked about - the defect's
        # signature was that only the first id of a batch was ever honored.
        assert any("location=loc-a" in u for u in role_requests)
        assert any("location=loc-b" in u for u in role_requests)


class TestDiscoverStateScoped:
    """Cigna's real server has no state/geography search parameter on
    ``PractitionerRole`` (confirmed live) - one-state scoping is a two-step
    flow: ``Location?address-state=`` first, then one
    ``PractitionerRole?location=`` request PER location id. See
    ``_location_search_url`` /
    ``_practitioner_role_by_location_url``'s docstrings for the live
    findings that drove this design."""

    def test_two_step_flow_scopes_by_state(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        # One request PER location now (see
        # _practitioner_role_by_location_url): loc-a has the practitioner,
        # loc-b legitimately has none - mirroring what the live server
        # returns for a location with no roles.
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        adapter = _adapter_with_pages(
            config,
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            },
        )
        records = list(adapter.discover())
        assert len(records) == 1
        assert records[0]["practitioner"]["id"] == "practitioner-ri"
        # The Location resources came from the state search response, not a
        # separate _include - resolved via the shared resource cache.
        assert records[0]["locations"]["loc-a"]["name"] == "Providence Family Clinic"

    def test_request_count_covers_both_steps(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        # One request PER location now (see
        # _practitioner_role_by_location_url): loc-a has the practitioner,
        # loc-b legitimately has none - mirroring what the live server
        # returns for a location with no roles.
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        adapter = _adapter_with_pages(
            config,
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            },
        )
        list(adapter.discover())
        assert (
            adapter.last_request_count == 3
        )  # one Location page + one PractitionerRole query per location

    def test_extract_and_normalize_state_scoped_record(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        # One request PER location now (see
        # _practitioner_role_by_location_url): loc-a has the practitioner,
        # loc-b legitimately has none - mirroring what the live server
        # returns for a location with no roles.
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        adapter = _adapter_with_pages(
            config,
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            },
        )
        raw = next(adapter.discover())
        normalized = adapter.normalize(adapter.extract(raw))
        assert normalized.provider.npi == STATE_SCOPED_PRACTITIONER_NPI
        assert normalized.provider.state == "RI"
        assert normalized.provider.city == "Providence"
        # primary_site_of_care mirrors practice_name (same primary-location/
        # organization selection) - the exporter's "Primary Site of Care"
        # column was silently blank for every Cigna-sourced provider before
        # this was wired up; both must carry the identical, non-null value.
        assert normalized.provider.practice_name is not None
        assert normalized.provider.primary_site_of_care == normalized.provider.practice_name


class TestDiscoveryStats:
    """Phase B profiling instrumentation - pure visibility, no behavior
    change. See ``adapters/providers/cigna_fhir/adapter.py::DiscoveryStats``.
    """

    def test_tracks_requests_by_endpoint_for_unscoped_flow(
        self, config: Config, initial_url: str
    ) -> None:
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert stats.total_requests == 2
        assert stats.requests_by_endpoint == {"PractitionerRole": 2}
        assert stats.continuation_requests == 1  # PAGE_2 followed PAGE_1's next link

    def test_tracks_requests_by_endpoint_for_state_scoped_flow(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        # One request PER location now (see
        # _practitioner_role_by_location_url): loc-a has the practitioner,
        # loc-b legitimately has none - mirroring what the live server
        # returns for a location with no roles.
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        adapter = _adapter_with_pages(
            config,
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            },
        )
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        # 2 PractitionerRole requests = one per enumerated location (loc-a,
        # loc-b), not one batch covering both - see
        # _practitioner_role_by_location_url.
        assert stats.requests_by_endpoint == {"Location": 1, "PractitionerRole": 2}
        assert stats.continuation_requests == 0

    def test_resource_counts_with_no_duplicates_in_pagination_fixture(
        self, config: Config, initial_url: str
    ) -> None:
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        # 1 Practitioner + 2 PractitionerRole + 2 Location + 2 Organization = 7
        assert sum(stats.resources_seen_by_type.values()) == 7
        assert stats.duplicate_resources_by_type == {}

    def test_reference_hits_and_misses(self, config: Config, initial_url: str) -> None:
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        # 1 practitioner hit + 2 location hits + 2 organization hits = 5, no misses
        assert stats.reference_hits == 5
        assert stats.reference_misses == 0

    def test_reference_miss_when_role_points_at_never_returned_resource(
        self, config: Config, initial_url: str
    ) -> None:
        adapter = _adapter_with_pages(config, {initial_url: MISSING_NPI_PAGE})
        # role-no-npi has no location/organization at all, so the only
        # reference resolved is the practitioner itself - a hit.
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert stats.reference_hits == 1
        assert stats.reference_misses == 0

    def test_providers_yielded_and_runtime_recorded(self, config: Config, initial_url: str) -> None:
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert stats.providers_yielded == 1
        assert stats.runtime_seconds >= 0
        assert stats.average_latency_seconds is not None
        assert len(stats.request_latencies_seconds) == 2

    def test_duplicate_resource_across_batches_is_counted(self, config: Config) -> None:
        """Two independent PractitionerRole batches both re-including the
        same Organization (shared across providers in different location
        batches) - pure network overhead, no new information the second
        time."""
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", 1)
        role_url_a = _practitioner_role_by_location_url(pc.cigna_fhir_base_url, "loc-a", 1)
        role_url_b = _practitioner_role_by_location_url(pc.cigna_fhir_base_url, "loc-b", 1)

        shared_org = {"resourceType": "Organization", "id": "org-shared", "name": "Shared Org"}
        batch_a = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": [
                {
                    "resource": {
                        "resourceType": "Practitioner",
                        "id": "p-a",
                        "identifier": [
                            {"system": "http://hl7.org/fhir/sid/us-npi", "value": JANE_DOE_NPI}
                        ],
                        "name": [{"family": "A", "given": ["Prac"]}],
                    }
                },
                {
                    "resource": {
                        "resourceType": "PractitionerRole",
                        "id": "role-a",
                        "practitioner": {"reference": "Practitioner/p-a"},
                        "organization": {"reference": "Organization/org-shared"},
                    }
                },
                {"resource": shared_org},
            ],
        }
        batch_b = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": [
                {
                    "resource": {
                        "resourceType": "Practitioner",
                        "id": "p-b",
                        "identifier": [
                            {
                                "system": "http://hl7.org/fhir/sid/us-npi",
                                "value": STATE_SCOPED_PRACTITIONER_NPI,
                            }
                        ],
                        "name": [{"family": "B", "given": ["Prac"]}],
                    }
                },
                {
                    "resource": {
                        "resourceType": "PractitionerRole",
                        "id": "role-b",
                        "practitioner": {"reference": "Practitioner/p-b"},
                        "organization": {"reference": "Organization/org-shared"},
                    }
                },
                {"resource": shared_org},  # same org, returned again
            ],
        }

        adapter = _adapter_with_pages(
            config,
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: batch_a,
                role_url_b: batch_b,
            },
        )
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert stats.resources_seen_by_type.get("Organization") == 2
        assert stats.duplicate_resources_by_type.get("Organization") == 1
        assert stats.distinct_resources_by_type["Organization"] == 1


class TestDiscoverMalformedBundles:
    def test_skips_malformed_entries_without_raising(
        self, config: Config, initial_url: str
    ) -> None:
        adapter = _adapter_with_pages(config, {initial_url: MALFORMED_PAGE})
        records = list(adapter.discover())
        assert len(records) == 1
        assert records[0]["practitioner"]["id"] == "practitioner-ok"


class TestExtractNpi:
    def test_extracts_npi_from_us_npi_identifier(self, config: Config, initial_url: str) -> None:
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        raw = next(adapter.discover())
        assert adapter.extract(raw)["npi"] == JANE_DOE_NPI

    def test_missing_npi_handled_gracefully(self, config: Config, initial_url: str) -> None:
        adapter = _adapter_with_pages(config, {initial_url: MISSING_NPI_PAGE})
        raw = next(adapter.discover())
        # extract() never raises on a missing NPI - downstream validation
        # (validator.validate_provider + NpiPolicy.require_npi) decides
        # whether to exclude the record, not this method.
        assert adapter.extract(raw)["npi"] is None


class TestExtractFields:
    @pytest.fixture()
    def extracted(self, config: Config, initial_url: str) -> dict:  # type: ignore[type-arg]
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        raw = next(adapter.discover())
        return adapter.extract(raw)

    def test_name_mapping(self, extracted: dict) -> None:  # type: ignore[type-arg]
        assert extracted["first_name"] == "Jane"
        assert extracted["last_name"] == "Doe"
        assert extracted["full_name"] == "Jane Doe"

    def test_credentials(self, extracted: dict) -> None:  # type: ignore[type-arg]
        assert extracted["credentials"] == "MD"

    def test_languages(self, extracted: dict) -> None:  # type: ignore[type-arg]
        assert extracted["languages"] == ["Spanish"]

    def test_specialty_and_taxonomy_per_role(self, extracted: dict) -> None:  # type: ignore[type-arg]
        by_code = {r["taxonomy_code"]: r["taxonomy_desc"] for r in extracted["roles"]}
        assert by_code == {"207Q00000X": "Family Medicine", "207R00000X": "Internal Medicine"}

    def test_accepting_new_patients_per_role(self, extracted: dict) -> None:  # type: ignore[type-arg]
        by_taxonomy = {r["taxonomy_code"]: r["accepting_new_patients"] for r in extracted["roles"]}
        assert by_taxonomy["207Q00000X"] is True
        assert by_taxonomy["207R00000X"] is False

    def test_multi_role_aggregation(self, extracted: dict) -> None:  # type: ignore[type-arg]
        assert len(extracted["roles"]) == 2

    def test_multi_location_aggregation(self, extracted: dict) -> None:  # type: ignore[type-arg]
        assert set(extracted["locations"]) == {"location-1", "location-2"}
        loc1 = extracted["locations"]["location-1"]
        assert loc1["city"] == "Hartford"
        assert loc1["state"] == "CT"
        assert loc1["zip"] == "06103"
        assert loc1["phone"] == "860-555-0100"

    def test_organization_extraction_including_missing_npi_org(
        self, extracted: dict  # type: ignore[type-arg]
    ) -> None:
        orgs = extracted["organizations"]
        assert orgs["org-1"]["npi"] == ORG_1_NPI
        assert orgs["org-2"]["npi"] is None  # org-2 carries no identifier at all
        assert orgs["org-2"]["name"] == "Hartford Internal Medicine Annex LLC"


class TestNormalize:
    @pytest.fixture()
    def normalized(self, config: Config, initial_url: str) -> NormalizedProvider:
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        raw = next(adapter.discover())
        return adapter.normalize(adapter.extract(raw))

    def test_returns_normalized_provider(self, normalized: NormalizedProvider) -> None:
        assert isinstance(normalized, NormalizedProvider)
        assert isinstance(normalized.provider, Provider)

    def test_one_provider_per_npi_with_primary_location_populated(
        self, normalized: NormalizedProvider
    ) -> None:
        provider = normalized.provider
        assert provider.npi == JANE_DOE_NPI
        assert provider.npi_valid is True
        assert provider.location_count == 2
        # Both roles have exactly one location each (a tie) - the
        # lexicographically smallest location id ("location-1") wins
        # deterministically, independent of fetch/page order.
        assert provider.address == "100 Main St"
        assert provider.city == "Hartford"
        assert provider.specialty == "Family Medicine"

    def test_all_locations_present_not_just_primary(self, normalized: NormalizedProvider) -> None:
        assert len(normalized.locations) == 2
        assert all(isinstance(loc, Location) for loc in normalized.locations)
        addresses = {loc.address for loc in normalized.locations}
        assert addresses == {"100 Main St", "200 Elm St"}
        # Primary location is sorted first.
        assert normalized.locations[0].address == "100 Main St"

    def test_all_organizations_present_with_relationship_label(
        self, normalized: NormalizedProvider
    ) -> None:
        assert len(normalized.organizations) == 2
        by_name = {org.name: (org, rel) for org, rel in normalized.organizations}
        org1, rel1 = by_name["Hartford Family Health Group"]
        assert isinstance(org1, Organization)
        assert org1.source_id == ORG_1_NPI
        assert rel1 == "affiliated"
        # Missing-organization-NPI fallback: dedupe key falls back to the
        # FHIR resource id when the organization carries no NPI.
        org2, _rel2 = by_name["Hartford Internal Medicine Annex LLC"]
        assert org2.source_id == "org-2"

    def test_organizations_tagged_with_source(self, normalized: NormalizedProvider) -> None:
        for org, _rel in normalized.organizations:
            assert org.source == "Cigna"


def _determinism_raw(roles: list[tuple[str, list[str]]]) -> dict:  # type: ignore[type-arg]
    """Build a raw discovery record for primary-location determinism tests:
    ``roles`` is [(role_id, [location_ids])]. Every referenced location gets
    a distinct address so the selected primary is observable through
    ``provider.address``."""
    loc_ids = sorted({lid for _rid, lids in roles for lid in lids})
    return {
        "practitioner": {
            "resourceType": "Practitioner",
            "id": "p-det",
            "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": JANE_DOE_NPI}],
            "name": [{"family": "Doe", "given": ["Jane"]}],
        },
        "roles": [
            {
                "resourceType": "PractitionerRole",
                "id": rid,
                "practitioner": {"reference": "Practitioner/p-det"},
                "location": [{"reference": f"Location/{lid}"} for lid in lids],
                "specialty": [],
            }
            for rid, lids in roles
        ],
        "locations": {
            lid: {
                "resourceType": "Location",
                "id": lid,
                "name": f"Clinic {lid}",
                "address": {
                    "line": [f"{i + 1}00 {lid} St"],
                    "city": "Providence",
                    "state": "RI",
                    "postalCode": "02903",
                },
            }
            for i, lid in enumerate(loc_ids)
        },
        "organizations": {},
    }


class TestPrimaryLocationDeterminism:
    """Primary-location selection must be a pure function of the
    practitioner's DISTINCT role set: independent of fetch order, fetch
    multiplicity (each role is re-fetched once per in-state location it
    references), checkpoint replay order, and storage backend. The previous
    implementation counted role OCCURRENCES (measured 2.4x non-uniform
    inflation on Rhode Island) and tie-broke on first-seen traversal
    order."""

    def _primary_address(self, config: Config, raw: dict) -> str | None:  # type: ignore[type-arg]
        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({}))
        return adapter.normalize(adapter.extract(raw)).provider.address

    def test_shuffled_role_order_yields_identical_primary(self, config: Config) -> None:
        roles = [("r-1", ["loc-x"]), ("r-2", ["loc-y"]), ("r-3", ["loc-y"])]
        forward = self._primary_address(config, _determinism_raw(roles))
        backward = self._primary_address(config, _determinism_raw(list(reversed(roles))))
        assert forward == backward
        assert forward == "200 loc-y St"  # loc-y wins outright (2 distinct roles vs 1)

    def test_exact_tie_breaks_to_smallest_location_id_not_first_seen(self, config: Config) -> None:
        # loc-z is seen first (would have won under first-seen); the tie
        # must instead break to the lexicographically smallest id, loc-a.
        raw = _determinism_raw([("r-1", ["loc-z"]), ("r-2", ["loc-a"])])
        assert self._primary_address(config, raw) == "100 loc-a St"

    def test_clear_winner_beats_smaller_id(self, config: Config) -> None:
        # Distinct-role count still dominates: loc-z has 2 roles, loc-a has
        # 1 - loc-z wins despite sorting after loc-a.
        raw = _determinism_raw([("r-1", ["loc-z"]), ("r-2", ["loc-z"]), ("r-3", ["loc-a"])])
        assert self._primary_address(config, raw) == "200 loc-z St"

    def test_apply_role_entries_dedupes_across_calls(self, config: Config) -> None:
        # One role re-fetched by two different per-location batches (the
        # real duplication mechanism) must land in `roles` exactly once.
        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({}))
        from adapters.providers.cigna_fhir.adapter import DiscoveryStats

        stats = DiscoveryStats()
        resources: dict = {}
        roles: list = []
        role = {
            "resourceType": "PractitionerRole",
            "id": "r-dup",
            "practitioner": {"reference": "Practitioner/p-1"},
            "location": [{"reference": "Location/loc-a"}, {"reference": "Location/loc-b"}],
        }
        adapter._apply_role_entries([role], resources, roles, stats)  # loc-a's batch
        adapter._apply_role_entries([role], resources, roles, stats)  # loc-b's batch
        assert len(roles) == 1
        assert stats.duplicate_resources_by_type == {"PractitionerRole": 1}

    def test_end_to_end_duplication_does_not_inflate_primary_selection(
        self, config: Config
    ) -> None:
        """The audit's worked example, end to end through discover():
        loc-a has 2 distinct single-location roles; loc-b/loc-c share one
        multi-location role that gets re-fetched by both their batches.
        Occurrence-counting saw a 2/2/2 three-way tie broken by first-seen
        (loc-b); distinct-role counting must pick loc-a (2 > 1)."""
        config.providers.cigna_fhir_state = "RI"
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)

        def loc(lid: str, street: str) -> dict:  # type: ignore[type-arg]
            return {
                "resource": {
                    "resourceType": "Location",
                    "id": lid,
                    "name": f"Clinic {lid}",
                    "address": {
                        "line": [street],
                        "city": "Providence",
                        "state": "RI",
                        "postalCode": "02903",
                    },
                }
            }

        # loc-b enumerated first, so under the old first-seen tie-break it
        # would have been the winner.
        location_bundle = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": [loc("loc-b", "1 B St"), loc("loc-c", "2 C St"), loc("loc-a", "3 A St")],
        }

        practitioner = {
            "resourceType": "Practitioner",
            "id": "p-1",
            "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": JANE_DOE_NPI}],
            "name": [{"family": "Doe", "given": ["Jane"]}],
        }

        def role(rid: str, lids: list[str]) -> dict:  # type: ignore[type-arg]
            return {
                "resourceType": "PractitionerRole",
                "id": rid,
                "practitioner": {"reference": "Practitioner/p-1"},
                "location": [{"reference": f"Location/{lid}"} for lid in lids],
                "specialty": [],
            }

        multi_role = role("r-multi", ["loc-b", "loc-c"])
        bundle_b = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": [{"resource": practitioner}, {"resource": multi_role}],
        }
        bundle_c = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": [{"resource": multi_role}],  # the re-fetch duplication
        }
        bundle_a = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": [
                {"resource": role("r-a1", ["loc-a"])},
                {"resource": role("r-a2", ["loc-a"])},
            ],
        }
        pages = {location_url: location_bundle}
        for lid, bundle in [("loc-b", bundle_b), ("loc-c", bundle_c), ("loc-a", bundle_a)]:
            pages[
                _practitioner_role_by_location_url(
                    pc.cigna_fhir_base_url, lid, pc.cigna_fhir_page_size
                )
            ] = bundle

        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher(pages))
        records = list(adapter.discover())
        assert len(records) == 1
        assert len(records[0]["roles"]) == 3  # r-multi present once, not twice

        normalized = adapter.normalize(adapter.extract(records[0]))
        assert normalized.provider.address == "3 A St"  # loc-a: 2 distinct roles beat 1


class TestNormalizeMissingNpi:
    def test_missing_npi_still_normalizes_without_raising(
        self, config: Config, initial_url: str
    ) -> None:
        adapter = _adapter_with_pages(config, {initial_url: MISSING_NPI_PAGE})
        raw = next(adapter.discover())
        normalized = adapter.normalize(adapter.extract(raw))
        assert normalized.provider.npi is None
        assert normalized.provider.npi_valid is True  # nothing to invalidate
        assert normalized.locations == []


def _counting_fetcher(pages: dict, fail_on: set[str] | None = None):  # type: ignore[no-untyped-def]
    """Like `make_fetcher`, but counts calls per URL and can be told to
    raise for specific URLs (simulating an interrupted run)."""
    fail_on = fail_on or set()
    calls: dict[str, int] = {}

    def fetch(url: str) -> dict:  # type: ignore[type-arg]
        calls[url] = calls.get(url, 0) + 1
        if url in fail_on:
            raise RuntimeError(f"simulated failure fetching {url}")
        return pages[url]

    return fetch, calls


class TestSilentFailureInstrumentation:
    """Correctness instrumentation (not profiling) - see
    ``DiscoveryStats.record_bundle_health``/``suspected_silent_failure``.
    Distinguishes the server's confirmed silent-failure signature
    (``"total": 0``) from an ordinary empty page (no ``total`` key at all),
    which is expected on the large majority of location queries and must
    never itself be flagged.
    """

    def test_total_zero_bundle_is_detected(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        config.retries.max_attempts = 2  # fast: one retry, no real backoff
        config.retries.backoff_base_seconds = 0.001
        config.retries.backoff_max_seconds = 0.01
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        total_zero_bundle = {"resourceType": "Bundle", "type": "searchset", "link": [], "total": 0}
        adapter = _adapter_with_pages(
            config,
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: total_zero_bundle,
            },
        )
        records = list(adapter.discover())
        stats = adapter.last_discovery_stats
        # Counted once at EXHAUSTION (persisted through both attempts), not
        # once per attempt - suspected_silent_failure means "data actually
        # at risk", and a retried-and-recovered blip must not trip it.
        assert stats.bundles_with_total_zero_by_endpoint == {"PractitionerRole": 1}
        assert stats.total_zero_retries_by_endpoint == {"PractitionerRole": 2}
        assert stats.suspected_silent_failure is True
        # The poisoned batch is now a FAILED batch (resume will re-fetch
        # it), not silently-empty data marked done.
        assert len(stats.failed_batches) == 1
        # The healthy batch still yields - one bad batch never aborts a run.
        assert {r["practitioner"]["id"] for r in records} == {"practitioner-ri"}

    def test_transient_total_zero_recovered_by_retry_is_not_a_failure(self, config: Config) -> None:
        """A total:0 page that a retry fixes is a blip, not a silent
        failure: the run must complete clean, with the recovery visible
        only in the retry counter."""
        config.providers.cigna_fhir_state = "RI"
        config.retries.max_attempts = 2
        config.retries.backoff_base_seconds = 0.001
        config.retries.backoff_max_seconds = 0.01
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        total_zero_bundle = {"resourceType": "Bundle", "type": "searchset", "link": [], "total": 0}
        calls: dict[str, int] = {}

        def flaky(url: str) -> dict:  # type: ignore[type-arg]
            calls[url] = calls.get(url, 0) + 1
            if url == role_url_b and calls[url] == 1:
                return total_zero_bundle  # transient - healthy on retry
            return {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            }[url]

        adapter = CignaFhirAdapter(config, page_fetcher=flaky)
        records = list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert calls[role_url_b] == 2  # one total:0, one clean retry
        assert stats.total_zero_retries_by_endpoint == {"PractitionerRole": 1}
        assert stats.bundles_with_total_zero_by_endpoint == {}  # never exhausted
        assert stats.suspected_silent_failure is False
        assert stats.failed_batches == []
        assert {r["practitioner"]["id"] for r in records} == {"practitioner-ri"}

    def test_fresh_enumeration_total_zero_fails_the_run_loudly(self, config: Config) -> None:
        """A persistent total:0 on a FRESH location-enumeration walk (no
        resumed cursor to blame) is a genuine silent failure: the old
        behavior recorded an undercounted enumeration as normal completion;
        it must now fail the run."""
        config.providers.cigna_fhir_state = "RI"
        config.retries.max_attempts = 1  # exhaust immediately, zero sleeps
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        total_zero_bundle = {"resourceType": "Bundle", "type": "searchset", "link": [], "total": 0}
        adapter = _adapter_with_pages(config, {location_url: total_zero_bundle})

        from adapters.providers.cigna_fhir.adapter import SilentTotalZeroError

        with pytest.raises(SilentTotalZeroError):
            list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert stats.bundles_with_total_zero_by_endpoint == {"Location": 1}

    def test_unscoped_walk_failure_is_recorded_not_raised(
        self, config: Config, initial_url: str
    ) -> None:
        """The unscoped path had no error containment at all - any fetch
        failure propagated a raw traceback out of discover() with the run
        row left unfinished. It must instead be recorded like every other
        batch failure (blocking mark_complete via had_errors)."""
        config.retries.max_attempts = 1
        adapter = _adapter_with_pages(config, {})  # every URL raises KeyError

        records = list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert records == []
        assert len(stats.failed_batches) == 1
        assert stats.failed_batches[0][0] == "unscoped"

    def test_persistent_total_zero_batch_is_refetched_on_resume(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """The whole point of retry-then-ERROR over detect-and-continue: a
        poisoned batch is marked error (not done-with-empty-data), the run
        is not marked complete, and a later resume re-fetches exactly that
        batch and recovers its data."""
        config.providers.cigna_fhir_state = "RI"
        config.retries.max_attempts = 1
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        total_zero_bundle = {"resourceType": "Bundle", "type": "searchset", "link": [], "total": 0}
        checkpoint_path = tmp_path / "checkpoints.db"

        # Run 1: loc-a's batch (which carries the practitioner) is poisoned.
        cp1 = CheckpointStore(checkpoint_path)
        adapter1 = CignaFhirAdapter(
            config,
            page_fetcher=make_fetcher(
                {
                    location_url: STATE_LOCATION_BUNDLE,
                    role_url_a: total_zero_bundle,
                    role_url_b: _EMPTY_BUNDLE,
                }
            ),
            checkpoint_store=cp1,
        )
        records1 = list(adapter1.discover())
        assert records1 == []  # the practitioner lived in the poisoned batch
        assert cp1.status("cigna_fhir:RI", "practitioner_role_by_location", "0") == "error"
        assert cp1.is_complete("cigna_fhir:RI") is False  # gated by had_errors
        cp1.close()

        # Run 2: server healthy again - only the errored batch re-fetches,
        # and its data is recovered in full.
        cp2 = CheckpointStore(checkpoint_path)
        fetch2, calls2 = _counting_fetcher(
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            }
        )
        adapter2 = CignaFhirAdapter(config, page_fetcher=fetch2, checkpoint_store=cp2)
        records2 = list(adapter2.discover())
        assert location_url not in calls2  # enumeration replayed from checkpoint
        assert calls2[role_url_a] == 1  # the errored batch, re-fetched
        assert role_url_b not in calls2  # the done batch, NOT re-fetched
        assert {r["practitioner"]["id"] for r in records2} == {"practitioner-ri"}
        assert cp2.is_complete("cigna_fhir:RI") is True
        cp2.close()

    def test_genuinely_empty_bundle_is_not_flagged_as_silent_failure(self, config: Config) -> None:
        """loc-b legitimately has no roles (no ``total`` key at all, matching
        what the live server returns for a normal empty result) - this must
        NOT set suspected_silent_failure. ~94% of locations are like this on
        a real state; flagging it would make every healthy run look broken.
        """
        config.providers.cigna_fhir_state = "RI"
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        adapter = _adapter_with_pages(
            config,
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            },
        )
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert stats.bundles_with_total_zero_by_endpoint == {}
        assert stats.empty_bundles_by_endpoint == {"PractitionerRole": 1}
        assert stats.suspected_silent_failure is False

    def test_location_enumeration_page_count_and_normal_termination(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        adapter = _adapter_with_pages(
            config,
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            },
        )
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert stats.location_enumeration_pages == 1
        assert stats.location_enumeration_completed_normally is True

    def test_location_enumeration_paginates_across_a_next_link(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        continuation_url = f"{pc.cigna_fhir_base_url}/Location?page=2"
        page_1 = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [{"relation": "next", "url": continuation_url}],
            "entry": STATE_LOCATION_BUNDLE["entry"][:1],
        }
        page_2 = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": STATE_LOCATION_BUNDLE["entry"][1:],
        }
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        adapter = _adapter_with_pages(
            config,
            {
                location_url: page_1,
                continuation_url: page_2,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            },
        )
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert stats.location_enumeration_pages == 2
        assert stats.location_enumeration_completed_normally is True

    def test_resumed_run_leaves_enumeration_status_unverified_not_false(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """A run resumed entirely from checkpoint never re-fetches location
        enumeration - its status for THIS session must be None (not
        verified), never False (which would wrongly read as a confirmed
        abnormal termination). Run 1 must genuinely FAIL (one location's
        role query raises) so the checkpoint is NOT marked complete and run
        2 actually resumes rather than restarting fresh - see
        StateScopedDiscovery.run()'s had_errors handling.
        """
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        location_url, role_url_a, role_url_b = self._urls_for_checkpoint_test(config)
        checkpoint_path = tmp_path / "checkpoints.db"

        cp1 = CheckpointStore(checkpoint_path)
        fetch1, _calls1 = _counting_fetcher(
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            },
            fail_on={role_url_b},
        )
        adapter1 = CignaFhirAdapter(config, page_fetcher=fetch1, checkpoint_store=cp1)
        list(adapter1.discover())
        assert adapter1.last_discovery_stats.location_enumeration_completed_normally is True
        cp1.close()

        cp2 = CheckpointStore(checkpoint_path)
        fetch2, calls2 = _counting_fetcher(
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            }
        )
        adapter2 = CignaFhirAdapter(config, page_fetcher=fetch2, checkpoint_store=cp2)
        list(adapter2.discover())
        assert location_url not in calls2  # confirms this run actually resumed
        assert adapter2.last_discovery_stats.location_enumeration_completed_normally is None
        assert adapter2.last_discovery_stats.location_enumeration_pages == 0
        cp2.close()

    def _urls_for_checkpoint_test(self, config: Config) -> tuple[str, str, str]:
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", 1)
        role_url_a = _practitioner_role_by_location_url(pc.cigna_fhir_base_url, "loc-a", 1)
        role_url_b = _practitioner_role_by_location_url(pc.cigna_fhir_base_url, "loc-b", 1)
        return location_url, role_url_a, role_url_b

    def test_role_queries_attempted_and_empty_counts(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
        role_url_a = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        )
        role_url_b = _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        )
        adapter = _adapter_with_pages(
            config,
            {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            },
        )
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert stats.role_queries_attempted == 2
        assert stats.role_queries_empty == 1

    def test_provider_location_relationships_discovered(
        self, config: Config, initial_url: str
    ) -> None:
        """Uses the existing multi-page pagination fixture (Dr. Jane Doe, 2
        roles at 2 distinct locations - PAGE_1/PAGE_2) as a ready-made
        multi-location practitioner - no new fixture needed."""
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        list(adapter.discover())
        stats = adapter.last_discovery_stats
        assert stats.providers_yielded == 1
        assert stats.provider_location_relationships_discovered == 2

    def test_concurrent_path_detects_total_zero_identically_to_sequential(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The async fetch path (_fetch_role_page_entries_async) has its own
        bundle-fetch loop that bypasses _fetch_timed - this pins that it
        applies the same retry-then-error total:0 handling, since it would
        be easy to change the sync path and forget the concurrent one
        exists."""
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        config.providers.cigna_fhir_max_concurrent_requests = 5
        config.retries.max_attempts = 2
        config.retries.backoff_base_seconds = 0.001
        config.retries.backoff_max_seconds = 0.01
        location_url, role_url_a, role_url_b = _two_batch_urls(config)
        batch_a, _batch_b = _two_batches()
        total_zero_bundle = {"resourceType": "Bundle", "type": "searchset", "link": [], "total": 0}

        client, _state = _mock_async_client({role_url_a: batch_a, role_url_b: total_zero_bundle})
        monkeypatch.setattr(cigna_adapter_module.httpx, "AsyncClient", lambda *a, **kw: client)
        adapter = CignaFhirAdapter(
            config, page_fetcher=make_fetcher({location_url: STATE_LOCATION_BUNDLE})
        )
        list(adapter.discover())
        asyncio.run(client.aclose())

        stats = adapter.last_discovery_stats
        assert stats.bundles_with_total_zero_by_endpoint == {"PractitionerRole": 1}
        assert stats.total_zero_retries_by_endpoint == {"PractitionerRole": 2}
        assert stats.suspected_silent_failure is True
        assert len(stats.failed_batches) == 1  # identical error path to sequential


class TestLocationEnumerationResumability:
    """Location enumeration is resumable at PAGE granularity (see
    ``StateScopedDiscovery._run_location_enumeration``) - an interruption
    mid-walk leaves per-page checkpoint items behind, and a later run
    continues from the saved FHIR cursor instead of re-walking from page 1.
    If that saved cursor turns out to be stale (the confirmed total:0
    signature - see ``DiscoveryStats``), the resume is abandoned and the
    walk restarts cleanly, exactly as it always has.
    """

    def _urls(self, config: Config) -> tuple[str, str, str, str]:
        pc = config.providers
        location_page1_url = _location_search_url(pc.cigna_fhir_base_url, "RI", 1)
        location_page2_url = f"{pc.cigna_fhir_base_url}/Location?page=2"
        role_url_a = _practitioner_role_by_location_url(pc.cigna_fhir_base_url, "loc-a", 1)
        role_url_b = _practitioner_role_by_location_url(pc.cigna_fhir_base_url, "loc-b", 1)
        return location_page1_url, location_page2_url, role_url_a, role_url_b

    def _location_pages(self, next_url: str) -> tuple[dict, dict]:  # type: ignore[type-arg]
        page1 = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [{"relation": "next", "url": next_url}],
            "entry": [STATE_LOCATION_BUNDLE["entry"][0]],  # loc-a
        }
        page2 = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": [STATE_LOCATION_BUNDLE["entry"][1]],  # loc-b
        }
        return page1, page2

    def test_interrupted_walk_resumes_from_the_saved_cursor_without_refetching_page_1(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        page1_url, page2_url, role_url_a, role_url_b = self._urls(config)
        page1, page2 = self._location_pages(page2_url)
        checkpoint_path = tmp_path / "checkpoints.db"

        # Run 1: page 1 fetches fine, page 2 raises (simulating a crash
        # mid-walk) - page 1 must already be checkpointed by the time this
        # propagates, since _on_page fires before the loop attempts page 2.
        cp1 = CheckpointStore(checkpoint_path)
        fetch1, calls1 = _counting_fetcher(
            {page1_url: page1, page2_url: page2}, fail_on={page2_url}
        )
        adapter1 = CignaFhirAdapter(config, page_fetcher=fetch1, checkpoint_store=cp1)
        with pytest.raises(RuntimeError):
            list(adapter1.discover())
        assert calls1[page1_url] == 1
        assert calls1[page2_url] == 1
        # Page 1's per-page checkpoint must exist despite the overall failure.
        assert cp1.status("cigna_fhir:RI", "location_enumeration_pages", "0") == "done"
        assert cp1.get_payload("cigna_fhir:RI", "location_enumeration_cursor", "current") == {
            "next_url": page2_url,
            "next_page_index": 1,
        }
        cp1.close()

        # Run 2: same checkpoint file, page 2 now succeeds - page 1 must
        # NOT be re-fetched.
        cp2 = CheckpointStore(checkpoint_path)
        fetch2, calls2 = _counting_fetcher(
            {
                page1_url: page1,
                page2_url: page2,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: _EMPTY_BUNDLE,
            }
        )
        adapter2 = CignaFhirAdapter(config, page_fetcher=fetch2, checkpoint_store=cp2)
        records = list(adapter2.discover())

        assert page1_url not in calls2  # resumed from checkpoint, no re-fetch
        assert calls2[page2_url] == 1
        assert {r["practitioner"]["id"] for r in records} == {"practitioner-ri"}
        # Once the whole phase completes, per-page bookkeeping must be gone.
        assert cp2.done_payloads("cigna_fhir:RI", "location_enumeration_pages") == {}
        assert cp2.get_payload("cigna_fhir:RI", "location_enumeration_cursor", "current") is None
        assert cp2.status("cigna_fhir:RI", "location_enumeration", "all") == "done"
        cp2.close()

    def test_stale_resumed_cursor_falls_back_to_a_clean_full_restart(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """The saved cursor from run 1 is dead by the time run 2 starts
        (returns the total:0 signature) - the resume must be abandoned and
        the walk redone from page 1, still producing the complete, correct
        result, with the recovery reclassified as a stale-cursor restart
        rather than a silent failure. This is the exact failure mode of a
        real Cigna cursor expiring across a long interruption (measured
        ~17min idle TTL).

        The stale response is served only on the FIRST hit of page2_url
        (the doomed resume); the restart's own walk gets the real page.
        That mirrors reality - a fresh walk mints a fresh server cursor -
        and matters for the assertion set: a page that stays total:0 on the
        restart too is a GENUINE silent failure and now correctly fails the
        run (previously it silently dropped page 2's locations)."""
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        config.retries.max_attempts = 1  # exhaust the stale cursor with zero sleeps
        page1_url, page2_url, role_url_a, role_url_b = self._urls(config)
        page1, page2 = self._location_pages(page2_url)
        total_zero_bundle = {"resourceType": "Bundle", "type": "searchset", "link": [], "total": 0}
        checkpoint_path = tmp_path / "checkpoints.db"

        cp1 = CheckpointStore(checkpoint_path)
        fetch1, _calls1 = _counting_fetcher(
            {page1_url: page1, page2_url: page2}, fail_on={page2_url}
        )
        adapter1 = CignaFhirAdapter(config, page_fetcher=fetch1, checkpoint_store=cp1)
        with pytest.raises(RuntimeError):
            list(adapter1.discover())
        cp1.close()

        # Run 2: the saved cursor (page2_url) is stale on first hit, real
        # afterwards. A FRESH walk from page1_url must still work.
        cp2 = CheckpointStore(checkpoint_path)
        calls2: dict[str, int] = {}
        pages2 = {
            page1_url: page1,
            page2_url: page2,
            role_url_a: STATE_ROLE_BUNDLE,
            role_url_b: _EMPTY_BUNDLE,
        }

        def fetch2(url: str) -> dict:  # type: ignore[type-arg]
            calls2[url] = calls2.get(url, 0) + 1
            if url == page2_url and calls2[url] == 1:
                return total_zero_bundle  # the expired resume cursor
            return pages2[url]

        adapter2 = CignaFhirAdapter(config, page_fetcher=fetch2, checkpoint_store=cp2)
        records = list(adapter2.discover())
        stats = adapter2.last_discovery_stats

        # The restart re-walked from page1_url (the resume alone can't
        # re-derive page 1's data), proving a genuine clean restart rather
        # than silent acceptance of partial data.
        assert calls2[page1_url] >= 1
        assert calls2[page2_url] == 2  # stale attempt + the restart's real fetch
        assert {r["practitioner"]["id"] for r in records} == {"practitioner-ri"}
        # Reclassification: a RECOVERED stale cursor is not a silent
        # failure - the completeness gate must not fail this healthy run.
        assert stats.stale_cursor_restarts == 1
        assert stats.bundles_with_total_zero_by_endpoint == {}
        assert stats.suspected_silent_failure is False
        assert stats.failed_batches == []
        assert cp2.done_payloads("cigna_fhir:RI", "location_enumeration_pages") == {}
        assert cp2.status("cigna_fhir:RI", "location_enumeration", "all") == "done"
        assert cp2.is_complete("cigna_fhir:RI") is True  # restart must not set had_errors
        cp2.close()

    def test_completed_run_leaves_no_leftover_per_page_checkpoint_items(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """A location enumeration that completes in ONE pass (no
        interruption at all) must still end with the per-page bookkeeping
        cleared - it's write-during-the-walk-only scratch state, not meant
        to persist once the whole-phase checkpoint exists."""
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        page1_url, page2_url, role_url_a, role_url_b = self._urls(config)
        page1, page2 = self._location_pages(page2_url)
        checkpoint_path = tmp_path / "checkpoints.db"

        cp = CheckpointStore(checkpoint_path)
        adapter = CignaFhirAdapter(
            config,
            page_fetcher=make_fetcher(
                {
                    page1_url: page1,
                    page2_url: page2,
                    role_url_a: STATE_ROLE_BUNDLE,
                    role_url_b: _EMPTY_BUNDLE,
                }
            ),
            checkpoint_store=cp,
        )
        list(adapter.discover())
        assert cp.done_payloads("cigna_fhir:RI", "location_enumeration_pages") == {}
        assert cp.get_payload("cigna_fhir:RI", "location_enumeration_cursor", "current") is None
        cp.close()


class TestCheckpointResume:
    """Durable resume (see providermap/checkpoint.py) - a batch that fails
    is retried on a later run without re-fetching anything already done."""

    def _urls(self, config: Config) -> tuple[str, str, str]:
        pc = config.providers
        location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", 1)
        role_url_a = _practitioner_role_by_location_url(pc.cigna_fhir_base_url, "loc-a", 1)
        role_url_b = _practitioner_role_by_location_url(pc.cigna_fhir_base_url, "loc-b", 1)
        return location_url, role_url_a, role_url_b

    def _batches(self) -> tuple[dict, dict]:  # type: ignore[type-arg]
        batch_a = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": [
                {
                    "resource": {
                        "resourceType": "Practitioner",
                        "id": "p-a",
                        "identifier": [
                            {"system": "http://hl7.org/fhir/sid/us-npi", "value": JANE_DOE_NPI}
                        ],
                        "name": [{"family": "A", "given": ["Prac"]}],
                    }
                },
                {
                    "resource": {
                        "resourceType": "PractitionerRole",
                        "id": "role-a",
                        "practitioner": {"reference": "Practitioner/p-a"},
                        "location": [{"reference": "Location/loc-a"}],
                    }
                },
            ],
        }
        batch_b = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": [
                {
                    "resource": {
                        "resourceType": "Practitioner",
                        "id": "p-b",
                        "identifier": [
                            {
                                "system": "http://hl7.org/fhir/sid/us-npi",
                                "value": STATE_SCOPED_PRACTITIONER_NPI,
                            }
                        ],
                        "name": [{"family": "B", "given": ["Prac"]}],
                    }
                },
                {
                    "resource": {
                        "resourceType": "PractitionerRole",
                        "id": "role-b",
                        "practitioner": {"reference": "Practitioner/p-b"},
                        "location": [{"reference": "Location/loc-b"}],
                    }
                },
            ],
        }
        return batch_a, batch_b

    def test_failed_batch_does_not_abort_the_run(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        location_url, role_url_a, role_url_b = self._urls(config)
        batch_a, batch_b = self._batches()

        cp = CheckpointStore(tmp_path / "checkpoints.db")
        fetch, _calls = _counting_fetcher(
            {location_url: STATE_LOCATION_BUNDLE, role_url_a: batch_a, role_url_b: batch_b},
            fail_on={role_url_b},
        )
        adapter = CignaFhirAdapter(config, page_fetcher=fetch, checkpoint_store=cp)
        records = list(adapter.discover())

        # Batch b failed - only batch a's practitioner comes through this run.
        assert len(records) == 1
        assert records[0]["practitioner"]["id"] == "p-a"
        assert cp.status("cigna_fhir:RI", "practitioner_role_by_location", "1") == "error"
        assert cp.status("cigna_fhir:RI", "practitioner_role_by_location", "0") == "done"
        cp.close()

    def test_resume_skips_already_done_work_and_retries_the_failed_batch(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        location_url, role_url_a, role_url_b = self._urls(config)
        batch_a, batch_b = self._batches()
        checkpoint_path = tmp_path / "checkpoints.db"

        # First run: batch b fails.
        cp1 = CheckpointStore(checkpoint_path)
        fetch1, calls1 = _counting_fetcher(
            {location_url: STATE_LOCATION_BUNDLE, role_url_a: batch_a, role_url_b: batch_b},
            fail_on={role_url_b},
        )
        adapter1 = CignaFhirAdapter(config, page_fetcher=fetch1, checkpoint_store=cp1)
        list(adapter1.discover())
        assert calls1[location_url] == 1
        assert calls1[role_url_a] == 1
        assert calls1[role_url_b] == 1
        cp1.close()

        # Second run: same checkpoint file, this time role_url_b succeeds -
        # location and batch a must NOT be re-fetched.
        cp2 = CheckpointStore(checkpoint_path)
        fetch2, calls2 = _counting_fetcher(
            {location_url: STATE_LOCATION_BUNDLE, role_url_a: batch_a, role_url_b: batch_b}
        )
        adapter2 = CignaFhirAdapter(config, page_fetcher=fetch2, checkpoint_store=cp2)
        records = list(adapter2.discover())

        assert location_url not in calls2  # resumed from checkpoint, no re-fetch
        assert role_url_a not in calls2  # already done, no re-fetch
        assert calls2[role_url_b] == 1  # previously-failed batch is retried

        practitioner_ids = {r["practitioner"]["id"] for r in records}
        assert practitioner_ids == {"p-a", "p-b"}
        cp2.close()

    def test_completed_run_is_marked_complete(self, config: Config, tmp_path) -> None:  # type: ignore[no-untyped-def]
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        location_url, role_url_a, role_url_b = self._urls(config)
        batch_a, batch_b = self._batches()

        cp = CheckpointStore(tmp_path / "checkpoints.db")
        fetch, _calls = _counting_fetcher(
            {location_url: STATE_LOCATION_BUNDLE, role_url_a: batch_a, role_url_b: batch_b}
        )
        adapter = CignaFhirAdapter(config, page_fetcher=fetch, checkpoint_store=cp)
        list(adapter.discover())

        assert cp.is_complete("cigna_fhir:RI") is True
        cp.close()

    def test_no_checkpoint_store_behaves_exactly_as_before(
        self, config: Config, initial_url: str
    ) -> None:
        """checkpoint_store=None (the default) - existing behavior,
        unaffected by any of the checkpointing machinery."""
        adapter = _adapter_with_pages(config, {initial_url: PAGE_1, PAGE_2_URL: PAGE_2})
        assert adapter.checkpoint_store is None
        records = list(adapter.discover())
        assert len(records) == 1


def _two_batch_urls(config: Config, page_size: int = 1) -> tuple[str, str, str]:
    pc = config.providers
    location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", page_size)
    role_url_a = _practitioner_role_by_location_url(pc.cigna_fhir_base_url, "loc-a", page_size)
    role_url_b = _practitioner_role_by_location_url(pc.cigna_fhir_base_url, "loc-b", page_size)
    return location_url, role_url_a, role_url_b


def _two_batches() -> tuple[dict, dict]:  # type: ignore[type-arg]
    batch_a = {
        "resourceType": "Bundle",
        "type": "searchset",
        "link": [],
        "entry": [
            {
                "resource": {
                    "resourceType": "Practitioner",
                    "id": "p-a",
                    "identifier": [
                        {"system": "http://hl7.org/fhir/sid/us-npi", "value": JANE_DOE_NPI}
                    ],
                    "name": [{"family": "A", "given": ["Prac"]}],
                }
            },
            {
                "resource": {
                    "resourceType": "PractitionerRole",
                    "id": "role-a",
                    "practitioner": {"reference": "Practitioner/p-a"},
                    "location": [{"reference": "Location/loc-a"}],
                }
            },
        ],
    }
    batch_b = {
        "resourceType": "Bundle",
        "type": "searchset",
        "link": [],
        "entry": [
            {
                "resource": {
                    "resourceType": "Practitioner",
                    "id": "p-b",
                    "identifier": [
                        {
                            "system": "http://hl7.org/fhir/sid/us-npi",
                            "value": STATE_SCOPED_PRACTITIONER_NPI,
                        }
                    ],
                    "name": [{"family": "B", "given": ["Prac"]}],
                }
            },
            {
                "resource": {
                    "resourceType": "PractitionerRole",
                    "id": "role-b",
                    "practitioner": {"reference": "Practitioner/p-b"},
                    "location": [{"reference": "Location/loc-b"}],
                }
            },
        ],
    }
    return batch_a, batch_b


def _mock_async_client(
    pages: dict, fail_once: set[str] | None = None, fail_always: set[str] | None = None  # type: ignore[type-arg]
) -> tuple[httpx.AsyncClient, dict]:  # type: ignore[type-arg]
    """An httpx.AsyncClient backed by MockTransport - no real network. Tracks
    call counts and peak concurrency. ``fail_once`` returns a retryable 503
    the first time a URL is hit, then succeeds; ``fail_always`` always
    returns a non-retryable 400."""
    fail_once = fail_once or set()
    fail_always = fail_always or set()
    state = {"calls": {}, "current": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.02)  # give concurrent tasks a chance to overlap
        state["current"] -= 1
        state["calls"][url] = state["calls"].get(url, 0) + 1
        if url in fail_always:
            return httpx.Response(400, json={"resourceType": "OperationOutcome"})
        if url in fail_once and state["calls"][url] == 1:
            return httpx.Response(503, json={"resourceType": "OperationOutcome"})
        return httpx.Response(200, json=pages[url])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, state


async def _drain_concurrent(
    adapter: CignaFhirAdapter,
    pending: list[tuple[str, str]],
    stats: Any,
    client: httpx.AsyncClient,
) -> dict:
    """_fetch_batches_concurrently is an async generator (streams results in
    pending's order instead of materializing a dict of everything up front -
    see its docstring); tests that only care about the final per-key
    results can drain it into a dict same as before."""
    results = {}
    async for batch_key, result in adapter._fetch_batches_concurrently(
        pending, stats, client=client
    ):
        results[batch_key] = result
    return results


class TestConcurrency:
    """Bounded concurrency for the PractitionerRole batch phase only - see
    adapters/providers/cigna_fhir/CONCURRENCY_DESIGN.md. cigna_fhir_max_concurrent_requests
    defaults to 1 (fully sequential, unaffected by any of this)."""

    def test_default_is_fully_sequential(self, config: Config) -> None:
        assert config.providers.cigna_fhir_max_concurrent_requests == 1

    def test_concurrent_fetch_respects_max_concurrent_limit(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        config.providers.cigna_fhir_max_concurrent_requests = 2
        location_url, role_url_a, role_url_b = _two_batch_urls(config)
        batch_a, batch_b = _two_batches()
        client, state = _mock_async_client({role_url_a: batch_a, role_url_b: batch_b})

        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({location_url: {}}))
        from adapters.providers.cigna_fhir.adapter import DiscoveryStats

        stats = DiscoveryStats()
        results = asyncio.run(
            _drain_concurrent(adapter, [("0", role_url_a), ("1", role_url_b)], stats, client)
        )
        asyncio.run(client.aclose())

        assert state["peak"] <= 2
        assert not isinstance(results["0"], Exception)
        assert not isinstance(results["1"], Exception)

    def test_concurrency_on_vs_off_yield_identical_output(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        location_url, role_url_a, role_url_b = _two_batch_urls(config)
        batch_a, batch_b = _two_batches()
        pages = {location_url: STATE_LOCATION_BUNDLE, role_url_a: batch_a, role_url_b: batch_b}

        # Sequential (default max_concurrent=1) - the existing sync path.
        sequential_adapter = _adapter_with_pages(config, pages)
        sequential_ids = {r["practitioner"]["id"] for r in sequential_adapter.discover()}

        # Concurrent path: monkeypatch httpx.AsyncClient (module-level, as
        # used inside _fetch_batches_concurrently) to a MockTransport backed
        # by the same fixture data - no real network either way.
        client, _state = _mock_async_client({role_url_a: batch_a, role_url_b: batch_b})
        monkeypatch.setattr(cigna_adapter_module.httpx, "AsyncClient", lambda *a, **kw: client)
        config.providers.cigna_fhir_max_concurrent_requests = 5
        concurrent_adapter = CignaFhirAdapter(
            config, page_fetcher=make_fetcher({location_url: STATE_LOCATION_BUNDLE})
        )
        concurrent_ids = {r["practitioner"]["id"] for r in concurrent_adapter.discover()}
        asyncio.run(client.aclose())

        assert concurrent_ids == sequential_ids == {"p-a", "p-b"}

    def test_retryable_status_succeeds_after_one_retry(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        config.retries.backoff_base_seconds = 0.001
        config.retries.backoff_max_seconds = 0.01
        _location_url, role_url_a, _role_url_b = _two_batch_urls(config)
        batch_a, _batch_b = _two_batches()
        client, state = _mock_async_client({role_url_a: batch_a}, fail_once={role_url_a})

        from adapters.providers.cigna_fhir.adapter import DiscoveryStats

        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({}))
        stats = DiscoveryStats()
        results = asyncio.run(_drain_concurrent(adapter, [("0", role_url_a)], stats, client))
        asyncio.run(client.aclose())

        assert not isinstance(results["0"], Exception)
        assert state["calls"][role_url_a] == 2  # one failure, one retry
        assert stats.retry_count == 1

    def test_non_retryable_status_fails_without_retrying(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        _location_url, role_url_a, _role_url_b = _two_batch_urls(config)
        client, state = _mock_async_client({}, fail_always={role_url_a})

        from adapters.providers.cigna_fhir.adapter import DiscoveryStats

        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({}))
        stats = DiscoveryStats()
        results = asyncio.run(_drain_concurrent(adapter, [("0", role_url_a)], stats, client))
        asyncio.run(client.aclose())

        assert isinstance(results["0"], Exception)
        assert state["calls"][role_url_a] == 1  # never retried - 400 is not in retry_on_status

    def test_failed_batch_via_concurrent_path_is_recorded_and_does_not_abort(
        self, config: Config
    ) -> None:
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_page_size = 1
        config.providers.cigna_fhir_max_concurrent_requests = 5
        config.retries.max_attempts = 1  # fail fast, no retry delay in this test
        location_url, role_url_a, role_url_b = _two_batch_urls(config)
        batch_a, _batch_b = _two_batches()
        client, _state = _mock_async_client({role_url_a: batch_a}, fail_always={role_url_b})

        adapter = CignaFhirAdapter(
            config, page_fetcher=make_fetcher({location_url: STATE_LOCATION_BUNDLE})
        )
        original = cigna_adapter_module.httpx.AsyncClient
        cigna_adapter_module.httpx.AsyncClient = lambda *a, **kw: client  # type: ignore[method-assign]
        try:
            records = list(adapter.discover())
        finally:
            cigna_adapter_module.httpx.AsyncClient = original  # type: ignore[method-assign]
        asyncio.run(client.aclose())

        assert len(records) == 1
        assert records[0]["practitioner"]["id"] == "p-a"
        stats = adapter.last_discovery_stats
        assert len(stats.failed_batches) == 1
        assert stats.failed_batches[0][0] == "1"


class _TrackedList(list):  # type: ignore[type-arg]
    """A weakref-able list subclass, used only so tests can hold a
    ``weakref.ref`` to a fetched batch's result - a plain ``list`` (what
    production code actually returns) does not support weak references.
    Going through a real JSON round-trip (``httpx.Response(json=...)`` then
    the client's ``response.json()``) always produces plain ``dict``/``list``
    regardless of what was serialized, so there is no way to preserve a
    custom type across that path - this is why the fetch method itself is
    stubbed below instead of routing through MockTransport."""


class TestBatchResultReachability:
    """Regression coverage for the retention bug found via a live 5.8h RI
    run: a completed asyncio.Task keeps its coroutine's return value cached
    on itself for as long as the Task is reachable (see
    asyncio/futures.py's Future._result - never cleared by result()). The
    `tasks` list in _fetch_batches_concurrently used to never drop a
    completed Task, so every batch's parsed FHIR bundle stayed reachable
    until the ENTIRE phase finished (measured: 4+ GB retained on live data)
    even though the caller had already consumed and wanted to discard it.

    This asserts the actual property that matters - a consumed batch's
    result becomes garbage-collectable promptly, not just "eventually at
    the end of discover()" - via weakref liveness, not memory/RSS sampling
    (which is slow and timing-flaky in CI). Deliberately does NOT assert on
    tracemalloc/psutil numbers, and deliberately does NOT inspect the
    `tasks` list or slot-clearing directly - it tests the observable
    contract, so it keeps failing the same way against any future
    regression that re-introduces the retention, regardless of mechanism.
    """

    async def _consume(
        self,
        adapter: CignaFhirAdapter,
        pending: list,
        stats,
        weakrefs: list,  # type: ignore[type-arg]
        *,
        n_batches: int,
    ) -> None:
        consumed = 0
        async for _batch_key, result in adapter._fetch_batches_concurrently(pending, stats):
            assert not isinstance(result, Exception)
            weakrefs.append(weakref.ref(result))
            consumed += 1
            gc.collect()
            # Every batch STRICTLY BEFORE this one must now be unreachable -
            # this one's own `result` is still legitimately alive in this
            # frame's local, so it is excluded from the check. The LAST
            # batch is deliberately NOT checked in here at all (see the
            # caller) - the coroutine currently driving this loop is itself
            # running inside an asyncio Task, and that Task transiently
            # keeps a strong reference to the last thing it awaited until
            # the Task finishes stepping (verified empirically via
            # gc.get_referrers: a finished `fetch_one()` Task, not `tasks`,
            # was the referrer) - that unwinds once asyncio.run() itself
            # returns, not while still inside the running coroutine.
            for j in range(consumed - 1):
                assert weakrefs[j]() is None, (
                    f"batch {j}'s result is still reachable after batch "
                    f"{consumed - 1} was consumed - a completed batch's data "
                    "is being kept alive past the point the caller consumed it"
                )
        assert consumed == n_batches

    def test_consumed_batch_results_become_collectable(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        # Small concurrency window (not 1, not unbounded) so completion can
        # run somewhat ahead of consumption without the whole phase
        # finishing before the first result is even awaited - the failure
        # mode this guards against only shows up when SOME batches are
        # already-done-but-not-yet-consumed while others are still pending.
        config.providers.cigna_fhir_max_concurrent_requests = 2

        n_batches = 6
        # URLs are never actually dereferenced - _fetch_role_page_entries_async
        # is stubbed below to bypass real HTTP/JSON entirely, since a real
        # JSON round-trip (MockTransport -> response.json()) always produces
        # plain dict/list regardless of what was serialized, which would
        # make the weakref-based check impossible (see _TrackedList).
        pending = [(f"loc-{i}", f"fake://loc-{i}") for i in range(n_batches)]

        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({}), checkpoint_store=None)

        async def fake_fetch_role_page_entries_async(client, url, s):  # noqa: ANN001, ARG001
            i = int(url.rsplit("-", 1)[-1])
            await asyncio.sleep(0.005 * (n_batches - i))  # staggered, not uniform
            return _TrackedList([{"resourceType": "PractitionerRole", "id": f"role-{i}"}])

        adapter._fetch_role_page_entries_async = fake_fetch_role_page_entries_async  # type: ignore[method-assign]
        from adapters.providers.cigna_fhir.adapter import DiscoveryStats

        stats = DiscoveryStats()
        weakrefs: list[weakref.ReferenceType] = []  # type: ignore[type-arg]
        asyncio.run(self._consume(adapter, pending, stats, weakrefs, n_batches=n_batches))

        # Only checkable once the event loop has fully shut down - the Task
        # that ran the coroutine above transiently holds the last thing it
        # awaited until the Task itself finishes, independent of anything
        # `_fetch_batches_concurrently` does (see `_consume`'s docstring).
        gc.collect()
        assert weakrefs[-1]() is None, "the final batch's result was never released"


class TestSyncFetchRetry:
    """_fetch_timed - the Location-enumeration and sequential
    (max_concurrent=1, the default) PractitionerRole fetch path - retries
    via the same RetryConfig the concurrent path already used. This gap
    (the sync path had no retry logic at all) was only found live: a
    production-scale run hit a transient 502 during Location enumeration
    and the whole multi-hour ingestion crashed instead of retrying. See
    _fetch_with_retry_sync's docstring."""

    def test_location_enumeration_retries_transient_status_then_succeeds(
        self, config: Config
    ) -> None:
        config.providers.cigna_fhir_state = "RI"
        config.retries.backoff_base_seconds = 0.001
        config.retries.backoff_max_seconds = 0.01
        location_url = _location_search_url(
            config.providers.cigna_fhir_base_url,
            config.providers.cigna_fhir_state,
            config.providers.cigna_fhir_page_size,
        )
        calls = {"n": 0}

        def flaky_fetcher(url: str) -> dict:
            if url == location_url:
                calls["n"] += 1
                if calls["n"] == 1:
                    request = httpx.Request("GET", url)
                    httpx.Response(502, request=request, json={}).raise_for_status()
                return STATE_LOCATION_BUNDLE
            return {}

        adapter = CignaFhirAdapter(config, page_fetcher=flaky_fetcher)
        list(adapter.discover())  # must not raise - the 502 is retried, not fatal

        assert calls["n"] == 2  # one failure, one retry
        assert adapter.last_discovery_stats.retry_count == 1

    def test_location_enumeration_does_not_retry_non_retryable_status(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        calls = {"n": 0}

        def always_fails(url: str) -> dict:
            calls["n"] += 1
            request = httpx.Request("GET", url)
            httpx.Response(400, request=request, json={}).raise_for_status()
            return {}  # unreachable

        adapter = CignaFhirAdapter(config, page_fetcher=always_fails)
        with pytest.raises(httpx.HTTPStatusError):
            list(adapter.discover())

        assert calls["n"] == 1  # never retried - 400 is not in retry_on_status


class TestLocationNameIsGeneratedAddress:
    """Cigna's Location.name is sometimes just that Location's own address
    lines auto-joined by dashes (observed live: single-line
    "45 WELLS ST # 104-WESTERLY-RI-02891", and - the case a narrower,
    single-line-only version of this check missed - multi-line
    "70 KENYON AVE UNIT 326-# 326-WAKEFIELD-RI-02879") rather than a real
    facility name - see _location_name_is_generated_address's docstring."""

    def test_exact_generated_address_match(self) -> None:
        assert _location_name_is_generated_address(
            "45 WELLS ST # 104-WESTERLY-RI-02891",
            ["45 WELLS ST # 104"],
            "WESTERLY",
            "RI",
            "02891-2927",  # zip+4 - only the 5-digit prefix should be compared
        )

    def test_multi_line_address_match(self) -> None:
        # Real example that slipped past an earlier version of this check,
        # which compared against ", "-joined lines instead of "-"-joined
        # individual lines - a second address line (unit/suite/building)
        # broke the exact match.
        assert _location_name_is_generated_address(
            "70 KENYON AVE UNIT 326-# 326-WAKEFIELD-RI-02879",
            ["70 KENYON AVE UNIT 326", "# 326"],
            "WAKEFIELD",
            "RI",
            "02879-4253",
        )

    def test_case_insensitive_match(self) -> None:
        assert _location_name_is_generated_address(
            "45 wells st # 104-westerly-ri-02891",
            ["45 WELLS ST # 104"],
            "WESTERLY",
            "RI",
            "02891",
        )

    def test_real_facility_name_is_not_flagged(self) -> None:
        assert not _location_name_is_generated_address(
            "Westerly Family Medicine",
            ["45 WELLS ST # 104"],
            "WESTERLY",
            "RI",
            "02891-2927",
        )

    def test_no_name_is_not_flagged(self) -> None:
        assert not _location_name_is_generated_address(
            None, ["45 WELLS ST # 104"], "WESTERLY", "RI", "02891"
        )

    def test_no_address_is_not_flagged(self) -> None:
        # Nothing to compare against - can't confirm it's a generated
        # address, so leave whatever name is present alone.
        assert not _location_name_is_generated_address(
            "45 WELLS ST # 104-WESTERLY-RI-02891", None, "WESTERLY", "RI", "02891"
        )


class TestPracticeNameAddressFiltering:
    """extract()/normalize() end-to-end: a Location whose name is really
    just its own address must not flow into practice_name or
    primary_site_of_care - see _location_name_is_generated_address."""

    def _raw_record(self, location_name: str) -> dict[str, Any]:
        return {
            "practitioner": {
                "resourceType": "Practitioner",
                "id": "pract-1",
                "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": JANE_DOE_NPI}],
                "name": [{"family": "Doe", "given": ["Jane"]}],
            },
            "roles": [
                {
                    "resourceType": "PractitionerRole",
                    "id": "role-1",
                    "practitioner": {"reference": "Practitioner/pract-1"},
                    "location": [{"reference": "Location/loc-1"}],
                    "organization": {"reference": "Organization/org-1"},
                    "specialty": [],
                }
            ],
            "locations": {
                "loc-1": {
                    "resourceType": "Location",
                    "id": "loc-1",
                    "name": location_name,
                    "address": {
                        "line": ["45 WELLS ST # 104"],
                        "city": "WESTERLY",
                        "state": "RI",
                        "postalCode": "02891-2927",
                    },
                }
            },
            "organizations": {
                "org-1": {
                    "resourceType": "Organization",
                    "id": "org-1",
                    "name": "Westerly Family Medicine",
                }
            },
        }

    def test_generated_address_name_falls_back_to_organization_name(self, config: Config) -> None:
        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({}))
        raw = self._raw_record("45 WELLS ST # 104-WESTERLY-RI-02891")

        extracted = adapter.extract(raw)
        assert extracted["locations"]["loc-1"]["name"] is None

        normalized = adapter.normalize(extracted)
        assert normalized.provider.practice_name == "Westerly Family Medicine"
        assert normalized.provider.primary_site_of_care == "Westerly Family Medicine"

    def test_multi_line_generated_address_name_falls_back_to_organization_name(
        self, config: Config
    ) -> None:
        # Real example that slipped past an earlier, single-line-only
        # version of this filter - a second address line (unit number)
        # broke the exact-match comparison.
        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({}))
        raw = self._raw_record("70 KENYON AVE UNIT 326-# 326-WAKEFIELD-RI-02879")
        raw["locations"]["loc-1"]["address"] = {
            "line": ["70 KENYON AVE UNIT 326", "# 326"],
            "city": "WAKEFIELD",
            "state": "RI",
            "postalCode": "02879-4253",
        }

        extracted = adapter.extract(raw)
        assert extracted["locations"]["loc-1"]["name"] is None

        normalized = adapter.normalize(extracted)
        assert normalized.provider.practice_name == "Westerly Family Medicine"
        assert normalized.provider.primary_site_of_care == "Westerly Family Medicine"

    def test_real_facility_name_is_preserved(self, config: Config) -> None:
        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({}))
        raw = self._raw_record("Westerly Family Medicine Clinic")

        extracted = adapter.extract(raw)
        assert extracted["locations"]["loc-1"]["name"] == "Westerly Family Medicine Clinic"

        normalized = adapter.normalize(extracted)
        assert normalized.provider.practice_name == "Westerly Family Medicine Clinic"
        assert normalized.provider.primary_site_of_care == "Westerly Family Medicine Clinic"


class _StubDatasetAdapter:
    """Duck-typed ProviderDatasetAdapter for CLI completion-gate tests: no
    network, no records - just a canned DiscoveryStats whose health verdict
    the exit-code gate must honor."""

    name = "stub"
    source = "Stub"
    requires_profile_url = False

    #: Set by the test before cmd_ingest_providers runs.
    canned_stats = None

    def __init__(self, config: Config, checkpoint_store=None) -> None:  # type: ignore[no-untyped-def]
        self.config = config
        self.last_request_count = 0
        self.last_discovery_stats = type(self).canned_stats

    def discover(self):  # type: ignore[no-untyped-def]
        yield from []


class TestIngestProvidersCompletionGate:
    """cmd_ingest_providers must exit nonzero when the run is incomplete
    (silent-failure signature, failed batches, errors, or an aborting
    exception) - previously every run exited 0, including both live
    release-gate runs that printed the silent-failure banner."""

    def _run_cmd(self, config: Config, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        import argparse

        from providermap import cli as cli_module

        config.database.path = str(tmp_path / "gate.db")
        config.providers.checkpoint_path = None
        monkeypatch.setattr(
            cli_module, "get_provider_dataset_adapter_class", lambda name: _StubDatasetAdapter
        )
        args = argparse.Namespace(dry_run=True, provider_adapter="stub", discovery_mode=None)
        cli_module.cmd_ingest_providers(config, args)

    def test_clean_run_exits_zero(self, config: Config, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from adapters.providers.cigna_fhir.adapter import DiscoveryStats

        _StubDatasetAdapter.canned_stats = DiscoveryStats()
        # Returning without SystemExit IS the assertion.
        self._run_cmd(config, tmp_path, monkeypatch)

    def test_silent_failure_signature_exits_nonzero(
        self, config: Config, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        from adapters.providers.cigna_fhir.adapter import DiscoveryStats

        stats = DiscoveryStats()
        stats.bundles_with_total_zero_by_endpoint["PractitionerRole"] = 1
        _StubDatasetAdapter.canned_stats = stats
        with pytest.raises(SystemExit) as excinfo:
            self._run_cmd(config, tmp_path, monkeypatch)
        assert excinfo.value.code == 1

    def test_failed_batches_exit_nonzero(
        self, config: Config, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        from adapters.providers.cigna_fhir.adapter import DiscoveryStats

        stats = DiscoveryStats()
        stats.failed_batches.append(("7", "RuntimeError('boom')"))
        _StubDatasetAdapter.canned_stats = stats
        with pytest.raises(SystemExit) as excinfo:
            self._run_cmd(config, tmp_path, monkeypatch)
        assert excinfo.value.code == 1

    def test_recovered_stale_cursor_restart_still_exits_zero(
        self, config: Config, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The reclassification contract end to end: a stale-cursor restart
        that recovered is healthy - it must not fail the gate."""
        from adapters.providers.cigna_fhir.adapter import DiscoveryStats

        stats = DiscoveryStats()
        stats.stale_cursor_restarts = 1
        _StubDatasetAdapter.canned_stats = stats
        self._run_cmd(config, tmp_path, monkeypatch)  # no SystemExit
