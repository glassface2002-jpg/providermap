"""Tests for providermap.map_data - the snapshot the dashboard reads.

Workbooks are produced by the real ``export_providers`` so a renamed export
header breaks these tests instead of silently blanking a dashboard column.
"""

from __future__ import annotations

import dataclasses
import json
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
    EXPORT_COLUMNS,
    NOT_ASSESSED,
    PARQUET_NAME,
    SNAPSHOT_COLUMNS,
    UNKNOWN,
    build_info,
    build_snapshot,
    load_export,
    write_snapshot,
)
from providermap.models import Confidence, Provider, ProviderType


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
        if addr.city == "PROVIDENCE":
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


class TestLoadExport:
    def test_renames_to_snapshot_columns_and_labels_missing_values(self, exports):
        df = load_export("Rhode Island", exports["Rhode Island"])
        assert list(df.columns) == ["dataset", *EXPORT_COLUMNS.values()]
        row = df.iloc[0]
        assert row["dataset"] == "Rhode Island"
        assert row["name"] == "CATHERINE A'VANT-FOWLER"
        assert row["zip"] == "02904-5762"
        assert row["provider_type"] == NOT_ASSESSED
        assert row["site_confidence"] == NOT_ASSESSED
        assert row["accepting_new_patients"] == UNKNOWN
        assert row["profile_url"] is None  # genuinely absent stays None, not "nan"

    def test_state_codes_are_normalised(self, db: Database, tmp_path: Path):
        """Sources contain typos such as 'Fl' / 'fL' / 'Co' that would split a state in the filter."""
        messy = [dataclasses.replace(_full(), state=" fL "), _sparse()]
        path = _export(db, tmp_path / "messy.xlsx", messy)
        assert sorted(load_export("x", path)["state"]) == ["FL", "RI"]

    def test_missing_expected_column_fails_loudly(self, tmp_path):
        bad = tmp_path / "bad.xlsx"
        pd.DataFrame({"Provider Name": ["x"]}).to_excel(bad, sheet_name="Providers", index=False)
        with pytest.raises(ValueError):
            load_export("bad", bad)


class TestBuildSnapshot:
    def test_combines_datasets_and_adds_coordinates(self, exports):
        df = build_snapshot(exports, fake_geocoder)
        assert list(df.columns) == SNAPSHOT_COLUMNS
        assert df["dataset"].tolist() == ["AdventHealth", "AdventHealth", "Rhode Island"]

        by_name = df.set_index("name")
        menezes = by_name.loc["Justin Menezes, MD"]
        assert menezes["geocode_precision"] == "address"
        assert menezes["county_fips"] == "12095"
        assert menezes["site_confidence"] == "high"
        assert by_name.loc["CATHERINE A'VANT-FOWLER", "geocode_precision"] == "zip"

    def test_row_without_usable_address_is_kept_but_unmapped(self, exports):
        df = build_snapshot(exports, fake_geocoder).set_index("name")
        row = df.loc["No Zip"]  # no ZIP, so no street-level key, but its city is known
        assert row["geocode_precision"] == "city"
        assert row["lat"] == pytest.approx(41.82, abs=0.2)

    def test_row_with_nothing_to_place_it_by_stays_unmapped(self, db, tmp_path):
        nowhere = dataclasses.replace(_no_zip(), city=None, state=None)
        path = _export(db, tmp_path / "nowhere.xlsx", [nowhere])
        df = build_snapshot({"x": path}, fake_geocoder)
        assert df["geocode_precision"].tolist() == ["none"]
        assert pd.isna(df["lat"].iloc[0]) and pd.isna(df["lon"].iloc[0])

    def test_geocoder_receives_every_address_once_across_datasets(self, exports):
        calls: list[list[Address]] = []

        def spy(addresses: list[Address]) -> dict[str, GeoResult]:
            calls.append(addresses)
            return {}

        build_snapshot(exports, spy)
        assert len(calls) == 1
        assert len(calls[0]) == 3


class TestWriteSnapshot:
    def test_writes_parquet_and_build_info(self, exports, tmp_path):
        df = build_snapshot(exports, fake_geocoder)
        out = tmp_path / "data"
        write_snapshot(df, out)

        reread = pd.read_parquet(out / PARQUET_NAME)
        assert list(reread.columns) == SNAPSHOT_COLUMNS
        assert len(reread) == 3

        info = json.loads((out / "build_info.json").read_text())
        assert info["total_providers"] == 3
        assert info["datasets"]["AdventHealth"] == {
            "providers": 2,
            "geocode_precision": {"address": 1, "zip": 0, "city": 1, "none": 0},
        }
        assert info["datasets"]["Rhode Island"]["geocode_precision"]["zip"] == 1

    def test_build_info_counts_every_precision(self, exports):
        info = build_info(build_snapshot(exports, fake_geocoder))
        total = sum(sum(d["geocode_precision"].values()) for d in info["datasets"].values())
        assert total == info["total_providers"]


class TestCliInputValidation:
    @pytest.mark.parametrize(
        "specs",
        [["no-equals-sign"], ["=x.xlsx"], ["Label="], ["A=x.xlsx", "A=y.xlsx"]],
    )
    def test_bad_input_exits_before_any_network_call(self, config: Config, specs: list[str]):
        with pytest.raises(SystemExit) as exc:
            cmd_build_map_data(config, Namespace(input=specs, out="unused"))
        assert exc.value.code == 2
