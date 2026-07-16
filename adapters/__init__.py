"""Adapter registry.

Adding support for a new health system's provider directory means writing a
new :class:`~adapters.base.SiteAdapter` subclass and registering it here.
Nothing in :mod:`providermap` needs to change.
"""

from __future__ import annotations

from .adventhealth.adapter import AdventHealthAdapter
from .base import SiteAdapter

ADAPTERS: dict[str, type[SiteAdapter]] = {
    AdventHealthAdapter.name: AdventHealthAdapter,
}


def get_adapter_class(name: str) -> type[SiteAdapter]:
    """Look up a registered adapter class by name.

    Raises ``ValueError`` listing the available names if ``name`` isn't
    registered, rather than a bare ``KeyError``.
    """
    try:
        return ADAPTERS[name]
    except KeyError:
        available = ", ".join(sorted(ADAPTERS)) or "(none registered)"
        raise ValueError(f"Unknown adapter '{name}'. Available: {available}") from None


__all__ = ["ADAPTERS", "SiteAdapter", "get_adapter_class"]
