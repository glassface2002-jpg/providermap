"""Tests for exact-name provider <-> organization matching (ROADMAP.md
stage 5). See providermap/organization_linking.py's module docstring for why
this deliberately never fuzzy-matches.
"""

from __future__ import annotations

from providermap.models import Organization
from providermap.organization_linking import best_match_for_provider, match_organization


def _org(name: str, normalized: str) -> Organization:
    return Organization(name=name, normalized_name=normalized, organization_id=1)


class TestMatchOrganization:
    def test_exact_normalized_match(self) -> None:
        orgs = {
            "banner desert medical center": _org(
                "Banner Desert Medical Center", "banner desert medical center"
            )
        }
        result = match_organization("Banner Desert Medical Center", orgs)
        assert result is not None
        assert result.name == "Banner Desert Medical Center"

    def test_case_and_whitespace_insensitive(self) -> None:
        orgs = {
            "banner desert medical center": _org(
                "Banner Desert Medical Center", "banner desert medical center"
            )
        }
        result = match_organization("  BANNER   DESERT MEDICAL   CENTER  ", orgs)
        assert result is not None

    def test_no_match_returns_none(self) -> None:
        orgs = {
            "banner desert medical center": _org(
                "Banner Desert Medical Center", "banner desert medical center"
            )
        }
        assert match_organization("Some Other Hospital", orgs) is None

    def test_partial_or_fuzzy_similarity_does_not_match(self) -> None:
        """Deliberately proves this is NOT fuzzy matching - a near-miss must
        not link, since a wrong link is worse than no link."""
        orgs = {
            "banner desert medical center": _org(
                "Banner Desert Medical Center", "banner desert medical center"
            )
        }
        assert match_organization("Banner Desert Medical Ctr", orgs) is None
        assert match_organization("Banner Desert", orgs) is None

    def test_none_candidate_returns_none(self) -> None:
        orgs = {
            "banner desert medical center": _org(
                "Banner Desert Medical Center", "banner desert medical center"
            )
        }
        assert match_organization(None, orgs) is None

    def test_empty_registry_returns_none(self) -> None:
        assert match_organization("Anything", {}) is None


class TestBestMatchForProvider:
    def test_prefers_hospital_affiliation_over_practice_name(self) -> None:
        orgs = {
            "hospital a": _org("Hospital A", "hospital a"),
            "hospital b": _org("Hospital B", "hospital b"),
        }
        result = best_match_for_provider(
            hospital_affiliation="Hospital A",
            practice_name="Hospital B",
            organizations_by_normalized_name=orgs,
        )
        assert result is not None
        assert result.name == "Hospital A"

    def test_falls_back_to_practice_name_when_affiliation_has_no_match(self) -> None:
        orgs = {"hospital b": _org("Hospital B", "hospital b")}
        result = best_match_for_provider(
            hospital_affiliation="Unmatched Affiliation",
            practice_name="Hospital B",
            organizations_by_normalized_name=orgs,
        )
        assert result is not None
        assert result.name == "Hospital B"

    def test_no_match_anywhere_returns_none(self) -> None:
        orgs = {"hospital b": _org("Hospital B", "hospital b")}
        result = best_match_for_provider(
            hospital_affiliation="Nope",
            practice_name="Also Nope",
            organizations_by_normalized_name=orgs,
        )
        assert result is None

    def test_both_none_returns_none(self) -> None:
        orgs = {"hospital b": _org("Hospital B", "hospital b")}
        assert best_match_for_provider(None, None, orgs) is None
