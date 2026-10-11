"""Tests for adapters/providers/cigna_fhir/discovery.py - the
DiscoveryStrategy abstraction, StateScopedDiscovery's move-only extraction,
and NationalDiscovery (Phase E, Stages 1-2).

StateScopedDiscovery's actual fetch/checkpoint/concurrency behavior is
already exercised end-to-end through ``CignaFhirAdapter.discover()`` in
tests/test_cigna_fhir_adapter.py (nothing about its logic changed in the
move - only where it lives). This file covers what's new: window
generation, NationalDiscovery's spool-then-materialize flow, and the one
completeness property that distinguishes it from the state-scoped strategy.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adapters.providers.cigna_fhir.adapter import CignaFhirAdapter, _national_window_url
from adapters.providers.cigna_fhir.discovery import (
    DiscoveryStrategy,
    NationalDiscovery,
    StateScopedDiscovery,
    _generate_lastupdated_windows,
    get_discovery_strategy,
)
from adapters.providers.cigna_fhir.fixtures import (
    JANE_DOE_NPI,
    STATE_SCOPED_PRACTITIONER_NPI,
    make_fetcher,
)
from providermap.config import Config


class TestGetDiscoveryStrategy:
    def test_state_scoped_is_the_default_selectable_mode(self) -> None:
        assert isinstance(get_discovery_strategy("state_scoped"), StateScopedDiscovery)

    def test_national_is_selectable(self) -> None:
        assert isinstance(get_discovery_strategy("national"), NationalDiscovery)

    def test_unknown_mode_raises_with_available_names(self) -> None:
        with pytest.raises(ValueError, match="state_scoped"):
            get_discovery_strategy("bogus")

    def test_both_strategies_are_discovery_strategy_instances(self) -> None:
        assert isinstance(get_discovery_strategy("state_scoped"), DiscoveryStrategy)
        assert isinstance(get_discovery_strategy("national"), DiscoveryStrategy)


class TestGenerateLastUpdatedWindows:
    def test_first_window_is_open_on_the_low_end(self) -> None:
        windows = _generate_lastupdated_windows(30, 90, datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert windows[0][0] is None

    def test_last_window_is_open_on_the_high_end(self) -> None:
        windows = _generate_lastupdated_windows(30, 90, datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert windows[-1][1] is None

    def test_windows_are_contiguous(self) -> None:
        windows = _generate_lastupdated_windows(30, 90, datetime(2026, 1, 1, tzinfo=timezone.utc))
        for (_, le), (ge, _) in zip(windows, windows[1:], strict=False):
            assert le == ge

    def test_lookback_shorter_than_window_still_yields_at_least_two_windows(self) -> None:
        # 1 window_day, 1 lookback_day -> the open-start and open-end windows
        # must both exist even in this degenerate case.
        windows = _generate_lastupdated_windows(1, 1, datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert len(windows) >= 2
        assert windows[0][0] is None
        assert windows[-1][1] is None

    def test_rejects_non_positive_window_days(self) -> None:
        with pytest.raises(ValueError, match="window_days"):
            _generate_lastupdated_windows(0, 90, datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_rejects_non_positive_lookback_days(self) -> None:
        with pytest.raises(ValueError, match="lookback_days"):
            _generate_lastupdated_windows(30, 0, datetime(2026, 1, 1, tzinfo=timezone.utc))


def _configure_national(config: Config, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Set every national-mode config field - callers must do this *before*
    computing window URLs (via :func:`_window_urls`) or constructing the
    adapter, since both read these same fields and must agree."""
    config.providers.cigna_fhir_discovery_mode = "national"
    config.providers.resource_spool_path = str(tmp_path / "spool.db")
    config.providers.cigna_fhir_window_days = 30
    config.providers.cigna_fhir_lookback_days = 90


def _national_adapter(config: Config, tmp_path, pages: dict) -> CignaFhirAdapter:  # type: ignore[no-untyped-def,type-arg]
    _configure_national(config, tmp_path)
    return CignaFhirAdapter(config, page_fetcher=make_fetcher(pages))


def _window_urls(config: Config) -> list[str]:
    pc = config.providers
    windows = _generate_lastupdated_windows(
        pc.cigna_fhir_window_days, pc.cigna_fhir_lookback_days, datetime.now(timezone.utc)
    )
    return [
        _national_window_url(pc.cigna_fhir_base_url, ge, le, pc.cigna_fhir_page_size)
        for ge, le in windows
    ]


# A practitioner whose role references locations in two different states -
# StateScopedDiscovery (fed the same shape of data, but scoped to one state)
# would only ever see the in-state one; National must see both, since it
# never filters by state at all.
_MULTI_STATE_BUNDLE = {
    "resourceType": "Bundle",
    "type": "searchset",
    "link": [],
    "entry": [
        {
            "resource": {
                "resourceType": "Practitioner",
                "id": "practitioner-multi",
                "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": JANE_DOE_NPI}],
                "name": [{"family": "Multi", "given": ["Prac"]}],
            }
        },
        {
            "resource": {
                "resourceType": "PractitionerRole",
                "id": "role-multi",
                "practitioner": {"reference": "Practitioner/practitioner-multi"},
                "location": [
                    {"reference": "Location/loc-ri"},
                    {"reference": "Location/loc-tx"},
                ],
                "organization": {"reference": "Organization/org-multi"},
            }
        },
        {
            "resource": {
                "resourceType": "Location",
                "id": "loc-ri",
                "name": "RI Site",
                "address": {
                    "line": ["1 Main St"],
                    "city": "Providence",
                    "state": "RI",
                    "postalCode": "02903",
                },
            }
        },
        {
            "resource": {
                "resourceType": "Location",
                "id": "loc-tx",
                "name": "TX Site",
                "address": {
                    "line": ["2 Main St"],
                    "city": "Austin",
                    "state": "TX",
                    "postalCode": "73301",
                },
            }
        },
        {
            "resource": {
                "resourceType": "Organization",
                "id": "org-multi",
                "name": "Multi State Group",
            }
        },
    ],
}


class TestNationalDiscoveryEndToEnd:
    def test_raises_without_a_resource_spool(self, config: Config) -> None:
        config.providers.cigna_fhir_discovery_mode = "national"
        # resource_spool_path left unset - adapter.resource_spool stays None.
        adapter = CignaFhirAdapter(config, page_fetcher=make_fetcher({}))
        with pytest.raises(ValueError, match="resource_spool"):
            list(adapter.discover())

    def test_yields_bare_id_keyed_dict_shape(self, config: Config, tmp_path) -> None:  # type: ignore[no-untyped-def]
        _configure_national(config, tmp_path)
        urls = _window_urls(config)
        pages = dict.fromkeys(
            urls, {"resourceType": "Bundle", "type": "searchset", "link": [], "entry": []}
        )
        # The multi-state bundle only needs to answer ONE window's URL; the
        # rest can be empty (they still get fetched, just contribute nothing).
        pages[urls[1]] = _MULTI_STATE_BUNDLE
        adapter = _national_adapter(config, tmp_path, pages)

        records = list(adapter.discover())
        assert len(records) == 1
        record = records[0]
        assert record["practitioner"]["id"] == "practitioner-multi"
        assert {r["id"] for r in record["roles"]} == {"role-multi"}
        # Bare ids, no "Location/" prefix - the mandated contract (see
        # ProviderDatasetAdapter.discover()'s docstring and extract()'s
        # dependence on this exact key shape).
        assert set(record["locations"]) == {"loc-ri", "loc-tx"}
        assert set(record["organizations"]) == {"org-multi"}

    def test_captures_locations_across_states_never_truncating(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """The completeness property that distinguishes National from
        StateScopedDiscovery: both of this practitioner's locations - one
        RI, one TX - are present. A state-scoped run limited to either state
        would drop the other (see NATIONAL_DISCOVERY_DESIGN.md Risk R1)."""
        _configure_national(config, tmp_path)
        urls = _window_urls(config)
        pages = dict.fromkeys(
            urls, {"resourceType": "Bundle", "type": "searchset", "link": [], "entry": []}
        )
        pages[urls[1]] = _MULTI_STATE_BUNDLE
        adapter = _national_adapter(config, tmp_path, pages)

        raw = next(adapter.discover())
        assert raw["locations"]["loc-ri"]["address"]["state"] == "RI"
        assert raw["locations"]["loc-tx"]["address"]["state"] == "TX"

    def test_extract_and_normalize_accept_national_output_unchanged(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """extract()/normalize() are strategy-agnostic - this is the direct
        proof the shared seam holds for National's yielded dicts, not just
        StateScopedDiscovery's."""
        _configure_national(config, tmp_path)
        urls = _window_urls(config)
        pages = dict.fromkeys(
            urls, {"resourceType": "Bundle", "type": "searchset", "link": [], "entry": []}
        )
        pages[urls[1]] = _MULTI_STATE_BUNDLE
        adapter = _national_adapter(config, tmp_path, pages)

        raw = next(adapter.discover())
        normalized = adapter.normalize(adapter.extract(raw))
        assert normalized.provider.npi == JANE_DOE_NPI
        assert len(normalized.locations) == 2
        assert len(normalized.organizations) == 1

    def test_duplicate_resources_across_windows_are_deduplicated_in_spool(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        _configure_national(config, tmp_path)
        urls = _window_urls(config)
        shared_org = {"resourceType": "Organization", "id": "org-shared", "name": "Shared"}
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
                {"resource": shared_org},  # same org, second window
            ],
        }
        pages = dict.fromkeys(
            urls, {"resourceType": "Bundle", "type": "searchset", "link": [], "entry": []}
        )
        pages[urls[0]] = batch_a
        pages[urls[1]] = batch_b
        adapter = _national_adapter(config, tmp_path, pages)

        records = list(adapter.discover())
        assert {r["practitioner"]["id"] for r in records} == {"p-a", "p-b"}
        stats = adapter.last_discovery_stats
        assert stats.duplicate_resources_by_type.get("Organization") == 1


class TestNationalDiscoveryCheckpointResume:
    def test_failed_window_does_not_abort_and_is_retried_on_resume(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        from providermap.checkpoint import CheckpointStore

        _configure_national(config, tmp_path)
        urls = _window_urls(config)
        empty = {"resourceType": "Bundle", "type": "searchset", "link": [], "entry": []}
        pages_ok = dict.fromkeys(urls, empty)
        pages_ok[urls[1]] = _MULTI_STATE_BUNDLE

        checkpoint_path = tmp_path / "checkpoints.db"

        def failing_fetch(url: str):  # type: ignore[no-untyped-def]
            if url == urls[1]:
                raise RuntimeError("simulated failure")
            return pages_ok[url]

        cp1 = CheckpointStore(checkpoint_path)
        adapter1 = _national_adapter(config, tmp_path, {})
        adapter1.checkpoint_store = cp1
        adapter1._page_fetcher = failing_fetch  # type: ignore[method-assign]
        records1 = list(adapter1.discover())
        assert records1 == []  # the only informative window failed
        assert adapter1.last_discovery_stats.failed_batches
        cp1.close()

        # Resume: same checkpoint + spool files, this time the window succeeds.
        cp2 = CheckpointStore(checkpoint_path)
        adapter2 = _national_adapter(config, tmp_path, pages_ok)
        adapter2.checkpoint_store = cp2
        records2 = list(adapter2.discover())
        assert len(records2) == 1
        assert records2[0]["practitioner"]["id"] == "practitioner-multi"
        cp2.close()
