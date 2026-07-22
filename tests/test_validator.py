"""Tests for providermap.validator - classification and record validation.

These are the tests that guard against the specific failure mode this
project exists to prevent: a facility or organization being stored as if it
were an individual provider.
"""

from __future__ import annotations

from providermap import parser_utils as P
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

    def test_nppes_period_formatted_credential_alone_still_classifies_physician(self) -> None:
        """Regression for a real, live-traced case: NPPES's own
        ``basic.credential`` is period-formatted ('M.D.') and the display name
        carries no credential at all (e.g. because the profile page/vCard
        display didn't repeat one), and the primary taxonomy description
        ('Internal Medicine, Nephrology') doesn't contain any of
        physician/surgeon/allopathic/osteopathic. Before the
        ``credential_tokens`` period-stripping fix, 'M.D.' normalized to
        'M.D' (not 'MD'), never matched PHYSICIAN_CREDENTIALS, and this fell
        through to ProviderType.UNKNOWN despite NPPES confirming an
        individual with a physician credential."""
        nppes = {
            "enumeration_type": "NPI-1",
            "basic": {"first_name": "MARK", "last_name": "LAGATTA", "credential": "M.D."},
            "taxonomies": [
                {"primary": True, "code": "207RN0300X", "desc": "Internal Medicine, Nephrology"}
            ],
        }
        result = V.classify("Mark LaGatta", "", nppes)
        assert result.provider_type == ProviderType.PHYSICIAN
        assert result.is_individual


class TestAppCredentialCoverage:
    """Regression coverage for a credential-set audit against 500 real
    NPPES records: 'NP-C' was confirmed missing from APP_CREDENTIALS in real
    data; 'APRN-BC' and 'RN-BC' were added as pattern-consistent additions
    (same '-BC' board-certification suffix already present via 'FNP-BC'),
    though not themselves observed in that sample."""

    def test_np_c_credential_is_advanced_practice_provider(self) -> None:
        """Confirmed from real NPPES data (Cindy Alvarez, NPI 1033633250,
        taxonomy 'Nurse Practitioner, Pediatrics') - was previously
        unmatched and fell through to ProviderType.UNKNOWN."""
        nppes = {
            "enumeration_type": "NPI-1",
            "basic": {"first_name": "CINDY", "last_name": "ALVAREZ", "credential": "NP-C"},
            "taxonomies": [
                {"primary": True, "code": "363LP2300X", "desc": "Nurse Practitioner, Pediatrics"}
            ],
        }
        result = V.classify("Cindy Alvarez", "", nppes)
        assert result.provider_type == ProviderType.ADVANCED_PRACTICE_PROVIDER
        assert result.is_individual

    def test_aprn_bc_credential_is_advanced_practice_provider(self) -> None:
        result = V.classify("Some Person, APRN-BC", "APRN-BC", None)
        assert result.provider_type == ProviderType.ADVANCED_PRACTICE_PROVIDER
        assert result.is_individual

    def test_rn_bc_credential_is_advanced_practice_provider(self) -> None:
        result = V.classify("Some Person, RN-BC", "RN-BC", None)
        assert result.provider_type == ProviderType.ADVANCED_PRACTICE_PROVIDER
        assert result.is_individual

    def test_existing_app_credentials_still_classify_unchanged(self) -> None:
        """Guards against the set expansion above accidentally disturbing
        credentials that already worked."""
        for display, cred in [
            ("Some Person, APRN", "APRN"),
            ("Some Person, FNP-C", "FNP-C"),
            ("Some Person, FNP-BC", "FNP-BC"),
            ("Some Person, PA-C", "PA-C"),
            ("Some Person, CRNA", "CRNA"),
        ]:
            result = V.classify(display, cred, None)
            assert result.provider_type == ProviderType.ADVANCED_PRACTICE_PROVIDER, cred

    def test_existing_physician_credential_still_classifies_unchanged(self) -> None:
        result = V.classify("Justin Menezes, MD", "MD", None)
        assert result.provider_type == ProviderType.PHYSICIAN


class TestVcardTitleFallbackRecovery:
    """Regression coverage for the pipeline-level vCard TITLE credential
    fallback (parser_utils.merge_vcard_title_credentials, wired into
    pipeline.py::process_url). Each test reproduces exactly what the
    pipeline does: merge the vCard-derived credentials in, then classify.
    NPI/name/taxonomy values are from real, live-captured AdventHealth
    records where NPPES's own ``credential`` field was blank but the vCard
    TITLE field still carried the credential."""

    def test_daniel_treiyer_recovered_via_vcard_title(self) -> None:
        # Real NPI 1063703403.
        nppes = {
            "enumeration_type": "NPI-1",
            "basic": {"first_name": "DANIEL", "last_name": "TREIYER", "credential": ""},
            "taxonomies": [{"primary": True, "code": "207R00000X", "desc": "Internal Medicine"}],
        }
        creds = P.merge_vcard_title_credentials("", "Daniel Treiyer, MD")
        result = V.classify("Daniel Treiyer", creds, nppes)
        assert result.provider_type == ProviderType.PHYSICIAN

    def test_hana_chaim_do_recovered_via_vcard_title(self) -> None:
        # Real NPI 1144250341.
        nppes = {
            "enumeration_type": "NPI-1",
            "basic": {"first_name": "HANA", "last_name": "CHAIM", "credential": ""},
            "taxonomies": [{"primary": True, "code": "207Q00000X", "desc": "Allergy & Immunology"}],
        }
        creds = P.merge_vcard_title_credentials("", "Hana T Chaim, DO")
        result = V.classify("Hana Chaim", creds, nppes)
        assert result.provider_type == ProviderType.PHYSICIAN

    def test_kyle_duffy_dmd_recovered_via_vcard_title(self) -> None:
        # Real NPI 1073929568 - a dental credential (DMD), still the
        # "correct clinician classification" per PHYSICIAN_CREDENTIALS
        # (which already includes DDS/DMD alongside MD/DO).
        nppes = {
            "enumeration_type": "NPI-1",
            "basic": {"first_name": "KYLE", "last_name": "DUFFY", "credential": ""},
            "taxonomies": [{"primary": True, "code": "1223D0001X", "desc": "Dentist"}],
        }
        creds = P.merge_vcard_title_credentials("", "Kyle Duffy, DMD")
        result = V.classify("Kyle Duffy", creds, nppes)
        assert result.provider_type == ProviderType.PHYSICIAN

    def test_marla_robbins_recovered_via_vcard_title(self) -> None:
        """Real NPI 1013987304: NPPES's own credential field is
        space-separated ('M D') - a distinct, explicitly-deferred
        whitespace-normalization gap, NOT fixed in this round. The vCard
        TITLE field is unaffected by that and carries a clean 'MD', which is
        what actually recovers this record today (the live-captured
        vcard.title was 'Marla A Robbins, MD', not a space-separated
        credential - the malformed 'M D' only ever appeared in NPPES's
        field, not the vCard)."""
        nppes = {
            "enumeration_type": "NPI-1",
            "basic": {"first_name": "MARLA", "last_name": "ROBBINS", "credential": "M D"},
            "taxonomies": [{"primary": True, "code": "208000000X", "desc": "Pediatrics"}],
        }
        creds = P.merge_vcard_title_credentials("", "Marla A Robbins, MD")
        result = V.classify("Marla Robbins", creds, nppes)
        assert result.provider_type == ProviderType.PHYSICIAN

    def test_whitney_breen_aprn_c_recovered_via_vcard_title(self) -> None:
        """Real NPI 1013464734: NPPES credential blank; vcard.title =
        'Whitney Breen, APRN-C'. Recovered by combining this fallback with
        adding 'APRN-C' to APP_CREDENTIALS."""
        nppes = {
            "enumeration_type": "NPI-1",
            "basic": {"first_name": "WHITNEY", "last_name": "BREEN", "credential": ""},
            "taxonomies": [
                {"primary": True, "code": "363LF0000X", "desc": "Nurse Practitioner, Family"}
            ],
        }
        creds = P.merge_vcard_title_credentials("", "Whitney Breen, APRN-C")
        result = V.classify("Whitney Breen", creds, nppes)
        assert result.provider_type == ProviderType.ADVANCED_PRACTICE_PROVIDER

    def test_title_with_no_recognized_credential_does_not_force_a_classification(self) -> None:
        """A vCard TITLE like 'German Mikheyev, Resident' has a comma but no
        recognized credential token ('Resident' isn't a licensure credential)
        - must not create a false positive classification. Real NPI
        1073993077, live-observed exactly this way."""
        nppes = {
            "enumeration_type": "NPI-1",
            "basic": {"first_name": "GERMAN", "last_name": "MIKHEYEV", "credential": ""},
            "taxonomies": [{"primary": True, "code": "213E00000X", "desc": "Podiatrist"}],
        }
        creds = P.merge_vcard_title_credentials("", "German Mikheyev, Resident")
        result = V.classify("German Mikheyev", creds, nppes)
        assert result.provider_type == ProviderType.UNKNOWN
        assert not result.is_individual


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
