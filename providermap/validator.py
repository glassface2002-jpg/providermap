"""Classification and validation.

The central decision this module makes is: *is this record a real individual
human being, or is it a facility/organization wearing a doctor's URL?*

Rule hierarchy, most authoritative first:

  1. NPPES enumeration_type       - authoritative when a lookup succeeded
  2. Credential tokens            - sub-types an NPI-1 into Physician vs. APP
  3. Organization name patterns   - fallback only, when NPPES has no answer;
                                     generic keywords here, plus whatever
                                     brand-specific patterns the site adapter
                                     supplies via ``organization_name_patterns``
  4. Unknown                      - when nothing is confident

Rule 3 is a heuristic and is marked as such in the result (``basis:
"name-pattern"``), so any record resting on it can be audited. Nothing is
ever invented: a record with no confident classification is excluded, not
guessed into providers.
"""

from __future__ import annotations

import re
from re import Pattern
from typing import Any

from .models import ClassificationResult, Confidence, NpiPolicy, Provider, ProviderType
from .parser_utils import credential_tokens

# Credential tokens that identify a licensed *physician* (doctoral clinician).
PHYSICIAN_CREDENTIALS = {"MD", "DO", "MBBS", "MBBCH", "MB", "DPM", "DDS", "DMD", "DC", "OD"}

# Credential tokens that identify an advanced practice provider.
APP_CREDENTIALS = {
    "APRN",
    "ARNP",
    "APN",
    "NP",
    "FNP",
    "FNP-C",
    "FNP-BC",
    "AGNP",
    "AGACNP",
    "ACNP",
    "PNP",
    "CPNP",
    "WHNP",
    "PMHNP",
    "CNM",
    "CNP",
    "CNS",
    "CRNA",
    "PA",
    "PA-C",
    "DNP",
    "MSN",
}

# Credential tokens for other licensed individuals: real people, but neither
# physicians nor APPs (therapists, audiologists, dietitians, ...).
OTHER_CLINICIAN_CREDENTIALS = {
    "PHD",
    "PSYD",
    "LCSW",
    "LMHC",
    "LPC",
    "LCPC",
    "MSW",
    "CSW",
    "AUD",
    "CCC-A",
    "CCC-SLP",
    "RD",
    "LD",
    "PHARMD",
    "DPT",
    "MA",
    "MS",
}

# Generic organizational name shapes, independent of any one health system's
# branding. Site adapters may add their own via `organization_name_patterns`.
# Ordered most specific first.
_GENERIC_ORG_PATTERNS: list[tuple[Pattern[str], str]] = [
    (re.compile(r"\bhospital\b|\bmedical cent(er|re)\b|\bregional health\b", re.I), "Hospital"),
    (re.compile(r"\burgent care\b|\bwalk-?in\b|\bexpress care\b", re.I), "Clinic"),
    (re.compile(r"\bclinic\b|\bhealth cent(er|re)\b|\bcare cent(er|re)\b", re.I), "Clinic"),
    (
        re.compile(
            r"\bmedical group\b|\bphysician network\b|\bassociates\b|"
            r"\bpartners\b|\b(llc|inc|pa|pllc|llp|corp)\b\.?$",
            re.I,
        ),
        "Organization",
    ),
    (
        re.compile(
            r"\bimaging\b|\blaborator(y|ies)\b|\bpharmacy\b|\brehab(ilitation)?\b|"
            r"\bsurgery cent(er|re)\b|\bemergency room\b|\bER\b",
            re.I,
        ),
        "Facility",
    ),
]


def classify(
    display_name: str | None,
    credentials: str | None,
    nppes: dict[str, Any] | None,
    extra_org_patterns: list[tuple[Pattern[str], str]] | None = None,
) -> ClassificationResult:
    """Decide what a directory record actually is.

    Parameters
    ----------
    display_name : the name as the site renders it
    credentials : credential string parsed off the display name, may be empty
    nppes : the raw NPPES API result dict, or ``None`` if lookup failed/unset
    extra_org_patterns : site-specific brand patterns, e.g. from
        ``adapter.organization_name_patterns()``
    """
    toks = credential_tokens(credentials or "")
    org_patterns = _GENERIC_ORG_PATTERNS + (extra_org_patterns or [])

    # ---- Rule 1: NPPES is authoritative on individual vs organization ----
    if nppes and nppes.get("enumeration_type"):
        etype = nppes["enumeration_type"]

        if etype == "NPI-2":
            org_name = (nppes.get("basic") or {}).get("organization_name")
            label = (
                _org_subtype(display_name, org_patterns)
                or _org_subtype(org_name, org_patterns)
                or "Organization"
            )
            return ClassificationResult(
                provider_type=ProviderType(label),
                confidence=Confidence.HIGH,
                basis="nppes:NPI-2",
                reason=f"NPI is enumerated to an organization, not a person ({label})",
            )

        if etype == "NPI-1":
            nppes_cred = (nppes.get("basic") or {}).get("credential") or ""
            toks |= credential_tokens(nppes_cred)

            if toks & PHYSICIAN_CREDENTIALS:
                return ClassificationResult(
                    ProviderType.PHYSICIAN, Confidence.HIGH, "nppes:NPI-1+credential"
                )
            if toks & APP_CREDENTIALS:
                return ClassificationResult(
                    ProviderType.ADVANCED_PRACTICE_PROVIDER,
                    Confidence.HIGH,
                    "nppes:NPI-1+credential",
                )
            if toks & OTHER_CLINICIAN_CREDENTIALS:
                tax = _taxonomy_desc(nppes)
                if tax and re.search(r"physician|surgeon", tax, re.I):
                    return ClassificationResult(
                        ProviderType.PHYSICIAN, Confidence.MEDIUM, "nppes:taxonomy"
                    )
                return ClassificationResult(
                    ProviderType.ADVANCED_PRACTICE_PROVIDER,
                    Confidence.LOW,
                    "nppes:NPI-1+other-credential",
                )

            # Individual confirmed by CMS but the specific type is unclear.
            if _looks_physician_taxonomy(nppes):
                return ClassificationResult(
                    ProviderType.PHYSICIAN, Confidence.LOW, "nppes:NPI-1+no-credential"
                )
            return ClassificationResult(
                ProviderType.UNKNOWN,
                Confidence.LOW,
                "nppes:NPI-1+no-credential",
                reason="NPI is an individual but provider type could not be determined",
            )

    # ---- NPPES unavailable. Fall back, and say so. ----
    org_label = _org_subtype(display_name, org_patterns)
    if org_label and not (toks & (PHYSICIAN_CREDENTIALS | APP_CREDENTIALS)):
        return ClassificationResult(
            ProviderType(org_label),
            Confidence.MEDIUM,
            "name-pattern",
            reason=f"Name matches an organizational pattern ({org_label}); "
            f"NPPES lookup unavailable",
        )

    if toks & PHYSICIAN_CREDENTIALS:
        return ClassificationResult(ProviderType.PHYSICIAN, Confidence.MEDIUM, "credential-only")
    if toks & APP_CREDENTIALS:
        return ClassificationResult(
            ProviderType.ADVANCED_PRACTICE_PROVIDER, Confidence.MEDIUM, "credential-only"
        )

    return ClassificationResult(
        ProviderType.UNKNOWN,
        Confidence.LOW,
        "none",
        reason="No NPPES record, no recognisable credentials, no org name pattern",
    )


def _org_subtype(name: str | None, patterns: list[tuple[Pattern[str], str]]) -> str | None:
    if not name:
        return None
    for pattern, label in patterns:
        if pattern.search(name):
            return label
    return None


def _taxonomy_desc(nppes: dict[str, Any] | None) -> str | None:
    if not nppes:
        return None
    for t in nppes.get("taxonomies") or []:
        if t.get("primary"):
            desc = t.get("desc")
            return str(desc) if desc is not None else None
    tax = nppes.get("taxonomies") or []
    if not tax:
        return None
    desc = tax[0].get("desc")
    return str(desc) if desc is not None else None


def _looks_physician_taxonomy(nppes: dict[str, Any] | None) -> bool:
    desc = _taxonomy_desc(nppes) or ""
    return bool(re.search(r"physician|surgeon|allopathic|osteopathic", desc, re.I))


# ---------------------------------------------------------------------- #
# Record-level validation
# ---------------------------------------------------------------------- #

DEFAULT_NPI_POLICY = NpiPolicy()


def validate_provider(
    provider: Provider,
    policy: NpiPolicy | None = None,
) -> tuple[bool, list[str]]:
    """Decide whether a classified individual is complete enough to store.

    Returns ``(is_valid, problems)``. A record with any problem is excluded
    rather than stored with holes in it - the pipeline never guesses.
    """
    policy = policy or DEFAULT_NPI_POLICY
    problems: list[str] = []

    name = (provider.full_name or "").strip()
    if not name:
        problems.append("No individual provider name")
    elif len(name.split()) < 2:
        problems.append("Name does not look like a person (single token)")

    if not provider.npi:
        if policy.require_npi:
            problems.append("No NPI")
    elif policy.checksum == "strict" and not provider.npi_valid:
        problems.append("NPI fails CMS checksum")

    if policy.require_nppes and provider.nppes_enumeration in (None, "not_found"):
        problems.append("No NPPES record and npi.require_nppes is set")

    # The whole point of the dataset is the site of care; no location, no row.
    if not provider.address:
        problems.append("No practice address")
    if not provider.city or not provider.state:
        problems.append("Incomplete address (missing city or state)")

    if not provider.profile_url:
        problems.append("No profile URL")

    substantive = [provider.full_name, provider.address, provider.specialty, provider.practice_name]
    if provider.phone and not any(substantive):
        problems.append("Only a phone number is present")

    return (len(problems) == 0), problems


def validation_notes(provider: Provider, policy: NpiPolicy | None = None) -> list[str]:
    """Non-fatal warnings worth carrying into ``source_notes``."""
    policy = policy or DEFAULT_NPI_POLICY
    notes: list[str] = []
    if provider.npi and not provider.npi_valid:
        if policy.checksum == "warn":
            notes.append("NPI fails CMS checksum (kept: npi.checksum=warn)")
        elif policy.checksum == "off":
            notes.append("NPI checksum not verified (npi.checksum=off)")
    if provider.nppes_enumeration == "not_found":
        notes.append("no NPPES record; classification is heuristic")
    if provider.nppes_name_match is False:
        notes.append("directory name does not match CMS legal name")
    return notes


def site_confidence(
    location_count: int,
    has_nppes: bool,
    name_matches: bool | None,
    classification_confidence: Confidence = Confidence.HIGH,
) -> Confidence:
    """How much to trust ``primary_site_of_care`` for this provider.

    Exists because "primary site" is only meaningful when a provider has one
    site. A cardiologist at five clinics has no primary site, and reporting
    one anyway would quietly corrupt a downstream billing join. Confidence
    also degrades when identity could not be confirmed - a dataset built with
    NPPES disabled is still usable, just honestly less certain.
    """
    if location_count > 3:
        return Confidence.LOW
    if classification_confidence == Confidence.LOW:
        return Confidence.LOW
    if location_count > 1:
        return Confidence.MEDIUM
    if not has_nppes or name_matches is False:
        return Confidence.MEDIUM
    if classification_confidence == Confidence.MEDIUM:
        return Confidence.MEDIUM
    return Confidence.HIGH


def names_match(directory_name: str, nppes: dict[str, Any] | None) -> bool | None:
    """Compare a directory's display name against the CMS legal name.

    Returns ``None`` when it cannot be checked. A mismatch does not exclude
    the record - people use middle names, married names, nicknames - but it
    is recorded so a human can review before trusting the join.
    """
    if not nppes or not directory_name:
        return None
    basic = nppes.get("basic") or {}
    first = (basic.get("first_name") or "").strip().lower()
    last = (basic.get("last_name") or "").strip().lower()
    if not last:
        return None
    d = re.sub(r"[^a-z ]", "", directory_name.lower())
    if last not in d:
        return False
    return not (first and first not in d)
