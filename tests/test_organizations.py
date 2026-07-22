"""Tests for the organization foundation (schema v4) and its first concrete
adapter, ``cms_hospitals`` (ROADMAP.md stage 3).

The schema/model/contract tests here prove the new schema is purely additive
(an old provider database gains the org tables without losing a row) and that
the adapter contract is wired correctly. ``Database.upsert_organization``
tests below cover the ingestion path itself - adapter-specific behaviour
(CSV column mapping, CMS's "Not Available" placeholder, ...) lives in
``tests/test_cms_hospitals_adapter.py`` instead.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adapters import ORGANIZATION_ADAPTERS, get_organization_adapter_class
from adapters.organizations.base import OrganizationAdapter
from adapters.organizations.cms_hospitals.adapter import CMSHospitalsAdapter
from providermap.config import Config
from providermap.database import Database
from providermap.models import Confidence, Organization, Provider, UpsertOutcome


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


class TestV4ToV5Migration:
    def test_v4_database_gains_website_checked_at_without_data_loss(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """v5 adds organizations.website_checked_at (see
        Database.mark_website_checked). A v4 database must gain it
        additively on open, keeping every existing organization row."""
        path = tmp_path / "v4.db"
        conn = sqlite3.connect(path)
        # A realistic v4 organizations table - every column that version
        # actually had, minus website_checked_at (the v5 addition).
        conn.executescript(
            """
            CREATE TABLE organizations (
                organization_id INTEGER PRIMARY KEY, name TEXT, normalized_name TEXT,
                organization_type TEXT, address TEXT, city TEXT, state TEXT, zip TEXT,
                phone TEXT, website TEXT, website_confidence TEXT, source TEXT,
                source_id TEXT, created_date DATETIME, updated_date DATETIME,
                UNIQUE(source, source_id));
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.execute(
            "INSERT INTO organizations (name, source, source_id) "
            "VALUES ('Existing Hospital', 'CMS', '1')"
        )
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '4')")
        conn.commit()
        conn.close()

        db = Database(path)  # _migrate() adds website_checked_at here

        row = db.conn.execute("SELECT name, website_checked_at FROM organizations").fetchone()
        assert row["name"] == "Existing Hospital"
        assert row["website_checked_at"] is None

        version = db.conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[
            "value"
        ]
        assert int(version) >= 5
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

    def test_registry_has_cms_hospitals_registered(self) -> None:
        assert ORGANIZATION_ADAPTERS["cms_hospitals"] is CMSHospitalsAdapter
        assert get_organization_adapter_class("cms_hospitals") is CMSHospitalsAdapter

    def test_unknown_organization_adapter_raises(self) -> None:
        with pytest.raises(ValueError):
            get_organization_adapter_class("not_a_real_adapter")


class TestUpsertOrganization:
    def test_first_import_is_new(self, db: Database) -> None:
        org = Organization(
            name="Banner Desert Medical Center",
            normalized_name="banner desert medical center",
            organization_type="hospital",
            state="AZ",
            source="CMS",
            source_id="030001",
        )
        oid, outcome = db.upsert_organization(org)
        assert oid is not None
        assert outcome == UpsertOutcome.NEW
        row = db.conn.execute(
            "SELECT * FROM organizations WHERE organization_id=?", (oid,)
        ).fetchone()
        assert row["name"] == "Banner Desert Medical Center"
        assert row["source_id"] == "030001"

    def test_reimporting_identical_data_is_unchanged(self, db: Database) -> None:
        org = Organization(
            name="Banner Desert Medical Center",
            normalized_name="banner desert medical center",
            organization_type="hospital",
            state="AZ",
            source="CMS",
            source_id="030001",
        )
        first_id, _ = db.upsert_organization(org)
        second_id, outcome = db.upsert_organization(org)
        assert second_id == first_id
        assert outcome == UpsertOutcome.UNCHANGED
        count = db.conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        assert count == 1

    def test_reimporting_changed_data_updates_the_row(self, db: Database) -> None:
        original = Organization(
            name="Banner Desert Medical Center",
            state="AZ",
            phone=None,
            source="CMS",
            source_id="030001",
        )
        updated = Organization(
            name="Banner Desert Medical Center",
            state="AZ",
            phone="(480) 412-3000",
            source="CMS",
            source_id="030001",
        )
        first_id, _ = db.upsert_organization(original)
        second_id, outcome = db.upsert_organization(updated)
        assert second_id == first_id
        assert outcome == UpsertOutcome.CHANGED
        row = db.conn.execute(
            "SELECT phone FROM organizations WHERE organization_id=?", (first_id,)
        ).fetchone()
        assert row["phone"] == "(480) 412-3000"

    def test_dedupes_on_source_and_source_id_not_name(self, db: Database) -> None:
        """Two different real hospitals must never collapse into one row just
        because a future adapter reuses a name - only (source, source_id)
        identifies a row here."""
        db.upsert_organization(Organization(name="Same Name Hospital", source="CMS", source_id="1"))
        db.upsert_organization(Organization(name="Same Name Hospital", source="CMS", source_id="2"))
        count = db.conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        assert count == 2


class TestOrganizationWebsiteEnrichment:
    """`set_organization_website` and `organizations_missing_website`
    (ROADMAP.md stage 4)."""

    def test_set_organization_website_stores_url_and_confidence(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(name="Banner Desert Medical Center", source="CMS", source_id="1")
        )
        assert oid is not None
        db.set_organization_website(oid, "https://bannerhealth.com", Confidence.MEDIUM)
        row = db.conn.execute(
            "SELECT website, website_confidence FROM organizations WHERE organization_id=?",
            (oid,),
        ).fetchone()
        assert row["website"] == "https://bannerhealth.com"
        assert row["website_confidence"] == "medium"

    def test_set_organization_website_records_optional_source(self, db: Database) -> None:
        """Phase 3 (HIFLD) provenance tracking - existing callers that don't
        pass `source` (Wikidata, guessing) leave it NULL, unchanged."""
        oid, _ = db.upsert_organization(
            Organization(name="Some Hospital", source="CMS", source_id="2")
        )
        assert oid is not None
        db.set_organization_website(
            oid, "http://www.somehospital.example", Confidence.MEDIUM, source="HIFLD"
        )
        row = db.conn.execute(
            "SELECT website_source FROM organizations WHERE organization_id=?", (oid,)
        ).fetchone()
        assert row["website_source"] == "HIFLD"

    def test_set_organization_website_source_defaults_to_null(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(name="Another Hospital", source="CMS", source_id="3")
        )
        assert oid is not None
        db.set_organization_website(oid, "https://another.example", Confidence.HIGH)
        row = db.conn.execute(
            "SELECT website_source FROM organizations WHERE organization_id=?", (oid,)
        ).fetchone()
        assert row["website_source"] is None

    def test_reimporting_via_upsert_never_clears_a_discovered_website(self, db: Database) -> None:
        """Regression test: `upsert_organization`'s UPDATE must never touch
        website/website_confidence, since a re-import from an adapter with
        no website field (every adapter today) would otherwise silently
        erase whatever `enrich-organizations` previously found."""
        org = Organization(
            name="Banner Desert Medical Center", phone=None, source="CMS", source_id="1"
        )
        oid, _ = db.upsert_organization(org)
        assert oid is not None
        db.set_organization_website(oid, "https://bannerhealth.com", Confidence.MEDIUM)

        # Re-import with a changed field (forces the CHANGED branch, not the
        # cheaper UNCHANGED touch-only branch) to prove even a real update
        # doesn't disturb the website columns.
        updated = Organization(
            name="Banner Desert Medical Center",
            phone="(480) 412-3000",
            source="CMS",
            source_id="1",
        )
        db.upsert_organization(updated)

        row = db.conn.execute(
            "SELECT website, website_confidence, phone FROM organizations WHERE organization_id=?",
            (oid,),
        ).fetchone()
        assert row["phone"] == "(480) 412-3000"  # the re-import's change did apply
        assert row["website"] == "https://bannerhealth.com"  # but website survived
        assert row["website_confidence"] == "medium"

    def test_organizations_missing_website_excludes_ones_that_have_one(self, db: Database) -> None:
        oid1, _ = db.upsert_organization(
            Organization(name="Has A Website", source="CMS", source_id="1")
        )
        oid2, _ = db.upsert_organization(
            Organization(name="No Website Yet", source="CMS", source_id="2")
        )
        assert oid1 is not None
        db.set_organization_website(oid1, "https://example.com", Confidence.LOW)

        missing = db.organizations_missing_website()
        assert [o.organization_id for o in missing] == [oid2]

    def test_organizations_missing_website_respects_limit(self, db: Database) -> None:
        for i in range(3):
            db.upsert_organization(
                Organization(name=f"Hospital {i}", source="CMS", source_id=str(i))
            )
        missing = db.organizations_missing_website(limit=2)
        assert len(missing) == 2

    def test_mark_website_checked_stamps_checked_at_without_setting_website(
        self, db: Database
    ) -> None:
        oid, _ = db.upsert_organization(
            Organization(name="No Site Found", source="CMS", source_id="1")
        )
        assert oid is not None
        db.mark_website_checked(oid)
        row = db.conn.execute(
            "SELECT website, website_checked_at FROM organizations WHERE organization_id=?",
            (oid,),
        ).fetchone()
        assert row["website"] is None
        assert row["website_checked_at"] is not None

    def test_retry_after_days_excludes_recently_checked_failures(self, db: Database) -> None:
        """Regression test for the "re-checks everything every run" gap:
        an organization checked (and found nothing) recently must not show
        up again until retry_after_days has passed."""
        oid, _ = db.upsert_organization(
            Organization(name="Recently Checked, Nothing Found", source="CMS", source_id="1")
        )
        assert oid is not None
        db.mark_website_checked(oid)

        missing = db.organizations_missing_website(retry_after_days=30)
        assert oid not in [o.organization_id for o in missing]

    def test_retry_after_days_still_includes_never_checked_organizations(
        self, db: Database
    ) -> None:
        oid, _ = db.upsert_organization(
            Organization(name="Never Checked At All", source="CMS", source_id="1")
        )
        assert oid is not None
        missing = db.organizations_missing_website(retry_after_days=30)
        assert oid in [o.organization_id for o in missing]

    def test_retry_after_days_includes_staleness_past_the_window(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(name="Checked Long Ago", source="CMS", source_id="1")
        )
        assert oid is not None
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(
            timespec="seconds"
        )
        db.conn.execute(
            "UPDATE organizations SET website_checked_at=? WHERE organization_id=?",
            (old_timestamp, oid),
        )
        missing = db.organizations_missing_website(retry_after_days=30)
        assert oid in [o.organization_id for o in missing]

    def test_retry_after_days_zero_means_always_recheck(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(name="Just Checked", source="CMS", source_id="1")
        )
        assert oid is not None
        db.mark_website_checked(oid)
        missing = db.organizations_missing_website(retry_after_days=0)
        assert oid in [o.organization_id for o in missing]

    def test_none_retry_after_days_means_no_filter_at_all(self, db: Database) -> None:
        """Default behavior (no argument passed) - unchanged from before this
        column existed, for callers that don't care about staleness."""
        oid, _ = db.upsert_organization(
            Organization(name="Just Checked Too", source="CMS", source_id="1")
        )
        assert oid is not None
        db.mark_website_checked(oid)
        missing = db.organizations_missing_website()
        assert oid in [o.organization_id for o in missing]


class TestProviderOrganizationLinking:
    """`link_provider_organization`, `providers_for_organization_linking`,
    and `organizations_by_normalized_name` (ROADMAP.md stage 5)."""

    def test_link_creates_a_row_and_is_idempotent(self, db: Database) -> None:
        pid, _ = db.upsert_provider(Provider(full_name="Dr. Smith", npi="1194013169"))
        oid, _ = db.upsert_organization(
            Organization(
                name="Banner Desert Medical Center",
                normalized_name="banner desert medical center",
                source="CMS",
                source_id="1",
            )
        )
        assert pid is not None and oid is not None

        created_first = db.link_provider_organization(pid, oid, source="name_match")
        created_second = db.link_provider_organization(pid, oid, source="name_match")
        assert created_first is True
        assert created_second is False  # already linked - UNIQUE constraint no-ops

        count = db.conn.execute("SELECT COUNT(*) FROM provider_organizations").fetchone()[0]
        assert count == 1

    def test_organizations_by_normalized_name_keys_correctly(self, db: Database) -> None:
        db.upsert_organization(
            Organization(
                name="Banner Desert Medical Center",
                normalized_name="banner desert medical center",
                source="CMS",
                source_id="1",
            )
        )
        by_name = db.organizations_by_normalized_name()
        assert "banner desert medical center" in by_name
        assert by_name["banner desert medical center"].name == "Banner Desert Medical Center"

    def test_providers_for_organization_linking_excludes_already_linked(self, db: Database) -> None:
        pid1, _ = db.upsert_provider(
            Provider(full_name="Dr. A", npi="1194013169", practice_name="Some Hospital")
        )
        pid2, _ = db.upsert_provider(
            Provider(full_name="Dr. B", npi="1215930367", practice_name="Some Hospital")
        )
        oid, _ = db.upsert_organization(
            Organization(name="Some Hospital", source="CMS", source_id="1")
        )
        assert pid1 is not None and pid2 is not None and oid is not None
        db.link_provider_organization(pid1, oid, source="name_match")

        candidates = db.providers_for_organization_linking()
        assert pid1 not in [p.provider_id for p in candidates]
        assert pid2 in [p.provider_id for p in candidates]

    def test_providers_for_organization_linking_excludes_providers_with_no_org_signal(
        self, db: Database
    ) -> None:
        db.upsert_provider(
            Provider(full_name="Dr. No Practice Info", npi="1710588744")
        )  # no practice_name or hospital_affiliation
        candidates = db.providers_for_organization_linking()
        assert candidates == []
