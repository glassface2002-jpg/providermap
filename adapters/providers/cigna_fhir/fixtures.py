"""Saved FHIR ``Bundle`` fixtures for offline ``CignaFhirAdapter`` tests.

Every NPI here has a valid CMS Luhn check digit (computed the same way
``providermap.parser_utils.npi_checksum_valid`` verifies one), so tests
exercise the real validation path, not a simplified stand-in for it - the
same discipline ``adapters/providers/adventhealth/fixtures.py`` documents for
its own dataset.

The two-page pilot scenario (``PAGE_1``/``PAGE_2``) is deliberately
adversarial in the direction this adapter's design guards against: Dr. Jane
Doe's two ``PractitionerRole`` resources, and the ``Location``/
``Organization`` resources each one references, are split across the two
pages, with her ``Practitioner`` resource included only once, on the first
page it's referenced from - a real Plan-Net server has no obligation to
repeat an ``_include``d resource on every page that references it. A correct
``discover()`` must resolve this across both pages before yielding, not
per-page.

* ``PAGE_1`` / ``PAGE_2`` - Dr. Jane Doe (NPI ``1111111112``), two roles at
  two locations/organizations, split across pages. The second role's
  organization (``org-2``) carries no NPI at all, exercising the
  missing-organization-NPI fallback (dedupe falls back to the FHIR resource
  id).
* ``MISSING_NPI_PAGE`` - a practitioner with no ``us-npi`` identifier at all.
* ``MALFORMED_PAGE`` - one well-formed practitioner (NPI ``2222222228``)
  alongside a deliberately malformed ``Bundle.entry`` (a ``resource`` with no
  ``resourceType``/``id``), to confirm one bad entry doesn't sink the page.
"""

from __future__ import annotations

from typing import Any

from ..cigna_fhir.adapter import PageFetcher

JANE_DOE_NPI = "1111111112"
NO_NPI_PRACTITIONER_ID = "practitioner-no-npi"
MALFORMED_PAGE_NPI = "2222222228"
ORG_1_NPI = "3333333334"

PAGE_1_URL = "https://fhir.cigna.com/ProviderDirectory/v1/PractitionerRole?page=1"
PAGE_2_URL = "https://fhir.cigna.com/ProviderDirectory/v1/PractitionerRole?page=2"

PAGE_1: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "link": [{"relation": "next", "url": PAGE_2_URL}],
    "entry": [
        {
            "resource": {
                "resourceType": "Practitioner",
                "id": "practitioner-jane-doe",
                "identifier": [
                    {"system": "http://hl7.org/fhir/sid/us-npi", "value": JANE_DOE_NPI}
                ],
                "name": [{"family": "Doe", "given": ["Jane"], "suffix": ["MD"]}],
                "communication": [{"text": "Spanish"}],
            }
        },
        {
            "resource": {
                "resourceType": "PractitionerRole",
                "id": "role-1",
                "practitioner": {"reference": "Practitioner/practitioner-jane-doe"},
                "organization": {"reference": "Organization/org-1"},
                "location": [{"reference": "Location/location-1"}],
                "specialty": [
                    {
                        "coding": [
                            {
                                "system": "http://nucc.org/provider-taxonomy",
                                "code": "207Q00000X",
                                "display": "Family Medicine",
                            }
                        ]
                    }
                ],
                "extension": [
                    {
                        "url": (
                            "http://hl7.org/fhir/us/davinci-pdex-plan-net/"
                            "StructureDefinition/newpatients"
                        ),
                        "extension": [
                            {
                                "url": "acceptingPatients",
                                "valueCodeableConcept": {"coding": [{"code": "newpt"}]},
                            }
                        ],
                    }
                ],
            }
        },
        {
            "resource": {
                "resourceType": "Location",
                "id": "location-1",
                "name": "Cigna Family Medicine Clinic",
                "address": {
                    "line": ["100 Main St"],
                    "city": "Hartford",
                    "state": "CT",
                    "postalCode": "06103",
                },
                "telecom": [{"system": "phone", "value": "860-555-0100"}],
            }
        },
        {
            "resource": {
                "resourceType": "Organization",
                "id": "org-1",
                "identifier": [
                    {"system": "http://hl7.org/fhir/sid/us-npi", "value": ORG_1_NPI}
                ],
                "name": "Hartford Family Health Group",
            }
        },
    ],
}

PAGE_2: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "link": [{"relation": "self", "url": PAGE_2_URL}],
    "entry": [
        {
            "resource": {
                "resourceType": "PractitionerRole",
                "id": "role-2",
                "practitioner": {"reference": "Practitioner/practitioner-jane-doe"},
                "organization": {"reference": "Organization/org-2"},
                "location": [{"reference": "Location/location-2"}],
                "specialty": [
                    {
                        "coding": [
                            {
                                "system": "http://nucc.org/provider-taxonomy",
                                "code": "207R00000X",
                                "display": "Internal Medicine",
                            }
                        ]
                    }
                ],
                "extension": [
                    {
                        "url": (
                            "http://hl7.org/fhir/us/davinci-pdex-plan-net/"
                            "StructureDefinition/newpatients"
                        ),
                        "extension": [
                            {
                                "url": "acceptingPatients",
                                "valueCodeableConcept": {"coding": [{"code": "existptonly"}]},
                            }
                        ],
                    }
                ],
            }
        },
        {
            "resource": {
                "resourceType": "Location",
                "id": "location-2",
                "name": "Cigna Internal Medicine Annex",
                "address": {
                    "line": ["200 Elm St"],
                    "city": "Hartford",
                    "state": "CT",
                    "postalCode": "06103",
                },
                "telecom": [{"system": "phone", "value": "860-555-0200"}],
            }
        },
        {
            "resource": {
                # No `identifier` at all - exercises the
                # missing-organization-NPI fallback (dedupe on FHIR id).
                "resourceType": "Organization",
                "id": "org-2",
                "name": "Hartford Internal Medicine Annex LLC",
            }
        },
    ],
}

MISSING_NPI_URL = "https://fhir.cigna.com/ProviderDirectory/v1/PractitionerRole?page=missing-npi"

MISSING_NPI_PAGE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "link": [],
    "entry": [
        {
            "resource": {
                "resourceType": "Practitioner",
                "id": NO_NPI_PRACTITIONER_ID,
                "identifier": [{"system": "http://example.org/local-id", "value": "abc123"}],
                "name": [{"family": "Roe", "given": ["Richard"], "suffix": ["DO"]}],
            }
        },
        {
            "resource": {
                "resourceType": "PractitionerRole",
                "id": "role-no-npi",
                "practitioner": {"reference": f"Practitioner/{NO_NPI_PRACTITIONER_ID}"},
                "specialty": [{"coding": [{"display": "General Practice"}]}],
            }
        },
    ],
}

MALFORMED_URL = "https://fhir.cigna.com/ProviderDirectory/v1/PractitionerRole?page=malformed"

MALFORMED_PAGE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "link": [],
    "entry": [
        # Deliberately malformed: no resourceType/id - discover() must skip
        # this entry, not raise.
        {"resource": {"foo": "bar"}},
        {"resource": None},
        {
            "resource": {
                "resourceType": "Practitioner",
                "id": "practitioner-ok",
                "identifier": [
                    {"system": "http://hl7.org/fhir/sid/us-npi", "value": MALFORMED_PAGE_NPI}
                ],
                "name": [{"family": "Smith", "given": ["Sam"]}],
            }
        },
        {
            "resource": {
                "resourceType": "PractitionerRole",
                "id": "role-ok",
                "practitioner": {"reference": "Practitioner/practitioner-ok"},
                "location": [{"reference": "Location/location-ok"}],
            }
        },
        {
            "resource": {
                "resourceType": "Location",
                "id": "location-ok",
                "name": "Sam Smith Clinic",
                "address": {
                    "line": ["1 Ok St"],
                    "city": "Hartford",
                    "state": "CT",
                    "postalCode": "06103",
                },
            }
        },
    ],
}


STATE_SCOPED_PRACTITIONER_NPI = "4444444440"
RI_LOCATION_IDS = ["loc-a", "loc-b"]

# Step 1 of the state-scoped pilot flow: `Location?address-state=RI`. Real
# Cigna Location ids are opaque hex strings ("d16-..."); these fixture ids
# are deliberately short/readable since only round-tripping through the
# adapter's own id handling is being tested, not the id format itself.
STATE_LOCATION_BUNDLE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "link": [],
    "entry": [
        {
            "resource": {
                "resourceType": "Location",
                "id": "loc-a",
                "name": "Providence Family Clinic",
                "address": {
                    "line": ["10 Ocean Ave"],
                    "city": "Providence",
                    "state": "RI",
                    "postalCode": "02903",
                },
            }
        },
        {
            "resource": {
                "resourceType": "Location",
                "id": "loc-b",
                "name": "Newport Health Center",
                "address": {
                    "line": ["20 Bay St"],
                    "city": "Newport",
                    "state": "RI",
                    "postalCode": "02840",
                },
            }
        },
    ],
}

# Step 2: `PractitionerRole?location=loc-a,loc-b&...` - only "loc-a" actually
# has a practitioner in this fixture, mirroring what live probing found
# (most Locations returned by an address-state search have zero linked
# PractitionerRoles).
STATE_ROLE_BUNDLE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "link": [],
    "entry": [
        {
            "resource": {
                "resourceType": "Practitioner",
                "id": "practitioner-ri",
                "identifier": [
                    {
                        "system": "http://hl7.org/fhir/sid/us-npi",
                        "value": STATE_SCOPED_PRACTITIONER_NPI,
                    }
                ],
                "name": [{"family": "Ocean", "given": ["Rhody"], "suffix": ["MD"]}],
            }
        },
        {
            "resource": {
                "resourceType": "PractitionerRole",
                "id": "role-ri",
                "practitioner": {"reference": "Practitioner/practitioner-ri"},
                "location": [{"reference": "Location/loc-a"}],
                "specialty": [{"coding": [{"display": "Family Medicine"}]}],
            }
        },
    ],
}


# ----------------------------------------------------------------------
# Discovery-mode validation fixtures (Phase E, Stage 4 - see
# NATIONAL_DISCOVERY_DESIGN.md and evidence/PHASE_E_CAPABILITY_PROBES.md).
#
# One shared "population" of three practitioners, all real (state_scoped
# and national each see a different slice of the SAME underlying resources,
# not different data) so the validation suite can exercise the project's
# revised invariant (see docs/adr/0001-national-discovery-strategy.md)
# against both strategies at once:
#
#   VALIDATION_SINGLE_STATE_NPI  - present only in RI. Both strategies see
#       the exact same one role/one location. No divergence is even
#       possible - the baseline "zero delta" case.
#   VALIDATION_SAFE_MULTI_STATE_NPI - 2 roles in RI, 1 in TX. state_scoped
#       (blind to TX) and national (sees both) both pick RI as the primary
#       location (more roles), by different routes but the same answer.
#       Only completeness (location_count, the locations list) differs -
#       the one delta V13's export-equivalence test is built to tolerate.
#   VALIDATION_RISK_MULTI_STATE_NPI - 1 role in RI, 2 in TX. state_scoped
#       has no choice but RI (it never learns TX exists); national picks TX
#       (more roles). A genuine primary-address contradiction for the same
#       NPI depending on which mode ran - Risk R1 - which the comparison
#       framework (see comparison.py) must detect and report, never
#       silently accept.
#
# All three share one Location (RI) enumerated by state_scoped's
# `Location?address-state=RI` step, one Organization, and are otherwise
# independent.
# ----------------------------------------------------------------------

VALIDATION_SINGLE_STATE_NPI = "5555555551"
VALIDATION_SAFE_MULTI_STATE_NPI = "5555555569"
VALIDATION_RISK_MULTI_STATE_NPI = "5555555577"

VALIDATION_RI_LOCATION_ID = "val-loc-ri"
VALIDATION_TX_LOCATION_ID = "val-loc-tx"
VALIDATION_ORG_ID = "val-org-shared"

_VALIDATION_RI_LOCATION: dict[str, Any] = {
    "resourceType": "Location",
    "id": VALIDATION_RI_LOCATION_ID,
    "name": "Providence Validation Clinic",
    "address": {
        "line": ["1 Validation Way"],
        "city": "Providence",
        "state": "RI",
        "postalCode": "02903",
    },
}

_VALIDATION_TX_LOCATION: dict[str, Any] = {
    "resourceType": "Location",
    "id": VALIDATION_TX_LOCATION_ID,
    "name": "Austin Validation Annex",
    "address": {
        "line": ["2 Validation Way"],
        "city": "Austin",
        "state": "TX",
        "postalCode": "73301",
    },
}

_VALIDATION_ORG: dict[str, Any] = {
    "resourceType": "Organization",
    "id": VALIDATION_ORG_ID,
    "name": "Validation Provider Group",
}


def _validation_practitioner(prac_id: str, npi: str, family: str) -> dict[str, Any]:
    return {
        "resourceType": "Practitioner",
        "id": prac_id,
        "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": npi}],
        "name": [{"family": family, "given": ["Validation"]}],
    }


def _validation_role(role_id: str, prac_id: str, location_id: str) -> dict[str, Any]:
    return {
        "resourceType": "PractitionerRole",
        "id": role_id,
        "practitioner": {"reference": f"Practitioner/{prac_id}"},
        "location": [{"reference": f"Location/{location_id}"}],
        "organization": {"reference": f"Organization/{VALIDATION_ORG_ID}"},
    }


_VALIDATION_PRACTITIONER_SINGLE = _validation_practitioner(
    "val-prac-single", VALIDATION_SINGLE_STATE_NPI, "Single"
)
_VALIDATION_PRACTITIONER_SAFE = _validation_practitioner(
    "val-prac-safe", VALIDATION_SAFE_MULTI_STATE_NPI, "Safe"
)
_VALIDATION_PRACTITIONER_RISK = _validation_practitioner(
    "val-prac-risk", VALIDATION_RISK_MULTI_STATE_NPI, "Risk"
)

# Single: one RI role, nothing else - present identically to both strategies.
_VALIDATION_ROLE_SINGLE_1 = _validation_role(
    "val-role-single-1", "val-prac-single", VALIDATION_RI_LOCATION_ID
)
# Safe: 2 RI roles + 1 TX role - RI wins the most-roles tiebreak either way.
_VALIDATION_ROLE_SAFE_1 = _validation_role(
    "val-role-safe-1", "val-prac-safe", VALIDATION_RI_LOCATION_ID
)
_VALIDATION_ROLE_SAFE_2 = _validation_role(
    "val-role-safe-2", "val-prac-safe", VALIDATION_RI_LOCATION_ID
)
_VALIDATION_ROLE_SAFE_3_TX = _validation_role(
    "val-role-safe-3", "val-prac-safe", VALIDATION_TX_LOCATION_ID
)
# Risk: 1 RI role + 2 TX roles - state_scoped can only ever pick RI (it's all
# it knows about); national picks TX (more roles). Deliberate contradiction.
_VALIDATION_ROLE_RISK_1 = _validation_role(
    "val-role-risk-1", "val-prac-risk", VALIDATION_RI_LOCATION_ID
)
_VALIDATION_ROLE_RISK_2_TX = _validation_role(
    "val-role-risk-2", "val-prac-risk", VALIDATION_TX_LOCATION_ID
)
_VALIDATION_ROLE_RISK_3_TX = _validation_role(
    "val-role-risk-3", "val-prac-risk", VALIDATION_TX_LOCATION_ID
)

# state_scoped's `Location?address-state=RI` step (see _location_search_url).
VALIDATION_STATE_LOCATION_BUNDLE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "link": [],
    "entry": [{"resource": _VALIDATION_RI_LOCATION}],
}

# state_scoped's `PractitionerRole?location=val-loc-ri&...` step (see
# _practitioner_role_by_locations_url). Deliberately omits loc-tx and every
# TX-only role - a real Cigna server has no way to return them for a
# location-scoped query, which is the whole truncation this fixture models
# (see PHASE_E_CAPABILITY_PROBES.md finding 2 and Risk R1).
VALIDATION_STATE_ROLE_BUNDLE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "link": [],
    "entry": [
        {"resource": _VALIDATION_PRACTITIONER_SINGLE},
        {"resource": _VALIDATION_ROLE_SINGLE_1},
        {"resource": _VALIDATION_PRACTITIONER_SAFE},
        {"resource": _VALIDATION_ROLE_SAFE_1},
        {"resource": _VALIDATION_ROLE_SAFE_2},
        {"resource": _VALIDATION_PRACTITIONER_RISK},
        {"resource": _VALIDATION_ROLE_RISK_1},
        {"resource": _VALIDATION_ORG},
    ],
}

# national's unscoped `_lastUpdated` window - the complete picture, all
# three practitioners' every role and both locations (see Finding 1).
VALIDATION_NATIONAL_BUNDLE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "link": [],
    "entry": [
        {"resource": _VALIDATION_PRACTITIONER_SINGLE},
        {"resource": _VALIDATION_ROLE_SINGLE_1},
        {"resource": _VALIDATION_PRACTITIONER_SAFE},
        {"resource": _VALIDATION_ROLE_SAFE_1},
        {"resource": _VALIDATION_ROLE_SAFE_2},
        {"resource": _VALIDATION_ROLE_SAFE_3_TX},
        {"resource": _VALIDATION_PRACTITIONER_RISK},
        {"resource": _VALIDATION_ROLE_RISK_1},
        {"resource": _VALIDATION_ROLE_RISK_2_TX},
        {"resource": _VALIDATION_ROLE_RISK_3_TX},
        {"resource": _VALIDATION_RI_LOCATION},
        {"resource": _VALIDATION_TX_LOCATION},
        {"resource": _VALIDATION_ORG},
    ],
}


def make_fetcher(pages: dict[str, dict[str, Any]]) -> PageFetcher:
    """Build a fake :data:`~adapters.providers.cigna_fhir.adapter.PageFetcher`
    serving canned bundles from a ``{url: bundle}`` mapping, with zero
    network access."""

    def fetch(url: str) -> dict[str, Any]:
        return pages[url]

    return fetch
