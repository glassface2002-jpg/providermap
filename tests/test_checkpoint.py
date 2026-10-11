"""Tests for providermap.checkpoint - durable resume infrastructure, not
tied to any one adapter."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from providermap.checkpoint import CheckpointStore


def _committed_status(path: Path, checkpoint_id: str, phase: str, key: str) -> str | None:
    """Read a status via a brand-new raw connection - i.e. only what's
    actually committed to disk, never a batched-but-uncommitted write still
    sitting in some other CheckpointStore's pending transaction. A read-only
    SELECT only ever needs a SHARED lock, so this never blocks on another
    connection's uncommitted RESERVED-lock write, unlike opening a second
    CheckpointStore would (its own schema-init commit could contend for the
    write lock)."""
    conn = sqlite3.connect(path, timeout=30)
    try:
        row = conn.execute(
            "SELECT status FROM checkpoint_items WHERE checkpoint_id=? AND phase=? AND key=?",
            (checkpoint_id, phase, key),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@pytest.fixture()
def store(tmp_path: Path) -> CheckpointStore:
    s = CheckpointStore(tmp_path / "checkpoints.db")
    yield s
    s.close()


class TestStartOrResume:
    def test_fresh_checkpoint_id_is_not_a_resume(self, store: CheckpointStore) -> None:
        assert store.start_or_resume("cigna_fhir:RI") is False

    def test_incomplete_prior_run_is_a_resume(self, store: CheckpointStore) -> None:
        store.start_or_resume("cigna_fhir:RI")
        assert store.start_or_resume("cigna_fhir:RI") is True

    def test_completed_prior_run_starts_fresh_again(self, store: CheckpointStore) -> None:
        store.start_or_resume("cigna_fhir:RI")
        store.mark_done("cigna_fhir:RI", "phase", "item-1", {"x": 1})
        store.mark_complete("cigna_fhir:RI")

        assert store.start_or_resume("cigna_fhir:RI") is False
        # A fresh start clears prior items - nothing carries over.
        assert store.status("cigna_fhir:RI", "phase", "item-1") is None

    def test_independent_checkpoint_ids_do_not_interfere(self, store: CheckpointStore) -> None:
        store.start_or_resume("cigna_fhir:RI")
        store.mark_done("cigna_fhir:RI", "phase", "item-1", {"x": 1})
        assert store.start_or_resume("cigna_fhir:FL") is False
        assert store.status("cigna_fhir:FL", "phase", "item-1") is None
        assert store.status("cigna_fhir:RI", "phase", "item-1") == "done"


class TestWorkItems:
    def test_unattempted_item_status_is_none(self, store: CheckpointStore) -> None:
        assert store.status("run-1", "phase", "never-touched") is None

    def test_mark_done_then_get_payload(self, store: CheckpointStore) -> None:
        store.mark_done("run-1", "phase", "item-1", {"entries": [1, 2, 3]})
        assert store.status("run-1", "phase", "item-1") == "done"
        assert store.get_payload("run-1", "phase", "item-1") == {"entries": [1, 2, 3]}

    def test_mark_error_then_status(self, store: CheckpointStore) -> None:
        store.mark_error("run-1", "phase", "item-1", "boom")
        assert store.status("run-1", "phase", "item-1") == "error"
        assert store.get_payload("run-1", "phase", "item-1") is None

    def test_error_can_be_superseded_by_done_on_retry(self, store: CheckpointStore) -> None:
        store.mark_error("run-1", "phase", "item-1", "boom")
        store.mark_done("run-1", "phase", "item-1", {"ok": True})
        assert store.status("run-1", "phase", "item-1") == "done"

    def test_done_payloads_returns_only_done_items_in_one_phase(
        self, store: CheckpointStore
    ) -> None:
        store.mark_done("run-1", "phase-a", "1", {"v": "a1"})
        store.mark_done("run-1", "phase-a", "2", {"v": "a2"})
        store.mark_error("run-1", "phase-a", "3", "boom")
        store.mark_done("run-1", "phase-b", "1", {"v": "b1"})

        result = store.done_payloads("run-1", "phase-a")
        assert result == {"1": {"v": "a1"}, "2": {"v": "a2"}}


class TestProgress:
    def test_progress_counts_done_and_error(self, store: CheckpointStore) -> None:
        store.mark_done("run-1", "phase", "1", {})
        store.mark_done("run-1", "phase", "2", {})
        store.mark_error("run-1", "phase", "3", "boom")

        progress = store.progress("run-1", "phase")
        assert progress.done == 2
        assert progress.error == 1

    def test_progress_on_untouched_phase_is_zero(self, store: CheckpointStore) -> None:
        progress = store.progress("run-1", "never-started")
        assert progress.done == 0
        assert progress.error == 0


class TestClear:
    def test_clear_removes_items_and_run_row(self, store: CheckpointStore) -> None:
        store.start_or_resume("run-1")
        store.mark_done("run-1", "phase", "1", {})
        store.clear("run-1")
        assert store.status("run-1", "phase", "1") is None
        assert store.start_or_resume("run-1") is False  # treated as fresh, not a resume


class TestClearPhase:
    def test_clear_phase_removes_only_that_phase(self, store: CheckpointStore) -> None:
        store.start_or_resume("run-1")
        store.mark_done("run-1", "phase-a", "1", {"x": 1})
        store.mark_done("run-1", "phase-b", "1", {"y": 2})
        store.clear_phase("run-1", "phase-a")
        assert store.status("run-1", "phase-a", "1") is None
        assert store.status("run-1", "phase-b", "1") == "done"  # untouched

    def test_clear_phase_leaves_the_run_itself_intact(self, store: CheckpointStore) -> None:
        store.start_or_resume("run-1")
        store.mark_done("run-1", "phase-a", "1", {})
        store.clear_phase("run-1", "phase-a")
        # Unlike clear(), the run is not reset to "fresh" - a caller that
        # cleared one phase to redo it is still mid-run, not starting over.
        assert store.start_or_resume("run-1") is True

    def test_clear_phase_on_an_empty_phase_is_a_harmless_no_op(
        self, store: CheckpointStore
    ) -> None:
        store.start_or_resume("run-1")
        store.clear_phase("run-1", "never-used-phase")  # must not raise
        assert store.status("run-1", "never-used-phase", "1") is None


class TestDurability:
    def test_state_survives_reopening_the_same_file(self, tmp_path: Path) -> None:
        path = tmp_path / "checkpoints.db"
        store1 = CheckpointStore(path)
        store1.start_or_resume("run-1")
        store1.mark_done("run-1", "phase", "1", {"v": 42})
        store1.close()

        store2 = CheckpointStore(path)
        assert store2.start_or_resume("run-1") is True
        assert store2.get_payload("run-1", "phase", "1") == {"v": 42}
        store2.close()


class TestCommitBatching:
    """commit_every controls how often mark_done/mark_error actually fsync -
    see CheckpointStore's docstring for the measured throughput motivation.
    Durability is checked via _committed_status, a raw read-only connection,
    since a batched-but-uncommitted write is still visible to the SAME
    connection's own reads (SQLite sees its own pending transaction) - only
    an independent connection can tell whether it actually reached disk."""

    def test_invalid_commit_every_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            CheckpointStore(tmp_path / "checkpoints.db", commit_every=0)

    def test_default_commit_every_one_is_durable_after_every_write(self, tmp_path: Path) -> None:
        path = tmp_path / "checkpoints.db"
        store = CheckpointStore(path)  # default commit_every=1
        store.mark_done("run-1", "phase", "1", {"v": 1})

        assert _committed_status(path, "run-1", "phase", "1") == "done"
        store.close()

    def test_batched_writes_are_not_durable_until_the_threshold(self, tmp_path: Path) -> None:
        path = tmp_path / "checkpoints.db"
        store = CheckpointStore(path, commit_every=3)
        store.mark_done("run-1", "phase", "1", {"v": 1})
        store.mark_done("run-1", "phase", "2", {"v": 2})

        assert _committed_status(path, "run-1", "phase", "1") is None  # not yet committed

        store.mark_done("run-1", "phase", "3", {"v": 3})  # 3rd write triggers commit

        assert _committed_status(path, "run-1", "phase", "1") == "done"
        assert _committed_status(path, "run-1", "phase", "3") == "done"
        store.close()

    def test_close_flushes_pending_batched_writes(self, tmp_path: Path) -> None:
        path = tmp_path / "checkpoints.db"
        store = CheckpointStore(path, commit_every=100)
        store.mark_done("run-1", "phase", "1", {"v": 1})  # far short of 100
        store.close()

        assert _committed_status(path, "run-1", "phase", "1") == "done"

    def test_flush_commits_without_closing(self, tmp_path: Path) -> None:
        path = tmp_path / "checkpoints.db"
        store = CheckpointStore(path, commit_every=100)
        store.mark_done("run-1", "phase", "1", {"v": 1})

        assert _committed_status(path, "run-1", "phase", "1") is None

        store.flush()

        assert _committed_status(path, "run-1", "phase", "1") == "done"
        store.close()

    def test_mark_complete_flushes_pending_batched_writes(self, tmp_path: Path) -> None:
        path = tmp_path / "checkpoints.db"
        store = CheckpointStore(path, commit_every=100)
        store.start_or_resume("run-1")
        store.mark_done("run-1", "phase", "1", {"v": 1})  # far short of 100
        store.mark_complete("run-1")

        assert _committed_status(path, "run-1", "phase", "1") == "done"
        store.close()
