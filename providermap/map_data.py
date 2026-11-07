"""Build the summary files the map dashboard reads - counts only, never provider rows.

The dashboard never touches the SQLite database or the pipeline. It reads small
summaries that this module produces from one or more provider sources, each
tagged with a ``dataset`` label so several runs can sit side by side:

* ``locations.parquet`` - one row per map point (a place where providers
  practice) with how many providers are there, split by type and by site
  confidence. A site name and street address are kept only for points with at
  least :data:`MIN_NAMED_PROVIDERS` providers (a clinic or hospital, not one
  person's office).
* ``by_state.csv`` and ``by_specialty.csv`` - provider counts per state, and per
  state and specialty.
* ``build_info.json`` - when it was built and which states each dataset covers.

Names, NPIs, phone numbers and profile links are not read for these files, so they
cannot end up in a published snapshot. Only when ``people=True`` (``--private-out``) are
names, credentials and NPIs also read, into a separate :data:`PRIVATE_NAME` file that is
meant for a *private* store and never for this repository (see :func:`write_people`).
A source is either a ``providers.xlsx`` export or a
*copy* of a pipeline database (:func:`load_database`; never point it at a
database a run is still writing to).

Fields a run did not collect are filled with explicit labels
(:data:`NOT_ASSESSED`, :data:`UNKNOWN`) rather than left blank, so filters and
charts never have to special-case missing values.

Provider type is the one field filled in beyond what a run recorded: a source
that carries no credentials (Cigna's directory) leaves it blank, so it is
inferred from the specialty text by :func:`infer_provider_type`, and the
``provider_type_source`` column records which rows are the source's own and
which are inferred, so the dashboard never presents a guess as fact.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd

from .geocode import PRECISION_NONE, PRECISIONS, Address, GeoResult, address_key, fallback_location

LOCATIONS_NAME = "locations.parquet"
BY_STATE_NAME = "by_state.csv"
BY_SPECIALTY_NAME = "by_specialty.csv"
INFO_NAME = "build_info.json"
PRIVATE_NAME = "providers_private.parquet"
HOSPITAL_PARQUET_NAME = "hospitals.parquet"

EXPORT_SHEET = "Providers"
NOT_ASSESSED = "Not assessed"  # run never computed this (e.g. site confidence)
UNKNOWN = "Unknown"
DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
MIN_NAMED_PROVIDERS = 5  # below this a point carries no site name or street address
TOP_SPECIALTIES = 3
COORDINATE_DECIMALS = 5  # ~1 m; keeps the file small

# providers.xlsx column header -> row column. Only what the summaries need: no names or NPIs.
EXPORT_COLUMNS = {
    "Specialty": "specialty",
    "Provider Type": "provider_type",
    "Primary Site of Care": "primary_site",
    "Address": "address",
    "City": "city",
    "State": "state",
    "ZIP": "zip",
    "Site Confidence": "site_confidence",
}
# Extra columns, read only for the private provider list (never for the public summaries).
PEOPLE_COLUMNS = {"Provider Name": "name", "Credentials": "credentials", "NPI": "npi"}
# The same columns straight from a pipeline database (``providers`` table, active rows only).
DATABASE_QUERY = """
    SELECT specialty, provider_type, primary_site_of_care AS primary_site, address, city,
           state, zip, site_confidence{people}
    FROM providers WHERE is_active = 1
"""
DATABASE_PEOPLE = ", full_name AS name, credentials, npi"
GEO_COLUMNS = ["lat", "lon", "county_fips", "geocode_precision"]
ROW_COLUMNS = [
    "dataset",
    *EXPORT_COLUMNS.values(),
    "provider_type_source",
    "lat",
    "lon",
    "geocode_precision",
]
# What the private provider list holds per provider. lat/lon (rounded like the public map points)
# plus dataset and state are how the dashboard finds the providers at a clicked point.
PRIVATE_COLUMNS = [
    "dataset",
    "state",
    "lat",
    "lon",
    "name",
    "credentials",
    "specialty",
    "provider_type",
    "provider_type_source",
    "npi",
    "primary_site",
    "address",
    "city",
    "zip",
]

# Provider type. The first two values must match the pipeline's ProviderType enum (a test
# checks), since AdventHealth's rows arrive already carrying them.
TYPE_PHYSICIAN = "Physician"
TYPE_APP = "Advanced Practice Provider"
TYPE_OTHER = "Other clinician"  # counselors, social workers, therapists, dentists, ...
TYPE_FROM_SOURCE = "Source"
TYPE_INFERRED = "Inferred from specialty"
_APP_SPECIALTIES = (
    "nurse practitioner",
    "physician assistant",
    "nurse anesthetist",
    "nurse midwife",
    "clinical nurse specialist",
)
# Count columns in locations.parquet and by_state.csv. Any other provider type is counted as
# "Other clinician", any other confidence as "Not assessed", so the parts always sum to the total.
TYPE_COLUMNS = {
    TYPE_PHYSICIAN: "n_physician",
    TYPE_APP: "n_app",
    TYPE_OTHER: "n_other",
    NOT_ASSESSED: "n_type_unknown",
}
CONFIDENCE_COLUMNS = {
    "high": "n_high",
    "medium": "n_medium",
    "low": "n_low",
    NOT_ASSESSED: "n_confidence_unknown",
}
PRECISION_COLUMNS = {precision: f"n_{precision}" for precision in PRECISIONS}

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


class Snapshot(NamedTuple):
    """Everything the dashboard reads, held in memory until :func:`write_snapshot`."""

    locations: pd.DataFrame
    by_state: pd.DataFrame
    by_specialty: pd.DataFrame
    info: dict[str, Any]
    people: pd.DataFrame | None = None  # private list (:data:`PRIVATE_COLUMNS`); never public


def infer_provider_type(specialty: str | None) -> str | None:
    """Best guess at a provider's type from the specialty name alone (``None`` if there is none).

    Advanced-practice roles are named outright ("Family Nurse Practitioner", "Physician
    Assistant"). The NPI taxonomy names physician specialties "<specialty> Physician". Any other
    specialty (counselor, optometrist, physical therapist, ...) is a different kind of clinician.
    Checked in that order, because "Physician Assistant" contains the word "physician". A
    specialty name does not prove a credential, so callers keep the result labelled as inferred.
    """
    if not specialty:
        return None
    text = specialty.lower()
    if any(marker in text for marker in _APP_SPECIALTIES):
        return TYPE_APP
    return TYPE_PHYSICIAN if "physician" in text else TYPE_OTHER


def _clean_rows(label: str, df: pd.DataFrame, people: bool = False) -> pd.DataFrame:
    """Normalise one source's rows into :data:`ROW_COLUMNS` (minus coordinates).

    With ``people`` the name, credentials and NPI columns are kept too."""
    extra = list(PEOPLE_COLUMNS.values()) if people else []
    df = df[[*EXPORT_COLUMNS.values(), *extra]].astype(object)
    df = df.where(df.notna(), None)
    df["state"] = df["state"].str.strip().str.upper().fillna(UNKNOWN)  # typos like "Fl", "fL"
    inferred = df["specialty"].map(infer_provider_type)
    df["provider_type_source"] = [
        TYPE_FROM_SOURCE if given is not None else TYPE_INFERRED if guess else NOT_ASSESSED
        for given, guess in zip(df["provider_type"], inferred, strict=True)
    ]
    types = df["provider_type"].fillna(inferred).fillna(NOT_ASSESSED)
    df["provider_type"] = types.where(types.isin(TYPE_COLUMNS), TYPE_OTHER)
    confidence = df["site_confidence"].fillna(NOT_ASSESSED)
    df["site_confidence"] = confidence.where(confidence.isin(CONFIDENCE_COLUMNS), NOT_ASSESSED)
    df["specialty"] = df["specialty"].fillna(UNKNOWN)
    df["city"] = df["city"].str.strip().str.title()
    for column in extra:
        text = df[column].str.strip()
        df[column] = text.where(text.notna() & (text != ""), None)
    df.insert(0, "dataset", label)
    return df


def load_export(label: str, path: Path, people: bool = False) -> pd.DataFrame:
    """Read one ``providers.xlsx`` into rows (without coordinates).

    Raises ``ValueError`` if the workbook is missing an expected column - a
    renamed export header should fail here, not silently blank a chart.
    """
    headers = [*EXPORT_COLUMNS, *(PEOPLE_COLUMNS if people else {})]
    df = pd.read_excel(path, sheet_name=EXPORT_SHEET, usecols=headers, dtype=str)
    return _clean_rows(label, df.rename(columns={**EXPORT_COLUMNS, **PEOPLE_COLUMNS}), people)


def load_database(
    label: str, path: Path, done_states: Collection[str], people: bool = False
) -> pd.DataFrame:
    """Read the active providers of a pipeline database *copy* into rows (no coordinates).

    Only states in ``done_states`` are kept: a state whose run is unfinished holds part of its
    providers, and a partial count would read as a real one. The file is opened read-only.
    """
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            DATABASE_QUERY.format(people=DATABASE_PEOPLE if people else ""), conn
        )
    finally:
        conn.close()
    rows = _clean_rows(label, df, people)
    return rows[rows["state"].isin({state.upper() for state in done_states})].reset_index(drop=True)


def state_codes(names: Collection[str]) -> list[str]:
    """Upper-case two-letter state codes from ``names``; ``ValueError`` names any that aren't."""
    codes = [name.strip().upper() for name in names]
    bad = [
        name
        for name, code in zip(names, codes, strict=True)
        if not (len(code) == 2 and code.isalpha())
    ]
    if bad:
        raise ValueError(f"not two-letter state codes: {', '.join(sorted(bad))}")
    return sorted(set(codes))


def load_source(
    label: str, path: Path, done_states: Collection[str] | None, people: bool = False
) -> pd.DataFrame:
    """Rows from ``path``: a database copy (needs ``done_states``) or a ``providers.xlsx``."""
    if path.suffix.lower() in DATABASE_SUFFIXES:
        if not done_states:
            raise ValueError(
                f"{label}: a database needs the list of finished states (done_states), because "
                "an unfinished state would show a partial count as if it were complete."
            )
        return load_database(label, path, done_states, people)
    return load_export(label, path, people)


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


def _counts(
    df: pd.DataFrame, keys: list[str], column: str, names: Mapping[str, str]
) -> pd.DataFrame:
    """Rows per ``keys`` group split by the values of ``column``, one named column per value."""
    wide = df.groupby([*keys, column]).size().unstack(fill_value=0)
    return wide.reindex(columns=list(names), fill_value=0).rename(columns=names)


def _top(df: pd.DataFrame, keys: list[str], column: str, n: int, with_counts: bool) -> pd.Series:
    """The ``n`` most common values of ``column`` per ``keys`` group, joined as one text value."""
    counts = df.dropna(subset=[column]).groupby([*keys, column]).size().rename("n").reset_index()
    counts = counts.sort_values(["n", column], ascending=[False, True]).groupby(keys).head(n)
    label = counts[column] + (" (" + counts["n"].astype(str) + ")" if with_counts else "")
    return label.groupby([counts[key] for key in keys]).agg(", ".join)


def aggregate_locations(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per map point: provider counts by type and confidence, plus a place label.

    Unplaced providers (precision ``none``) are not here; they are still in the state and
    specialty counts. The site name and street address are blanked for points below
    :data:`MIN_NAMED_PROVIDERS` providers and for approximate (ZIP or city centre) points.
    """
    placed = rows[rows["geocode_precision"] != PRECISION_NONE].copy()
    placed["lat"] = placed["lat"].round(COORDINATE_DECIMALS)
    placed["lon"] = placed["lon"].round(COORDINATE_DECIMALS)
    keys = ["dataset", "state", "lat", "lon", "geocode_precision"]
    out = placed.groupby(keys).size().rename("providers").to_frame()
    out = out.join(_counts(placed, keys, "provider_type", TYPE_COLUMNS))
    out = out.join(_counts(placed, keys, "site_confidence", CONFIDENCE_COLUMNS))
    out["city"] = _top(placed, keys, "city", 1, with_counts=False)
    out["specialties"] = _top(placed, keys, "specialty", TOP_SPECIALTIES, with_counts=True)
    out["site"] = _top(placed, keys, "primary_site", 1, with_counts=False)
    out["address"] = _top(placed, keys, "address", 1, with_counts=False)
    out = out.reset_index()
    named = (out["providers"] >= MIN_NAMED_PROVIDERS) & (out["geocode_precision"] == "address")
    out["site"] = out["site"].where(named, None)
    out["address"] = out["address"].where(named, None)
    return out.rename(columns={"geocode_precision": "precision"})


def summarize_by_state(rows: pd.DataFrame) -> pd.DataFrame:
    """Providers per dataset and state, split by type, confidence and map precision."""
    keys = ["dataset", "state"]
    out = rows.groupby(keys).size().rename("providers").to_frame()
    out = out.join(_counts(rows, keys, "provider_type", TYPE_COLUMNS))
    out = out.join(_counts(rows, keys, "site_confidence", CONFIDENCE_COLUMNS))
    out = out.join(_counts(rows, keys, "geocode_precision", PRECISION_COLUMNS))
    inferred = rows[rows["provider_type_source"] == TYPE_INFERRED]
    out["n_type_inferred"] = inferred.groupby(keys).size().reindex(out.index, fill_value=0)
    return out.reset_index()


def summarize_by_specialty(rows: pd.DataFrame) -> pd.DataFrame:
    """Providers per dataset, state and specialty."""
    keys = ["dataset", "state", "specialty"]
    return rows.groupby(keys).size().rename("providers").reset_index()


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
    out["lat"] = pd.Series([r.lat for r in results], index=df.index, dtype="float64")
    out["lon"] = pd.Series([r.lon for r in results], index=df.index, dtype="float64")
    out["county_fips"] = [r.county_fips for r in results]
    out["geocode_precision"] = [r.precision for r in results]
    return out


def build_snapshot(
    inputs: Mapping[str, Path],
    geocoder: Geocoder,
    done_states: Collection[str] | None = None,
    people: bool = False,
) -> Snapshot:
    """Load every ``{label: source}``, geocode them together, return the summaries.

    A database source is cut down to ``done_states`` (see :func:`load_database`); a
    ``providers.xlsx`` is taken whole and covers the states that appear in it. With ``people``
    the result also carries the private provider list (:func:`private_list`).
    """
    frames = [load_source(label, path, done_states, people) for label, path in inputs.items()]
    combined = pd.concat(frames, ignore_index=True)
    if combined.empty:
        raise ValueError(
            "No provider rows to summarize (is every state in the database unfinished?)."
        )
    located = add_coordinates(combined, geocoder)
    rows = located[ROW_COLUMNS]
    covered = {
        label: (
            sorted({s.upper() for s in done_states})
            if path.suffix.lower() in DATABASE_SUFFIXES and done_states
            else sorted(set(frame["state"]) - {UNKNOWN})
        )
        for (label, path), frame in zip(inputs.items(), frames, strict=True)
    }
    snapshot = summarize(rows, covered)
    return snapshot._replace(people=private_list(located)) if people else snapshot


def private_list(located: pd.DataFrame) -> pd.DataFrame:
    """The providers that have a map point, as :data:`PRIVATE_COLUMNS`, sorted by name.

    Coordinates are rounded exactly as in :func:`aggregate_locations`, so a clicked map point
    finds its providers by ``(dataset, state, lat, lon)``."""
    placed = located[located["geocode_precision"] != PRECISION_NONE].copy()
    placed["lat"] = placed["lat"].round(COORDINATE_DECIMALS)
    placed["lon"] = placed["lon"].round(COORDINATE_DECIMALS)
    return placed[PRIVATE_COLUMNS].sort_values(["name"], ignore_index=True)


def summarize(rows: pd.DataFrame, states_done: Mapping[str, list[str]]) -> Snapshot:
    """The dashboard's files from geocoded provider rows (:data:`ROW_COLUMNS`)."""
    locations = aggregate_locations(rows)
    by_state = summarize_by_state(rows)
    points = locations.groupby(["dataset", "state"]).size().rename("map_points")
    by_state = by_state.join(points, on=["dataset", "state"])
    by_state["map_points"] = by_state["map_points"].fillna(0).astype(int)
    return Snapshot(
        locations=locations,
        by_state=by_state,
        by_specialty=summarize_by_specialty(rows),
        info=build_info(rows, states_done),
    )


def build_hospital_snapshot(trauma_csv: Path, nicu_csv: Path, geocoder: Geocoder) -> pd.DataFrame:
    """Load the trauma-centre and NICU CSVs, geocode them together, return the snapshot."""
    combined = pd.concat(
        [load_trauma_centers(trauma_csv), load_nicu_centers(nicu_csv)], ignore_index=True
    )
    return add_coordinates(combined, geocoder)[HOSPITAL_COLUMNS]


def build_info(rows: pd.DataFrame, states_done: Mapping[str, list[str]]) -> dict[str, Any]:
    """Provenance for the dashboard: when it was built and what each dataset covers."""
    totals = rows.groupby("dataset", sort=False).size()
    return {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_providers": len(rows),
        "datasets": {
            str(label): {"providers": int(totals[label]), "states_done": states_done[str(label)]}
            for label in totals.index
        },
    }


def write_hospital_snapshot(df: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_dir / HOSPITAL_PARQUET_NAME, index=False)


def write_snapshot(snapshot: Snapshot, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot.locations.to_parquet(out_dir / LOCATIONS_NAME, index=False)
    snapshot.by_state.to_csv(out_dir / BY_STATE_NAME, index=False)
    snapshot.by_specialty.to_csv(out_dir / BY_SPECIALTY_NAME, index=False)
    (out_dir / INFO_NAME).write_text(json.dumps(snapshot.info, indent=2) + "\n", encoding="utf-8")


def write_people(people: pd.DataFrame, out_dir: Path) -> Path:
    """Write the private provider list to ``out_dir``. This file holds names and NPIs: it is for
    a private store, and must never be committed to a public repository."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / PRIVATE_NAME
    people.to_parquet(path, index=False)
    return path
