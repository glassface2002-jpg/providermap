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

Commit batching mirrors ``CheckpointStore.commit_every`` exactly, and for
the same measured reason: commit-per-write collapsed to single-digit
writes/sec at realistic file sizes in the offline benchmark that motivated
the checkpoint fix, and a spool absorbs an order of magnitude more writes
per state than the checkpoint does. The durability rule that makes batching
safe here: **the spool must be flushed before any checkpoint item that
refers to its contents is marked done** (see
``NationalDiscovery._handle_window_result`` and the spool-mode state-scoped
batch path). The checkpoint is allowed to lag the spool - a crash then
merely re-fetches work whose spooled rows are re-upserted idempotently -
but the checkpoint must never lead it, or resume would skip work whose
spooled rows never reached disk.

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

    def __init__(self, path: str | Path, commit_every: int = 1) -> None:
        if commit_every < 1:
            raise ValueError(f"commit_every must be >= 1, got {commit_every}")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._commit_every = commit_every
        self._writes_since_commit = 0

    def _write_committed(self) -> None:
        """Call after every ``upsert`` - commits immediately if
        ``commit_every`` is ``1`` (the default), or once every
        ``commit_every`` calls otherwise. Mirrors
        ``CheckpointStore._write_committed`` exactly."""
        self._writes_since_commit += 1
        if self._writes_since_commit >= self._commit_every:
            self.conn.commit()
            self._writes_since_commit = 0

    def flush(self) -> None:
        """Commit any writes batched by ``commit_every`` that haven't been
        flushed yet. MUST be called before any checkpoint item referring to
        spooled contents is marked done - see the module docstring's
        durability rule. Called automatically by :meth:`close`."""
        if self._writes_since_commit:
            self.conn.commit()
            self._writes_since_commit = 0

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
        self._write_committed()

    def get(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT body FROM spool_resources WHERE resource_type=? AND resource_id=?",
            (resource_type, resource_id),
        ).fetchone()
        return json.loads(row["body"]) if row else None

    def exists(self, resource_type: str, resource_id: str) -> bool:
        """Membership check without deserializing the stored body - the
        duplicate-stats check runs once per fetched resource (~80k+ per
        state), and ``get()`` there would ``json.loads`` a full FHIR body
        just to be discarded."""
        row = self.conn.execute(
            "SELECT 1 FROM spool_resources WHERE resource_type=? AND resource_id=? LIMIT 1",
            (resource_type, resource_id),
        ).fetchone()
        return row is not None

    def clear(self) -> None:
        """Discard every spooled resource. The spool is derived, rebuildable
        state - callers clear it on a FRESH (non-resume) discovery start so
        rows left by a previous run/mode/scope can never leak into this
        run's materialize pass (stale rows would resurrect server-deleted
        resources and inflate yields)."""
        self.conn.execute("DELETE FROM spool_resources")
        self.conn.commit()
        self._writes_since_commit = 0

    def distinct_practitioner_refs(self) -> list[str]:
        """Every distinct practitioner a spooled ``PractitionerRole``
        references, in deterministic (sorted) order - the materialize
        pass's outer loop. This list is bounded by the number of
        practitioners, not the number of resources spooled (locations,
        organizations, duplicate re-includes), which is what keeps this
        step's own memory use small even at national scale. The ORDER BY
        makes emission order a pure function of the spooled data rather
        than of SQLite internals.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT practitioner_ref FROM spool_resources "
            "WHERE practitioner_ref IS NOT NULL ORDER BY practitioner_ref"
        ).fetchall()
        return [row["practitioner_ref"] for row in rows]

    def roles_for_practitioner(self, practitioner_ref: str) -> list[dict[str, Any]]:
        """Every spooled ``PractitionerRole`` for one practitioner, in
        deterministic (resource-id) order - an indexed lookup, not a scan,
        so materializing one practitioner never touches more than that
        practitioner's own rows. The ORDER BY exists for the same reason as
        ``distinct_practitioner_refs``'s: downstream grouping must not
        depend on storage-engine iteration order."""
        rows = self.conn.execute(
            "SELECT body FROM spool_resources WHERE practitioner_ref=? ORDER BY resource_id",
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
        self.flush()
        self.conn.close()
