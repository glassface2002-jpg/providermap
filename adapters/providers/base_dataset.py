"""The provider-dataset-adapter contract.

The counterpart to :class:`adapters.organizations.base.OrganizationAdapter`,
for ingesting *provider* directories that are published as a queryable
dataset/API rather than scraped from rendered HTML - e.g. a FHIR Provider
Directory. Where :class:`~adapters.base.SiteAdapter` is built around fetching
and parsing one website's profile pages, a provider dataset adapter is built
around structured records the source already exposes, so its shape mirrors
:class:`~adapters.organizations.base.OrganizationAdapter` rather than
``SiteAdapter``:

    discover()   ->  yield one complete raw record per practitioner
    extract()    ->  pull the fields we care about out of one raw record
    normalize()  ->  turn those fields into a canonical NormalizedProvider

A provider dataset source can describe one practitioner across several
practice locations and organization affiliations at once - something a bare
:class:`~providermap.models.Provider` (one primary address) can't hold.
:class:`NormalizedProvider` carries that full relationship set as an
in-memory transport type. It intentionally lives here, not in
``providermap/models.py``: it has no database table of its own and is never
read or written directly by :class:`~providermap.database.Database` - a
caller unpacks it into existing ``Provider``/``Location``/``Organization``
upserts (see ``providermap/cli.py::cmd_ingest_providers``), so it belongs
with the adapter layer that produces it, not the shared model vocabulary
every other module imports.

Concrete adapters live in ``adapters/providers/<source>/`` and register in
``adapters/__init__.py``'s ``PROVIDER_DATASET_ADAPTERS``. This is a third
registry, parallel to ``ADAPTERS`` (:class:`~adapters.base.SiteAdapter`,
scraped provider directories - e.g. ``adventhealth``) and
``ORGANIZATION_ADAPTERS`` (:class:`~adapters.organizations.base.OrganizationAdapter`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from providermap.checkpoint import CheckpointStore
from providermap.config import Config
from providermap.models import Location, Organization, Provider


@dataclass
class NormalizedProvider:
    """One practitioner's full relationship set, as produced by a
    :class:`ProviderDatasetAdapter`.

    Pure in-memory transport - no database table, no schema change.
    ``provider`` carries only a *primary* location's address (there's exactly
    one address on :class:`~providermap.models.Provider`); ``locations`` and
    ``organizations`` carry everything else the source reported, so a caller
    that wants the full picture (every practice location, every organization
    affiliation) has it, while ``Database.upsert_provider`` keeps working
    unmodified.
    """

    provider: Provider
    locations: list[Location] = field(default_factory=list)
    # (organization, relationship label) - the label is source-defined free
    # text (e.g. "affiliated"), passed straight through to the existing
    # ``Database.link_provider_organization(source=...)`` call; it is not a
    # new column anywhere.
    organizations: list[tuple[Organization, str]] = field(default_factory=list)


class ProviderDatasetAdapter(ABC):
    """Everything the pipeline needs to know to ingest one provider dataset
    source.

    Like :class:`~adapters.organizations.base.OrganizationAdapter`, an
    adapter is *not* responsible for HTTP, rate limiting, caching, or
    storage - only for answering "what does this source's data look like,
    and what is one canonical :class:`NormalizedProvider` for a given raw
    record".
    """

    #: Short, stable identifier used in logs, run records, and the CLI
    #: (``--provider-adapter cigna_fhir``). Lowercase, no spaces.
    name: str

    #: Human-facing provenance label, matching a row in the ``sources`` table
    #: and passed as ``source=`` to ``Database.link_provider_organization``.
    #: e.g. "Cigna".
    source: str

    #: Capability declaration, not a conditional: a provider dataset/API
    #: source identifies a practitioner by a stable id (NPI/FHIR id), not a
    #: browsable profile page, so ``False`` is the right default for this
    #: whole category of adapter. ``cmd_ingest_providers`` reads this to
    #: build a copy of ``config.npi`` (a :class:`~providermap.models.NpiPolicy`)
    #: with ``require_profile_url`` set accordingly before calling
    #: :func:`~providermap.validator.validate_provider` - the validator
    #: itself stays adapter-agnostic; only the policy passed to it varies.
    #: A future dataset adapter whose source *does* publish profile pages
    #: can override this to ``True``.
    requires_profile_url: bool = False

    def __init__(
        self, config: Config, checkpoint_store: CheckpointStore | None = None
    ) -> None:
        self.config = config
        #: Optional durable resume support (see ``providermap/checkpoint.py``).
        #: ``None`` (the default) reproduces the original, non-resumable
        #: behavior exactly - a concrete adapter's ``discover()`` decides
        #: what a "phase"/"work item" means for its own source and is free
        #: to ignore this entirely if it has no long-running phases worth
        #: checkpointing. Reusable infrastructure, not a required adapter
        #: capability.
        self.checkpoint_store = checkpoint_store

    @abstractmethod
    def discover(self) -> Iterator[dict[str, Any]]:
        """Yield one complete raw record per practitioner from the source.

        A source whose records for one practitioner can be split across
        several paginated responses (e.g. a FHIR search ``Bundle``) must
        resolve that *inside* ``discover()`` - grouping by the
        practitioner's stable identity across every page before yielding -
        so each item this generator produces is already one complete
        practitioner's data. :meth:`extract`/:meth:`normalize` never see a
        partial record split across a page boundary.
        """

    @abstractmethod
    def extract(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Pull the fields we care about out of one raw record.

        Maps this source's own field names onto the neutral keys
        :meth:`normalize` expects, dropping everything irrelevant. Kept
        separate from :meth:`normalize` so source-specific field mapping can
        be tested independently.
        """

    @abstractmethod
    def normalize(self, extracted: dict[str, Any]) -> NormalizedProvider:
        """Turn extracted fields into a canonical :class:`NormalizedProvider`.

        Responsible for canonicalization that isn't source-specific -
        picking one primary location to populate ``Provider``'s address
        fields when the source reports several, building each
        :class:`~providermap.models.Location`'s ``address_key``, tagging
        ``source``/``source_id`` on every
        :class:`~providermap.models.Organization` - so that two adapters
        describing the same real practitioner produce comparable output.
        """
