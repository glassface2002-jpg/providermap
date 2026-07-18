"""The organization-adapter contract.

The counterpart to :class:`adapters.base.SiteAdapter` (which covers provider
*directories*), for ingesting healthcare *organizations* - hospitals, health
systems, clinics, medical groups, facilities. Where a provider adapter is
built around fetching and parsing one website's profile pages, an
organization adapter is built around a *dataset* (a CMS bulk download, a
registry export, a manual list), so its shape is deliberately simpler and
source-oriented:

    discover()   ->  yield raw records from the source
    extract()    ->  pull the fields we care about out of one raw record
    normalize()  ->  turn those fields into a canonical Organization

This mirrors the provider pipeline's discover -> enrich -> normalize spirit
without forcing a provider-shaped interface onto a fundamentally different
kind of source. Concrete adapters (e.g. a future ``cms_hospitals``) live in
``adapters/organizations/<source>/`` and register themselves in
``adapters/__init__.py``'s ``ORGANIZATION_ADAPTERS``.

No concrete organization adapter ships yet - this is the foundation only.
See ``ROADMAP.md`` for how it fits the platform vision.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from providermap.config import Config
from providermap.models import Organization


class OrganizationAdapter(ABC):
    """Everything the pipeline needs to know to ingest one organization source.

    Like :class:`~adapters.base.SiteAdapter`, an adapter is *not* responsible
    for HTTP, rate limiting, caching, or storage - only for answering "what
    does this source's data look like, and what is one canonical
    :class:`~providermap.models.Organization` for a given raw record".
    """

    #: Short, stable identifier used in logs, run records, and (eventually)
    #: the CLI. Lowercase, no spaces. e.g. "cms_hospitals".
    name: str

    #: Human-facing provenance label, matching a row in the ``sources`` table.
    #: e.g. "CMS". Recorded on every Organization this adapter produces.
    source: str

    def __init__(self, config: Config) -> None:
        self.config = config

    @abstractmethod
    def discover(self) -> Iterator[dict[str, Any]]:
        """Yield raw organization records from the source, one at a time.

        "Raw" means whatever shape the source hands over (a CSV row, a JSON
        object, ...) - normalization happens later, not here. Yielding rather
        than returning a list keeps large bulk datasets streaming instead of
        loading wholesale into memory.
        """

    @abstractmethod
    def extract(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Pull the fields we care about out of one raw record.

        Maps this source's own column/field names onto the neutral keys the
        adapter's :meth:`normalize` expects, dropping everything irrelevant.
        Kept separate from :meth:`normalize` so source-specific field mapping
        and canonicalization can be tested independently.
        """

    @abstractmethod
    def normalize(self, extracted: dict[str, Any]) -> Organization:
        """Turn extracted fields into a canonical :class:`Organization`.

        Responsible for canonicalization that isn't source-specific -
        building ``normalized_name``, tidying address casing, tagging
        ``source``/``source_id`` - so that two adapters describing the same
        real hospital produce comparable rows.
        """
