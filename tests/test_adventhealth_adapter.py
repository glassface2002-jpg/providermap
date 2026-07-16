"""Tests for adapters.adventhealth.adapter - URL recognition and parsing."""

from __future__ import annotations

from adapters.adventhealth.adapter import AdventHealthAdapter


class TestProfileUrlRecognition:
    def test_doctors_path_extracts_npi(self, adapter: AdventHealthAdapter) -> None:
        url = "https://www.adventhealth.com/doctors/centra-care-ocala-1710588744"
        assert adapter.extract_npi(url) == "1710588744"

    def test_find_doctor_path_extracts_npi(self, adapter: AdventHealthAdapter) -> None:
        url = "https://www.adventhealth.com/find-doctor/doctor/justin-menezes-md-1194013169"
        assert adapter.extract_npi(url) == "1194013169"

    def test_listing_page_is_not_a_profile(self, adapter: AdventHealthAdapter) -> None:
        assert adapter.extract_npi("https://www.adventhealth.com/doctors") is None

    def test_unrelated_path_is_not_a_profile(self, adapter: AdventHealthAdapter) -> None:
        url = "https://www.adventhealth.com/locations/hospitals/orlando"
        assert adapter.extract_npi(url) is None

    def test_profile_link_pattern_matches_listing_hrefs(
        self,
        adapter: AdventHealthAdapter,
    ) -> None:
        html = (
            '<a href="/doctors/justin-menezes-md-1194013169">View Profile</a>'
            '<a href="/other-page">Not a profile</a>'
        )
        matches = adapter.profile_link_pattern().findall(html)
        assert matches == ["/doctors/justin-menezes-md-1194013169"]


class TestVcardUrl:
    def test_builds_correct_url(self, adapter: AdventHealthAdapter) -> None:
        url = adapter.vcard_url("1710588744")
        assert url == "https://www.adventhealth.com/physician/vcard/1710588744"


class TestProfileHtmlParsing:
    def test_extracts_name_specialty_and_location_count(
        self,
        adapter: AdventHealthAdapter,
    ) -> None:
        html = (
            "<html><head>"
            '<meta property="og:title" content="Justin Menezes, MD" />'
            "<title>Justin Menezes, MD | Family Medicine | Orlando, FL | AdventHealth</title>"
            "</head><body>"
            '<a href="/request-appointment-0?npi=1194013169&location=15050100">Book</a>'
            '<a href="/request-appointment-0?npi=1194013169&location=15050101">Book</a>'
            "</body></html>"
        )
        page = adapter.parse_profile_html(html)
        assert page.display_name == "Justin Menezes, MD"
        assert page.specialty == "Family Medicine"
        assert len(page.location_ids) == 2

    def test_duplicate_location_ids_are_deduplicated(
        self,
        adapter: AdventHealthAdapter,
    ) -> None:
        html = '<a href="/x?npi=1&location=1">a</a>' '<a href="/x?npi=1&location=1">a again</a>'
        page = adapter.parse_profile_html(html)
        assert len(page.location_ids) == 1

    def test_404_page_is_flagged(self, adapter: AdventHealthAdapter) -> None:
        html = "<html><body><h1>Page not found</h1></body></html>"
        page = adapter.parse_profile_html(html)
        assert page.is_error_page

    def test_empty_html_does_not_raise(self, adapter: AdventHealthAdapter) -> None:
        page = adapter.parse_profile_html("")
        assert page.display_name is None
        assert page.location_ids == []


class TestJsonLdDrivenParsing:
    """JSON-LD is tried before the og:title/<title> HTML scrape - see
    parse_profile_html's docstring. These exercise the full mapping via the
    adapter, not just parser_utils.parse_jsonld_physician in isolation
    (covered separately in test_parser_utils.py)."""

    def test_full_jsonld_populates_every_new_field(self, adapter: AdventHealthAdapter) -> None:
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type": "Physician", "name": "Justin Menezes, MD", '
            '"medicalSpecialty": ["Family Medicine"], '
            '"hospitalAffiliation": {"@type": "Hospital", "name": "AdventHealth Orlando"}, '
            '"acceptingNewPatients": true, '
            '"aggregateRating": {"ratingValue": 4.8, "reviewCount": 132}, '
            '"knowsLanguage": ["English", "Spanish"], '
            '"acceptedInsurance": ["Aetna", "Cigna"]}'
            "</script>"
            "</head><body></body></html>"
        )
        page = adapter.parse_profile_html(html)
        assert page.display_name == "Justin Menezes, MD"
        assert page.specialty == "Family Medicine"
        assert page.hospital_affiliation == "AdventHealth Orlando"
        assert page.accepting_new_patients is True
        assert page.rating == 4.8
        assert page.rating_count == 132
        assert page.languages == ["English", "Spanish"]
        assert page.insurance_accepted == ["Aetna", "Cigna"]

    def test_partial_jsonld_leaves_unsupplied_fields_at_default(
        self, adapter: AdventHealthAdapter
    ) -> None:
        html = (
            '<script type="application/ld+json">'
            '{"@type": "Physician", "name": "Ana Ruiz-Sanchez"}'
            "</script>"
        )
        page = adapter.parse_profile_html(html)
        assert page.display_name == "Ana Ruiz-Sanchez"
        assert page.hospital_affiliation is None
        assert page.accepting_new_patients is None
        assert page.rating is None
        assert page.rating_count is None
        assert page.languages == []
        assert page.insurance_accepted == []

    def test_malformed_jsonld_falls_back_to_html_scraping(
        self, adapter: AdventHealthAdapter
    ) -> None:
        html = (
            "<html><head>"
            '<script type="application/ld+json">{"@type": "Physician", "name": </script>'
            '<meta property="og:title" content="David Chen, MD" />'
            "<title>David Chen, MD | Cardiovascular Disease | Orlando, FL | AdventHealth</title>"
            "</head><body></body></html>"
        )
        page = adapter.parse_profile_html(html)
        assert page.display_name == "David Chen, MD"
        assert page.specialty == "Cardiovascular Disease"
        assert page.hospital_affiliation is None

    def test_no_jsonld_at_all_falls_back_to_html_scraping(
        self, adapter: AdventHealthAdapter
    ) -> None:
        html = (
            "<html><head>"
            '<meta property="og:title" content="Priya Nair, APRN" />'
            "<title>Priya Nair, APRN | Family Medicine | Tampa, FL | AdventHealth</title>"
            "</head><body></body></html>"
        )
        page = adapter.parse_profile_html(html)
        assert page.display_name == "Priya Nair, APRN"
        assert page.specialty == "Family Medicine"
        assert page.hospital_affiliation is None
        assert page.rating is None


class TestOrganizationNamePatterns:
    def test_adventhealth_brand_prefix_is_a_facility_fallback(
        self,
        adapter: AdventHealthAdapter,
    ) -> None:
        patterns = adapter.organization_name_patterns()
        assert any(p.search("AdventHealth Imaging at Altamonte") for p, _ in patterns)

    def test_centra_care_brand_is_a_clinic_fallback(self, adapter: AdventHealthAdapter) -> None:
        """Regression test: this pattern was dropped when generic and
        AdventHealth-specific org patterns were split apart during the
        adapter-framework refactor, and had to be restored here since
        "Centra Care" carries no generic clinic keyword on its own."""
        patterns = adapter.organization_name_patterns()
        assert any(p.search("AdventHealth Centra Care Ocala") for p, _ in patterns)
