"""Compares two discovery strategies' normalized output for the same
source data.

Phase E, Stage 4 - Implementation Directive item 5 ("comparison/dry-run
support"). This is the tool the V1-V13 validation tests
(``tests/test_cigna_fhir_validation.py``) are built around, and the
mechanism that operationalizes the project's revised invariant (see
``NATIONAL_DISCOVERY_DESIGN.md`` and ``docs/adr/0001-national-discovery-strategy.md``):

    1. Identity is absolute for any NPI both modes produce.
    2. Relationships are monotonic - one strategy's location set for a
       shared NPI must be a subset of the other's, never contradictory.
    3. Scope-derived scalars (the primary location, and everything derived
       from it) must never silently disagree - a divergence must be
       reported, never picked-and-forgotten (Risk R1).

This module only detects and reports; it renders no verdict on which side
"should" be more complete - that judgment belongs to the caller/test, since
``a``/``b`` are just caller-labeled inputs (e.g. state_scoped vs. national).

Deliberately package-local (``adapters/providers/cigna_fhir/``), not shared
adapter infrastructure - the same promotion criteria as ``DiscoveryStrategy``
itself (see ``discovery.py``'s module docstring): promote only once a second
adapter demonstrates the same comparison need in working code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adapters.providers.base_dataset import NormalizedProvider

# Practitioner-derived identity fields (invariant rule 1) - deliberately
# excludes specialty/taxonomy/practice_name/address/location_count, which
# are scope-sensitive by design (rule 3) and checked separately via
# primary_location_divergences below.
_IDENTITY_FIELDS: tuple[str, ...] = (
    "full_name",
    "first_name",
    "last_name",
    "credentials",
    "languages",
    "npi_valid",
)


@dataclass
class PrimaryLocationDivergence:
    npi: str
    a_address: tuple[str | None, str | None, str | None, str | None]
    b_address: tuple[str | None, str | None, str | None, str | None]


@dataclass
class DiscoveryComparison:
    """Result of comparing two ``{npi: NormalizedProvider}`` maps."""

    only_in_a: set[str] = field(default_factory=set)
    only_in_b: set[str] = field(default_factory=set)
    identity_mismatches: dict[str, list[str]] = field(default_factory=dict)
    # npi -> address_keys present in `a`'s locations but missing from `b`'s.
    relationship_regressions: dict[str, set[str]] = field(default_factory=dict)
    primary_location_divergences: list[PrimaryLocationDivergence] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """No invariant violation for any NPI shared by both sides.

        Does NOT require ``a`` and ``b`` to cover the same NPIs, or the
        same locations, per NPI - one side legitimately knowing more is
        the entire point of national discovery, not a violation. This is
        ``True`` only when nothing shared between them ever contradicts.
        """
        return not (
            self.identity_mismatches
            or self.relationship_regressions
            or self.primary_location_divergences
        )


def compare_normalized_providers(
    a: dict[str, NormalizedProvider], b: dict[str, NormalizedProvider]
) -> DiscoveryComparison:
    """Compare two discovery strategies' normalized output, keyed by NPI.

    Checks only the NPIs present on both sides - one side having NPIs the
    other lacks is recorded in ``only_in_a``/``only_in_b``, not treated as
    a mismatch (a scope-limited strategy is expected to see fewer).
    """
    comparison = DiscoveryComparison(
        only_in_a=set(a) - set(b),
        only_in_b=set(b) - set(a),
    )

    for npi in sorted(set(a) & set(b)):
        pa, pb = a[npi].provider, b[npi].provider

        mismatches = [f for f in _IDENTITY_FIELDS if getattr(pa, f) != getattr(pb, f)]
        if mismatches:
            comparison.identity_mismatches[npi] = mismatches

        keys_a = {loc.address_key for loc in a[npi].locations if loc.address_key}
        keys_b = {loc.address_key for loc in b[npi].locations if loc.address_key}
        missing_from_b = keys_a - keys_b
        if missing_from_b:
            comparison.relationship_regressions[npi] = missing_from_b

        addr_a = (pa.address, pa.city, pa.state, pa.zip)
        addr_b = (pb.address, pb.city, pb.state, pb.zip)
        if addr_a != addr_b:
            comparison.primary_location_divergences.append(
                PrimaryLocationDivergence(npi=npi, a_address=addr_a, b_address=addr_b)
            )

    return comparison
