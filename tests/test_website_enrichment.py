"""Tests for the best-effort website discovery module (ROADMAP.md stage 4).

Network access is a fake, in-memory `DomainChecker` throughout - this
module's own design keeps the real `httpx`-backed checker
(`http_domain_checker`) separate specifically so these tests never touch the
network, matching the rest of this suite.
"""

from __future__ import annotations

from providermap.models import Confidence, Organization
from providermap.website_enrichment import (
    _distinctive_tokens,
    candidate_domains,
    discover_website,
)


class TestCandidateDomains:
    def test_strips_trailing_generic_words_one_at_a_time(self) -> None:
        domains = candidate_domains("Banner Desert Medical Center")
        # "center" then "medical" are generic and get stripped in turn;
        # "desert" is not, so the loop stops there.
        assert "bannerdesertmedicalcenter.com" in domains
        assert "bannerdesertmedical.com" in domains
        assert "bannerdesert.com" in domains
        assert "banner.com" not in domains  # would have required a 4th strip

    def test_includes_both_tlds_for_every_slug(self) -> None:
        domains = candidate_domains("Mesa General Hospital")
        assert "mesageneralhospital.com" in domains
        assert "mesageneralhospital.org" in domains

    def test_stops_immediately_when_no_trailing_generic_word(self) -> None:
        domains = candidate_domains("Acme Widgets")
        # "widgets" isn't generic, so only the full-name slug is tried.
        assert domains == ["acmewidgets.com", "acmewidgets.org"]

    def test_none_or_empty_name_yields_no_candidates(self) -> None:
        assert candidate_domains(None) == []
        assert candidate_domains("") == []
        assert candidate_domains("   ") == []


class TestDistinctiveTokens:
    def test_filters_out_generic_words_and_short_words(self) -> None:
        tokens = _distinctive_tokens("Banner Desert Medical Center")
        assert "desert" in tokens
        assert "medical" not in tokens  # generic
        assert "center" not in tokens  # generic

    def test_none_name_yields_no_tokens(self) -> None:
        assert _distinctive_tokens(None) == []


class TestDiscoverWebsite:
    async def test_first_resolving_candidate_with_name_match_is_medium_confidence(self) -> None:
        org = Organization(name="Banner Desert Medical Center", normalized_name=None)

        async def checker(url: str) -> str | None:
            if url == "https://bannerdesertmedicalcenter.com":
                return "<html>Welcome to Banner Desert Medical Center</html>"
            return None

        url, confidence = await discover_website(org, checker, requests_per_second=1000)
        assert url == "https://bannerdesertmedicalcenter.com"
        assert confidence == Confidence.MEDIUM

    async def test_resolving_candidate_without_name_match_is_low_confidence(self) -> None:
        org = Organization(name="Banner Desert Medical Center")

        async def checker(url: str) -> str | None:
            if url == "https://bannerdesertmedicalcenter.com":
                return "<html>Unrelated page content</html>"
            return None

        url, confidence = await discover_website(org, checker, requests_per_second=1000)
        assert url == "https://bannerdesertmedicalcenter.com"
        assert confidence == Confidence.LOW

    async def test_never_returns_high_confidence(self) -> None:
        org = Organization(name="Banner Desert Medical Center")

        async def checker(url: str) -> str | None:
            return "<html>Banner Desert Medical Center - official site</html>"

        _, confidence = await discover_website(org, checker, requests_per_second=1000)
        assert confidence != Confidence.HIGH

    async def test_stops_at_the_first_resolving_candidate(self) -> None:
        org = Organization(name="Banner Desert Medical Center")
        calls: list[str] = []

        async def checker(url: str) -> str | None:
            calls.append(url)
            if url == "https://bannerdesertmedicalcenter.com":
                return "<html>found it</html>"
            return "<html>should never be reached</html>"

        await discover_website(org, checker, requests_per_second=1000)
        assert calls == ["https://bannerdesertmedicalcenter.com"]

    async def test_nothing_resolves_returns_none_none(self) -> None:
        org = Organization(name="Banner Desert Medical Center")

        async def checker(url: str) -> str | None:
            return None

        url, confidence = await discover_website(org, checker, requests_per_second=1000)
        assert url is None
        assert confidence is None

    async def test_no_name_returns_none_without_calling_checker(self) -> None:
        org = Organization(name=None)
        called = False

        async def checker(url: str) -> str | None:
            nonlocal called
            called = True
            return "<html>should never be called</html>"

        url, confidence = await discover_website(org, checker, requests_per_second=1000)
        assert url is None
        assert confidence is None
        assert called is False
