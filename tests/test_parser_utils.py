"""Tests for providermap.parser_utils - pure functions, no adapter needed."""

from __future__ import annotations

import re

from providermap.parser_utils import (
    clean_phone,
    credential_tokens,
    extract_matches,
    normalize_address_key,
    npi_checksum_valid,
    parse_vcard,
    split_name_and_credentials,
    split_person_name,
)


class TestNpiChecksum:
    def test_valid_real_npis(self) -> None:
        # Both taken from the live AdventHealth directory during development:
        # one an individual, one an organization. Checksum validity does not
        # depend on which.
        assert npi_checksum_valid("1710588744")
        assert npi_checksum_valid("1194013169")

    def test_rejects_wrong_length(self) -> None:
        assert not npi_checksum_valid("123")
        assert not npi_checksum_valid("12345678901")

    def test_rejects_non_digits(self) -> None:
        assert not npi_checksum_valid("12345abcde")

    def test_rejects_bad_checksum(self) -> None:
        assert not npi_checksum_valid("1234567890")

    def test_rejects_empty(self) -> None:
        assert not npi_checksum_valid("")


class TestNameAndCredentials:
    def test_single_credential(self) -> None:
        assert split_name_and_credentials("Justin Menezes, MD") == ("Justin Menezes", "MD")

    def test_multiple_credentials(self) -> None:
        assert split_name_and_credentials("Ana Ruiz-Sanchez, APRN, FNP-C") == (
            "Ana Ruiz-Sanchez",
            "APRN, FNP-C",
        )

    def test_no_credentials(self) -> None:
        assert split_name_and_credentials("AdventHealth Centra Care Ocala") == (
            "AdventHealth Centra Care Ocala",
            "",
        )

    def test_empty_input(self) -> None:
        assert split_name_and_credentials("") == ("", "")

    def test_credential_tokens_normalizes(self) -> None:
        assert credential_tokens("APRN, FNP-C") == {"APRN", "FNP-C"}

    def test_credential_tokens_empty(self) -> None:
        assert credential_tokens("") == set()


class TestPersonNameSplit:
    def test_two_part_name(self) -> None:
        assert split_person_name("Justin Menezes") == ("Justin", "Menezes")

    def test_suffix_is_not_treated_as_surname(self) -> None:
        assert split_person_name("Robert Sullivan Jr") == ("Robert", "Sullivan")

    def test_single_token_returns_none(self) -> None:
        assert split_person_name("Cher") == (None, None)

    def test_empty_returns_none(self) -> None:
        assert split_person_name("") == (None, None)


class TestPhoneCleaning:
    def test_various_formats_normalize(self) -> None:
        assert clean_phone("(407) 646-7070") == "407-646-7070"
        assert clean_phone("4076467070") == "407-646-7070"
        assert clean_phone("1-407-646-7070") == "407-646-7070"

    def test_invalid_length_returns_none(self) -> None:
        assert clean_phone("12345") is None
        assert clean_phone("") is None


class TestVCardParsing:
    def test_real_response_shape(self) -> None:
        # Verbatim (except formatting) from the live
        # /physician/vcard/{npi} endpoint captured during development.
        text = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "FN: AdventHealth Centra Care Ocala - Family Medicine\n"
            "ORG: AdventHealth Centra Care Ocala\n"
            "TEL;WORK;VOICE:352-401-8401\n"
            "ADR;WORK;PREF:;;AdventHealth - AdventHealth Centra Care Ocala;"
            "3708 Southwest College Road ;Ocala;FL;34474;US\n"
            "TITLE:AdventHealth Centra Care Ocala\n"
            "END:VCARD"
        )
        vc = parse_vcard(text)
        assert vc.organization == "AdventHealth Centra Care Ocala"
        assert vc.phone == "352-401-8401"
        assert vc.address == "3708 Southwest College Road"
        assert vc.city == "Ocala"
        assert vc.state == "FL"
        assert vc.zip == "34474"

    def test_empty_input(self) -> None:
        vc = parse_vcard("")
        assert vc.organization is None

    def test_non_vcard_text(self) -> None:
        vc = parse_vcard("<html>not a vcard</html>")
        assert vc.full_name is None


class TestAddressNormalization:
    def test_spelling_variants_collapse(self) -> None:
        a = normalize_address_key("3708 Southwest College Road", "Ocala", "FL", "34474")
        b = normalize_address_key("3708 SW College Rd.", "Ocala", "FL", "34474")
        c = normalize_address_key("3708   sw college road ", "Ocala", "fl", "34474-1234")
        assert a == b == c

    def test_different_address_produces_different_key(self) -> None:
        a = normalize_address_key("100 Main St", "Orlando", "FL", "32801")
        b = normalize_address_key("200 Main St", "Orlando", "FL", "32801")
        assert a != b

    def test_missing_address_or_zip_returns_none(self) -> None:
        assert normalize_address_key(None, "Orlando", "FL", "32801") is None
        assert normalize_address_key("100 Main St", "Orlando", "FL", None) is None


class TestExtractMatches:
    def test_dedupes_preserving_order(self) -> None:
        html = (
            '<a href="/doctors/a-1">x</a><a href="/doctors/b-2">y</a>'
            '<a href="/doctors/a-1">dup</a>'
        )
        pattern = re.compile(r'href="(/doctors/[a-z]-\d)"')
        assert extract_matches(html, pattern) == ["/doctors/a-1", "/doctors/b-2"]

    def test_no_matches(self) -> None:
        assert extract_matches("<p>nothing here</p>", re.compile(r"/doctors/\d+")) == []
