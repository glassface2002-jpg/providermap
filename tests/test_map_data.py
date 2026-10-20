"""Tests for providermap.map_data - the summaries the dashboard reads.

Workbooks are produced by the real ``export_providers`` so a renamed export
header breaks these tests instead of silently blanking a dashboard column.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import pandas as pd
import pytest

from providermap.cli import cmd_build_map_data
from providermap.config import Config
from providermap.database import Database
from providermap.exporter import export_providers
from providermap.geocode import Address, GeoResult, address_key
from providermap.map_data import (
    BY_SPECIALTY_NAME,
    BY_STATE_NAME,
    CONFIDENCE_COLUMNS,
    EXPORT_COLUMNS,
    HOSPITAL_COLUMNS,
    HOSPITAL_PARQUET_NAME,
    INFO_NAME,
    LOCATIONS_NAME,
    MIN_NAMED_PROVIDERS,
    NOT_ASSESSED,
    NOT_STATED,
    PRECISION_COLUMNS,
    ROW_COLUMNS,
    TYPE_APP,
    TYPE_COLUMNS,
    TYPE_FROM_SOURCE,
    TYPE_INFERRED,
    TYPE_OTHER,
    TYPE_PHYSICIAN,
    UNKNOWN,
    aggregate_locations,
    build_hospital_snapshot,
    build_snapshot,
    infer_provider_type,
    load_database,
    load_export,
    state_codes,
    summarize_by_state,
    write_hospital_snapshot,
    write_snapshot,
)
from providermap.models import Confidence, Provider, ProviderType

# Strings that identify a person. None of them may reach the written summaries.
PERSONAL = ["Menezes", "Justin", "1194013169", "example.org", "VANT-FOWLER", "1265446264"]


def _export(db: Database, path: Path, providers: list[Provider]) -> Path:
    for p in providers:
        db.upsert_provider(p)
    export_providers(db, path)
    return path


def _full() -> Provider:
    return Provider(
        full_name="Justin Menezes, MD",
        first_name="Justin",
        last_name="Menezes",
        credentials="MD",
        provider_type=ProviderType.PHYSICIAN,
        specialty="Family Medicine",
        primary_site_of_care="AdventHealth Family Medicine at Winter Park",
        address="133 Benmore Drive Suite 200",
        city="Winter Park",
        state="FL",
        zip="32792",
        profile_url="https://example.org/doctors/justin-menezes-1194013169",
        npi="1194013169",
        site_confidence=Confidence.HIGH,
    )


def _sparse() -> Provider:
    """A run that never classified type or confidence (like the Rhode Island run)."""
    return Provider(
        full_name="CATHERINE A'VANT-FOWLER",
        first_name="CATHERINE",
        last_name="A'VANT-FOWLER",
        specialty="Counselor",
        address="530 N MAIN ST",
        city="PROVIDENCE",
        state="RI",
        zip="02904-5762",
        npi="1265446264",
    )


def _no_zip() -> Provider:
    return Provider(
        full_name="No Zip",
        first_name="No",
        last_name="Zip",
        address="1 Main St",
        city="Providence",
        state="RI",
        npi="1234567893",
    )


def fake_geocoder(addresses: list[Address]) -> dict[str, GeoResult]:
    """Every addressable row 'matches' at a fixed point, except Providence -> ZIP centroid."""
    out = {}
    for addr in addresses:
        key = address_key(addr)
        if key is None:
            continue
        if (addr.city or "").upper() == "PROVIDENCE":
            out[key] = GeoResult(41.85, -71.43, None, "zip")
        else:
            out[key] = GeoResult(28.59, -81.32, "12095", "address")
    return out


@pytest.fixture()
def exports(db: Database, tmp_path: Path) -> dict[str, Path]:
    fl = _export(db, tmp_path / "fl.xlsx", [_full(), _no_zip()])
    ri_db = Database(tmp_path / "ri.db")
    try:
        ri = _export(ri_db, tmp_path / "ri.xlsx", [_sparse()])
    finally:
        ri_db.close()
    return {"AdventHealth": fl, "Rhode Island": ri}


@pytest.fixture()
def db_copy(db: Database, tmp_path: Path) -> Path:
    """A copy of a pipeline database holding one Florida, one Rhode Island and one inactive row."""
    for p in (_full(), _sparse(), dataclasses.replace(_no_zip(), state="AZ")):
        db.upsert_provider(p)
    copy = tmp_path / "copy.db"
    target = sqlite3.connect(copy)
    try:
        db.conn.backup(target)
        target.execute("UPDATE providers SET is_active = 0 WHERE state = 'AZ'")
        target.commit()
    finally:
        target.close()
    return copy


class TestLoadExport:
    def test_keeps_only_summary_columns_and_labels_missing_values(self, exports):
        df = load_export("Rhode Island", exports["Rhode Island"])
        assert list(df.columns) == ["dataset", *EXPORT_COLUMNS.values(), "provider_type_source"]
        row = df.iloc[0]
        assert row["dataset"] == "Rhode Island"
        assert row["city"] == "Providence"  # tidied from PROVIDENCE
        assert row["provider_type"] == TYPE_OTHER  # the source gave none: inferred from "Counselor"
        assert row["provider_type_source"] == TYPE_INFERRED
        assert row["site_confidence"] == NOT_ASSESSED
        assert row["primary_site"] is None  # genuinely absent stays None, not "nan"

    def test_no_column_identifies_a_person(self):
        assert not {"name", "npi", "profile_url", "phone"} & set(ROW_COLUMNS)

    def test_state_codes_are_normalised(self, db: Database, tmp_path: Path):
        """Sources contain typos such as 'Fl' / 'fL' / 'Co' that would split a state."""
        messy = [dataclasses.replace(_full(), state=" fL "), _sparse()]
        path = _export(db, tmp_path / "messy.xlsx", messy)
        assert sorted(load_export("x", path)["state"]) == ["FL", "RI"]

    def test_missing_expected_column_fails_loudly(self, tmp_path):
        bad = tmp_path / "bad.xlsx"
        pd.DataFrame({"Provider Name": ["x"]}).to_excel(bad, sheet_name="Providers", index=False)
        with pytest.raises(ValueError):
            load_export("bad", bad)

    def test_unfamiliar_values_are_bucketed_so_counts_still_sum(self, db, tmp_path):
        odd = dataclasses.replace(_full(), provider_type=ProviderType.CLINIC)
        df = load_export("x", _export(db, tmp_path / "odd.xlsx", [odd]))
        assert df["provider_type"].iloc[0] == TYPE_OTHER
        assert set(df["provider_type"]) <= set(TYPE_COLUMNS)
        assert set(df["site_confidence"]) <= set(CONFIDENCE_COLUMNS)


class TestLoadDatabase:
    def test_keeps_only_finished_states_and_active_rows(self, db_copy):
        df = load_database("Cigna", db_copy, {"fl", "AZ"})  # RI unfinished, AZ row inactive
        assert df["state"].tolist() == ["FL"]
        assert list(df.columns) == ["dataset", *EXPORT_COLUMNS.values(), "provider_type_source"]

    def test_reads_the_same_values_as_the_export(self, db_copy, tmp_path):
        copy = Database(db_copy)
        try:
            export_providers(copy, tmp_path / "all.xlsx")
        finally:
            copy.close()
        from_xlsx = load_export("x", tmp_path / "all.xlsx")
        from_xlsx = from_xlsx[from_xlsx["state"] == "FL"].reset_index(drop=True)
        from_db = load_database("x", db_copy, {"FL"})
        pd.testing.assert_frame_equal(from_db, from_xlsx)

    def test_opens_read_only(self, db_copy):
        before = db_copy.read_bytes()
        load_database("x", db_copy, {"FL"})
        assert db_copy.read_bytes() == before


class TestStateCodes:
    def test_normalises_and_dedupes(self):
        assert state_codes([" ri", "AZ", "RI"]) == ["AZ", "RI"]

    @pytest.mark.parametrize("bad", ["Rhode Island", "R1", "", "state_RI"])
    def test_rejects_anything_else_by_name(self, bad):
        with pytest.raises(ValueError, match="not two-letter state codes"):
            state_codes(["AZ", bad])


class TestInferProviderType:
    @pytest.mark.parametrize(
        ("specialty", "expected"),
        [
            ("Family Nurse Practitioner", TYPE_APP),
            ("Pediatric Nurse Practitioner", TYPE_APP),
            ("Physician Assistant", TYPE_APP),  # contains "physician" but is not a physician
            ("Certified Registered Nurse Anesthetist", TYPE_APP),
            ("Certified Nurse Midwife", TYPE_APP),
            ("Internal Medicine Physician", TYPE_PHYSICIAN),
            ("Orthopaedic Surgery Physician", TYPE_PHYSICIAN),
            ("Counselor", TYPE_OTHER),
            ("Optometrist", TYPE_OTHER),
            ("Midwife", TYPE_OTHER),  # a lay/professional midwife, not an APRN
            ("Surgical Assistant", TYPE_OTHER),
            ("Registered Nurse", TYPE_OTHER),
            (None, None),
            ("", None),
        ],
    )
    def test_rules(self, specialty, expected):
        assert infer_provider_type(specialty) == expected

    def test_matches_the_pipelines_own_provider_type_values(self):
        assert ProviderType.PHYSICIAN.value == TYPE_PHYSICIAN
        assert ProviderType.ADVANCED_PRACTICE_PROVIDER.value == TYPE_APP

    def test_source_types_are_kept_and_only_gaps_are_inferred(self, exports):
        df = load_export("AdventHealth", exports["AdventHealth"]).set_index("city")
        menezes = df.loc["Winter Park"]
        assert menezes["provider_type"] == TYPE_PHYSICIAN
        assert menezes["provider_type_source"] == TYPE_FROM_SOURCE  # never overwritten
        no_specialty = df.loc["Providence"]  # no type and no specialty to infer from
        assert no_specialty["provider_type"] == NOT_ASSESSED
        assert no_specialty["provider_type_source"] == NOT_ASSESSED


def _rows(count: int = 1, **overrides: object) -> pd.DataFrame:
    """Provider rows already geocoded, for testing the summaries without a geocoder."""
    base: dict[str, object] = {
        "dataset": "Cigna",
        "specialty": "Family Medicine",
        "provider_type": TYPE_PHYSICIAN,
        "primary_site": "Mercy Clinic",
        "address": "1 Main St",
        "city": "Phoenix",
        "state": "AZ",
        "zip": "85001",
        "site_confidence": "high",
        "provider_type_source": TYPE_FROM_SOURCE,
        "lat": 33.45,
        "lon": -112.07,
        "geocode_precision": "address",
    } | overrides
    return pd.DataFrame([base] * count)[ROW_COLUMNS]


class TestAggregateLocations:
    def test_counts_providers_by_type_and_confidence_at_each_point(self):
        rows = pd.concat(
            [
                _rows(3),
                _rows(2, provider_type=TYPE_APP, site_confidence="low"),
                _rows(1, provider_type=NOT_ASSESSED, site_confidence=NOT_ASSESSED),
                _rows(4, lat=33.5),
            ],
            ignore_index=True,
        )
        out = aggregate_locations(rows).sort_values("providers", ascending=False)
        first = out.iloc[0]
        assert (first["providers"], first["n_physician"], first["n_app"]) == (6, 3, 2)
        assert (first["n_high"], first["n_low"], first["n_confidence_unknown"]) == (3, 2, 1)
        assert first["n_type_unknown"] == 1
        assert len(out) == 2

    def test_type_and_confidence_counts_always_sum_to_providers(self):
        out = aggregate_locations(pd.concat([_rows(3), _rows(2, lat=1.0)], ignore_index=True))
        assert (out[list(TYPE_COLUMNS.values())].sum(axis=1) == out["providers"]).all()
        assert (out[list(CONFIDENCE_COLUMNS.values())].sum(axis=1) == out["providers"]).all()

    def test_big_sites_are_named_and_small_ones_are_not(self):
        rows = pd.concat(
            [_rows(MIN_NAMED_PROVIDERS), _rows(MIN_NAMED_PROVIDERS - 1, lat=1.0)],
            ignore_index=True,
        )
        out = aggregate_locations(rows).set_index("providers")
        assert out.loc[MIN_NAMED_PROVIDERS, "site"] == "Mercy Clinic"
        assert out.loc[MIN_NAMED_PROVIDERS, "address"] == "1 Main St"
        assert out.loc[MIN_NAMED_PROVIDERS - 1, ["site", "address"]].isna().all()

    def test_approximate_points_are_never_named_even_when_big(self):
        out = aggregate_locations(_rows(50, geocode_precision="zip"))
        assert out[["site", "address"]].isna().all().all()
        assert out["precision"].tolist() == ["zip"]

    def test_unplaced_providers_are_left_out(self):
        rows = pd.concat([_rows(2), _rows(3, lat=None, lon=None, geocode_precision="none")])
        assert aggregate_locations(rows)["providers"].tolist() == [2]

    def test_top_specialties_come_with_counts_most_common_first(self):
        rows = pd.concat(
            [
                _rows(3, specialty="Cardiology"),
                _rows(1, specialty="Dermatology"),
                _rows(2, specialty="Family Medicine"),
                _rows(1, specialty="Allergy"),
            ],
            ignore_index=True,
        )
        assert aggregate_locations(rows)["specialties"].iloc[0] == (
            "Cardiology (3), Family Medicine (2), Allergy (1)"
        )

    def test_nearby_coordinates_merge_at_one_metre(self):
        rows = pd.concat([_rows(1, lat=33.4500001), _rows(1, lat=33.4500002)])
        assert len(aggregate_locations(rows)) == 1


class TestSummarizeByState:
    def test_counts_every_provider_including_unplaced_ones(self):
        rows = pd.concat(
            [
                _rows(2),
                _rows(1, lat=None, lon=None, geocode_precision="none"),
                _rows(1, state="RI"),
            ],
            ignore_index=True,
        )
        out = summarize_by_state(rows).set_index("state")
        assert out["providers"].to_dict() == {"AZ": 3, "RI": 1}
        assert out.loc["AZ", ["n_address", "n_none"]].tolist() == [2, 1]
        assert (out[list(PRECISION_COLUMNS.values())].sum(axis=1) == out["providers"]).all()

    def test_counts_inferred_types(self):
        rows = pd.concat(
            [_rows(2, provider_type_source=TYPE_INFERRED), _rows(1)], ignore_index=True
        )
        assert summarize_by_state(rows)["n_type_inferred"].tolist() == [2]


class TestBuildSnapshot:
    def test_combines_datasets_and_reports_what_each_covers(self, exports):
        snap = build_snapshot(exports, fake_geocoder)
        assert snap.info["total_providers"] == 3
        assert snap.info["datasets"]["AdventHealth"] == {
            "providers": 2,
            "states_done": ["FL", "RI"],
        }
        assert snap.info["datasets"]["Rhode Island"] == {"providers": 1, "states_done": ["RI"]}
        assert snap.by_state["providers"].sum() == 3
        assert snap.locations["providers"].sum() == 3
        assert snap.by_specialty["providers"].sum() == 3

    def test_states_report_how_many_map_points_they_have(self, exports):
        snap = build_snapshot(exports, fake_geocoder)
        per_state = snap.locations.groupby(["dataset", "state"]).size()
        for row in snap.by_state.itertuples():
            assert row.map_points == per_state.get((row.dataset, row.state), 0)

    def test_places_providers_by_precision(self, exports):
        locations = build_snapshot(exports, fake_geocoder).locations
        assert set(locations["precision"]) == {"address", "zip", "city"}
        zip_row = locations[locations["precision"] == "zip"].iloc[0]
        assert (zip_row["lat"], zip_row["lon"]) == (41.85, -71.43)

    def test_missing_specialty_is_labelled_not_dropped(self, exports):
        by_specialty = build_snapshot(exports, fake_geocoder).by_specialty
        assert UNKNOWN in set(by_specialty["specialty"])

    def test_row_with_nothing_to_place_it_by_stays_off_the_map_but_in_the_counts(
        self, db, tmp_path
    ):
        nowhere = dataclasses.replace(_no_zip(), city=None)
        path = _export(db, tmp_path / "nowhere.xlsx", [nowhere])
        snap = build_snapshot({"x": path}, fake_geocoder)
        assert snap.locations.empty or snap.locations["providers"].sum() == 0
        assert snap.by_state["n_none"].tolist() == [1]

    def test_geocoder_receives_every_address_once_across_datasets(self, exports):
        calls: list[list[Address]] = []

        def spy(addresses: list[Address]) -> dict[str, GeoResult]:
            calls.append(addresses)
            return {}

        build_snapshot(exports, spy)
        assert len(calls) == 1
        assert len(calls[0]) == 3

    def test_a_database_is_cut_down_to_its_finished_states(self, db_copy):
        snap = build_snapshot({"Cigna": db_copy}, fake_geocoder, done_states=["FL", "AZ"])
        assert snap.by_state["state"].tolist() == ["FL"]
        # AZ is finished but has no active providers: still reported as covered.
        assert snap.info["datasets"]["Cigna"]["states_done"] == ["AZ", "FL"]

    def test_a_database_without_a_finished_list_is_refused(self, db_copy):
        with pytest.raises(ValueError, match="finished states"):
            build_snapshot({"Cigna": db_copy}, fake_geocoder)

    def test_nothing_finished_is_an_error_not_an_empty_snapshot(self, db_copy):
        with pytest.raises(ValueError, match="No provider rows"):
            build_snapshot({"Cigna": db_copy}, fake_geocoder, done_states=["CA"])


class TestWriteSnapshot:
    def test_writes_four_files_that_read_back(self, exports, tmp_path):
        snap = build_snapshot(exports, fake_geocoder)
        out = tmp_path / "data"
        write_snapshot(snap, out)
        assert sorted(p.name for p in out.iterdir()) == sorted(
            [LOCATIONS_NAME, BY_STATE_NAME, BY_SPECIALTY_NAME, INFO_NAME]
        )
        assert len(pd.read_parquet(out / LOCATIONS_NAME)) == len(snap.locations)
        assert pd.read_csv(out / BY_STATE_NAME)["providers"].sum() == 3
        assert json.loads((out / INFO_NAME).read_text())["total_providers"] == 3

    def test_no_file_contains_anything_that_identifies_a_person(self, exports, tmp_path):
        out = tmp_path / "data"
        write_snapshot(build_snapshot(exports, fake_geocoder), out)
        text = "".join(
            (out / name).read_text(encoding="utf-8")
            for name in (BY_STATE_NAME, BY_SPECIALTY_NAME, INFO_NAME)
        )
        locations = pd.read_parquet(out / LOCATIONS_NAME)
        text += locations.astype(str).to_csv() + " ".join(locations.columns)
        for needle in PERSONAL:
            assert needle not in text


class TestCliInputValidation:
    def _args(self, **overrides: object) -> Namespace:
        return Namespace(input=["A=x.xlsx"], done_states=[], done_dir=None, out="unused") | overrides  # type: ignore[operator]

    @pytest.mark.parametrize(
        "specs",
        [["no-equals-sign"], ["=x.xlsx"], ["Label="], ["A=x.xlsx", "A=y.xlsx"]],
    )
    def test_bad_input_exits_before_any_network_call(self, config: Config, specs: list[str]):
        with pytest.raises(SystemExit) as exc:
            cmd_build_map_data(
                config, Namespace(input=specs, done_states=[], done_dir=None, out="u")
            )
        assert exc.value.code == 2

    def test_bad_state_code_exits(self, config: Config):
        args = Namespace(input=["A=x.xlsx"], done_states=["AZ,Arizona"], done_dir=None, out="u")
        with pytest.raises(SystemExit) as exc:
            cmd_build_map_data(config, args)
        assert exc.value.code == 2

    def test_database_without_finished_states_exits(self, config: Config, db_copy: Path):
        args = Namespace(input=[f"A={db_copy}"], done_states=[], done_dir=None, out="u")
        with pytest.raises(SystemExit) as exc:
            cmd_build_map_data(config, args)
        assert exc.value.code == 2

    def test_done_dir_markers_are_read_by_name(self, config: Config, db_copy: Path, tmp_path: Path):
        markers = tmp_path / "done"
        markers.mkdir()
        (markers / "state_FL.done").touch()  # not a bare two-letter name
        args = Namespace(input=[f"A={db_copy}"], done_states=[], done_dir=str(markers), out="u")
        with pytest.raises(SystemExit) as exc:
            cmd_build_map_data(config, args)
        assert exc.value.code == 2


# Shaped exactly like the real files: UTF-8 BOM, extra columns, mixed case, blanks.
TRAUMA_CSV = (
    "\ufeffHospital Name,Address,City,State,State Abbrev,ZIP,County,Phone,Trauma Level,ACS Verified\n"
    "Huntsville Hospital,101 SIVLEY RD,Huntsville,Alabama,al,35801,MADISON,(256) 265-1000,i,1\n"
    "Children's of Alabama,1600 SEVENTH AVENUE SOUTH,Birmingham,Alabama,AL,35233,JEFFERSON,,,0\n"
    "Providence Hospital,,Mobile,Alabama,AL,,,,II,0\n"
)
NICU_CSV = (
    "\ufeffHospital Name,Address,City,State,State Abbrev,ZIP,NICU Level,NICU Beds\n"
    "Huntsville Hospital,101 Sivley Rd SW,Huntsville,Alabama,AL,35801,III,55\n"
    "Mystery Hospital,,Nowhereville,Alabama,AL,,II,n/a\n"
)


@pytest.fixture()
def hospital_csvs(tmp_path: Path) -> tuple[Path, Path]:
    trauma, nicu = tmp_path / "trauma.csv", tmp_path / "nicu.csv"
    trauma.write_text(TRAUMA_CSV, encoding="utf-8")
    nicu.write_text(NICU_CSV, encoding="utf-8")
    return trauma, nicu


class TestHospitalSnapshot:
    def test_combines_both_files_with_one_row_per_hospital_per_kind(self, hospital_csvs):
        df = build_hospital_snapshot(*hospital_csvs, fake_geocoder)
        assert list(df.columns) == HOSPITAL_COLUMNS
        assert df["kind"].value_counts().to_dict() == {"trauma": 3, "nicu": 2}
        # "Huntsville Hospital" is in both files and stays one row in each layer.
        assert (df["name"] == "Huntsville Hospital").sum() == 2

    def test_trauma_rows_are_normalised(self, hospital_csvs):
        df = build_hospital_snapshot(*hospital_csvs, fake_geocoder)
        trauma = df[df["kind"] == "trauma"].set_index("name")
        assert trauma.loc["Huntsville Hospital", "state"] == "AL"  # was lower-case
        assert trauma.loc["Huntsville Hospital", "level"] == "I"
        assert trauma.loc["Huntsville Hospital", "acs_verified"] == "Yes"
        assert trauma.loc["Children's of Alabama", "level"] == NOT_STATED  # blank level
        assert trauma.loc["Children's of Alabama", "acs_verified"] == "No"
        assert pd.isna(trauma["nicu_beds"]).all()

    def test_nicu_rows_carry_level_and_bed_count(self, hospital_csvs):
        df = build_hospital_snapshot(*hospital_csvs, fake_geocoder)
        nicu = df[df["kind"] == "nicu"].set_index("name")
        assert nicu.loc["Huntsville Hospital", "level"] == "III"
        assert nicu.loc["Huntsville Hospital", "nicu_beds"] == 55
        assert pd.isna(nicu.loc["Mystery Hospital", "nicu_beds"])  # "n/a" is not a number
        assert nicu["acs_verified"].isna().all()

    def test_missing_address_falls_back_to_city_and_unknown_city_stays_unplaced(
        self, hospital_csvs
    ):
        df = build_hospital_snapshot(*hospital_csvs, fake_geocoder)
        by_name = df.set_index("name")
        assert by_name.loc["Providence Hospital", "geocode_precision"] == "city"  # Mobile, AL
        assert pd.notna(by_name.loc["Providence Hospital", "lat"])
        assert by_name.loc["Mystery Hospital", "geocode_precision"] == "none"
        assert pd.isna(by_name.loc["Mystery Hospital", "lat"])

    def test_snapshot_round_trips_through_parquet(self, hospital_csvs, tmp_path):
        df = build_hospital_snapshot(*hospital_csvs, fake_geocoder)
        write_hospital_snapshot(df, tmp_path / "out")
        reread = pd.read_parquet(tmp_path / "out" / HOSPITAL_PARQUET_NAME)
        assert list(reread.columns) == HOSPITAL_COLUMNS
        assert len(reread) == len(df)
        assert reread["nicu_beds"].dropna().tolist() == [55]

    def test_missing_expected_column_fails_loudly(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("Hospital Name,City\nX,Y\n", encoding="utf-8")
        with pytest.raises(ValueError):
            build_hospital_snapshot(bad, bad, fake_geocoder)
