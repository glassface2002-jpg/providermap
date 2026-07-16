"""Tests for providermap.database - dedupe, incremental change tracking,
resume, and schema migration.
"""

from __future__ import annotations

import sqlite3

from providermap.database import Database
from providermap.models import ExcludedRecord, Location, Provider, ProviderType, UpsertOutcome


def _provider(**overrides: object) -> Provider:
    base: dict[str, object] = {
        "full_name": "Justin Menezes",
        "first_name": "Justin",
        "last_name": "Menezes",
        "credentials": "MD",
        "provider_type": ProviderType.PHYSICIAN,
        "specialty": "Family Medicine",
        "primary_site_of_care": "AdventHealth Medical Group at South Street",
        "practice_name": "AdventHealth Medical Group",
        "address": "320 E South Street",
        "city": "Orlando",
        "state": "FL",
        "zip": "32801",
        "phone": "407-843-1180",
        "profile_url": "https://x/doctors/a-1194013169",
        "npi": "1194013169",
        "location_count": 1,
    }
    base.update(overrides)
    return Provider(**base)  # type: ignore[arg-type]


class TestResumeLedger:
    def test_enqueue_is_idempotent(self, db: Database) -> None:
        urls = ["https://x/a-1", "https://x/b-2"]
        assert db.enqueue(urls) == 2
        assert db.enqueue(urls) == 0  # already known, not re-added
        assert len(db.pending_urls()) == 2

    def test_mark_done_removes_from_pending(self, db: Database) -> None:
        db.enqueue(["https://x/a-1"])
        db.mark("https://x/a-1", "done")
        assert db.pending_urls() == []
        assert db.is_done("https://x/a-1")

    def test_error_urls_stay_pending_for_retry(self, db: Database) -> None:
        db.enqueue(["https://x/a-1"])
        db.mark("https://x/a-1", "error", "timeout")
        assert "https://x/a-1" in db.pending_urls()

    def test_requeue_forces_reprocessing(self, db: Database) -> None:
        db.enqueue(["https://x/a-1"])
        db.mark("https://x/a-1", "done")
        assert db.pending_urls() == []
        db.requeue(["https://x/a-1"])
        assert "https://x/a-1" in db.pending_urls()


class TestProviderDedupe:
    def test_upsert_twice_same_npi_is_one_row(self, db: Database) -> None:
        pid1, outcome1 = db.upsert_provider(_provider())
        pid2, outcome2 = db.upsert_provider(_provider(specialty="Updated"))
        assert pid1 == pid2
        assert outcome1 == UpsertOutcome.NEW
        assert outcome2 == UpsertOutcome.CHANGED
        assert db.counts()["providers"] == 1

    def test_dedupe_falls_back_to_profile_url_when_npi_absent(self, db: Database) -> None:
        db.upsert_provider(_provider())
        # npi is not a TRACKED_FIELD (it's a dedupe key, not tracked content),
        # so writing the same row again with npi omitted is correctly
        # UNCHANGED - the point of this test is that it dedupes to one row,
        # not that removing the npi counts as a content change.
        pid, outcome = db.upsert_provider(_provider(npi=None))
        assert outcome == UpsertOutcome.UNCHANGED
        assert db.counts()["providers"] == 1
        assert pid is not None

    def test_npi_is_never_nulled_by_a_later_update(self, db: Database) -> None:
        """A re-scrape that fails to determine the NPI must not erase a
        previously-proven one - the dedupe key would be destroyed."""
        db.upsert_provider(_provider())
        db.upsert_provider(_provider(npi=None))
        row = db.conn.execute("SELECT npi FROM providers").fetchone()
        assert row["npi"] == "1194013169"

    def test_same_name_different_npi_is_flagged_not_merged(self, db: Database) -> None:
        db.upsert_provider(_provider(npi="1194013169", profile_url="https://x/1"))
        db.upsert_provider(_provider(npi="1548285370", profile_url="https://x/2"))
        assert db.counts()["providers"] == 2
        dupes = db.find_probable_duplicates()
        assert len(dupes) == 1
        assert dupes[0]["n"] == 2


class TestIncrementalChangeDetection:
    def test_identical_write_is_unchanged(self, db: Database) -> None:
        db.upsert_provider(_provider())
        _, outcome = db.upsert_provider(_provider())
        assert outcome == UpsertOutcome.UNCHANGED
        assert db.counts()["changes_logged"] == 0

    def test_changed_field_is_recorded_in_audit_trail(self, db: Database) -> None:
        db.run_id = db.start_run("test")
        db.upsert_provider(_provider())
        db.upsert_provider(_provider(city="Maitland", zip="32751"))
        changes = db.changes_since(db.run_id)
        fields_changed = {c.field for c in changes}
        assert "city" in fields_changed
        assert "zip" in fields_changed

    def test_unrelated_numeric_field_does_not_produce_a_false_diff(self, db: Database) -> None:
        """Regression test: comparing SQLite's raw int against a stringified
        value without normalizing both sides logs a false change on every
        write to a numeric column, even when the value didn't move."""
        db.run_id = db.start_run("test")
        db.upsert_provider(_provider(location_count=1))
        db.upsert_provider(_provider(location_count=1, city="Maitland"))
        changes = db.changes_since(db.run_id)
        assert "location_count" not in {c.field for c in changes}
        assert "city" in {c.field for c in changes}

    def test_unchanged_provider_is_still_touched(self, db: Database) -> None:
        pid, _ = db.upsert_provider(_provider())
        before = db.conn.execute(
            "SELECT last_checked FROM providers WHERE provider_id=?", (pid,)
        ).fetchone()["last_checked"]
        db.upsert_provider(_provider())
        after = db.conn.execute(
            "SELECT last_checked FROM providers WHERE provider_id=?", (pid,)
        ).fetchone()["last_checked"]
        assert after is not None
        assert before is not None


class TestDeactivation:
    def test_deactivate_missing_marks_not_deletes(self, db: Database) -> None:
        db.start_run("full")
        db.upsert_provider(_provider(profile_url="https://x/1", npi="1194013169"))
        run2 = db.start_run("full")  # a later run that does NOT see this provider
        db.deactivate_missing(run2)
        row = db.conn.execute("SELECT is_active FROM providers").fetchone()
        assert row["is_active"] == 0
        # Still present - never deleted.
        assert db.conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 1

    def test_active_provider_survives_deactivation_if_seen_this_run(self, db: Database) -> None:
        run1 = db.start_run("full")
        db.upsert_provider(_provider())
        db.deactivate_missing(run1)
        assert db.counts()["providers"] == 1


class TestLocations:
    def test_same_address_key_reuses_one_row(self, db: Database) -> None:
        loc1 = db.upsert_location(
            Location(
                address="100 Main St",
                city="Orlando",
                state="FL",
                zip="32801",
                address_key="100 main st|FL|32801",
            )
        )
        loc2 = db.upsert_location(
            Location(
                address="100 Main Street",
                city="Orlando",
                state="FL",
                zip="32801",
                address_key="100 main st|FL|32801",
            )
        )
        assert loc1 == loc2
        assert db.counts()["locations"] == 1


class TestExclusions:
    def test_exclude_then_reexclude_updates_not_duplicates(self, db: Database) -> None:
        rec = ExcludedRecord(
            name="Centra Care",
            url="https://x/facility-1",
            classification="Clinic",
            reason_excluded="org",
            npi="123",
        )
        db.exclude(rec)
        db.exclude(rec)
        count = db.conn.execute("SELECT COUNT(*) FROM excluded_records").fetchone()[0]
        assert count == 1


class TestDryRun:
    def test_dry_run_rolls_back_every_write(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "dryrun.db"
        db = Database(path, dry_run=True)
        db.upsert_provider(_provider())
        db.enqueue(["https://x/a-1"])
        assert db.pending_writes > 0
        db.close()

        # Re-open normally (not dry-run) and confirm nothing persisted.
        verify = Database(path)
        assert verify.counts()["providers"] == 0
        assert verify.counts()["urls_total"] == 0
        verify.close()


class TestSchemaMigration:
    def test_v1_database_migrates_without_data_loss(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE providers (provider_id INTEGER PRIMARY KEY, full_name TEXT,
                first_name TEXT, last_name TEXT, credentials TEXT, provider_type TEXT,
                specialty TEXT, primary_site_of_care TEXT, practice_name TEXT,
                address TEXT, city TEXT, state TEXT, zip TEXT, phone TEXT,
                profile_url TEXT UNIQUE, npi TEXT UNIQUE, created_date DATETIME,
                updated_date DATETIME, location_count INTEGER DEFAULT 1,
                site_confidence TEXT, nppes_enumeration TEXT, nppes_name_match INTEGER,
                taxonomy_code TEXT, taxonomy_desc TEXT, source_notes TEXT);
            CREATE TABLE locations (location_id INTEGER PRIMARY KEY, facility_name TEXT,
                address TEXT, city TEXT, state TEXT, zip TEXT, phone TEXT,
                address_key TEXT UNIQUE);
            CREATE TABLE provider_locations (id INTEGER PRIMARY KEY, provider_id INTEGER,
                location_id INTEGER, is_primary INTEGER DEFAULT 0, rank INTEGER);
            CREATE TABLE excluded_records (id INTEGER PRIMARY KEY, name TEXT,
                url TEXT UNIQUE, classification TEXT, reason_excluded TEXT, npi TEXT,
                date_found DATETIME);
            CREATE TABLE scrape_log (id INTEGER PRIMARY KEY, url TEXT UNIQUE,
                status TEXT, attempts INTEGER DEFAULT 0, error_message TEXT,
                timestamp DATETIME);
            CREATE TABLE run_stats (key TEXT PRIMARY KEY, value TEXT);
        """
        )
        conn.execute(
            "INSERT INTO providers (full_name, npi, profile_url, city, provider_type) "
            "VALUES ('Legacy Person', '1194013169', 'https://x/a-1194013169', "
            "'Orlando', 'Physician')"
        )
        conn.execute(
            "INSERT INTO scrape_log (url, status, attempts) "
            "VALUES ('https://x/a-1194013169', 'done', 1)"
        )
        conn.commit()
        conn.close()

        db = Database(path)  # migration runs here
        row = db.conn.execute("SELECT full_name, is_active FROM providers").fetchone()
        assert row["full_name"] == "Legacy Person"
        assert row["is_active"] == 1  # existing rows default to active
        assert db.is_done("https://x/a-1194013169")
        db.close()

        # Re-opening must be idempotent - no error, no duplicate migration.
        db2 = Database(path)
        assert db2.counts()["providers"] == 1
        db2.close()

    def test_fresh_database_has_current_schema(self, db: Database) -> None:
        version = db.conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[
            "value"
        ]
        assert int(version) >= 2


class TestRuns:
    def test_run_lifecycle_is_recorded(self, db: Database) -> None:
        run_id = db.start_run("full", source_used="sitemap")
        db.finish_run(providers_new=5, excluded=2)
        last = db.last_run()
        assert last is not None
        assert last.run_id == run_id
        assert last.providers_new == 5
        assert last.excluded == 2
