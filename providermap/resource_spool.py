"""Durable, disk-backed resource cache for national-scale FHIR discovery
(Phase E, Stage 2 - see ``adapters/providers/cigna_fhir/NATIONAL_DISCOVERY_DESIGN.md``).

The existing state-scoped discovery strategy caches every fetched FHIR
resource in an in-memory ``dict`` for the life of one ``discover()`` call -
fine at one state's scale, measured to reach 3.87 GB at Florida's. National
discovery's whole point is to never hold more than one page in memory at
once, so instead of one big dict it spools every resource straight to this
module's SQLite-backed store, then a second "materialize" pass reads back
out of it one practitioner at a time.

Like :class:`~providermap.checkpoint.CheckpointStore`, this is a dedicated
SQLite file, separate from ``providermap.db`` - operational state, not
queryable provider data. It never needs a schema-version bump, and the file
is freely deletable with zero effect on stored provider/organization data.
Every write is a single, immediately-committed statement (see
``CheckpointStore``'s own docstring for why that's what makes durability
here simple to reason about rather than something this module has to
re-litigate).

``practitioner_ref`` is stored as an explicit, indexed column - computed by
the caller (via the same ``_parse_reference`` helper the rest of the adapter
already uses) at insert time - rather than extracted from the JSON body via
a SQL JSON function. This avoids any dependency on the SQLite build's JSON1
support, and keeps the one query the materialize pass actually needs
(distinct practitioners, and each one's roles) a plain indexed lookup.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spool_resources (
    resource_type    TEXT NOT NULL,
    resource_id      TEXT NOT NULL,
    body             TEXT NOT NULL,   -- JSON
    practitioner_ref TEXT,            -- set only for PractitionerRole rows
    PRIMARY KEY (resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_spool_practitioner_ref
    ON spool_resources (practitioner_ref)
    WHERE practitioner_ref IS NOT NULL;
"""


class ResourceSpool:
    """A disk-backed ``(resourceType, id) -> resource body`` cache, plus one
    extra indexed lookup (``roles_for_practitioner``) that the materialize
    pass needs. Not a general-purpose store - just what national discovery
    requires, kept exactly as thin as :class:`~providermap.checkpoint.CheckpointStore`
    already models for a similar kind of operational state.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def upsert(
        self,
        resource_type: str,
        resource_id: str,
        body: dict[str, Any],
        practitioner_ref: str | None = None,
    ) -> None:
        """Insert or replace one resource. ``INSERT OR REPLACE`` on the
        ``(resource_type, resource_id)`` primary key is what absorbs the
        measured 79% duplicate-Organization re-includes as cheap
        idempotent writes instead of unbounded in-memory growth."""
        self.conn.execute(
            "INSERT OR REPLACE INTO spool_resources "
            "(resource_type, resource_id, body, practitioner_ref) VALUES (?, ?, ?, ?)",
            (resource_type, resource_id, json.dumps(body), practitioner_ref),
        )
        self.conn.commit()

    def get(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT body FROM spool_resources WHERE resource_type=? AND resource_id=?",
            (resource_type, resource_id),
        ).fetchone()
        return json.loads(row["body"]) if row else None

    def distinct_practitioner_refs(self) -> list[str]:
        """Every distinct practitioner a spooled ``PractitionerRole``
        references - the materialize pass's outer loop. This list is
        bounded by the number of practitioners, not the number of resources
        spooled (locations, organizations, duplicate re-includes), which is
        what keeps this step's own memory use small even at national scale.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT practitioner_ref FROM spool_resources "
            "WHERE practitioner_ref IS NOT NULL"
        ).fetchall()
        return [row["practitioner_ref"] for row in rows]

    def roles_for_practitioner(self, practitioner_ref: str) -> list[dict[str, Any]]:
        """Every spooled ``PractitionerRole`` for one practitioner - an
        indexed lookup, not a scan, so materializing one practitioner never
        touches more than that practitioner's own rows."""
        rows = self.conn.execute(
            "SELECT body FROM spool_resources WHERE practitioner_ref=?",
            (practitioner_ref,),
        ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def resource_counts_by_type(self) -> dict[str, int]:
        """Distinct resource count per type - used for progress reporting
        and the bounded-memory validation (V9), not by discovery itself."""
        rows = self.conn.execute(
            "SELECT resource_type, COUNT(*) AS n FROM spool_resources GROUP BY resource_type"
        ).fetchall()
        return {row["resource_type"]: row["n"] for row in rows}

    def close(self) -> None:
        self.conn.close()
