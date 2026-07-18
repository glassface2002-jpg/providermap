"""End-to-end pipeline test against the offline fixture dataset.

This is the automated equivalent of ``providermap test`` on the command
line: it runs discovery, NPPES lookup, classification, validation, dedupe,
and storage against the 100-record fixture with zero network access, and
checks the result against known-correct expectations. Every edge case listed
in ``adapters/providers/adventhealth/fixtures.py``'s module docstring is exercised here.
"""

from __future__ import annotations

import asyncio

import pytest

from adapters.providers.adventhealth.adapter import AdventHealthAdapter
from adapters.providers.adventhealth.fixtures import DATASET, EXPECTED, OfflineFetcher
from providermap.config import Config
from providermap.database import Database
from providermap.net import Cache
from providermap.nppes import NppesClient
from providermap.pipeline import Pipeline


async def _run_full_pipeline(config: Config, db: Database) -> dict[str, int]:
    adapter = AdventHealthAdapter(config)
    pipeline = Pipeline(config, db, adapter)
    db.start_run("test")
    cache = Cache(".cache", enabled=False)

    async with OfflineFetcher(config, cache) as fetcher:
        nppes = NppesClient(fetcher, config)
        await pipeline.discover_urls(fetcher)
        tally = await pipeline.enrich_all(fetcher, nppes)
    return tally.as_dict()


@pytest.fixture()
def offline_run(config: Config, db: Database) -> dict[str, int]:
    return asyncio.run(_run_full_pipeline(config, db))


class TestFixtureDatasetShape:
    """Sanity checks on the fixture itself, so a broken test fixture doesn't
    masquerade as a passing pipeline."""

    def test_total_record_count(self) -> None:
        assert len(DATASET["records"]) == EXPECTED["total_urls"]

    def test_every_url_is_unique_or_a_deliberate_alias(self) -> None:
        urls = [r.url for r in DATASET["records"]]
        assert len(urls) == len(set(urls)), "fixture URLs must be unique"


class TestPipelineAgainstFixture:
    def test_correct_number_of_individuals_stored(self, offline_run: dict[str, int]) -> None:
        assert offline_run["new"] == EXPECTED["individuals"]

    def test_organizations_and_edge_cases_are_excluded(
        self,
        offline_run: dict[str, int],
    ) -> None:
        assert offline_run["excluded"] >= EXPECTED["organizations"]

    def test_no_processing_errors(self, offline_run: dict[str, int]) -> None:
        assert offline_run["error"] == 0

    def test_same_name_providers_are_flagged_not_merged(
        self,
        offline_run: dict[str, int],
        db: Database,
    ) -> None:
        dupes = db.find_probable_duplicates()
        assert len(dupes) == EXPECTED["same_name_groups"]
        assert dupes[0]["n"] == 2  # two distinct NPIs sharing one name

    def test_multi_location_provider_gets_low_confidence(
        self,
        offline_run: dict[str, int],
        db: Database,
    ) -> None:
        row = db.conn.execute(
            "SELECT site_confidence, location_count FROM providers " "WHERE full_name LIKE 'Amara%'"
        ).fetchone()
        assert row is not None
        assert row["location_count"] == 4
        assert row["site_confidence"] == "low"

    def test_duplicate_url_alias_deduplicates_to_one_provider(
        self,
        offline_run: dict[str, int],
        db: Database,
    ) -> None:
        first_record = DATASET["records"][0]
        count = db.conn.execute(
            "SELECT COUNT(*) FROM providers WHERE npi=?", (first_record.npi,)
        ).fetchone()[0]
        assert count == 1

    def test_bad_npi_checksum_is_kept_under_default_warn_policy(
        self,
        offline_run: dict[str, int],
        db: Database,
    ) -> None:
        row = db.conn.execute(
            "SELECT source_notes FROM providers WHERE npi='1234567890'"
        ).fetchone()
        assert row is not None
        assert "checksum" in row["source_notes"].lower()

    def test_facility_masquerading_as_provider_is_excluded(
        self,
        offline_run: dict[str, int],
        db: Database,
    ) -> None:
        row = db.conn.execute(
            "SELECT COUNT(*) FROM providers WHERE full_name LIKE '%Centra Care%'"
        ).fetchone()[0]
        assert row == 0
        excluded = db.conn.execute(
            "SELECT COUNT(*) FROM excluded_records WHERE name LIKE '%Centra Care%'"
        ).fetchone()[0]
        assert excluded >= 1


class TestJsonLdFieldsFlowThroughPipeline:
    """Records 0/1/2 in the fixture dataset carry, respectively, full,
    partial, and malformed JSON-LD (see fixtures.build_dataset) - this checks
    the new Provider fields survive discovery -> parsing -> classification ->
    storage for all three, not just the adapter-level unit tests."""

    def test_full_jsonld_record_stores_every_new_field(
        self, offline_run: dict[str, int], db: Database
    ) -> None:
        npi = DATASET["records"][0].npi
        p = db.get_provider(npi=npi)
        assert p is not None
        assert p.hospital_affiliation == "AdventHealth Orlando"
        assert p.accepting_new_patients is True
        assert p.rating == 4.8
        assert p.rating_count == 132
        assert p.languages == ["English", "Spanish"]
        assert p.insurance_accepted == ["Aetna", "Cigna", "UnitedHealthcare"]

    def test_partial_jsonld_record_leaves_unsupplied_fields_null(
        self, offline_run: dict[str, int], db: Database
    ) -> None:
        npi = DATASET["records"][1].npi
        p = db.get_provider(npi=npi)
        assert p is not None
        assert p.hospital_affiliation is None
        assert p.accepting_new_patients is None
        assert p.rating is None
        assert p.languages == []

    def test_malformed_jsonld_record_falls_back_and_still_stores_correctly(
        self, offline_run: dict[str, int], db: Database
    ) -> None:
        rec = DATASET["records"][2]
        p = db.get_provider(npi=rec.npi)
        assert p is not None
        assert p.full_name == f"{rec.first} {rec.last}"
        assert p.hospital_affiliation is None


class TestPipelineWithNppesDisabled:
    """The pipeline must still produce a full dataset with NPPES off - just a
    less confident one. This is the config.example.yaml `nppes.enabled: false`
    path."""

    def test_still_classifies_everyone(self, config: Config, db: Database) -> None:
        config.nppes.enabled = False
        tally = asyncio.run(_run_full_pipeline(config, db))
        assert tally["new"] == EXPECTED["individuals"]

    def test_confidence_degrades_to_medium(self, config: Config, db: Database) -> None:
        config.nppes.enabled = False
        asyncio.run(_run_full_pipeline(config, db))
        high_confidence = db.conn.execute(
            "SELECT COUNT(*) FROM providers WHERE site_confidence='high'"
        ).fetchone()[0]
        # Without NPPES confirming identity, almost nothing should remain 'high'.
        assert high_confidence < EXPECTED["individuals"] // 2


class TestPipelineWithNpiChecksumStrict:
    def test_bad_checksum_is_excluded_under_strict_policy(
        self,
        config: Config,
        db: Database,
    ) -> None:
        from providermap.models import NpiPolicy

        # NpiPolicy is immutable (frozen) - rebuild it rather than mutate.
        config.npi = NpiPolicy(checksum="strict")
        asyncio.run(_run_full_pipeline(config, db))
        row = db.conn.execute("SELECT COUNT(*) FROM providers WHERE npi='1234567890'").fetchone()[0]
        assert row == 0


class TestNppesCircuitBreaker:
    def test_trips_after_repeated_failure_and_run_still_completes(
        self,
        config: Config,
        db: Database,
    ) -> None:
        class DeadNppesFetcher(OfflineFetcher):
            async def get(
                self, url: str, limiter: object = None, use_cache: bool = True
            ) -> str | None:
                if "npiregistry" in url:
                    return None
                return await super().get(url, limiter, use_cache)

        async def run_with_dead_nppes() -> tuple[NppesClient, dict[str, int]]:
            adapter = AdventHealthAdapter(config)
            pipeline = Pipeline(config, db, adapter)
            db.start_run("test")
            cache = Cache(".cache", enabled=False)
            async with DeadNppesFetcher(config, cache) as fetcher:
                nppes = NppesClient(fetcher, config)
                await pipeline.discover_urls(fetcher)
                tally = await pipeline.enrich_all(fetcher, nppes)
            return nppes, tally.as_dict()

        nppes, tally = asyncio.run(run_with_dead_nppes())
        assert nppes.tripped
        assert tally["new"] > 0  # the run still produced a dataset
