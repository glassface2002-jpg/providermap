"""Provider <-> organization linking (ROADMAP.md stage 5, completing the
provider -> organization -> location chain).

Matching here is deliberately **exact-name-only**, never fuzzy. Contrast
with ``providermap/website_enrichment.py``: a low-confidence website guess
degrades a nice-to-have field, but mislinking a provider to the wrong
hospital corrupts the answer to the actual question this project exists to
answer (*where does this provider actually practice?*). A provider whose
``hospital_affiliation`` / ``practice_name`` doesn't exactly match an
organization already in the database is left unlinked rather than guessed
at - see ``providermap/database.py``'s ``providers_for_organization_linking``
for how an unlinked provider stays eligible for a future, more specific
organization adapter or a corrected name to link against.
"""

from __future__ import annotations

from .models import Organization
from .parser_utils import normalize_org_name


def match_organization(
    candidate_name: str | None,
    organizations_by_normalized_name: dict[str, Organization],
) -> Organization | None:
    """Exact match on normalized name only - see module docstring for why
    this deliberately doesn't fuzzy-match."""
    key = normalize_org_name(candidate_name)
    if not key:
        return None
    return organizations_by_normalized_name.get(key)


def best_match_for_provider(
    hospital_affiliation: str | None,
    practice_name: str | None,
    organizations_by_normalized_name: dict[str, Organization],
) -> Organization | None:
    """Try ``hospital_affiliation`` first - when present (from JSON-LD, see
    ``ProfilePage``), it's a more deliberate signal of organizational
    affiliation than ``practice_name``, which is populated for nearly every
    provider but is closer to "where the phone number and address came
    from" than "who they're affiliated with".
    """
    return match_organization(
        hospital_affiliation, organizations_by_normalized_name
    ) or match_organization(practice_name, organizations_by_normalized_name)
