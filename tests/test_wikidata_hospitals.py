"""Tests for the Wikidata bulk hospital website lookup (ROADMAP.md stage 4
follow-on). Only `_parse_sparql_response` - pure JSON parsing - is unit
tested; `fetch_wikidata_hospital_websites` itself does real network I/O and
is exercised only via manual/live verification, matching how
`http_domain_checker` in website_enrichment.py is treated.
"""

from __future__ import annotations

from providermap.wikidata_hospitals import _parse_sparql_response


def _binding(label: str | None, website: str | None) -> dict:
    row: dict = {}
    if label is not None:
        row["hospitalLabel"] = {"xml:lang": "en", "type": "literal", "value": label}
    if website is not None:
        row["website"] = {"type": "uri", "value": website}
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
