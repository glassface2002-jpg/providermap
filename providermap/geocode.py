"""Street-level geocoding via the US Census batch geocoder (free, no API key).

The map dashboard needs a latitude/longitude per provider address, and nothing
in the pipeline's database or exports carries one. This module turns a set of
addresses into coordinates:

1. Addresses are deduplicated on :func:`address_key` (suite/unit text removed
   first), so the thousands of providers sharing a building cost one lookup.
2. Unseen keys are sent to the Census batch endpoint, at most
   :data:`CHUNK_SIZE` per request, and the answers are written to a CSV cache
   after every chunk - a failed run resumes where it stopped, and a rerun
   sends nothing it already knows (including known non-matches).
3. An address the Census cannot match falls back to its ZIP-code centroid,
   offline, and is labelled ``precision="zip"`` so the dashboard can say so.
   An address with no usable ZIP is ``precision="none"`` and is not mapped.

The ZIP fallback is applied on every call and never cached: it is free and
offline, and it must not mask a later Census match.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

import httpx
import zipcodes

from .parser_utils import normalize_address_key

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
BENCHMARK = "Public_AR_Current"
VINTAGE = "Current_Current"
CHUNK_SIZE = 10_000  # the Census batch endpoint's documented per-request maximum

PRECISION_ADDRESS = "address"
PRECISION_ZIP = "zip"
PRECISION_NONE = "none"

_CACHE_FIELDS = ["key", "lat", "lon", "county_fips"]

# Suite/unit text makes the Census matcher miss otherwise-valid street
# addresses, and keeps one building from collapsing to one lookup.
_UNIT_RE = re.compile(
    r"(?:^|[\s,]+)(?:suite|ste|unit|apt|bldg|building|floor|room|rm)\b\.?.*$|\s*#.*$",
    re.IGNORECASE,
)


class Address(NamedTuple):
    street: str | None
    city: str | None
    state: str | None
    zipcode: str | None


class GeoResult(NamedTuple):
    lat: float | None
    lon: float | None
    county_fips: str | None
    precision: str


_UNRESOLVED = GeoResult(None, None, None, PRECISION_NONE)


def strip_unit(street: str | None) -> str | None:
    """Drop suite/unit/floor text: ``'133 Benmore Dr Suite 200'`` -> ``'133 Benmore Dr'``."""
    if not street:
        return None
    return _UNIT_RE.sub("", street).strip() or None


def address_key(address: Address) -> str | None:
    """Stable dedupe key, or ``None`` when the address has no street or ZIP."""
    return normalize_address_key(
        strip_unit(address.street), address.city, address.state, address.zipcode
    )


def census_client(user_agent: str) -> httpx.Client:
    # Batch requests of 10K addresses take minutes on the Census side.
    return httpx.Client(timeout=300.0, headers={"User-Agent": user_agent})


def zip_centroid(zipcode: str | None) -> tuple[float, float] | None:
    """``(lat, lon)`` of a 5-digit ZIP's centre, from the offline ``zipcodes`` data."""
    zip5 = re.sub(r"\D", "", zipcode or "")[:5]
    if len(zip5) != 5:
        return None
    matches = zipcodes.matching(zip5)
    if not matches:
        return None
    try:
        return float(matches[0]["lat"]), float(matches[0]["long"])
    except (KeyError, ValueError):
        return None


def parse_batch_response(text: str, ids: dict[str, str]) -> dict[str, GeoResult]:
    """Parse Census batch CSV into ``{key: GeoResult}``.

    ``ids`` maps the row ids we sent to our address keys. Every sent key gets
    an entry: matches carry coordinates, ``No_Match`` / ``Tie`` / malformed rows
    are recorded as unresolved so they are not re-sent on the next run.
    """
    results: dict[str, GeoResult] = {key: _UNRESOLVED for key in ids.values()}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 6 or row[0] not in ids or row[2] != "Match":
            continue
        try:
            lon_s, lat_s = row[5].split(",")
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            continue
        county = row[8] + row[9] if len(row) >= 10 and row[8] and row[9] else None
        results[ids[row[0]]] = GeoResult(lat, lon, county, PRECISION_ADDRESS)
    return results


def _load_cache(path: Path) -> dict[str, GeoResult]:
    if not path.exists():
        return {}
    cache: dict[str, GeoResult] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["lat"] and row["lon"]:
                cache[row["key"]] = GeoResult(
                    float(row["lat"]),
                    float(row["lon"]),
                    row["county_fips"] or None,
                    PRECISION_ADDRESS,
                )
            else:
                cache[row["key"]] = _UNRESOLVED
    return cache


def _save_cache(path: Path, cache: dict[str, GeoResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_CACHE_FIELDS)
        for key, r in cache.items():
            writer.writerow([key, r.lat or "", r.lon or "", r.county_fips or ""])


def _census_batch(client: httpx.Client, rows: list[tuple[str, Address]]) -> dict[str, GeoResult]:
    """One Census request for ``rows`` = ``[(key, address), ...]``."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    ids: dict[str, str] = {}
    for i, (key, addr) in enumerate(rows):
        ids[str(i)] = key
        writer.writerow([i, strip_unit(addr.street), addr.city, addr.state, addr.zipcode])
    resp = client.post(
        CENSUS_BATCH_URL,
        data={"benchmark": BENCHMARK, "vintage": VINTAGE},
        files={"addressFile": ("addresses.csv", buf.getvalue().encode("utf-8"), "text/csv")},
    )
    resp.raise_for_status()
    return parse_batch_response(resp.text, ids)


def geocode_addresses(
    addresses: Iterable[Address], cache_path: Path, client: httpx.Client
) -> dict[str, GeoResult]:
    """Geocode ``addresses``; returns ``{address_key: GeoResult}``.

    Addresses without a usable key (no street or ZIP) are absent from the
    result; callers treat a missing key as unmapped. Raises ``httpx.HTTPError``
    if the Census service fails - answers from earlier chunks are already
    cached, so rerunning resumes rather than restarts.
    """
    unique: dict[str, Address] = {}
    for addr in addresses:
        key = address_key(addr)
        if key is not None:
            unique.setdefault(key, addr)

    cache = _load_cache(cache_path)
    pending = [(key, addr) for key, addr in unique.items() if key not in cache]
    for start in range(0, len(pending), CHUNK_SIZE):
        cache.update(_census_batch(client, pending[start : start + CHUNK_SIZE]))
        _save_cache(cache_path, cache)

    results: dict[str, GeoResult] = {}
    for key, addr in unique.items():
        result = cache[key]
        if result.precision == PRECISION_NONE:
            centroid = zip_centroid(addr.zipcode)
            if centroid is not None:
                result = GeoResult(centroid[0], centroid[1], None, PRECISION_ZIP)
        results[key] = result
    return results
