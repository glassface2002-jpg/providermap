"""Tests for the VA Facilities API bulk hospital website lookup (ROADMAP.md
Phase 5.4). Only pure parsing/matching (`_build_va_index`) is unit tested;
`fetch_va_facility_index` itself does real network I/O and is exercised only
via manual/live verification (Phase 5.4.0's validation queries), matching
how `fetch_hifld_hospital_index`/`fetch_wikidata_hospital_websites` are
treated. The one exception is the no-op no-api-key guard, which is safe and
useful to test directly since it returns before any network call.
"""

from __future__ import annotations

from providermap.va_facilities import VaFacilityIndex, _build_va_index, fetch_va_facility_index


def _row(name=None, website=None, classification="VA Medical Center (VAMC)", state=None) -> dict:
    return {
        "name": name,
        "website": website,
        "classification": classification,
        "address": {"physical": {"state": state}} if state else {"physical": {}},
    }


class TestBuildVaIndex:
    def test_basic_match_resolves(self) -> None:
        rows = [
            _row(
                "White River Junction VA Medical Center",
                "https://www.va.gov/white-river-junction-health-care/locations/"
                "white-river-junction-va-medical-center/",
                state="VT",
            )
        ]
        index = _build_va_index(rows)
        expected = (
            "https://www.va.gov/white-river-junction-health-care/locations/"
            "white-river-junction-va-medical-center/"
        )
        assert index.by_name["white river junction va medical center"] == expected
        assert index.by_name_state[("white river junction va medical center", "VT")] == expected

    def test_non_vamc_classification_is_excluded(self) -> None:
        """The API mixes VA Medical Centers with cemeteries, Vet Centers,
        and regional benefit offices under one endpoint - only VAMC rows
        are hospital-equivalent and relevant here."""
        rows = [
            _row("Some National Cemetery", "https://cem.va.gov/x", classification="Soldiers Lot"),
            _row("Some Vet Center", "https://va.gov/vet-center", classification=None),
        ]
        index = _build_va_index(rows)
        assert index == VaFacilityIndex()

    def test_missing_website_is_skipped(self) -> None:
        rows = [_row("Some VAMC", None, state="TX")]
        index = _build_va_index(rows)
        assert index == VaFacilityIndex()

    def test_missing_name_is_skipped(self) -> None:
        rows = [_row(None, "https://a.example", state="TX")]
        index = _build_va_index(rows)
        assert index == VaFacilityIndex()

    def test_state_aware_index_disambiguates_same_name_different_states(self) -> None:
        rows = [
            _row("Community VA Medical Center", "https://a.example", state="OH"),
            _row("Community VA Medical Center", "https://b.example", state="TX"),
        ]
        index = _build_va_index(rows)
        assert "community va medical center" not in index.by_name
        assert index.by_name_state[("community va medical center", "OH")] == "https://a.example"
        assert index.by_name_state[("community va medical center", "TX")] == "https://b.example"

    def test_ambiguous_name_without_state_data_is_excluded_from_by_name(self) -> None:
        rows = [
            _row("Generic VA Medical Center", "https://a.example"),
            _row("Generic VA Medical Center", "https://b.example"),
        ]
        index = _build_va_index(rows)
        assert "generic va medical center" not in index.by_name

    def test_malformed_rows_are_skipped_not_raised_on(self) -> None:
        assert _build_va_index([]) == VaFacilityIndex()
        assert _build_va_index(["not a dict"]) == VaFacilityIndex()  # type: ignore[list-item]
        assert _build_va_index([{}]) == VaFacilityIndex()
        assert _build_va_index([{"address": "not a dict"}]) == VaFacilityIndex()


class TestFetchVaFacilityIndexNoKeyGuard:
    def test_no_api_key_returns_empty_index_without_a_network_call(self) -> None:
        result = fetch_va_facility_index(
            None, "https://sandbox-api.va.gov/services/va_facilities/v1", "test-agent"
        )
        assert result == VaFacilityIndex()
