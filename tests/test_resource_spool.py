"""Tests for providermap.resource_spool - disk-backed resource cache for
national-scale discovery, not tied to any one adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from providermap.resource_spool import ResourceSpool


@pytest.fixture()
def spool(tmp_path: Path) -> ResourceSpool:
    s = ResourceSpool(tmp_path / "spool.db")
    yield s
    s.close()


class TestUpsertAndGet:
    def test_unknown_resource_is_none(self, spool: ResourceSpool) -> None:
        assert spool.get("Location", "missing") is None

    def test_upsert_then_get(self, spool: ResourceSpool) -> None:
        spool.upsert("Location", "loc-1", {"resourceType": "Location", "id": "loc-1", "name": "X"})
        assert spool.get("Location", "loc-1") == {
            "resourceType": "Location",
            "id": "loc-1",
            "name": "X",
        }

    def test_upsert_replaces_existing(self, spool: ResourceSpool) -> None:
        spool.upsert("Location", "loc-1", {"name": "old"})
        spool.upsert("Location", "loc-1", {"name": "new"})
        assert spool.get("Location", "loc-1") == {"name": "new"}

    def test_same_id_different_type_is_distinct(self, spool: ResourceSpool) -> None:
        spool.upsert("Location", "1", {"kind": "location"})
        spool.upsert("Organization", "1", {"kind": "organization"})
        assert spool.get("Location", "1") == {"kind": "location"}
        assert spool.get("Organization", "1") == {"kind": "organization"}


class TestPractitionerIndex:
    def test_distinct_practitioner_refs_ignores_null(self, spool: ResourceSpool) -> None:
        spool.upsert("Location", "loc-1", {"x": 1}, practitioner_ref=None)
        spool.upsert("PractitionerRole", "role-1", {"x": 1}, practitioner_ref="prac-a")
        spool.upsert("PractitionerRole", "role-2", {"x": 2}, practitioner_ref="prac-b")
        assert set(spool.distinct_practitioner_refs()) == {"prac-a", "prac-b"}

    def test_distinct_practitioner_refs_deduplicates(self, spool: ResourceSpool) -> None:
        spool.upsert("PractitionerRole", "role-1", {"x": 1}, practitioner_ref="prac-a")
        spool.upsert("PractitionerRole", "role-2", {"x": 2}, practitioner_ref="prac-a")
        assert spool.distinct_practitioner_refs() == ["prac-a"]

    def test_roles_for_practitioner_returns_only_that_practitioners_roles(
        self, spool: ResourceSpool
    ) -> None:
        spool.upsert("PractitionerRole", "role-1", {"id": "role-1"}, practitioner_ref="prac-a")
        spool.upsert("PractitionerRole", "role-2", {"id": "role-2"}, practitioner_ref="prac-a")
        spool.upsert("PractitionerRole", "role-3", {"id": "role-3"}, practitioner_ref="prac-b")
        roles = spool.roles_for_practitioner("prac-a")
        assert {r["id"] for r in roles} == {"role-1", "role-2"}

    def test_roles_for_unknown_practitioner_is_empty(self, spool: ResourceSpool) -> None:
        assert spool.roles_for_practitioner("nobody") == []


class TestResourceCounts:
    def test_counts_by_type(self, spool: ResourceSpool) -> None:
        spool.upsert("Location", "1", {})
        spool.upsert("Location", "2", {})
        spool.upsert("Organization", "1", {})
        assert spool.resource_counts_by_type() == {"Location": 2, "Organization": 1}

    def test_empty_spool_has_no_counts(self, spool: ResourceSpool) -> None:
        assert spool.resource_counts_by_type() == {}


class TestDurability:
    def test_state_survives_reopening_the_same_file(self, tmp_path: Path) -> None:
        path = tmp_path / "spool.db"
        s1 = ResourceSpool(path)
        s1.upsert("Practitioner", "p-1", {"name": "Jane"}, practitioner_ref=None)
        s1.close()

        s2 = ResourceSpool(path)
        assert s2.get("Practitioner", "p-1") == {"name": "Jane"}
        s2.close()


def _committed_row_count(path: Path) -> int:
    """Row count via a brand-new raw connection - i.e. only what actually
    reached disk, never a batched-but-uncommitted write still sitting in
    another connection's pending transaction (same technique as
    test_checkpoint.py's _committed_status)."""
    import sqlite3

    conn = sqlite3.connect(path, timeout=30)
    try:
        return conn.execute("SELECT COUNT(*) FROM spool_resources").fetchone()[0]
    finally:
        conn.close()


class TestCommitBatching:
    """commit_every mirrors CheckpointStore's batching exactly - see
    resource_spool.py's module docstring for the durability rule
    (flush-before-checkpoint-marker) that makes batching safe here."""

    def test_invalid_commit_every_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            ResourceSpool(tmp_path / "spool.db", commit_every=0)

    def test_default_commit_every_one_is_durable_after_every_write(self, tmp_path: Path) -> None:
        path = tmp_path / "spool.db"
        spool = ResourceSpool(path)  # default commit_every=1
        spool.upsert("Location", "loc-1", {"x": 1})
        assert _committed_row_count(path) == 1
        spool.close()

    def test_batched_writes_are_not_durable_until_the_threshold(self, tmp_path: Path) -> None:
        path = tmp_path / "spool.db"
        spool = ResourceSpool(path, commit_every=3)
        spool.upsert("Location", "loc-1", {"x": 1})
        spool.upsert("Location", "loc-2", {"x": 2})
        assert _committed_row_count(path) == 0  # not yet committed

        spool.upsert("Location", "loc-3", {"x": 3})  # 3rd write triggers commit
        assert _committed_row_count(path) == 3
        spool.close()

    def test_flush_commits_without_closing(self, tmp_path: Path) -> None:
        path = tmp_path / "spool.db"
        spool = ResourceSpool(path, commit_every=100)
        spool.upsert("Location", "loc-1", {"x": 1})
        assert _committed_row_count(path) == 0

        spool.flush()
        assert _committed_row_count(path) == 1
        spool.close()

    def test_close_flushes_pending_batched_writes(self, tmp_path: Path) -> None:
        path = tmp_path / "spool.db"
        spool = ResourceSpool(path, commit_every=100)
        spool.upsert("Location", "loc-1", {"x": 1})
        spool.close()
        assert _committed_row_count(path) == 1


class TestExists:
    def test_exists_agrees_with_get(self, spool: ResourceSpool) -> None:
        spool.upsert("Location", "loc-1", {"x": 1})
        assert spool.exists("Location", "loc-1") is True
        assert spool.exists("Location", "missing") is False
        assert spool.exists("Organization", "loc-1") is False  # type is part of the key


class TestClear:
    def test_clear_removes_everything(self, spool: ResourceSpool) -> None:
        spool.upsert("Location", "loc-1", {"x": 1})
        spool.upsert("PractitionerRole", "r-1", {"x": 2}, practitioner_ref="p-1")
        spool.clear()
        assert spool.get("Location", "loc-1") is None
        assert spool.distinct_practitioner_refs() == []
        assert spool.resource_counts_by_type() == {}

    def test_clear_resets_pending_batch_state(self, tmp_path: Path) -> None:
        path = tmp_path / "spool.db"
        spool = ResourceSpool(path, commit_every=100)
        spool.upsert("Location", "loc-1", {"x": 1})  # pending, uncommitted
        spool.clear()
        # The clear itself commits; the pending counter must reset so the
        # next batch isn't off by the discarded writes.
        assert _committed_row_count(path) == 0
        spool.upsert("Location", "loc-2", {"x": 2})
        spool.flush()
        assert _committed_row_count(path) == 1
        spool.close()


class TestDeterministicOrdering:
    """Emission order downstream (the materialize pass) must be a pure
    function of the spooled data, not of SQLite's internal iteration
    order - both accessors sort explicitly."""

    def test_roles_for_practitioner_ordered_by_resource_id(self, spool: ResourceSpool) -> None:
        # Inserted deliberately out of lexicographic order.
        spool.upsert("PractitionerRole", "r-c", {"id": "r-c"}, practitioner_ref="p-1")
        spool.upsert("PractitionerRole", "r-a", {"id": "r-a"}, practitioner_ref="p-1")
        spool.upsert("PractitionerRole", "r-b", {"id": "r-b"}, practitioner_ref="p-1")
        assert [r["id"] for r in spool.roles_for_practitioner("p-1")] == ["r-a", "r-b", "r-c"]

    def test_distinct_practitioner_refs_sorted(self, spool: ResourceSpool) -> None:
        spool.upsert("PractitionerRole", "r-1", {}, practitioner_ref="p-z")
        spool.upsert("PractitionerRole", "r-2", {}, practitioner_ref="p-a")
        spool.upsert("PractitionerRole", "r-3", {}, practitioner_ref="p-m")
        assert spool.distinct_practitioner_refs() == ["p-a", "p-m", "p-z"]
