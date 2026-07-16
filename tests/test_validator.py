"""Tests for providermap.validator - classification and record validation.

These are the tests that guard against the specific failure mode this
project exists to prevent: a facility or organization being stored as if it
were an individual provider.
"""

from __future__ import annotations

from providermap import validator as V
from providermap.models import Confidence, NpiPolicy, Provider, ProviderType

ORG_NPPES = {
    "enumeration_type": "NPI-2",
    "basic": {"organization_name": "ADVENTHEALTH CENTRA CARE OCALA"},
    "taxonomies": [],
}
PHYSICIAN_NPPES = {
    "enumeration_type": "NPI-1",
    "basic": {"first_name": "JUSTIN", "last_name": "MENEZES", "credential": "M.D."},
    "taxonomies": [{"primary": True, "code": "207Q00000X", "desc": "Family Medicine Physician"}],
}
APP_NPPES = {
    "enumeration_type": "NPI-1",
    "basic": {"first_name": "WENDY", "last_name": "ACOSTA LUNA", "credential": "APRN"},
    "taxonomies": [{"primary": True, "code": "363L00000X", "desc": "Nurse Practitioner"}],
}
# The adversarial case: an organization whose name contains "MD", which a
# credential-only heuristic would misclassify as a physician.
TRICKY_ORG_NPPES = {
    "enumeration_type": "NPI-2",
    "basic": {"organization_name": "SMITH AND JONES MD PA"},
    "taxonomies": [],
}


class TestClassifyWithNppes:
    """NPPES is authoritative when available - these are the high-confidence paths."""

    def test_organization_is_never_a_physician_even_with_facility_directory_url(self) -> None:
        result = V.classify("AdventHealth Centra Care Ocala", "", ORG_NPPES)
        assert not result.is_individual
        assert result.confidence == Confidence.HIGH
        assert result.basis == "nppes:NPI-2"

    def test_physician_credential(self) -> None:
        result = V.classify("Justin Menezes, MD", "MD", PHYSICIAN_NPPES)
        assert result.provider_type == ProviderType.PHYSICIAN
        assert result.is_individual
        assert result.confidence == Confidence.HIGH

    def test_advanced_practice_provider_credential(self) -> None:
        result = V.classify("Wendy Acosta Luna, APRN", "APRN", APP_NPPES)
        assert result.provider_type == ProviderType.ADVANCED_PRACTICE_PROVIDER
        assert result.is_individual

    def test_org_with_md_in_name_is_not_fooled_by_credential_heuristic(self) -> None:
        """The adversarial case that motivates using NPPES at all: name-based
        or credential-based heuristics alone get this wrong."""
        result = V.classify("Smith and Jones, MD", "MD", TRICKY_ORG_NPPES)
        assert not result.is_individual
        assert result.provider_type == ProviderType.ORGANIZATION


class TestClassifyWithoutNppes:
    """NPPES unavailable - degrades to heuristics, confidence drops, but the
    pipeline still produces a usable classification rather than failing."""

    def test_hospital_name_pattern(self) -> None:
        result = V.classify("AdventHealth Hendersonville Hospital", "", None)
        assert result.provider_type == ProviderType.HOSPITAL
        assert result.confidence == Confidence.MEDIUM
        assert result.basis == "name-pattern"

    def test_clinic_name_pattern(self) -> None:
        result = V.classify("Sunshine Urgent Care", "", None)
        assert result.provider_type == ProviderType.CLINIC
        assert result.confidence == Confidence.MEDIUM
        assert result.basis == "name-pattern"

    def test_credential_only_physician(self) -> None:
        result = V.classify("Justin Menezes, MD", "MD", None)
        assert result.provider_type == ProviderType.PHYSICIAN
        assert result.confidence == Confidence.MEDIUM

    def test_credential_only_app(self) -> None:
        result = V.classify("Some Person, PA-C", "PA-C", None)
        assert result.provider_type == ProviderType.ADVANCED_PRACTICE_PROVIDER

    def test_the_adversarial_case_is_caught_by_name_pattern_without_nppes(self) -> None:
        """An organization's display name has no comma, so
        split_name_and_credentials never extracts a credential for it in the
        real pipeline - `credentials` arrives empty here, same as it would
        from parsing "Smith and Jones, MD PA" as a single un-split string.
        With no credential tokens to force the physician branch, the
        organizational-suffix pattern (trailing "PA") correctly catches it."""
        result = V.classify("Smith and Jones, MD PA", "", None)
        assert not result.is_individual

    def test_a_real_physician_with_credentials_is_not_overridden_by_a_loose_name_pattern(
        self,
    ) -> None:
        """The inverse of the case above: when credentials clearly identify a
        person, an incidental organizational-looking word in their display
        name must not reclassify them as a facility."""
        result = V.classify("Justin Menezes, MD", "MD", None)
        assert result.is_individual

    def test_truly_ambiguous_record_is_unknown_not_guessed(self) -> None:
        result = V.classify("Mystery Entry", "", None)
        assert result.provider_type == ProviderType.UNKNOWN
        assert result.reason is not None

    def test_adapter_supplied_org_pattern_is_used(self) -> None:
        """A site adapter can supply brand-specific patterns (e.g. its own
        health system's name) layered on top of the generic ones. Uses a name
        with no generic-keyword overlap so the adapter pattern is what
        actually decides the outcome."""
        import re

        brand_pattern = [(re.compile(r"^BrandX\b"), "Facility")]
        result = V.classify("BrandX Diagnostics", "", None, brand_pattern)
        assert result.provider_type == ProviderType.FACILITY


class TestValidateProvider:
    def test_complete_record_is_valid(self) -> None:
        p = Provider(
            full_name="Justin Menezes",
            npi="1194013169",
            npi_valid=True,
            address="320 E South St",
            city="Orlando",
            state="FL",
            profile_url="https://x/1",
        )
        ok, problems = V.validate_provider(p)
        assert ok
        assert problems == []

    def test_missing_address_is_excluded_not_guessed(self) -> None:
        p = Provider(
            full_name="Justin Menezes",
            npi="1194013169",
            npi_valid=True,
            city="Orlando",
            state="FL",
            profile_url="https://x/1",
        )
        ok, problems = V.validate_provider(p)
        assert not ok
        assert "No practice address" in problems

    def test_phone_only_record_is_excluded(self) -> None:
        p = Provider(
            phone="407-555-1212", npi="1194013169", npi_valid=True, profile_url="https://x/1"
        )
        ok, problems = V.validate_provider(p)
        assert not ok
        assert any("phone number" in msg for msg in problems)

    def test_no_name_is_excluded(self) -> None:
        p = Provider(
            npi="1194013169",
            npi_valid=True,
            address="1 Main St",
            city="Orlando",
            state="FL",
            profile_url="https://x/1",
        )
        ok, _ = V.validate_provider(p)
        assert not ok


class TestNpiPolicy:
    def test_checksum_strict_excludes_bad_npi(self) -> None:
        p = Provider(
            full_name="Iris Calloway",
            npi="1234567890",
            npi_valid=False,
            address="1 Main St",
            city="Orlando",
            state="FL",
            profile_url="https://x/1",
        )
        ok, problems = V.validate_provider(p, NpiPolicy(checksum="strict"))
        assert not ok
        assert "NPI fails CMS checksum" in problems

    def test_checksum_warn_keeps_bad_npi(self) -> None:
        p = Provider(
            full_name="Iris Calloway",
            npi="1234567890",
            npi_valid=False,
            address="1 Main St",
            city="Orlando",
            state="FL",
            profile_url="https://x/1",
        )
        ok, _ = V.validate_provider(p, NpiPolicy(checksum="warn"))
        assert ok

    def test_checksum_off_keeps_bad_npi(self) -> None:
        p = Provider(
            full_name="Iris Calloway",
            npi="1234567890",
            npi_valid=False,
            address="1 Main St",
            city="Orlando",
            state="FL",
            profile_url="https://x/1",
        )
        ok, _ = V.validate_provider(p, NpiPolicy(checksum="off"))
        assert ok

    def test_require_nppes_excludes_unconfirmed_records(self) -> None:
        p = Provider(
            full_name="Elena Vasquez",
            npi="1194013169",
            npi_valid=True,
            nppes_enumeration="not_found",
            address="1 Main St",
            city="Orlando",
            state="FL",
            profile_url="https://x/1",
        )
        ok, problems = V.validate_provider(p, NpiPolicy(require_nppes=True))
        assert not ok
        assert any("require_nppes" in msg for msg in problems)

    def test_invalid_policy_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="checksum"):
            NpiPolicy(checksum="bogus")


class TestSiteConfidence:
    def test_single_location_confirmed_identity_is_high(self) -> None:
        assert V.site_confidence(1, True, True, Confidence.HIGH) == Confidence.HIGH

    def test_four_locations_is_low_regardless_of_everything_else(self) -> None:
        assert V.site_confidence(4, True, True, Confidence.HIGH) == Confidence.LOW

    def test_two_locations_is_medium(self) -> None:
        assert V.site_confidence(2, True, True, Confidence.HIGH) == Confidence.MEDIUM

    def test_no_nppes_degrades_to_medium(self) -> None:
        assert V.site_confidence(1, False, None, Confidence.HIGH) == Confidence.MEDIUM

    def test_name_mismatch_degrades_to_medium(self) -> None:
        assert V.site_confidence(1, True, False, Confidence.HIGH) == Confidence.MEDIUM


class TestNamesMatch:
    def test_matching_name(self) -> None:
        assert V.names_match("Justin Menezes, MD", PHYSICIAN_NPPES) is True

    def test_mismatched_surname(self) -> None:
        assert V.names_match("Someone Else, MD", PHYSICIAN_NPPES) is False

    def test_no_nppes_record_is_unknown(self) -> None:
        assert V.names_match("Justin Menezes, MD", None) is None
