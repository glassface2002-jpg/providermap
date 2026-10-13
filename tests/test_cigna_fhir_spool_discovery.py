"""Tests for spool-backed state-scoped discovery
(``config.providers.cigna_fhir_state_scoped_spool``) - the flag-gated
memory fix for the measured 6.07 GB peak RSS on the smallest state tested.

The legacy in-memory path is untouched by this flag and is protected by
the existing suite (``test_cigna_fhir_adapter.py``,
``test_cigna_fhir_discovery.py``); these tests only exercise the NEW
spool-mode branch (``StateScopedDiscovery._run_spool``) and its
equivalence with the legacy path.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from adapters.providers.cigna_fhir.adapter import CignaFhirAdapter
from adapters.providers.cigna_fhir.comparison import compare_normalized_providers
from adapters.providers.cigna_fhir.fixtures import (
    STATE_LOCATION_BUNDLE,
    STATE_ROLE_BUNDLE,
    STATE_SCOPED_PRACTITIONER_NPI,
    make_fetcher,
)
from providermap.checkpoint import CheckpointStore
from providermap.config import Config


def _location_url(config: Config) -> str:
    from adapters.providers.cigna_fhir.adapter import _location_search_url

    pc = config.providers
    return _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)


def _role_urls(config: Config) -> tuple[str, str]:
    from adapters.providers.cigna_fhir.adapter import _practitioner_role_by_location_url

    pc = config.providers
    return (
        _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-a", pc.cigna_fhir_page_size
        ),
        _practitioner_role_by_location_url(
            pc.cigna_fhir_base_url, "loc-b", pc.cigna_fhir_page_size
        ),
    )


def _spool_config(config: Config, tmp_path: Path) -> Config:
    config.providers.cigna_fhir_state = "RI"
    config.providers.cigna_fhir_state_scoped_spool = True
    config.providers.resource_spool_path = str(tmp_path / "spool.db")
    return config


class TestSpoolModeRequiresPath:
    def test_enabling_without_spool_path_raises(self, config: Config) -> None:
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_state_scoped_spool = True
        # resource_spool_path left at its default null - the adapter never
        # builds a spool, so run() must raise rather than silently fall
        # back to the unbounded in-memory path this flag exists to avoid.
        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({}))
        with pytest.raises(ValueError, match="resource_spool_path"):
            list(adapter.discover())


class TestSpoolModeEquivalence:
    """The whole migration gate: spool mode and the legacy in-memory path
    must produce IDENTICAL canonical output over the same fixture data -
    same NPIs, same identity fields, same relationships, same primary
    location. See comparison.compare_normalized_providers."""

    def _normalized_by_npi(self, adapter: CignaFhirAdapter) -> dict:  # type: ignore[type-arg]
        result = {}
        for raw in adapter.discover():
            normalized = adapter.normalize(adapter.extract(raw))
            result[normalized.provider.npi] = normalized
        return result

    def test_spool_and_legacy_paths_agree(self, config: Config, tmp_path: Path) -> None:
        location_url = _location_url(config)
        role_url_a, role_url_b = _role_urls(config)
        pages = {
            location_url: STATE_LOCATION_BUNDLE,
            role_url_a: STATE_ROLE_BUNDLE,
            role_url_b: {"resourceType": "Bundle", "type": "searchset", "link": [], "entry": []},
        }

        legacy_config = copy.deepcopy(config)
        legacy_config.providers.cigna_fhir_state = "RI"
        legacy_adapter = CignaFhirAdapter(legacy_config, page_fetcher=make_fetcher(pages))
        legacy_by_npi = self._normalized_by_npi(legacy_adapter)

        spool_config = _spool_config(config, tmp_path)
        spool_adapter = CignaFhirAdapter(spool_config, page_fetcher=make_fetcher(pages))
        spool_by_npi = self._normalized_by_npi(spool_adapter)

        comparison = compare_normalized_providers(legacy_by_npi, spool_by_npi)
        assert comparison.is_clean
        assert set(legacy_by_npi) == set(spool_by_npi) == {STATE_SCOPED_PRACTITIONER_NPI}

        legacy_provider = legacy_by_npi[STATE_SCOPED_PRACTITIONER_NPI].provider
        spool_provider = spool_by_npi[STATE_SCOPED_PRACTITIONER_NPI].provider
        assert spool_provider.address == legacy_provider.address
        assert spool_provider.location_count == legacy_provider.location_count

    def test_spool_mode_stats_match_legacy_after_dedup(
        self, config: Config, tmp_path: Path
    ) -> None:
        """Post-WS1, both paths iterate DISTINCT roles - stats must
        converge (this would NOT have held before roles were deduplicated,
        since legacy counted occurrences and spool mode always counted
        distinct rows)."""
        location_url = _location_url(config)
        role_url_a, role_url_b = _role_urls(config)
        pages = {
            location_url: STATE_LOCATION_BUNDLE,
            role_url_a: STATE_ROLE_BUNDLE,
            role_url_b: {"resourceType": "Bundle", "type": "searchset", "link": [], "entry": []},
        }
        spool_config = _spool_config(config, tmp_path)
        spool_adapter = CignaFhirAdapter(spool_config, page_fetcher=make_fetcher(pages))
        list(spool_adapter.discover())
        stats = spool_adapter.last_discovery_stats
        assert stats.role_queries_attempted == 2  # loc-a, loc-b
        assert stats.role_queries_empty == 1  # loc-b
        assert stats.providers_yielded == 1
        assert (
            stats.provider_location_relationships_discovered == 1
        )  # the role references loc-a only


class TestSpoolModeResumability:
    def test_resume_skips_done_batches_no_replay(self, config: Config, tmp_path: Path) -> None:
        location_url = _location_url(config)
        role_url_a, role_url_b = _role_urls(config)
        spool_config = _spool_config(config, tmp_path)
        checkpoint_path = tmp_path / "checkpoints.db"

        # Run 1: loc-b's batch fails.
        cp1 = CheckpointStore(checkpoint_path)
        calls1: dict[str, int] = {}

        def fetch1(url: str) -> dict:  # type: ignore[type-arg]
            calls1[url] = calls1.get(url, 0) + 1
            if url == role_url_b:
                raise RuntimeError("simulated transient failure")
            return {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
            }[url]

        adapter1 = CignaFhirAdapter(spool_config, page_fetcher=fetch1, checkpoint_store=cp1)
        records1 = list(adapter1.discover())
        assert {r["practitioner"]["id"] for r in records1} == {"practitioner-ri"}
        checkpoint_id = "cigna_fhir:RI:spool"
        assert cp1.status(checkpoint_id, "role_by_location_spool", "loc-a") == "done"
        assert cp1.status(checkpoint_id, "role_by_location_spool", "loc-b") == "error"
        assert cp1.is_complete(checkpoint_id) is False
        cp1.close()

        # Run 2: loc-b now succeeds - loc-a must NOT be re-fetched (its
        # data is already durably in the spool).
        cp2 = CheckpointStore(checkpoint_path)
        calls2: dict[str, int] = {}

        def fetch2(url: str) -> dict:  # type: ignore[type-arg]
            calls2[url] = calls2.get(url, 0) + 1
            return {
                location_url: STATE_LOCATION_BUNDLE,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "link": [],
                    "entry": [],
                },
            }[url]

        adapter2 = CignaFhirAdapter(spool_config, page_fetcher=fetch2, checkpoint_store=cp2)
        records2 = list(adapter2.discover())
        assert location_url not in calls2  # location_enumeration_spool already done
        assert role_url_a not in calls2  # loc-a already done, no replay needed
        assert calls2[role_url_b] == 1  # only the errored batch re-fetched
        assert {r["practitioner"]["id"] for r in records2} == {"practitioner-ri"}
        assert cp2.is_complete(checkpoint_id) is True
        cp2.close()

    def test_stale_cursor_in_spool_mode_clears_spool_and_restarts(
        self, config: Config, tmp_path: Path
    ) -> None:
        from providermap.resource_spool import ResourceSpool

        pc = config.providers
        pc.cigna_fhir_page_size = 1
        page1_url = _location_url(config)
        page2_url = f"{pc.cigna_fhir_base_url}/Location?page=2"
        role_url_a, role_url_b = _role_urls(config)
        page1 = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [{"relation": "next", "url": page2_url}],
            "entry": [STATE_LOCATION_BUNDLE["entry"][0]],  # loc-a
        }
        page2 = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": [],
            "entry": [STATE_LOCATION_BUNDLE["entry"][1]],  # loc-b
        }
        total_zero_bundle = {"resourceType": "Bundle", "type": "searchset", "link": [], "total": 0}
        spool_config = _spool_config(config, tmp_path)
        spool_config.retries.max_attempts = 1  # exhaust immediately, zero sleeps
        checkpoint_path = tmp_path / "checkpoints.db"

        cp1 = CheckpointStore(checkpoint_path)

        def fetch1(url: str) -> dict:  # type: ignore[type-arg]
            if url == page2_url:
                raise RuntimeError("simulated crash mid-walk")
            return page1

        adapter1 = CignaFhirAdapter(spool_config, page_fetcher=fetch1, checkpoint_store=cp1)
        with pytest.raises(RuntimeError):
            list(adapter1.discover())
        cp1.close()

        # Run 2: the saved cursor (page2_url) is stale on first hit only.
        cp2 = CheckpointStore(checkpoint_path)
        calls2: dict[str, int] = {}

        def fetch2(url: str) -> dict:  # type: ignore[type-arg]
            calls2[url] = calls2.get(url, 0) + 1
            if url == page2_url and calls2[url] == 1:
                return total_zero_bundle
            return {
                page1_url: page1,
                page2_url: page2,
                role_url_a: STATE_ROLE_BUNDLE,
                role_url_b: {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "link": [],
                    "entry": [],
                },
            }[url]

        adapter2 = CignaFhirAdapter(spool_config, page_fetcher=fetch2, checkpoint_store=cp2)
        records2 = list(adapter2.discover())
        stats2 = adapter2.last_discovery_stats

        assert calls2[page1_url] >= 1  # restart re-walked from page 1
        assert calls2[page2_url] == 2  # stale attempt + the restart's real fetch
        assert {r["practitioner"]["id"] for r in records2} == {"practitioner-ri"}
        assert stats2.stale_cursor_restarts == 1
        assert stats2.suspected_silent_failure is False  # reclassified, not flagged
        assert cp2.is_complete("cigna_fhir:RI:spool") is True

        spool = ResourceSpool(spool_config.providers.resource_spool_path)
        assert spool.get("Location", "loc-a") is not None
        assert spool.get("Location", "loc-b") is not None
        spool.close()
        cp2.close()


class TestSpoolClearOnFreshStart:
    def test_fresh_run_does_not_inherit_a_previous_runs_spool_rows(
        self, config: Config, tmp_path: Path
    ) -> None:
        """A spool file can persist rows from an earlier, unrelated run
        (by design, so RESUMES can reuse it) - but a genuinely FRESH start
        must not inherit them, or stale/foreign resources would leak into
        this run's materialize pass."""
        from providermap.resource_spool import ResourceSpool

        spool_path = tmp_path / "spool.db"
        spool = ResourceSpool(str(spool_path))
        spool.upsert(
            "Practitioner",
            "foreign-practitioner",
            {"resourceType": "Practitioner", "id": "foreign-practitioner"},
        )
        spool.upsert(
            "PractitionerRole",
            "foreign-role",
            {"resourceType": "PractitionerRole", "id": "foreign-role"},
            practitioner_ref="foreign-practitioner",
        )
        spool.close()

        location_url = _location_url(config)
        role_url_a, role_url_b = _role_urls(config)
        pages = {
            location_url: STATE_LOCATION_BUNDLE,
            role_url_a: STATE_ROLE_BUNDLE,
            role_url_b: {"resourceType": "Bundle", "type": "searchset", "link": [], "entry": []},
        }
        config.providers.cigna_fhir_state = "RI"
        config.providers.cigna_fhir_state_scoped_spool = True
        config.providers.resource_spool_path = str(spool_path)
        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher(pages))
        records = list(adapter.discover())  # no checkpoint store -> always a fresh start

        assert {r["practitioner"]["id"] for r in records} == {"practitioner-ri"}
        # The foreign practitioner must be gone, not silently yielded too.
        assert "foreign-practitioner" not in {r["practitioner"]["id"] for r in records}
