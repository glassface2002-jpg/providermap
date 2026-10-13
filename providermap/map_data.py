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

from .geocode import UNRESOLVED, Address, GeoResult, address_key

PARQUET_NAME = "providers.parquet"
INFO_NAME = "build_info.json"

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

Geocoder = Callable[[list[Address]], dict[str, GeoResult]]


def load_export(label: str, path: Path) -> pd.DataFrame:
    """Read one ``providers.xlsx`` into snapshot columns (without coordinates).

    Raises ``ValueError`` if the workbook is missing an expected column - a
    renamed export header should fail here, not silently blank a chart.
    """
    df = pd.read_excel(path, sheet_name=EXPORT_SHEET, usecols=list(EXPORT_COLUMNS), dtype=str)
    df = df.rename(columns=EXPORT_COLUMNS)[list(EXPORT_COLUMNS.values())]
    df = df.astype(object).where(df.notna(), None)
    df["provider_type"] = df["provider_type"].fillna(NOT_ASSESSED)
    df["site_confidence"] = df["site_confidence"].fillna(NOT_ASSESSED)
    df["accepting_new_patients"] = df["accepting_new_patients"].fillna(UNKNOWN)
    df.insert(0, "dataset", label)
    return df


def add_coordinates(df: pd.DataFrame, geocoder: Geocoder) -> pd.DataFrame:
    """Return ``df`` plus :data:`GEO_COLUMNS`. Unmappable rows are kept, marked ``none``."""
    addresses = [
        Address(*row) for row in zip(df["address"], df["city"], df["state"], df["zip"], strict=True)
    ]
    found = geocoder(addresses)
    results = []
    for addr in addresses:
        key = address_key(addr)
        results.append(found.get(key, UNRESOLVED) if key else UNRESOLVED)

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


def build_info(df: pd.DataFrame) -> dict[str, Any]:
    """Provenance for the Data Quality tab: row counts and geocode precision per dataset."""
    datasets: dict[str, Any] = {}
    for label, group in df.groupby("dataset", sort=False):
        counts = group["geocode_precision"].value_counts()
        datasets[str(label)] = {
            "providers": len(group),
            "geocode_precision": {p: int(counts.get(p, 0)) for p in ("address", "zip", "none")},
        }
    return {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_providers": len(df),
        "datasets": datasets,
    }


def write_snapshot(df: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_dir / PARQUET_NAME, index=False)
    (out_dir / INFO_NAME).write_text(json.dumps(build_info(df), indent=2) + "\n", encoding="utf-8")
