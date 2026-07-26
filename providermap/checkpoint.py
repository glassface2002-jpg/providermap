"""Durable checkpoint/resume infrastructure for long-running, multi-phase
ingestion work.

Not tied to Cigna, FHIR, or even provider datasets specifically - a
:class:`CheckpointStore` just persists, for a given ``checkpoint_id``, which
named **phases** exist and which opaque **work-item keys** within each phase
are ``done``/``error`` (anything else is implicitly ``pending``), plus a
small JSON-serializable payload attached to each done item. What a "phase" or
"work item" *means* is entirely up to the caller - this module only
guarantees the bookkeeping survives a crash, a Ctrl-C, a reboot, or a
deliberate restart.

Built for :class:`~adapters.providers.cigna_fhir.adapter.CignaFhirAdapter`'s
two-step ``discover()`` (see that module for why full national ingestion is
measured in hours, not minutes - checkpointing is what makes a multi-hour run
survivable), but designed to be adopted by any future provider dataset
adapter with the same "many independent fetch units, long total runtime"
shape - see ``adapters/providers/base_dataset.py``'s
``ProviderDatasetAdapter.__init__``.

Storage: a dedicated SQLite file, separate from the main ``providermap.db``.
Checkpoint state is operational/ephemeral (progress bookkeeping for a fetch
that hasn't finished), not queryable provider data - keeping it out of the
main schema means it never needs a schema-version bump, and a checkpoint file
can be deleted at any time with zero effect on stored provider/organization
data. Every write is a single, immediately-committed SQLite statement, so a
process killed at any point leaves the store in a valid, resumable state -
whatever was already marked ``done`` stays ``done``; nothing is ever
half-written (SQLite's own atomicity is what provides this, not any locking
this module adds).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoint_runs (
    checkpoint_id TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    completed_at  TEXT
);

CREATE TABLE IF NOT EXISTS checkpoint_items (
    checkpoint_id TEXT NOT NULL,
    phase         TEXT NOT NULL,
    key           TEXT NOT NULL,
    status        TEXT NOT NULL,  -- 'done' | 'error' - no row at all means 'pending'
    payload       TEXT,           -- JSON, present when status='done'
    error         TEXT,           -- present when status='error'
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (checkpoint_id, phase, key)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PhaseProgress:
    """How much of one phase is done, for progress visibility."""

    done: int = 0
    error: int = 0


class CheckpointStore:
    """Durable phase/work-item progress tracking for one or more resumable
    runs, identified by caller-chosen ``checkpoint_id`` strings.

    One store can hold multiple independent runs (e.g. Cigna scoped to "RI"
    and Cigna scoped to "FL" are unrelated checkpoint ids in the same file) -
    callers pick a ``checkpoint_id`` that's stable for "the same logical run"
    and different for "a different logical run", typically derived from the
    adapter name plus whatever scopes it (see
    ``CignaFhirAdapter``'s ``f"{self.name}:{self._state or 'unscoped'}"``).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # run lifecycle
    # ------------------------------------------------------------------ #

    def start_or_resume(self, checkpoint_id: str) -> bool:
        """Register a run, or resume an interrupted one. Returns ``True`` if
        this ``checkpoint_id`` had a prior run that was **not** marked
        complete (i.e. was interrupted) - the caller should read back
        :meth:`done_payloads` and skip already-done work. Returns ``False``
        for a genuinely fresh start.

        A ``checkpoint_id`` whose prior run *did* complete is treated as a
        fresh start too (its old items are cleared first) rather than
        forever refusing to run again - checkpointing exists to survive one
        run's interruption, not to make re-ingesting the same scope a
        one-time-only operation (a periodic refresh of the same state should
        work exactly like the first run).
        """
        row = self.conn.execute(
            "SELECT completed_at FROM checkpoint_runs WHERE checkpoint_id=?", (checkpoint_id,)
        ).fetchone()
        if row is not None and row["completed_at"] is None:
            return True
        if row is not None:
            self.clear(checkpoint_id)
        self.conn.execute(
            "INSERT INTO checkpoint_runs (checkpoint_id, started_at) VALUES (?, ?)",
            (checkpoint_id, _now()),
        )
        self.conn.commit()
        return False

    def is_complete(self, checkpoint_id: str) -> bool:
        row = self.conn.execute(
            "SELECT completed_at FROM checkpoint_runs WHERE checkpoint_id=?", (checkpoint_id,)
        ).fetchone()
        return bool(row and row["completed_at"])

    def mark_complete(self, checkpoint_id: str) -> None:
        self.conn.execute(
            "UPDATE checkpoint_runs SET completed_at=? WHERE checkpoint_id=?",
            (_now(), checkpoint_id),
        )
        self.conn.commit()

    def clear(self, checkpoint_id: str) -> None:
        """Discard all state for this ``checkpoint_id`` - forces the next
        ``start_or_resume`` to be a fresh start rather than a resume."""
        self.conn.execute(
            "DELETE FROM checkpoint_items WHERE checkpoint_id=?", (checkpoint_id,)
        )
        self.conn.execute(
            "DELETE FROM checkpoint_runs WHERE checkpoint_id=?", (checkpoint_id,)
        )
        self.conn.commit()

    def clear_phase(self, checkpoint_id: str, phase: str) -> None:
        """Discard all work-item state for one phase only, leaving every
        other phase and the run's own started_at/completed_at untouched.

        For when a phase's partial progress turns out to be untrustworthy
        (e.g. a resumed continuation token that turned out to be stale) and
        must be redone from scratch, without discarding progress already
        made in other phases of the same run - unlike :meth:`clear`, which
        is scoped to the whole run.
        """
        self.conn.execute(
            "DELETE FROM checkpoint_items WHERE checkpoint_id=? AND phase=?",
            (checkpoint_id, phase),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # work items
    # ------------------------------------------------------------------ #

    def status(self, checkpoint_id: str, phase: str, key: str) -> str | None:
        """``"done"``, ``"error"``, or ``None`` (never attempted / pending)."""
        row = self.conn.execute(
            "SELECT status FROM checkpoint_items WHERE checkpoint_id=? AND phase=? AND key=?",
            (checkpoint_id, phase, key),
        ).fetchone()
        return row["status"] if row else None

    def get_payload(self, checkpoint_id: str, phase: str, key: str) -> dict[str, Any] | None:
        """The stored payload for a ``done`` item, or ``None``."""
        row = self.conn.execute(
            "SELECT payload FROM checkpoint_items WHERE checkpoint_id=? AND phase=? "
            "AND key=? AND status='done'",
            (checkpoint_id, phase, key),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def done_payloads(self, checkpoint_id: str, phase: str) -> dict[str, dict[str, Any]]:
        """Every ``done`` item's payload in one phase, keyed by work-item
        key - what a resumed run reads back instead of re-fetching."""
        rows = self.conn.execute(
            "SELECT key, payload FROM checkpoint_items WHERE checkpoint_id=? AND phase=? "
            "AND status='done'",
            (checkpoint_id, phase),
        ).fetchall()
        return {row["key"]: json.loads(row["payload"]) for row in rows}

    def mark_done(self, checkpoint_id: str, phase: str, key: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO checkpoint_items "
            "(checkpoint_id, phase, key, status, payload, error, updated_at) "
            "VALUES (?, ?, ?, 'done', ?, NULL, ?)",
            (checkpoint_id, phase, key, json.dumps(payload), _now()),
        )
        self.conn.commit()

    def mark_error(self, checkpoint_id: str, phase: str, key: str, error: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO checkpoint_items "
            "(checkpoint_id, phase, key, status, payload, error, updated_at) "
            "VALUES (?, ?, ?, 'error', NULL, ?, ?)",
            (checkpoint_id, phase, key, error, _now()),
        )
        self.conn.commit()

    def progress(self, checkpoint_id: str, phase: str) -> PhaseProgress:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as n FROM checkpoint_items "
            "WHERE checkpoint_id=? AND phase=? GROUP BY status",
            (checkpoint_id, phase),
        ).fetchall()
        counts = {row["status"]: row["n"] for row in rows}
        return PhaseProgress(done=counts.get("done", 0), error=counts.get("error", 0))

    def close(self) -> None:
        self.conn.close()
