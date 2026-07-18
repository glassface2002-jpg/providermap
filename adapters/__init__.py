"""Adapter registries.

ProviderMap ingests two kinds of healthcare data, each behind its own
adapter contract and registry:

* **Provider directories** - :class:`~adapters.base.SiteAdapter`, registered
  in :data:`ADAPTERS`. Add a health system by writing a subclass and
  registering it here; nothing in :mod:`providermap` changes.
* **Organizations** - :class:`~adapters.organizations.base.OrganizationAdapter`,
  registered in :data:`ORGANIZATION_ADAPTERS` (empty for now - the
  organization ingestion side is foundation-only; see ``ROADMAP.md``).

The two registries are intentionally parallel so the CLI and pipeline can
treat "which adapter" uniformly once organization sources come online.
"""

from __future__ import annotations

from .base import SiteAdapter
from .organizations.base import OrganizationAdapter
from .providers.adventhealth.adapter import AdventHealthAdapter

# ---------------------------------------------------------------------- #
# Provider directory adapters
# ---------------------------------------------------------------------- #

ADAPTERS: dict[str, type[SiteAdapter]] = {
    AdventHealthAdapter.name: AdventHealthAdapter,
}


def get_adapter_class(name: str) -> type[SiteAdapter]:
    """Look up a registered provider adapter class by name.

    Raises ``ValueError`` listing the available names if ``name`` isn't
    registered, rather than a bare ``KeyError``.
    """
    try:
        return ADAPTERS[name]
    except KeyError:
        available = ", ".join(sorted(ADAPTERS)) or "(none registered)"
        raise ValueError(f"Unknown adapter '{name}'. Available: {available}") from None


# ---------------------------------------------------------------------- #
# Organization adapters (foundation - none registered yet)
# ---------------------------------------------------------------------- #

ORGANIZATION_ADAPTERS: dict[str, type[OrganizationAdapter]] = {}


def get_organization_adapter_class(name: str) -> type[OrganizationAdapter]:
    """Look up a registered organization adapter class by name.

    Mirrors :func:`get_adapter_class`. The registry is empty until the first
    concrete organization source (e.g. ``cms_hospitals``) ships.
    """
    try:
        return ORGANIZATION_ADAPTERS[name]
    except KeyError:
        available = ", ".join(sorted(ORGANIZATION_ADAPTERS)) or "(none registered)"
        raise ValueError(f"Unknown organization adapter '{name}'. Available: {available}") from None


__all__ = [
    "ADAPTERS",
    "ORGANIZATION_ADAPTERS",
    "OrganizationAdapter",
    "SiteAdapter",
    "get_adapter_class",
    "get_organization_adapter_class",
]
