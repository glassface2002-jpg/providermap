"""Build the geocoded provider snapshot that the map dashboard reads.

The dashboard never touches the SQLite database or the pipeline: it reads one
Parquet file, :data:`PARQUET_NAME`, plus a small JSON file describing how it
was built. This module produces both from one or more ``providers.xlsx``
exports (the files ``providermap export`` writes), so the dashboard can show
data from several runs - each tagged with a ``dataset`` label - side by side.

:data:`SNAPSHOT_COLUMNS` is the contract between this module and the
dashboard. Fields a run did not collect are filled with explicit labels
(:data:`NOT_ASSESSED`, :data:`UNKNOWN`) rather than left blank, so filters
and charts never have to special-case missing values.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .geocode import PRECISIONS, Address, GeoResult, address_key, fallback_location

PARQUET_NAME = "providers.parquet"
INFO_NAME = "build_info.json"
HOSPITAL_PARQUET_NAME = "hospitals.parquet"

EXPORT_SHEET = "Providers"
NOT_ASSESSED = "Not assessed"  # run never computed this (e.g. site confidence)
UNKNOWN = "Unknown"

# providers.xlsx column header -> snapshot column. Order is the snapshot order.
EXPORT_COLUMNS = {
    "Provider Name": "name",
    "Credentials": "credentials",
    "Specialty": "specialty",
    "Provider Type": "provider_type",
    "Primary Site of Care": "primary_site",
    "Address": "address",
    "City": "city",
    "State": "state",
    "ZIP": "zip",
    "NPI": "npi",
    "Profile URL": "profile_url",
    "Site Confidence": "site_confidence",
    "Accepting New Patients": "accepting_new_patients",
}
GEO_COLUMNS = ["lat", "lon", "county_fips", "geocode_precision"]
SNAPSHOT_COLUMNS = ["dataset", *EXPORT_COLUMNS.values(), *GEO_COLUMNS]

# Hospital layers (trauma centres and NICUs): one row per hospital per kind. A hospital that is
# both appears twice, once per kind, so each layer stands alone and no cross-file name matching
# is needed.
KIND_TRAUMA = "trauma"
KIND_NICU = "nicu"
NOT_STATED = "Not stated"  # a trauma-centre row with no level (mostly children's hospitals)
HOSPITAL_COLUMNS = [
    "kind",
    "name",
    "address",
    "city",
    "state",
    "zip",
    "level",
    "nicu_beds",
    "acs_verified",
    "lat",
    "lon",
    "geocode_precision",
]

Geocoder = Callable[[list[Address]], dict[str, GeoResult]]


def load_export(label: str, path: Path) -> pd.DataFrame:
    """Read one ``providers.xlsx`` into snapshot columns (without coordinates).

    Raises ``ValueError`` if the workbook is missing an expected column - a
    renamed export header should fail here, not silently blank a chart.
    """
    df = pd.read_excel(path, sheet_name=EXPORT_SHEET, usecols=list(EXPORT_COLUMNS), dtype=str)
    df = df.rename(columns=EXPORT_COLUMNS)[list(EXPORT_COLUMNS.values())]
    df = df.astype(object).where(df.notna(), None)
    df["state"] = df["state"].str.strip().str.upper()  # sources contain typos like "Fl", "fL"
    df["provider_type"] = df["provider_type"].fillna(NOT_ASSESSED)
    df["site_confidence"] = df["site_confidence"].fillna(NOT_ASSESSED)
    df["accepting_new_patients"] = df["accepting_new_patients"].fillna(UNKNOWN)
    df.insert(0, "dataset", label)
    return df


def _read_hospital_csv(path: Path, columns: dict[str, str]) -> pd.DataFrame:
    """Read a hospital CSV into clean, lower-case-named string columns (blanks become None)."""
    df = pd.read_csv(path, encoding="utf-8-sig", usecols=list(columns), dtype=str)
    df = df.rename(columns=columns)
    for column in df.columns:
        text = df[column].str.strip()
        df[column] = text.where(text.notna() & (text != ""), None)
    df["state"] = df["state"].str.upper()
    return df


def load_trauma_centers(path: Path) -> pd.DataFrame:
    df = _read_hospital_csv(
        path,
        {
            "Hospital Name": "name",
            "Address": "address",
            "City": "city",
            "State Abbrev": "state",
            "ZIP": "zip",
            "Trauma Level": "level",
            "ACS Verified": "acs_verified",
        },
    )
    df["kind"] = KIND_TRAUMA
    df["level"] = df["level"].str.upper().fillna(NOT_STATED)
    df["acs_verified"] = df["acs_verified"].map({"1": "Yes", "0": "No"})
    df["nicu_beds"] = pd.Series(pd.NA, index=df.index, dtype="Int64")
    return df


def load_nicu_centers(path: Path) -> pd.DataFrame:
    df = _read_hospital_csv(
        path,
        {
            "Hospital Name": "name",
            "Address": "address",
            "City": "city",
            "State Abbrev": "state",
            "ZIP": "zip",
            "NICU Level": "level",
            "NICU Beds": "nicu_beds",
        },
    )
    df["kind"] = KIND_NICU
    df["level"] = df["level"].str.upper()
    df["nicu_beds"] = pd.to_numeric(df["nicu_beds"], errors="coerce").astype("Int64")
    df["acs_verified"] = None
    return df


def add_coordinates(df: pd.DataFrame, geocoder: Geocoder) -> pd.DataFrame:
    """Return ``df`` plus :data:`GEO_COLUMNS`. Unplaceable rows are kept, marked ``none``."""
    addresses = [
        Address(*row) for row in zip(df["address"], df["city"], df["state"], df["zip"], strict=True)
    ]
    found = geocoder(addresses)
    results = []
    for addr in addresses:
        key = address_key(addr)
        match = found.get(key) if key else None
        # No street-level key (missing street or ZIP): still place it approximately if we can.
        results.append(match or fallback_location(addr))

    out = df.copy()
    out["lat"] = [r.lat for r in results]
    out["lon"] = [r.lon for r in results]
    out["county_fips"] = [r.county_fips for r in results]
    out["geocode_precision"] = [r.precision for r in results]
    return out


def build_snapshot(inputs: Mapping[str, Path], geocoder: Geocoder) -> pd.DataFrame:
    """Load every ``{label: providers.xlsx}``, geocode them together, return the snapshot."""
    frames = [load_export(label, path) for label, path in inputs.items()]
    combined = pd.concat(frames, ignore_index=True)
    return add_coordinates(combined, geocoder)[SNAPSHOT_COLUMNS]


def build_hospital_snapshot(trauma_csv: Path, nicu_csv: Path, geocoder: Geocoder) -> pd.DataFrame:
    """Load the trauma-centre and NICU CSVs, geocode them together, return the snapshot."""
    combined = pd.concat(
        [load_trauma_centers(trauma_csv), load_nicu_centers(nicu_csv)], ignore_index=True
    )
    return add_coordinates(combined, geocoder)[HOSPITAL_COLUMNS]


def build_info(df: pd.DataFrame) -> dict[str, Any]:
    """Provenance for the Data Quality tab: row counts and geocode precision per dataset."""
    datasets: dict[str, Any] = {}
    for label, group in df.groupby("dataset", sort=False):
        counts = group["geocode_precision"].value_counts()
        datasets[str(label)] = {
            "providers": len(group),
            "geocode_precision": {p: int(counts.get(p, 0)) for p in PRECISIONS},
        }
    return {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_providers": len(df),
        "datasets": datasets,
    }


def write_hospital_snapshot(df: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_dir / HOSPITAL_PARQUET_NAME, index=False)


def write_snapshot(df: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_dir / PARQUET_NAME, index=False)
    (out_dir / INFO_NAME).write_text(json.dumps(build_info(df), indent=2) + "\n", encoding="utf-8")
