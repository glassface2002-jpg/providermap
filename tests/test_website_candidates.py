"""Tests for the website candidate audit trail (ROADMAP.md Phase 5.3):
the v8 schema (website_candidates), its Database CRUD methods, and
foreign-key integrity. Mirrors the structure of
tests/test_identity_resolution.py's v7 migration/CRUD/FK test classes.

The actual cli.py wiring (cmd_enrich_organizations logging every candidate
it considers) has no dedicated unit test here, matching this project's
existing convention for that function - it has no prior unit tests either,
and is verified only via manual/live dry-run, the same treatment given to
fetch_wikidata_hospital_websites and fetch_hifld_hospital_index.
"""

from __future__ import annotations

import sqlite3

import pytest

from providermap.database import Database
from providermap.models import Confidence, Organization, ResolutionMethod


class TestWebsiteCandidateCRUD:
    def test_add_and_read_back_a_promoted_candidate(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(name="Test Hospital", source="CMS", source_id="1")
        )
        assert oid is not None
        db.add_website_candidate(
            oid,
            "https://test.example",
            ResolutionMethod.WIKIDATA,
            reachable=None,
            confidence=Confidence.HIGH,
            promoted=True,
        )
        candidates = db.get_candidates_for_organization(oid)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.organization_id == oid
        assert c.candidate_url == "https://test.example"
        assert c.resolution_method == ResolutionMethod.WIKIDATA
        assert c.reachable is None
        assert c.confidence == Confidence.HIGH
        assert c.promoted is True

    def test_rejected_hifld_candidate_is_logged_as_not_promoted(self, db: Database) -> None:
        """The concrete Phase 5.3 audit win: a HIFLD match that failed its
        reachability check must still be a queryable row, not silently
        dropped."""
        oid, _ = db.upsert_organization(
            Organization(name="Test Hospital", source="CMS", source_id="1")
        )
        assert oid is not None
        db.add_website_candidate(
            oid,
            "https://dead-hifld-url.example",
            ResolutionMethod.HIFLD,
            reachable=False,
            promoted=False,
        )
        candidates = db.get_candidates_for_organization(oid)
        assert len(candidates) == 1
        assert candidates[0].reachable is False
        assert candidates[0].promoted is False
        assert candidates[0].confidence is None

    def test_multiple_candidates_for_the_same_organization_are_all_kept(self, db: Database) -> None:
        """A rejected HIFLD candidate followed by a successful guess must
        both survive - logging is additive, never a single-slot overwrite."""
        oid, _ = db.upsert_organization(
            Organization(name="Test Hospital", source="CMS", source_id="1")
        )
        assert oid is not None
        db.add_website_candidate(
            oid, "https://dead.example", ResolutionMethod.HIFLD, reachable=False, promoted=False
        )
        db.add_website_candidate(
            oid,
            "https://guessed.example",
            ResolutionMethod.GUESS,
            reachable=True,
            confidence=Confidence.MEDIUM,
            promoted=True,
        )
        candidates = db.get_candidates_for_organization(oid)
        assert len(candidates) == 2
        assert [c.promoted for c in candidates] == [False, True]

    def test_optional_system_membership_reference(self, db: Database) -> None:
        """The one-directional, nullable tie to the identity layer - a
        candidate may cite a membership row, but it's optional."""
        oid, _ = db.upsert_organization(
            Organization(name="Test Hospital", source="CMS", source_id="1")
        )
        sid = db.get_or_create_health_system("Test System", "test system")
        assert oid is not None
        membership_id = db.set_system_membership(oid, sid)
        db.add_website_candidate(
            oid,
            "https://system-domain.example",
            ResolutionMethod.SYSTEM_DOMAIN,
            reachable=True,
            confidence=Confidence.MEDIUM,
            system_membership_id=membership_id,
            promoted=True,
        )
        candidates = db.get_candidates_for_organization(oid)
        assert candidates[0].system_membership_id == membership_id

    def test_no_candidates_returns_empty_list(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(name="Test Hospital", source="CMS", source_id="1")
        )
        assert oid is not None
        assert db.get_candidates_for_organization(oid) == []


class TestForeignKeyIntegrity:
    def test_candidate_with_nonexistent_organization_raises(self, db: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO website_candidates (organization_id, candidate_url) VALUES (?, ?)",
                (99999, "https://x.example"),
            )

    def test_candidate_with_nonexistent_system_membership_raises(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(name="Test Hospital", source="CMS", source_id="1")
        )
        assert oid is not None
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO website_candidates "
                "(organization_id, candidate_url, system_membership_id) VALUES (?, ?, ?)",
                (oid, "https://x.example", 99999),
            )


class TestV7ToV8Migration:
    def test_v7_database_gains_website_candidates_without_data_loss(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """v8 adds website_candidates. A v7 database must gain it
        additively on open, keeping every existing row - mirroring
        TestV6ToV7Migration in test_identity_resolution.py."""
        path = tmp_path / "v7.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE organizations (
                organization_id INTEGER PRIMARY KEY, name TEXT, normalized_name TEXT,
                organization_type TEXT, address TEXT, city TEXT, state TEXT, zip TEXT,
                phone TEXT, website TEXT, website_confidence TEXT, source TEXT,
                source_id TEXT, created_date DATETIME, updated_date DATETIME,
                website_checked_at DATETIME, website_source TEXT,
                UNIQUE(source, source_id));
            CREATE TABLE health_systems (system_id INTEGER PRIMARY KEY, name TEXT,
                normalized_name TEXT, primary_domain TEXT, created_at DATETIME);
            CREATE TABLE organization_aliases (id INTEGER PRIMARY KEY,
                organization_id INTEGER, alias TEXT, normalized_alias TEXT,
                source TEXT, confidence TEXT, created_at DATETIME);
            CREATE TABLE organization_system_membership (membership_id INTEGER PRIMARY KEY,
                organization_id INTEGER, system_id INTEGER, source TEXT, confidence TEXT,
                valid_from DATETIME, valid_to DATETIME, created_at DATETIME);
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.execute(
            "INSERT INTO organizations (name, source, source_id) "
            "VALUES ('Existing Hospital', 'CMS', '1')"
        )
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '7')")
        conn.commit()
        conn.close()

        db = Database(path)  # SCHEMA (creates website_candidates) + _migrate run here

        row = db.conn.execute("SELECT name FROM organizations").fetchone()
        assert row["name"] == "Existing Hospital"

        tables = {
            r["name"] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "website_candidates" in tables
        assert db.conn.execute("SELECT COUNT(*) FROM website_candidates").fetchone()[0] == 0

        version = db.conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[
            "value"
        ]
        assert int(version) >= 8
        db.close()

    def test_reopening_the_migrated_database_is_idempotent(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "reopen.db"
        db = Database(path)
        oid, _ = db.upsert_organization(Organization(name="Test", source="CMS", source_id="1"))
        assert oid is not None
        db.add_website_candidate(oid, "https://x.example", ResolutionMethod.GUESS)
        db.close()

        db2 = Database(path)  # migration + CREATE TABLE IF NOT EXISTS run again
        count = db2.conn.execute("SELECT COUNT(*) FROM website_candidates").fetchone()[0]
        assert count == 1  # not duplicated
        db2.close()
