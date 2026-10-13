"""Phase E, Stage 4 - the validation framework required by the Phase E
Implementation Directive: V1-V13, offline, against one shared fixture
population (see ``adapters/providers/cigna_fhir/fixtures.py``'s
"Discovery-mode validation fixtures" section) run through BOTH discovery
strategies.

Not every criterion in the directive is a dedicated test here:

* V8 (checkpoint recovery) is already covered end-to-end for both
  strategies - ``TestNationalDiscoveryCheckpointResume`` in
  ``test_cigna_fhir_discovery.py`` and the equivalent state_scoped tests in
  ``test_cigna_fhir_adapter.py``. Nothing new to add.
* V9/V10 (bounded memory, reduced request count) are operational
  characteristics that only mean something at real scale - a 3-practitioner
  fixture proves nothing about memory/request behavior at a million
  practitioners. These are measured live, per
  ``NATIONAL_DISCOVERY_DESIGN.md``'s migration plan (calibration probe,
  staging dry-run) - not reproducible offline, so not faked here.
* V12 (full suite green) isn't a test in this file - it's the fact that
  running the entire suite (this file included) exits 0.

V1-V7, V11, V13 are implemented below.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from adapters.providers.base_dataset import NormalizedProvider
from adapters.providers.cigna_fhir.adapter import (
    CignaFhirAdapter,
    _location_search_url,
    _national_window_url,
    _practitioner_role_by_location_url,
)
from adapters.providers.cigna_fhir.comparison import compare_normalized_providers
from adapters.providers.cigna_fhir.discovery import _generate_lastupdated_windows
from adapters.providers.cigna_fhir.fixtures import (
    VALIDATION_NATIONAL_BUNDLE,
    VALIDATION_RISK_MULTI_STATE_NPI,
    VALIDATION_SAFE_MULTI_STATE_NPI,
    VALIDATION_SINGLE_STATE_NPI,
    VALIDATION_STATE_LOCATION_BUNDLE,
    VALIDATION_STATE_ROLE_BUNDLE,
    make_fetcher,
)
from providermap.config import Config
from providermap.database import Database
from providermap.exporter import export_providers
from providermap.validator import validate_provider

_ALL_VALIDATION_NPIS = {
    VALIDATION_SINGLE_STATE_NPI,
    VALIDATION_SAFE_MULTI_STATE_NPI,
    VALIDATION_RISK_MULTI_STATE_NPI,
}

# ---------------------------------------------------------------------- #
# shared setup helpers
# ---------------------------------------------------------------------- #


def _state_scoped_adapter(config: Config) -> CignaFhirAdapter:
    config.providers.cigna_fhir_state = "RI"
    pc = config.providers
    location_url = _location_search_url(pc.cigna_fhir_base_url, "RI", pc.cigna_fhir_page_size)
    role_url = _practitioner_role_by_location_url(
        pc.cigna_fhir_base_url, "val-loc-ri", pc.cigna_fhir_page_size
    )
    pages = {
        location_url: VALIDATION_STATE_LOCATION_BUNDLE,
        role_url: VALIDATION_STATE_ROLE_BUNDLE,
    }
    return CignaFhirAdapter(config, page_fetcher=make_fetcher(pages))


def _national_adapter(config: Config, tmp_path) -> CignaFhirAdapter:  # type: ignore[no-untyped-def]
    config.providers.cigna_fhir_discovery_mode = "national"
    config.providers.resource_spool_path = str(tmp_path / "spool.db")
    config.providers.cigna_fhir_window_days = 30
    config.providers.cigna_fhir_lookback_days = 90
    pc = config.providers
    windows = _generate_lastupdated_windows(
        pc.cigna_fhir_window_days, pc.cigna_fhir_lookback_days, datetime.now(timezone.utc)
    )
    urls = [
        _national_window_url(pc.cigna_fhir_base_url, ge, le, pc.cigna_fhir_page_size)
        for ge, le in windows
    ]
    empty = {"resourceType": "Bundle", "type": "searchset", "link": [], "entry": []}
    pages = dict.fromkeys(urls, empty)
    pages[urls[1]] = VALIDATION_NATIONAL_BUNDLE
    return CignaFhirAdapter(config, page_fetcher=make_fetcher(pages))


def _normalized_by_npi(adapter: CignaFhirAdapter) -> dict[str, NormalizedProvider]:
    result: dict[str, NormalizedProvider] = {}
    for raw in adapter.discover():
        normalized = adapter.normalize(adapter.extract(raw))
        assert normalized.provider.npi
        result[normalized.provider.npi] = normalized
    return result


def _ingest_all(adapter: CignaFhirAdapter, db: Database) -> dict[str, int]:
    """The write sequence ``cmd_ingest_providers`` uses (NPI-first dedupe,
    enrichment-only for existing NPIs, additive location/organization
    links) - deliberately mirrored here rather than reused wholesale, since
    the CLI function also does run-table bookkeeping and printing that
    these tests have no need for. Not a second persistence path: every call
    below is the exact same ``Database`` method the CLI uses.
    """
    npi_policy = replace(adapter.config.npi, require_profile_url=adapter.requires_profile_url)
    tally = {"new": 0, "existing": 0, "excluded": 0}
    for raw in adapter.discover():
        normalized = adapter.normalize(adapter.extract(raw))
        provider = normalized.provider
        is_valid, _problems = validate_provider(provider, npi_policy)
        if not is_valid:
            tally["excluded"] += 1
            continue
        existing = db.get_provider(npi=provider.npi) if provider.npi else None
        if existing is None:
            provider_id, _ = db.upsert_provider(provider)
            tally["new"] += 1
        else:
            provider_id = existing.provider_id
            tally["existing"] += 1
        for i, loc in enumerate(normalized.locations):
            location_id = db.upsert_location(loc)
            if provider_id is not None and location_id is not None:
                db.link(provider_id, location_id, is_primary=(i == 0), enrich_only=True)
        for org, _relationship in normalized.organizations:
            organization_id, _ = db.upsert_organization(org)
            if provider_id is not None and organization_id is not None:
                db.link_provider_organization(provider_id, organization_id, source=adapter.source)
    return tally


# ---------------------------------------------------------------------- #
# V1 - yielded-dict contract (bare-id keys, required keys present)
# ---------------------------------------------------------------------- #


class TestV1YieldedDictContract:
    def _assert_contract(self, records: list[dict]) -> None:  # type: ignore[type-arg]
        assert records
        for record in records:
            assert record["practitioner"] is not None
            assert record["roles"]
            for loc_id in record["locations"]:
                assert "/" not in loc_id
            for org_id in record["organizations"]:
                assert "/" not in org_id

    def test_state_scoped_satisfies_the_contract(self, config: Config) -> None:
        self._assert_contract(list(_state_scoped_adapter(config).discover()))

    def test_national_satisfies_the_contract(self, config: Config, tmp_path) -> None:  # type: ignore[no-untyped-def]
        self._assert_contract(list(_national_adapter(config, tmp_path).discover()))


# ---------------------------------------------------------------------- #
# V2 - identity is absolute (invariant rule 1)
# ---------------------------------------------------------------------- #


class TestV2IdentityIsAbsolute:
    def test_shared_npis_have_no_identity_mismatch(self, config: Config, tmp_path) -> None:  # type: ignore[no-untyped-def]
        state = _normalized_by_npi(_state_scoped_adapter(config))
        national = _normalized_by_npi(_national_adapter(config, tmp_path))

        assert set(state) == _ALL_VALIDATION_NPIS
        assert set(national) == _ALL_VALIDATION_NPIS

        comparison = compare_normalized_providers(state, national)
        assert comparison.identity_mismatches == {}


# ---------------------------------------------------------------------- #
# V3 - relationships are monotonic, never contradictory (invariant rule 2)
# ---------------------------------------------------------------------- #


class TestV3RelationshipsAreMonotonic:
    def test_state_scoped_locations_are_a_subset_of_national(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        state = _normalized_by_npi(_state_scoped_adapter(config))
        national = _normalized_by_npi(_national_adapter(config, tmp_path))

        comparison = compare_normalized_providers(state, national)
        assert comparison.relationship_regressions == {}

    def test_safe_multi_state_npi_gains_a_location_under_national(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        state = _normalized_by_npi(_state_scoped_adapter(config))
        national = _normalized_by_npi(_national_adapter(config, tmp_path))

        assert len(state[VALIDATION_SAFE_MULTI_STATE_NPI].locations) == 1
        assert len(national[VALIDATION_SAFE_MULTI_STATE_NPI].locations) == 2


# ---------------------------------------------------------------------- #
# V4 - zero duplicate providers across sequential runs of different modes
# ---------------------------------------------------------------------- #


class TestV4NoDuplicateProviders:
    def test_running_state_scoped_then_national_creates_no_duplicate_npi_rows(
        self, config: Config, tmp_path, db: Database
    ) -> None:  # type: ignore[no-untyped-def]
        _ingest_all(_state_scoped_adapter(config), db)
        _ingest_all(_national_adapter(config, tmp_path), db)

        dupes = db.conn.execute(
            "SELECT npi, COUNT(*) c FROM providers GROUP BY npi HAVING c > 1"
        ).fetchall()
        assert dupes == []
        assert db.counts()["providers"] == len(_ALL_VALIDATION_NPIS)


# ---------------------------------------------------------------------- #
# V5 - primary-location divergence is detected, not silent (Risk R1)
# ---------------------------------------------------------------------- #


class TestV5PrimaryLocationDivergenceIsDetected:
    def test_only_the_risk_npi_diverges(self, config: Config, tmp_path) -> None:  # type: ignore[no-untyped-def]
        state = _normalized_by_npi(_state_scoped_adapter(config))
        national = _normalized_by_npi(_national_adapter(config, tmp_path))

        comparison = compare_normalized_providers(state, national)
        diverging_npis = {d.npi for d in comparison.primary_location_divergences}
        assert diverging_npis == {VALIDATION_RISK_MULTI_STATE_NPI}

    def test_the_divergence_is_exactly_ri_vs_tx(self, config: Config, tmp_path) -> None:  # type: ignore[no-untyped-def]
        state = _normalized_by_npi(_state_scoped_adapter(config))
        national = _normalized_by_npi(_national_adapter(config, tmp_path))

        comparison = compare_normalized_providers(state, national)
        divergence = next(
            d
            for d in comparison.primary_location_divergences
            if d.npi == VALIDATION_RISK_MULTI_STATE_NPI
        )
        assert divergence.a_address[2] == "RI"  # index 2 = state, see PrimaryLocationDivergence
        assert divergence.b_address[2] == "TX"


# ---------------------------------------------------------------------- #
# V6 - enrichment-only preserved under national mode
# ---------------------------------------------------------------------- #


class TestV6EnrichmentOnlyPreserved:
    def test_a_provider_seeded_by_state_scoped_is_not_overwritten_by_national(
        self, config: Config, tmp_path, db: Database
    ) -> None:  # type: ignore[no-untyped-def]
        """Repeats Phase C's mutation test under Stage 4's shared fixture:
        whichever run creates a provider row first "wins" the scalar
        fields - a later run against a different scope only ever adds
        relationships, never rewrites ``providers.address`` etc. See
        ``Database.upsert_provider``'s enrichment-only contract.
        """
        _ingest_all(_state_scoped_adapter(config), db)
        seeded = db.get_provider(npi=VALIDATION_RISK_MULTI_STATE_NPI)
        assert seeded is not None
        assert seeded.state == "RI"

        _ingest_all(_national_adapter(config, tmp_path), db)
        after = db.get_provider(npi=VALIDATION_RISK_MULTI_STATE_NPI)
        assert after is not None
        assert after.state == "RI", (
            "national mode overwrote a provider scalar that state_scoped "
            "already established - upsert_provider's enrichment-only "
            "contract (existing NPI -> row untouched) has regressed."
        )

    def test_provider_locations_is_primary_is_enrichment_only_across_modes(
        self, config: Config, tmp_path, db: Database
    ) -> None:  # type: ignore[no-untyped-def]
        """Regression test for a gap this validation framework surfaced
        and which has since been fixed: ``upsert_provider``'s
        enrichment-only contract protected ``providers.address/city/
        state/zip`` (see the test above), but ``Database.link()``
        (``provider_locations.is_primary``) had no equivalent contract -
        it always ``INSERT OR REPLACE``d whatever the *current* call said.
        ``normalize()`` sorts each run's own primary location first, so
        for the RISK NPI, state_scoped's run marked RI ``is_primary=1``,
        and national's later run - which independently picks TX as ITS
        primary (Risk R1) - would flip RI to ``is_primary=0`` and mark TX
        ``is_primary=1``, leaving ``providers.address`` (still RI) and
        ``provider_locations.is_primary`` (now TX) disagreeing about the
        same provider's primary location.

        Fixed via ``Database.link(..., enrich_only=True)`` (see its
        docstring): an already-recorded relationship's ``is_primary`` is
        never re-litigated, and a new relationship is never allowed to
        claim ``is_primary=True`` while the provider already has a
        different primary location. ``cmd_ingest_providers`` now passes
        ``enrich_only=True`` on every ``link()`` call, mirrored here via
        ``_ingest_all``. If this test starts failing, that contract has
        regressed - don't just delete it, restore the guarantee.
        """
        _ingest_all(_state_scoped_adapter(config), db)
        provider_id = db.get_provider(npi=VALIDATION_RISK_MULTI_STATE_NPI).provider_id

        def primary_location_state(pid: int) -> str | None:
            row = db.conn.execute(
                "SELECT l.state FROM provider_locations pl "
                "JOIN locations l ON l.location_id = pl.location_id "
                "WHERE pl.provider_id = ? AND pl.is_primary = 1",
                (pid,),
            ).fetchone()
            return row["state"] if row else None

        assert primary_location_state(provider_id) == "RI"

        _ingest_all(_national_adapter(config, tmp_path), db)

        assert primary_location_state(provider_id) == "RI", (
            "national's later pass flipped which location is flagged "
            "primary in provider_locations, even though it never touches "
            "providers.address for an already-known NPI - the two tables "
            "now disagree about this provider's primary location. See "
            "Database.link()'s enrich_only parameter."
        )
        assert db.get_provider(npi=VALIDATION_RISK_MULTI_STATE_NPI).state == "RI"

        # TX must still be recorded - enrich_only blocks a contested
        # primary re-assignment, it must not silently drop the
        # relationship national genuinely discovered.
        tx_row = db.conn.execute(
            "SELECT pl.is_primary FROM provider_locations pl "
            "JOIN locations l ON l.location_id = pl.location_id "
            "WHERE pl.provider_id = ? AND l.state = 'TX'",
            (provider_id,),
        ).fetchone()
        assert tx_row is not None
        assert tx_row["is_primary"] == 0


# ---------------------------------------------------------------------- #
# V7 - deterministic + idempotent (same fixture twice -> zero new writes)
# ---------------------------------------------------------------------- #


class TestV7DeterministicAndIdempotent:
    def test_running_national_twice_produces_zero_new_provider_changes_on_the_second_pass(
        self, config: Config, tmp_path, db: Database
    ) -> None:  # type: ignore[no-untyped-def]
        _ingest_all(_national_adapter(config, tmp_path / "run1"), db)
        before = db.counts()
        changes_before = db.conn.execute("SELECT COUNT(*) c FROM provider_changes").fetchone()["c"]

        _ingest_all(_national_adapter(config, tmp_path / "run2"), db)
        after = db.counts()
        changes_after = db.conn.execute("SELECT COUNT(*) c FROM provider_changes").fetchone()["c"]

        assert after["providers"] == before["providers"]
        assert changes_after == changes_before


# ---------------------------------------------------------------------- #
# V11 - zero export regression
# ---------------------------------------------------------------------- #


class TestV11ExportRegression:
    def test_national_populated_db_exports_cleanly(
        self, config: Config, tmp_path, db: Database
    ) -> None:  # type: ignore[no-untyped-def]
        _ingest_all(_national_adapter(config, tmp_path), db)
        out = tmp_path / "providers.xlsx"
        n = export_providers(db, out)
        assert n == len(_ALL_VALIDATION_NPIS)
        assert out.exists()


# ---------------------------------------------------------------------- #
# V13 - export equivalence across discovery modes (the migration gate)
# ---------------------------------------------------------------------- #


class TestV13ExportEquivalence:
    """Runs the SAME shared fixture through both strategies into two
    separate disposable DBs, exports both, and diffs the result against
    the one documented closed-set delta: national may carry a higher
    ``Location Count`` than state_scoped for a multi-state practitioner
    (locations relationship rows in the (subset) direction only). Any
    *other* column difference - most importantly Address/City/State/ZIP,
    the primary-location columns Risk R1 warns about - fails the test
    rather than being silently absorbed into "expected variance".
    """

    _IDENTITY_COLUMNS = ("Provider Name", "Credentials", "Languages", "NPI")

    def _rows_by_npi(self, path) -> dict[str, dict]:  # type: ignore[no-untyped-def,type-arg]
        from openpyxl import load_workbook

        wb = load_workbook(path)
        ws = wb["Providers"]
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        rows = {}
        for row in ws.iter_rows(min_row=2, values_only=True):
            record = dict(zip(headers, row, strict=True))
            rows[record["NPI"]] = record
        return rows

    def test_identity_columns_match_and_location_count_delta_is_closed_set(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        db_a = Database(tmp_path / "state_scoped.db")
        db_b = Database(tmp_path / "national.db")
        try:
            _ingest_all(_state_scoped_adapter(config), db_a)
            _ingest_all(_national_adapter(config, tmp_path), db_b)

            out_a = tmp_path / "state_scoped_providers.xlsx"
            out_b = tmp_path / "national_providers.xlsx"
            export_providers(db_a, out_a)
            export_providers(db_b, out_b)

            rows_a = self._rows_by_npi(out_a)
            rows_b = self._rows_by_npi(out_b)
            assert set(rows_a) == set(rows_b) == _ALL_VALIDATION_NPIS

            for npi in _ALL_VALIDATION_NPIS:
                for col in self._IDENTITY_COLUMNS:
                    assert rows_a[npi][col] == rows_b[npi][col], (
                        f"{col} differs for NPI {npi} between discovery modes - "
                        f"this is an identity field and must never differ."
                    )

            # SINGLE never had a second location to discover - zero delta.
            single = VALIDATION_SINGLE_STATE_NPI
            assert rows_a[single]["Location Count"] == rows_b[single]["Location Count"] == 1
            assert rows_a[single]["Address"] == rows_b[single]["Address"]

            # SAFE: the one documented, allowed delta - location_count
            # differs, primary address (the RI clinic) does not.
            safe = VALIDATION_SAFE_MULTI_STATE_NPI
            assert rows_a[safe]["Location Count"] == 1
            assert rows_b[safe]["Location Count"] == 2
            assert rows_a[safe]["Address"] == rows_b[safe]["Address"]
            assert rows_a[safe]["State"] == rows_b[safe]["State"] == "RI"
        finally:
            db_a.close()
            db_b.close()

    def test_risk_npi_primary_address_divergence_is_enumerated_not_silently_tolerated(
        self, config: Config, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """The RISK practitioner is the one case in this fixture where the
        two modes pick a genuinely different primary location (Risk R1).
        This is NOT an allowed delta - V13's job is to make sure it is
        always caught and explicitly reported, never lost in an "only
        location_count differs" assumption. If this test ever starts
        failing because the addresses now match, that's good news (Risk
        R1 got resolved) - update this test, don't delete it.
        """
        db_a = Database(tmp_path / "state_scoped_risk.db")
        db_b = Database(tmp_path / "national_risk.db")
        try:
            _ingest_all(_state_scoped_adapter(config), db_a)
            _ingest_all(_national_adapter(config, tmp_path), db_b)

            out_a = tmp_path / "state_scoped_risk_providers.xlsx"
            out_b = tmp_path / "national_risk_providers.xlsx"
            export_providers(db_a, out_a)
            export_providers(db_b, out_b)

            rows_a = self._rows_by_npi(out_a)
            rows_b = self._rows_by_npi(out_b)
            risk = VALIDATION_RISK_MULTI_STATE_NPI

            assert rows_a[risk]["State"] == "RI"
            assert rows_b[risk]["State"] == "TX"
            assert rows_a[risk]["Address"] != rows_b[risk]["Address"], (
                "Expected the documented Risk R1 divergence for the RISK "
                "NPI (state_scoped picks RI, national picks TX) - if this "
                "no longer diverges, Risk R1 has been resolved and this "
                "test should be updated to reflect that, not left as a "
                "silently-passing assertion."
            )
        finally:
            db_a.close()
            db_b.close()
