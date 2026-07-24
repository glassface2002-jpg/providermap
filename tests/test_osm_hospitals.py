"""Tests for the OSM bulk hospital website lookup (ROADMAP.md Phase 6.1).
Only pure parsing/matching (`_build_osm_index`) and the cache-loading
graceful-skip behavior are unit tested; `extract_hospital_records` (pyosmium
PBF parsing) and `refresh_osm_cache`/`download_pbf` do real, heavy I/O and
are exercised only via manual/live verification - the same "exercised only
via manual/live verification" treatment this project already gives every
other network/bulk-parse function (`fetch_hifld_hospital_index`,
`fetch_va_facility_index`, ...).
"""

from __future__ import annotations

import json

from providermap.osm_hospitals import (
    OsmHospitalIndex,
    _build_osm_index,
    _clean,
    fetch_osm_hospital_index,
)


def _record(name=None, website=None, alt_name=None, official_name=None, state=None) -> dict:
    return {
        "name": name,
        "website": website,
        "alt_name": alt_name,
        "official_name": official_name,
        "state": state,
    }


class TestClean:
    def test_strips_and_passes_through_a_real_value(self) -> None:
        assert _clean("  https://example.com  ") == "https://example.com"

    def test_placeholder_spellings_become_none(self) -> None:
        for placeholder in ("", "N/A", "na", "NONE", "-", "   "):
            assert _clean(placeholder) is None

    def test_non_string_becomes_none(self) -> None:
        assert _clean(None) is None
        assert _clean(-999) is None


class TestBuildOsmIndex:
    def test_basic_match_resolves(self) -> None:
        records = [
            _record(
                "White River Junction VA Medical Center",
                "https://www.va.gov/white-river-junction-health-care/",
                state="VT",
            )
        ]
        index = _build_osm_index(records)
        key = "white river junction va medical center"
        assert index.by_name[key] == "https://www.va.gov/white-river-junction-health-care/"
        assert index.by_name_state[(key, "VT")] == (
            "https://www.va.gov/white-river-junction-health-care/"
        )

    def test_missing_website_is_skipped(self) -> None:
        records = [_record("Some Hospital", None, state="TX")]
        assert _build_osm_index(records) == OsmHospitalIndex()

    def test_missing_name_is_skipped(self) -> None:
        records = [_record(None, "https://a.example", state="TX")]
        assert _build_osm_index(records) == OsmHospitalIndex()

    def test_alt_name_and_official_name_both_resolve(self) -> None:
        records = [
            _record(
                "Thorek Memorial Hospital",
                "https://www.thorek.org/",
                alt_name="Thorek Hospital",
                official_name="Thorek Memorial Hospital and Medical Center",
                state="IL",
            )
        ]
        index = _build_osm_index(records)
        assert index.by_name["thorek memorial hospital"] == "https://www.thorek.org/"
        assert index.by_name["thorek hospital"] == "https://www.thorek.org/"
        assert index.by_name["thorek memorial hospital and medical center"] == (
            "https://www.thorek.org/"
        )

    def test_state_aware_index_disambiguates_same_name_different_states(self) -> None:
        records = [
            _record("Community Hospital", "https://a.example", state="OH"),
            _record("Community Hospital", "https://b.example", state="TX"),
        ]
        index = _build_osm_index(records)
        assert "community hospital" not in index.by_name
        assert index.by_name_state[("community hospital", "OH")] == "https://a.example"
        assert index.by_name_state[("community hospital", "TX")] == "https://b.example"

    def test_ambiguous_name_without_state_data_is_excluded_from_by_name(self) -> None:
        records = [
            _record("Generic Medical Center", "https://a.example"),
            _record("Generic Medical Center", "https://b.example"),
        ]
        index = _build_osm_index(records)
        assert "generic medical center" not in index.by_name

    def test_malformed_records_are_skipped_not_raised_on(self) -> None:
        assert _build_osm_index([]) == OsmHospitalIndex()
        assert _build_osm_index(["not a dict"]) == OsmHospitalIndex()  # type: ignore[list-item]
        assert _build_osm_index([{}]) == OsmHospitalIndex()


class TestFetchOsmHospitalIndexGracefulSkip:
    def test_none_cache_path_returns_empty_index_without_touching_disk(self) -> None:
        assert fetch_osm_hospital_index(None) == OsmHospitalIndex()

    def test_missing_cache_file_returns_empty_index(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        missing = tmp_path / "does-not-exist.json"
        assert fetch_osm_hospital_index(str(missing)) == OsmHospitalIndex()

    def test_malformed_cache_file_returns_empty_index_not_an_exception(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        bad = tmp_path / "bad.json"
        bad.write_text("not valid json{", encoding="utf-8")
        assert fetch_osm_hospital_index(str(bad)) == OsmHospitalIndex()

    def test_valid_cache_file_loads_and_builds_the_index(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        cache = tmp_path / "osm_cache.json"
        cache.write_text(
            json.dumps([_record("Thorek Memorial Hospital", "https://www.thorek.org/", state="IL")]),
            encoding="utf-8",
        )
        index = fetch_osm_hospital_index(str(cache))
        assert index.by_name["thorek memorial hospital"] == "https://www.thorek.org/"
