"""Tests for the HIFLD bulk hospital website lookup (ROADMAP.md Phase 3).
Only pure parsing/matching (`_build_hospital_index`) is unit tested;
`fetch_hifld_hospital_index` itself does real network I/O and is exercised
only via manual/live verification, matching how
`fetch_wikidata_hospital_websites` is treated.
"""

from __future__ import annotations

from providermap.hifld_hospitals import HifldHospitalIndex, _build_hospital_index, _clean


def _row(name=None, website=None, alt_name=None, state=None) -> dict:
    return {"NAME": name, "WEBSITE": website, "ALT_NAME": alt_name, "STATE": state}


class TestClean:
    def test_strips_and_passes_through_a_real_value(self) -> None:
        assert _clean("  https://example.com  ") == "https://example.com"

    def test_not_available_placeholder_becomes_none(self) -> None:
        assert _clean("NOT AVAILABLE") == None  # noqa: E711
        assert _clean("not available") is None
        assert _clean("  Not Available  ") is None

    def test_other_placeholder_spellings_become_none(self) -> None:
        for placeholder in ("N/A", "NA", "NONE", "-", "", "   ", "No Website"):
            assert _clean(placeholder) is None

    def test_non_string_becomes_none(self) -> None:
        assert _clean(None) is None
        assert _clean(-999) is None


class TestBuildHospitalIndex:
    def test_basic_match_resolves(self) -> None:
        rows = [_row("Andalusia Health", "http://www.andalusiaregionalhospital.com", state="AL")]
        index = _build_hospital_index(rows)
        assert index.by_name["andalusia health"] == "http://www.andalusiaregionalhospital.com"
        assert index.by_name_state[("andalusia health", "AL")] == (
            "http://www.andalusiaregionalhospital.com"
        )

    def test_not_available_website_is_skipped_entirely(self) -> None:
        rows = [_row("Some Hospital", "NOT AVAILABLE", state="TX")]
        index = _build_hospital_index(rows)
        assert index == HifldHospitalIndex()

    def test_not_available_alt_name_does_not_create_a_bogus_key(self) -> None:
        rows = [_row("Real Hospital", "https://real.example", alt_name="NOT AVAILABLE", state="TX")]
        index = _build_hospital_index(rows)
        assert "not available" not in index.by_name
        assert index.by_name["real hospital"] == "https://real.example"

    def test_alt_name_match_resolves(self) -> None:
        rows = [
            _row(
                "Massachusetts General Hospital",
                "https://www.massgeneral.org/",
                alt_name="Mass General",
                state="MA",
            )
        ]
        index = _build_hospital_index(rows)
        assert index.by_name["mass general"] == "https://www.massgeneral.org/"
        assert index.by_name["massachusetts general hospital"] == "https://www.massgeneral.org/"

    def test_state_aware_index_disambiguates_same_name_different_states(self) -> None:
        """Confirmed live: HIFLD has the same same-name-different-hospital
        collision risk Wikidata does."""
        rows = [
            _row("Community Hospital", "https://a.example", state="OH"),
            _row("Community Hospital", "https://b.example", state="TX"),
        ]
        index = _build_hospital_index(rows)
        assert "community hospital" not in index.by_name
        assert index.by_name_state[("community hospital", "OH")] == "https://a.example"
        assert index.by_name_state[("community hospital", "TX")] == "https://b.example"

    def test_ambiguous_name_without_state_data_is_excluded_from_by_name(self) -> None:
        rows = [
            _row("Generic Medical Center", "https://a.example"),
            _row("Generic Medical Center", "https://b.example"),
        ]
        index = _build_hospital_index(rows)
        assert "generic medical center" not in index.by_name

    def test_row_missing_name_is_skipped(self) -> None:
        rows = [_row(None, "https://a.example", state="TX")]
        index = _build_hospital_index(rows)
        assert index == HifldHospitalIndex()

    def test_malformed_rows_are_skipped_not_raised_on(self) -> None:
        assert _build_hospital_index([]) == HifldHospitalIndex()
        assert _build_hospital_index(["not a dict"]) == HifldHospitalIndex()  # type: ignore[list-item]
        assert _build_hospital_index([{}]) == HifldHospitalIndex()
