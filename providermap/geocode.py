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
3. An address the Census cannot match falls back, offline, to its ZIP-code
   centre (``precision="zip"``), then to its city's centre
   (``precision="city"``), so the dashboard can say how approximate it is.
   An address with neither is ``precision="none"`` and is not mapped.

The ZIP/city fallback is applied on every call and never cached: it is free
and offline, and it must not mask a later Census match.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from collections.abc import Callable, Iterable
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import httpx
import zipcodes

from .parser_utils import normalize_address_key

logger = logging.getLogger(__name__)

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
BENCHMARK = "Public_AR_Current"
VINTAGE = "Current_Current"
# The endpoint accepts up to 10,000 addresses per request, but a request that size can run past
# the client timeout when the service is slow, losing the whole batch. Smaller requests are saved
# to the cache one at a time, so a failure costs a few minutes of work instead of a lot.
CHUNK_SIZE = 2_500
ATTEMPTS = 3  # tries per request when the service times out or errors
RETRY_DELAY_SECONDS = 20.0  # waits this long times the attempt number between tries
# A request the service keeps rejecting is split in half until the address it dislikes is found.
# More than this many rejected addresses in one run means the service itself is down.
MAX_REJECTED = 20

PRECISION_ADDRESS = "address"
PRECISION_ZIP = "zip"
PRECISION_CITY = "city"
PRECISION_NONE = "none"
PRECISIONS = (PRECISION_ADDRESS, PRECISION_ZIP, PRECISION_CITY, PRECISION_NONE)

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


UNRESOLVED = GeoResult(None, None, None, PRECISION_NONE)


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


_CityKey = tuple[str, str]
_Point = tuple[float, float]


def _mean_points(sums: dict[_CityKey, list[float]]) -> dict[_CityKey, _Point]:
    return {key: (lat / n, lon / n) for key, (lat, lon, n) in sums.items()}


@lru_cache(maxsize=1)
def _city_centres() -> tuple[dict[_CityKey, _Point], dict[_CityKey, _Point]]:
    """(STATE, lowercase city) -> mean (lat, lon) of that city's ZIP centres, offline.

    Two indexes: ZIPs whose primary city is that name, and ZIPs that only list it as an
    acceptable alternative. The second is a last resort, since an alternative name can span
    several towns.
    """
    primary: dict[_CityKey, list[float]] = {}
    alternative: dict[_CityKey, list[float]] = {}
    for z in zipcodes.list_all():
        try:
            lat, lon = float(z["lat"]), float(z["long"])
        except (KeyError, ValueError):
            continue
        for sums, names in (
            (primary, [z["city"]]),
            (alternative, z.get("acceptable_cities") or []),
        ):
            for name in names:
                total = sums.setdefault((z["state"], name.lower()), [0.0, 0.0, 0.0])
                total[0] += lat
                total[1] += lon
                total[2] += 1
    return _mean_points(primary), _mean_points(alternative)


def city_centroid(city: str | None, state: str | None) -> tuple[float, float] | None:
    """``(lat, lon)`` of a city's centre (case-insensitive), from the offline ZIP data."""
    if not city or not state:
        return None
    key = (state.strip().upper(), city.strip().lower())
    primary, alternative = _city_centres()
    return primary.get(key) or alternative.get(key)


def fallback_location(address: Address) -> GeoResult:
    """Approximate position when there is no street-level match: ZIP centre, else city centre."""
    zip_point = zip_centroid(address.zipcode)
    if zip_point is not None:
        return GeoResult(zip_point[0], zip_point[1], None, PRECISION_ZIP)
    city_point = city_centroid(address.city, address.state)
    if city_point is not None:
        return GeoResult(city_point[0], city_point[1], None, PRECISION_CITY)
    return UNRESOLVED


def parse_batch_response(text: str, ids: dict[str, str]) -> dict[str, GeoResult]:
    """Parse Census batch CSV into ``{key: GeoResult}``.

    ``ids`` maps the row ids we sent to our address keys. Every sent key gets
    an entry: matches carry coordinates, ``No_Match`` / ``Tie`` / malformed rows
    are recorded as unresolved so they are not re-sent on the next run.
    """
    results: dict[str, GeoResult] = {key: UNRESOLVED for key in ids.values()}
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
                cache[row["key"]] = UNRESOLVED
    return cache


def _save_cache(path: Path, cache: dict[str, GeoResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_CACHE_FIELDS)
        for key, r in cache.items():
            writer.writerow([key, r.lat or "", r.lon or "", r.county_fips or ""])


def _one_line(text: str | None) -> str:
    """``text`` with every run of whitespace (tabs, line breaks, ...) collapsed to one space; the
    batch file has one address per line, so a stray line break would corrupt the request."""
    return " ".join((text or "").split())


def _census_batch(client: httpx.Client, rows: list[tuple[str, Address]]) -> dict[str, GeoResult]:
    """One Census request for ``rows`` = ``[(key, address), ...]``."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    ids: dict[str, str] = {}
    for i, (key, addr) in enumerate(rows):
        ids[str(i)] = key
        street, city, state, zipcode = (
            _one_line(part)
            for part in (strip_unit(addr.street), addr.city, addr.state, addr.zipcode)
        )
        writer.writerow([i, street, city, state, zipcode])
    resp = client.post(
        CENSUS_BATCH_URL,
        data={"benchmark": BENCHMARK, "vintage": VINTAGE},
        files={"addressFile": ("addresses.csv", buf.getvalue().encode("utf-8"), "text/csv")},
    )
    resp.raise_for_status()
    return parse_batch_response(resp.text, ids)


def _census_batch_with_retry(
    client: httpx.Client, rows: list[tuple[str, Address]], attempts: int = ATTEMPTS
) -> dict[str, GeoResult]:
    """:func:`_census_batch`, tried up to ``attempts`` times on a timeout or server error."""
    for attempt in range(1, attempts + 1):
        try:
            return _census_batch(client, rows)
        except httpx.HTTPStatusError as err:
            if err.response.status_code < 500 or attempt == attempts:
                raise
        except httpx.TransportError:  # includes timeouts
            if attempt == attempts:
                raise
        time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def _geocode_chunk(
    client: httpx.Client,
    rows: list[tuple[str, Address]],
    rejected: list[tuple[str, Address]],
    attempts: int = ATTEMPTS,
) -> dict[str, GeoResult]:
    """Geocode ``rows``; when the service answers with a server error, split them in half and
    try each half, down to single addresses, so one address it cannot handle does not lose the
    rest. Such addresses are appended to ``rejected`` (and re-raised once there are more than
    :data:`MAX_REJECTED`, which means the service is down). Timeouts and client errors raise."""
    try:
        return _census_batch_with_retry(client, rows, attempts)
    except httpx.HTTPStatusError as err:
        if err.response.status_code < 500:
            raise
        if len(rows) == 1:
            rejected.extend(rows)
            if len(rejected) > MAX_REJECTED:
                raise
            return {}
        middle = len(rows) // 2
        found = _geocode_chunk(client, rows[:middle], rejected, 1)
        found.update(_geocode_chunk(client, rows[middle:], rejected, 1))
        return found


def geocode_addresses(
    addresses: Iterable[Address],
    cache_path: Path,
    client: httpx.Client,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, GeoResult]:
    """Geocode ``addresses``; returns ``{address_key: GeoResult}``.

    Addresses without a usable key (no street or ZIP) are absent from the
    result; callers treat a missing key as unmapped. Raises ``httpx.HTTPError``
    if the Census service fails - answers from earlier chunks are already
    cached, so rerunning resumes rather than restarts. An address the service keeps
    rejecting is placed by ZIP or city and, not being cached, tried again next run.
    ``on_progress(done, total)`` is called after each saved chunk of the addresses that were
    not cached already.
    """
    unique: dict[str, Address] = {}
    for addr in addresses:
        key = address_key(addr)
        if key is not None:
            unique.setdefault(key, addr)

    cache = _load_cache(cache_path)
    pending = [(key, addr) for key, addr in unique.items() if key not in cache]
    rejected: list[tuple[str, Address]] = []
    for start in range(0, len(pending), CHUNK_SIZE):
        cache.update(_geocode_chunk(client, pending[start : start + CHUNK_SIZE], rejected))
        _save_cache(cache_path, cache)
        if on_progress:
            on_progress(min(start + CHUNK_SIZE, len(pending)), len(pending))

    if rejected:
        logger.warning(
            "The Census service rejected %d address(es); they are placed by ZIP or city instead "
            "and will be tried again next run.",
            len(rejected),
        )
    results: dict[str, GeoResult] = {}
    for key, addr in unique.items():
        result = cache.get(key)
        if result is not None and result.precision != PRECISION_NONE:
            results[key] = result
        else:  # no match, or rejected by the service: place by ZIP or city
            results[key] = fallback_location(addr)
    return results
