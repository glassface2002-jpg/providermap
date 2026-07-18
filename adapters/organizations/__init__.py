"""Organization adapters.

Ingest healthcare *organizations* (hospitals, health systems, clinics,
medical groups, facilities), as opposed to the provider *directory* adapters
in the sibling packages. Add a new source by writing an
:class:`~adapters.organizations.base.OrganizationAdapter` subclass in
``adapters/organizations/<source>/`` and registering it in
``adapters/__init__.py``'s ``ORGANIZATION_ADAPTERS``.

No concrete organization adapter ships yet - this package is the foundation
(see ``ROADMAP.md``).
"""

from __future__ import annotations

from .base import OrganizationAdapter

__all__ = ["OrganizationAdapter"]
