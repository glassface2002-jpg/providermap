"""Tests for the Wikidata bulk hospital website lookup (ROADMAP.md stage 4
follow-on, plus the Phase 1 alias/state-matching improvement). Only pure
parsing functions (`_parse_sparql_response`, `_build_hospital_index`) are
unit tested; `fetch_wikidata_hospital_websites` itself does real network I/O
and is exercised only via manual/live verification, matching how
`http_domain_checker` in website_enrichment.py is treated.
"""

from __future__ import annotations

from providermap.wikidata_hospitals import (
    WikidataHospitalIndex,
    _build_hospital_index,
    _parse_sparql_response,
)


def _binding(label: str | None, website: str | None) -> dict:
    row: dict = {}
    if label is not None:
        row["hospitalLabel"] = {"xml:lang": "en", "type": "literal", "value": label}
    if website is not None:
        row["website"] = {"type": "uri", "value": website}
    return row


def _full_binding(
    label: str | None,
    website: str | None,
    alt: str | None = None,
    state: str | None = None,
) -> dict:
    row = _binding(label, website)
    if alt is not None:
        row["altLabel"] = {"xml:lang": "en", "type": "literal", "value": alt}
    if state is not None:
        row["stateLabel"] = {"xml:lang": "en", "type": "literal", "value": state}
    return row


class TestParseSparqlResponse:
    def test_extracts_normalized_name_to_website_mapping(self) -> None:
        data = {
            "head": {"vars": ["hospitalLabel", "website"]},
            "results": {
                "bindings": [
                    _binding("Banner Desert Medical Center", "https://bannerhealth.com/desert"),
                ]
            },
        }
        result = _parse_sparql_response(data)
        assert result == {"banner desert medical center": "https://bannerhealth.com/desert"}

    def test_multiple_rows(self) -> None:
        data = {
            "results": {
                "bindings": [
                    _binding("Hospital A", "https://a.example"),
                    _binding("Hospital B", "https://b.example"),
                ]
            }
        }
        result = _parse_sparql_response(data)
        assert result == {
            "hospital a": "https://a.example",
            "hospital b": "https://b.example",
        }

    def test_row_missing_label_is_skipped(self) -> None:
        data = {"results": {"bindings": [_binding(None, "https://a.example")]}}
        assert _parse_sparql_response(data) == {}

    def test_row_missing_website_is_skipped(self) -> None:
        data = {"results": {"bindings": [_binding("Hospital A", None)]}}
        assert _parse_sparql_response(data) == {}

    def test_empty_bindings_returns_empty_dict(self) -> None:
        assert _parse_sparql_response({"results": {"bindings": []}}) == {}

    def test_missing_results_key_returns_empty_dict_not_an_exception(self) -> None:
        assert _parse_sparql_response({}) == {}

    def test_malformed_shape_returns_empty_dict_not_an_exception(self) -> None:
        assert _parse_sparql_response({"results": "not a dict"}) == {}
        assert _parse_sparql_response({"results": {"bindings": "not a list"}}) == {}
        assert _parse_sparql_response({"results": {"bindings": ["not a dict"]}}) == {}


class TestBuildHospitalIndex:
    """The Phase 1 alias/state-aware index - see WikidataHospitalIndex's
    docstring for the ambiguity-exclusion rules this guards."""

    def test_existing_exact_primary_label_match_still_resolves(self) -> None:
        """The original, simplest case must keep working unchanged."""
        data = {
            "results": {
                "bindings": [
                    _full_binding("Banner Desert Medical Center", "https://bannerhealth.com/desert"),
                ]
            }
        }
        index = _build_hospital_index(data)
        assert index.by_name["banner desert medical center"] == "https://bannerhealth.com/desert"

    def test_alias_only_match_resolves_via_by_name(self) -> None:
        """A CMS-style legal name that only matches Wikidata's altLabel (not
        its chosen primary label) must still resolve."""
        data = {
            "results": {
                "bindings": [
                    _full_binding(
                        "Massachusetts General Hospital",
                        "https://www.massgeneral.org/",
                        alt="Mass General",
                    ),
                ]
            }
        }
        index = _build_hospital_index(data)
        assert index.by_name["mass general"] == "https://www.massgeneral.org/"
        # the primary label is still present too
        assert index.by_name["massachusetts general hospital"] == "https://www.massgeneral.org/"

    def test_state_aware_index_disambiguates_same_name_different_states(self) -> None:
        """Two distinct real hospitals sharing a name in different states -
        confirmed live to be common (e.g. real 'Holy Cross Hospital' exists
        in AZ, FL, IL, and MD as four different hospitals). The plain
        by_name index must NOT pick one arbitrarily; by_name_state must
        resolve each correctly."""
        data = {
            "results": {
                "bindings": [
                    _full_binding("Holy Cross Hospital", "https://holycrossaz.example", state="Arizona"),
                    _full_binding("Holy Cross Hospital", "https://holycrossfl.example", state="Florida"),
                ]
            }
        }
        index = _build_hospital_index(data)
        # ambiguous at the plain-name level - never guessed at
        assert "holy cross hospital" not in index.by_name
        # but state-keyed lookup resolves each correctly
        assert index.by_name_state[("holy cross hospital", "AZ")] == "https://holycrossaz.example"
        assert index.by_name_state[("holy cross hospital", "FL")] == "https://holycrossfl.example"

    def test_ambiguous_alias_across_different_hospitals_is_excluded(self) -> None:
        """A generic alias/name shared by genuinely different hospitals with
        different websites (confirmed live, e.g. 'Mercy Hospital' names 13
        distinct real hospitals) must never resolve via by_name - it's
        excluded entirely rather than picking one arbitrarily."""
        data = {
            "results": {
                "bindings": [
                    _full_binding("Mercy Hospital Iowa City", "https://a.example", alt="Mercy Hospital"),
                    _full_binding("Mercy Hospital Joplin", "https://b.example", alt="Mercy Hospital"),
                ]
            }
        }
        index = _build_hospital_index(data)
        assert "mercy hospital" not in index.by_name
        # the distinctive, unambiguous primary labels still resolve fine
        assert index.by_name["mercy hospital iowa city"] == "https://a.example"
        assert index.by_name["mercy hospital joplin"] == "https://b.example"

    def test_state_label_variants_normalize_to_the_same_abbreviation(self) -> None:
        """Wikidata sometimes disambiguates a state's label against the
        country of the same name (e.g. 'Georgia (U.S. state)')."""
        data = {
            "results": {
                "bindings": [
                    _full_binding("Test Hospital", "https://test.example", state="Georgia (U.S. state)"),
                ]
            }
        }
        index = _build_hospital_index(data)
        assert index.by_name_state[("test hospital", "GA")] == "https://test.example"

    def test_row_missing_website_is_skipped(self) -> None:
        data = {"results": {"bindings": [_full_binding("Hospital A", None)]}}
        index = _build_hospital_index(data)
        assert index == WikidataHospitalIndex()

    def test_malformed_shape_returns_empty_index_not_an_exception(self) -> None:
        assert _build_hospital_index({"results": "not a dict"}) == WikidataHospitalIndex()
        assert _build_hospital_index({"results": {"bindings": "not a list"}}) == WikidataHospitalIndex()
        assert _build_hospital_index({"results": {"bindings": ["not a dict"]}}) == WikidataHospitalIndex()
        assert _build_hospital_index({}) == WikidataHospitalIndex()
