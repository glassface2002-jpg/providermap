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
        assert spool.get("Location", "loc-1") == {"resourceType": "Location", "id": "loc-1", "name": "X"}

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
