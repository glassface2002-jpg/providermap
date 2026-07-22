"""Tests for organization identity resolution (ROADMAP.md Phase 5.2):
the v7 schema (health_systems, organization_aliases,
organization_system_membership), its Database CRUD methods, foreign-key
integrity, and the pure Wikidata alias-grouping/backfill logic in
``providermap/identity_resolution.py``.

Only pure parsing/grouping functions (`_build_alias_index`) are unit tested
for the network-facing piece - `fetch_wikidata_alias_index` itself does real
network I/O and is exercised only via manual/live verification, matching how
`fetch_wikidata_hospital_websites` is treated in test_wikidata_hospitals.py.
"""

from __future__ import annotations

import sqlite3

import pytest

from providermap.database import Database
from providermap.identity_resolution import (
    HEALTH_SYSTEM_SEEDS,
    WikidataAliasIndex,
    _build_alias_index,
    backfill_organization_aliases,
    get_candidate_keys,
    seed_health_systems,
)
from providermap.models import IdentityConfidence, Organization


def _binding(hospital: str | None, label: str | None, alt: str | None = None, state: str | None = None) -> dict:
    row: dict = {}
    if hospital is not None:
        row["hospital"] = {"type": "uri", "value": hospital}
    if label is not None:
        row["hospitalLabel"] = {"xml:lang": "en", "type": "literal", "value": label}
    if alt is not None:
        row["altLabel"] = {"xml:lang": "en", "type": "literal", "value": alt}
    if state is not None:
        row["stateLabel"] = {"xml:lang": "en", "type": "literal", "value": state}
    return row


class TestBuildAliasIndex:
    """See WikidataAliasIndex's docstring for the ambiguity-exclusion rules
    this guards - the same "never guess" discipline already proven in
    wikidata_hospitals.py/hifld_hospitals.py, applied to per-item grouping."""

    def test_primary_label_and_alias_both_land_in_the_same_item(self) -> None:
        data = {
            "results": {
                "bindings": [
                    _binding(
                        "https://www.wikidata.org/entity/Q1",
                        "Wellmont Holston Valley Medical Center",
                        alt="Holston Valley Medical Center",
                    ),
                ]
            }
        }
        index = _build_alias_index(data)
        assert index.by_name["wellmont holston valley medical center"] == 0
        assert index.by_name["holston valley medical center"] == 0
        assert index.item_names[0] == {
            "wellmont holston valley medical center": "Wellmont Holston Valley Medical Center",
            "holston valley medical center": "Holston Valley Medical Center",
        }

    def test_multiple_rows_for_the_same_item_are_grouped_together(self) -> None:
        """SPARQL's OPTIONAL joins produce one row per (item, altLabel, state)
        combination - two altLabel rows for the same hospital entity must
        still collapse into one item_names entry, not two separate items."""
        data = {
            "results": {
                "bindings": [
                    _binding("https://www.wikidata.org/entity/Q1", "Test Hospital", alt="TH"),
                    _binding("https://www.wikidata.org/entity/Q1", "Test Hospital", alt="Test H"),
                ]
            }
        }
        index = _build_alias_index(data)
        assert len(index.item_names) == 1
        assert index.item_names[0] == {
            "test hospital": "Test Hospital",
            "th": "TH",
            "test h": "Test H",
        }

    def test_same_name_on_two_different_items_is_excluded_from_by_name(self) -> None:
        """Two genuinely different hospitals sharing a normalized alias
        (confirmed live, e.g. "Mercy Hospital" names 13 distinct real
        hospitals per wikidata_hospitals.py's own docstring) must never
        resolve via by_name - excluded entirely rather than picked arbitrarily."""
        data = {
            "results": {
                "bindings": [
                    _binding(
                        "https://www.wikidata.org/entity/Q1",
                        "Mercy Hospital Iowa City",
                        alt="Mercy Hospital",
                    ),
                    _binding(
                        "https://www.wikidata.org/entity/Q2",
                        "Mercy Hospital Joplin",
                        alt="Mercy Hospital",
                    ),
                ]
            }
        }
        index = _build_alias_index(data)
        assert "mercy hospital" not in index.by_name
        # the distinctive, unambiguous primary labels still resolve fine
        assert index.by_name["mercy hospital iowa city"] == 0
        assert index.by_name["mercy hospital joplin"] == 1

    def test_state_aware_index_disambiguates_same_name_different_states(self) -> None:
        data = {
            "results": {
                "bindings": [
                    _binding(
                        "https://www.wikidata.org/entity/Q1",
                        "Holy Cross Hospital",
                        state="Arizona",
                    ),
                    _binding(
                        "https://www.wikidata.org/entity/Q2",
                        "Holy Cross Hospital",
                        state="Florida",
                    ),
                ]
            }
        }
        index = _build_alias_index(data)
        assert "holy cross hospital" not in index.by_name
        assert index.by_name_state[("holy cross hospital", "AZ")] == 0
        assert index.by_name_state[("holy cross hospital", "FL")] == 1

    def test_row_missing_hospital_uri_is_skipped(self) -> None:
        data = {"results": {"bindings": [_binding(None, "Hospital A")]}}
        index = _build_alias_index(data)
        assert index == WikidataAliasIndex()

    def test_malformed_shape_returns_empty_index_not_an_exception(self) -> None:
        assert _build_alias_index({"results": "not a dict"}) == WikidataAliasIndex()
        assert _build_alias_index({"results": {"bindings": "not a list"}}) == WikidataAliasIndex()
        assert _build_alias_index({"results": {"bindings": ["not a dict"]}}) == WikidataAliasIndex()
        assert _build_alias_index({}) == WikidataAliasIndex()


class TestSeedHealthSystems:
    def test_seeds_all_six_systems(self, db: Database) -> None:
        count = seed_health_systems(db)
        assert count == len(HEALTH_SYSTEM_SEEDS)
        rows = db.conn.execute("SELECT COUNT(*) FROM health_systems").fetchone()[0]
        assert rows == len(HEALTH_SYSTEM_SEEDS)

    def test_ballad_health_domain_is_seeded(self, db: Database) -> None:
        seed_health_systems(db)
        system = db.health_system_by_normalized_name("ballad health")
        assert system is not None
        assert system.primary_domain == "balladhealth.org"

    def test_reseeding_is_idempotent(self, db: Database) -> None:
        seed_health_systems(db)
        seed_health_systems(db)
        rows = db.conn.execute("SELECT COUNT(*) FROM health_systems").fetchone()[0]
        assert rows == len(HEALTH_SYSTEM_SEEDS)


class TestBackfillOrganizationAliases:
    def test_matched_organization_gets_the_items_other_names_as_aliases(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(
                name="Wellmont Holston Valley Medical Center",
                normalized_name="wellmont holston valley medical center",
                state="TN",
                source="CMS",
                source_id="1",
            )
        )
        assert oid is not None
        index = WikidataAliasIndex(
            by_name={"wellmont holston valley medical center": 0, "holston valley medical center": 0},
            by_name_state={},
            item_names=(
                {
                    "wellmont holston valley medical center": "Wellmont Holston Valley Medical Center",
                    "holston valley medical center": "Holston Valley Medical Center",
                },
            ),
        )
        result = backfill_organization_aliases(db, index)
        assert result["organizations_matched"] == 1
        assert result["aliases_added"] == 1

        aliases = db.get_aliases_for_organization(oid)
        assert len(aliases) == 1
        assert aliases[0].alias == "Holston Valley Medical Center"
        assert aliases[0].normalized_alias == "holston valley medical center"
        assert aliases[0].source == "wikidata"
        assert aliases[0].confidence == IdentityConfidence.HIGH

    def test_unmatched_organization_is_skipped_not_guessed(self, db: Database) -> None:
        db.upsert_organization(
            Organization(
                name="Some Unrelated Hospital",
                normalized_name="some unrelated hospital",
                source="CMS",
                source_id="1",
            )
        )
        index = WikidataAliasIndex(by_name={}, by_name_state={}, item_names=())
        result = backfill_organization_aliases(db, index)
        assert result["organizations_matched"] == 0
        assert result["aliases_added"] == 0

    def test_rerunning_backfill_does_not_duplicate_aliases(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(
                name="Test Hospital",
                normalized_name="test hospital",
                source="CMS",
                source_id="1",
            )
        )
        assert oid is not None
        index = WikidataAliasIndex(
            by_name={"test hospital": 0, "th": 0},
            by_name_state={},
            item_names=({"test hospital": "Test Hospital", "th": "TH"},),
        )
        backfill_organization_aliases(db, index)
        backfill_organization_aliases(db, index)
        aliases = db.get_aliases_for_organization(oid)
        assert len(aliases) == 1


class TestHealthSystemCRUD:
    def test_get_or_create_returns_the_same_id_on_a_second_call(self, db: Database) -> None:
        first = db.get_or_create_health_system("Ascension", "ascension", "ascension.org")
        second = db.get_or_create_health_system("Ascension", "ascension", "ascension.org")
        assert first == second
        count = db.conn.execute("SELECT COUNT(*) FROM health_systems").fetchone()[0]
        assert count == 1

    def test_fills_in_a_previously_unknown_domain_without_overwriting_a_known_one(
        self, db: Database
    ) -> None:
        sid = db.get_or_create_health_system("Mercy", "mercy", primary_domain=None)
        db.get_or_create_health_system("Mercy", "mercy", primary_domain="mercy.net")
        system = db.get_health_system(sid)
        assert system is not None
        assert system.primary_domain == "mercy.net"

        # A later call with a *different* domain must not overwrite the one
        # already recorded - only fills in a gap, never guesses over a fact.
        db.get_or_create_health_system("Mercy", "mercy", primary_domain="something-else.example")
        system = db.get_health_system(sid)
        assert system is not None
        assert system.primary_domain == "mercy.net"


class TestOrganizationAliasCRUD:
    def test_add_alias_and_read_it_back(self, db: Database) -> None:
        oid, _ = db.upsert_organization(Organization(name="Test Hospital", source="CMS", source_id="1"))
        assert oid is not None
        db.add_organization_alias(
            oid, "Test Hosp", "test hosp", source="manual", confidence=IdentityConfidence.MEDIUM
        )
        aliases = db.get_aliases_for_organization(oid)
        assert len(aliases) == 1
        assert aliases[0].organization_id == oid
        assert aliases[0].confidence == IdentityConfidence.MEDIUM

    def test_adding_the_same_alias_twice_is_idempotent(self, db: Database) -> None:
        oid, _ = db.upsert_organization(Organization(name="Test Hospital", source="CMS", source_id="1"))
        assert oid is not None
        first = db.add_organization_alias(oid, "Test Hosp", "test hosp")
        second = db.add_organization_alias(oid, "Test Hosp", "test hosp")
        assert first == second
        assert len(db.get_aliases_for_organization(oid)) == 1


class TestOrganizationSystemMembershipCRUD:
    def test_set_and_get_current_membership(self, db: Database) -> None:
        oid, _ = db.upsert_organization(Organization(name="Test Hospital", source="CMS", source_id="1"))
        sid = db.get_or_create_health_system("Test System", "test system")
        assert oid is not None
        db.set_system_membership(oid, sid, source="manual_seed", confidence=IdentityConfidence.MEDIUM)

        membership = db.get_current_system_membership(oid)
        assert membership is not None
        assert membership.system_id == sid
        assert membership.valid_to is None

    def test_resetting_the_same_membership_is_idempotent(self, db: Database) -> None:
        oid, _ = db.upsert_organization(Organization(name="Test Hospital", source="CMS", source_id="1"))
        sid = db.get_or_create_health_system("Test System", "test system")
        assert oid is not None
        first = db.set_system_membership(oid, sid)
        second = db.set_system_membership(oid, sid)
        assert first == second
        count = db.conn.execute(
            "SELECT COUNT(*) FROM organization_system_membership WHERE organization_id=?", (oid,)
        ).fetchone()[0]
        assert count == 1

    def test_switching_systems_closes_the_old_membership_and_opens_a_new_one(
        self, db: Database
    ) -> None:
        """An acquisition/rebrand is a new row, never an overwrite - history
        must be preserved (see OrganizationSystemMembership's docstring)."""
        oid, _ = db.upsert_organization(Organization(name="Test Hospital", source="CMS", source_id="1"))
        old_system = db.get_or_create_health_system("Old System", "old system")
        new_system = db.get_or_create_health_system("New System", "new system")
        assert oid is not None

        old_membership_id = db.set_system_membership(oid, old_system)
        new_membership_id = db.set_system_membership(oid, new_system)
        assert new_membership_id != old_membership_id

        current = db.get_current_system_membership(oid)
        assert current is not None
        assert current.system_id == new_system

        old_row = db.conn.execute(
            "SELECT valid_to FROM organization_system_membership WHERE membership_id=?",
            (old_membership_id,),
        ).fetchone()
        assert old_row["valid_to"] is not None  # closed, not deleted

    def test_organizations_for_system_returns_only_current_members(self, db: Database) -> None:
        oid1, _ = db.upsert_organization(Organization(name="Hospital A", source="CMS", source_id="1"))
        oid2, _ = db.upsert_organization(Organization(name="Hospital B", source="CMS", source_id="2"))
        sid = db.get_or_create_health_system("Test System", "test system")
        other_sid = db.get_or_create_health_system("Other System", "other system")
        assert oid1 is not None and oid2 is not None
        db.set_system_membership(oid1, sid)
        db.set_system_membership(oid2, other_sid)

        members = db.organizations_for_system(sid)
        assert [o.organization_id for o in members] == [oid1]


class TestForeignKeyIntegrity:
    """PRAGMA foreign_keys=ON is real enforcement in this schema (see
    database.py), not just documentation - these tables must actually be
    protected by it."""

    def test_membership_with_nonexistent_system_raises(self, db: Database) -> None:
        oid, _ = db.upsert_organization(Organization(name="Test Hospital", source="CMS", source_id="1"))
        assert oid is not None
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO organization_system_membership (organization_id, system_id) "
                "VALUES (?, ?)",
                (oid, 99999),
            )

    def test_alias_with_nonexistent_organization_raises(self, db: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO organization_aliases (organization_id, alias, normalized_alias) "
                "VALUES (?, ?, ?)",
                (99999, "Alias", "alias"),
            )


class TestV6ToV7Migration:
    def test_v6_database_gains_identity_tables_without_data_loss(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """v7 adds health_systems/organization_aliases/
        organization_system_membership. A v6 database must gain them
        additively on open, keeping every existing organization row -
        mirroring TestV3ToV4Migration/TestV4ToV5Migration in
        test_organizations.py."""
        path = tmp_path / "v6.db"
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
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.execute(
            "INSERT INTO organizations (name, source, source_id) "
            "VALUES ('Existing Hospital', 'CMS', '1')"
        )
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '6')")
        conn.commit()
        conn.close()

        db = Database(path)  # SCHEMA (creates the 3 new tables) + _migrate run here

        row = db.conn.execute("SELECT name FROM organizations").fetchone()
        assert row["name"] == "Existing Hospital"

        tables = {
            r["name"] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"health_systems", "organization_aliases", "organization_system_membership"} <= tables
        assert db.conn.execute("SELECT COUNT(*) FROM health_systems").fetchone()[0] == 0

        version = db.conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone()["value"]
        assert int(version) >= 7
        db.close()

    def test_reopening_the_migrated_database_is_idempotent(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "reopen.db"
        db = Database(path)
        seed_health_systems(db)
        db.close()

        db2 = Database(path)  # migration + CREATE TABLE IF NOT EXISTS run again
        count = db2.conn.execute("SELECT COUNT(*) FROM health_systems").fetchone()[0]
        assert count == len(HEALTH_SYSTEM_SEEDS)  # not duplicated
        db2.close()


class TestGetCandidateKeys:
    """`get_candidate_keys` (ROADMAP.md Phase 5.5) - the single place
    cross-source matching's key-generation policy lives. Callers (Wikidata/
    HIFLD/VA matching in cli.py) never hardcode "own name plus which alias
    tier" themselves; these tests pin down what that policy actually is
    today so a future change to it shows up here, not as a surprise in the
    live cascade.
    """

    def test_organization_with_no_aliases_returns_just_its_own_name(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(
                name="Test Hospital", normalized_name="test hospital", source="CMS", source_id="1"
            )
        )
        org = Organization(
            organization_id=oid, name="Test Hospital", normalized_name="test hospital"
        )
        assert get_candidate_keys(org, db) == ["test hospital"]

    def test_own_name_is_always_first_followed_by_high_confidence_aliases(
        self, db: Database
    ) -> None:
        oid, _ = db.upsert_organization(
            Organization(
                name="Wellmont Holston Valley Medical Center",
                normalized_name="wellmont holston valley medical center",
                source="CMS",
                source_id="1",
            )
        )
        assert oid is not None
        db.add_organization_alias(
            oid,
            "Holston Valley Medical Center",
            "holston valley medical center",
            source="wikidata",
            confidence=IdentityConfidence.HIGH,
        )
        org = Organization(
            organization_id=oid,
            name="Wellmont Holston Valley Medical Center",
            normalized_name="wellmont holston valley medical center",
        )
        assert get_candidate_keys(org, db) == [
            "wellmont holston valley medical center",
            "holston valley medical center",
        ]

    def test_medium_and_low_confidence_aliases_are_excluded(self, db: Database) -> None:
        oid, _ = db.upsert_organization(
            Organization(
                name="Test Hospital", normalized_name="test hospital", source="CMS", source_id="1"
            )
        )
        assert oid is not None
        db.add_organization_alias(
            oid, "Medium Alias", "medium alias", confidence=IdentityConfidence.MEDIUM
        )
        db.add_organization_alias(
            oid, "Low Alias", "low alias", confidence=IdentityConfidence.LOW
        )
        db.add_organization_alias(
            oid, "High Alias", "high alias", confidence=IdentityConfidence.HIGH
        )
        org = Organization(
            organization_id=oid, name="Test Hospital", normalized_name="test hospital"
        )
        assert get_candidate_keys(org, db) == ["test hospital", "high alias"]

    def test_an_alias_identical_to_the_organizations_own_name_is_not_duplicated(
        self, db: Database
    ) -> None:
        oid, _ = db.upsert_organization(
            Organization(
                name="Test Hospital", normalized_name="test hospital", source="CMS", source_id="1"
            )
        )
        assert oid is not None
        db.add_organization_alias(
            oid, "Test Hospital", "test hospital", confidence=IdentityConfidence.HIGH
        )
        org = Organization(
            organization_id=oid, name="Test Hospital", normalized_name="test hospital"
        )
        assert get_candidate_keys(org, db) == ["test hospital"]

    def test_organization_with_no_name_and_no_id_returns_an_empty_list(self) -> None:
        org = Organization(organization_id=None, name=None, normalized_name=None)
        # A fresh Database() would be needed to look up aliases, but there's
        # no organization_id to look up in the first place - the function
        # must short-circuit before ever touching the database.
        assert get_candidate_keys(org, db=None) == []  # type: ignore[arg-type]
