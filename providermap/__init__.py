"""ProviderMap - a reusable provider-directory ingestion pipeline.

Turns a healthcare system's public provider directory into a queryable
SQLite database mapping provider -> primary site of care, via a pluggable
:class:`adapters.base.SiteAdapter` per site. AdventHealth ships as the
reference adapter.
"""

__version__ = "1.0.0"
