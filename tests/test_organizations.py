"""Tests for the organization foundation (schema v4).

These cover only the *foundation* added in Phase 1 - the new tables, the
Organization model, and the OrganizationAdapter contract. No concrete
organization adapter exists yet, so there is no ingestion behaviour to test.

The point of these tests is safety: prove the new schema is purely additive
(an old provider database gains the org tables without losing a row) and that
the new contract is wired correctly, so the existing provider suite staying
green plus these passing means Phase 1 broke nothing.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from adapters import ORGANIZATION_ADAPTERS, get_organization_adapter_class
from adapters.organizations.base import OrganizationAdapter
from providermap.config import Config
from providermap.database import Database
from providermap.models import Organization


class TestOrganizationSchema:
    def test_fresh_db_has_the_three_new_tables(self, db: Database) -> None:
        names = {
            r["name"] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"organizations", "provider_organizations", "sources"} <= names

    def test_fresh_db_is_schema_version_4(self, db: Database) -> None:
        version = db.conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[
            "value"
        ]
        assert int(version) >= 4

    def test_sources_table_is_seeded(self, db: Database) -> None:
        rows = {r["name"] for r in db.conn.execute("SELECT name FROM sources")}
        assert {"NPPES", "CMS", "AdventHealth", "Manual Review"} <= rows

    def test_source_seed_is_idempotent(self, db: Database, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Re-opening the same database file must not duplicate seed rows -
        the SCHEMA (with its INSERT OR IGNORE) runs on every open."""
        before = db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        db.close()
        db2 = Database(db.path)
        after = db2.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        db2.close()
        assert before == after

    def test_org_dedupe_constraint_holds(self, db: Database) -> None:
        db.conn.execute(
            "INSERT INTO organizations (name, source, source_id) VALUES (?, ?, ?)",
            ("Banner Desert Medical Center", "CMS", "030002"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO organizations (name, source, source_id) VALUES (?, ?, ?)",
                ("Banner Desert (dup)", "CMS", "030002"),
            )


class TestV3ToV4Migration:
    def test_v3_database_gains_org_tables_without_data_loss(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """v4 adds organizations/provider_organizations/sources. A v3 database
        must gain them additively on open, keeping every existing provider
        row - mirroring the v1->v2 and v2->v3 migration tests in
        test_database.py."""
        path = tmp_path / "v3.db"
        conn = sqlite3.connect(path)
        # A realistic v3 providers table (the full column set a real v3
        # database has). It must include the columns the post-migration
        # INDEXES reference - e.g. last_name - because `CREATE TABLE IF NOT
        # EXISTS` leaves an already-present table's shape untouched, and only
        # the org *tables* are new in v4. The other tables are omitted here on
        # purpose: they don't pre-exist, so SCHEMA creates them in full.
        conn.executescript(
            """
            CREATE TABLE providers (provider_id INTEGER PRIMARY KEY, full_name TEXT,
                first_name TEXT, last_name TEXT, credentials TEXT, provider_type TEXT,
                specialty TEXT, primary_site_of_care TEXT, practice_name TEXT,
                address TEXT, city TEXT, state TEXT, zip TEXT, phone TEXT,
                profile_url TEXT UNIQUE, npi TEXT UNIQUE, created_date DATETIME,
                updated_date DATETIME, location_count INTEGER DEFAULT 1,
                site_confidence TEXT, nppes_enumeration TEXT, nppes_name_match INTEGER,
                taxonomy_code TEXT, taxonomy_desc TEXT, source_notes TEXT,
                content_hash TEXT, first_seen DATETIME, last_seen DATETIME,
                last_checked DATETIME, last_seen_run_id INTEGER,
                is_active INTEGER DEFAULT 1, hospital_affiliation TEXT,
                accepting_new_patients INTEGER, rating REAL, rating_count INTEGER,
                languages TEXT, insurance_accepted TEXT);
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.execute(
            "INSERT INTO providers (full_name, npi, profile_url, city, provider_type, "
            "is_active) VALUES ('Existing Person', '1194013169', "
            "'https://x/a-1194013169', 'Orlando', 'Physician', 1)"
        )
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '3')")
        conn.commit()
        conn.close()

        db = Database(path)  # SCHEMA (creates org tables) + _migrate run here

        # Existing provider row is untouched.
        row = db.conn.execute("SELECT full_name FROM providers").fetchone()
        assert row["full_name"] == "Existing Person"

        # The three org tables now exist and are empty (except seeded sources).
        tables = {
            r["name"] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"organizations", "provider_organizations", "sources"} <= tables
        assert db.conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] >= 4

        version = db.conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[
            "value"
        ]
        assert int(version) >= 4
        db.close()


class TestOrganizationModel:
    def test_constructs_with_defaults(self) -> None:
        org = Organization(name="Banner Desert Medical Center")
        assert org.name == "Banner Desert Medical Center"
        assert org.organization_id is None
        assert org.source is None


class TestOrganizationAdapterContract:
    def test_base_class_is_abstract(self, config: Config) -> None:
        with pytest.raises(TypeError):
            OrganizationAdapter(config)  # type: ignore[abstract]

    def test_a_concrete_subclass_can_be_built(self, config: Config) -> None:
        class _StubAdapter(OrganizationAdapter):
            name = "stub"
            source = "Manual Review"

            def discover(self):  # type: ignore[no-untyped-def]
                yield {"raw_name": "Banner Desert Medical Center", "st": "AZ"}

            def extract(self, raw: dict[str, Any]) -> dict[str, Any]:
                return {"name": raw["raw_name"], "state": raw["st"]}

            def normalize(self, extracted: dict[str, Any]) -> Organization:
                return Organization(
                    name=extracted["name"],
                    normalized_name=extracted["name"].lower().strip(),
                    state=extracted["state"],
                    source=self.source,
                )

        adapter = _StubAdapter(config)
        raw = next(iter(adapter.discover()))
        org = adapter.normalize(adapter.extract(raw))
        assert org.name == "Banner Desert Medical Center"
        assert org.normalized_name == "banner desert medical center"
        assert org.state == "AZ"
        assert org.source == "Manual Review"

    def test_registry_exists_and_is_empty(self) -> None:
        assert ORGANIZATION_ADAPTERS == {}
        with pytest.raises(ValueError):
            get_organization_adapter_class("cms_hospitals")
