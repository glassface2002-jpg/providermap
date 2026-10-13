"""Bulk hospital website lookup via OpenStreetMap (ROADMAP.md Phase 6.1 -
measured worth building in Phase 6.0's live validation before this was
written, per this project's "measure before build" discipline: a 75-org
random sample of the missing-website residual found 19 OSM matches with a
``website`` tag, 10 of which survived the existing reachability + distinctive-
token page-content gates with **zero false positives**, projecting to
roughly 257 organizations across the full residual - the largest single free
gain found since HIFLD, and by construction 100% additive since every
sampled organization was, by definition, already missed by Wikidata, HIFLD,
and VA).

Unlike Wikidata/HIFLD/VA, this module never calls a live API during
``enrich-organizations``. Phase 6.0's own validation run hit **504/429
errors from the public Overpass API on two different instances** (the
primary ``overpass-api.de`` and the ``overpass.kumi.systems`` mirror) - not
a fluke, a measured reliability problem with depending on that service
synchronously inside a production run. Instead:

* :func:`refresh_osm_cache` (heavy, explicit, run periodically via
  ``providermap refresh-osm-cache`` - never invoked automatically) downloads
  a bulk Geofabrik ``.osm.pbf`` extract (the entire US, ~12GB, confirmed via
  Geofabrik's own ``Content-Length``) and streams through it with `pyosmium
  <https://osmcode.org/pyosmium/>`_ to pull out only hospital-tagged
  features, writing a small local JSON cache. The download
  (:func:`download_pbf`) is resumable - Geofabrik's server confirmed
  ``Accept-Ranges: bytes`` support, so an interrupted run picks up where it
  left off on the next call rather than restarting - and is verified
  against Geofabrik's published ``.md5`` checksum
  (:func:`verify_pbf_checksum`) before parsing, raising rather than
  silently indexing a corrupt/truncated file.
* :func:`fetch_osm_hospital_index` (fast, called every
  ``enrich-organizations`` run) only ever reads that local cache file - no
  network call, no dependency on Overpass's uptime, no PBF reparsing. Same
  graceful-skip contract as VA: no cache configured or present means this
  source is skipped entirely, never an error.

Operational notes (see this module's own README-style comment at the
bottom of the file for measured, not estimated, figures from the first real
refresh): expect roughly a multi-GB download, a parse pass that peaks well
under the file's own size in memory (pyosmium streams; only matched
hospital records are accumulated), and a refresh cadence of weeks-to-months
- OSM edits accumulate gradually, and this data isn't time-critical the way
HIFLD's staleness problem was.

Matching logic mirrors ``hifld_hospitals.py``/``va_facilities.py`` exactly:
normalized name (or alt/official name) optionally disambiguated by US state,
with any key mapping to more than one distinct website excluded entirely
rather than resolved arbitrarily.

A small, deliberately isolated live-lookup helper
(:func:`fetch_live_state_hospitals`) is also provided for ad-hoc/manual use
(e.g. refreshing one state's data without a full PBF re-parse) - it is
**not** called by :func:`fetch_osm_hospital_index` or the enrichment cascade
and carries its own retry/backoff, matching the pattern Phase 6.0's
measurement scripts already proved out against Overpass's instability.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .parser_utils import normalize_org_name

_PLACEHOLDERS = {"", "n/a", "na", "none", "-"}

# Tags OSM uses for a hospital's website; either may be present.
_WEBSITE_TAGS = ("website", "contact:website")
_NAME_TAGS = ("name", "alt_name", "official_name")


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if v and v.lower() not in _PLACEHOLDERS else None


@dataclass(frozen=True)
class OsmHospitalIndex:
    """Lookup structure for OSM hospital websites - same shape and same
    ambiguity-exclusion discipline as
    :class:`~providermap.hifld_hospitals.HifldHospitalIndex` and
    :class:`~providermap.va_facilities.VaFacilityIndex`.

    ``by_name``: normalized hospital name/alt_name/official_name -> website,
    excluding any key that maps to more than one distinct website.

    ``by_name_state``: ``(normalized name, USPS state abbreviation)`` ->
    website, disambiguating a name shared by hospitals in different states.

    Every website value here has passed placeholder-filtering but has
    **not** been reachability-checked - callers must do that themselves
    before trusting/persisting a match (see ``cli.py::cmd_enrich_organizations``).
    """

    by_name: dict[str, str] = field(default_factory=dict)
    by_name_state: dict[tuple[str, str], str] = field(default_factory=dict)


def _build_osm_index(records: list[dict[str, object]]) -> OsmHospitalIndex:
    """Pure parsing/matching over already-extracted OSM tag dicts - the part
    worth testing offline. Each record is expected to carry (a subset of)
    ``name``/``alt_name``/``official_name``/``website``/``state`` keys, the
    same shape :func:`extract_hospital_records` and the JSON cache both
    produce. Never raises: a record missing a usable name or website is
    simply skipped.
    """
    key_sites: dict[str, set[str]] = {}
    key_state_sites: dict[tuple[str, str], set[str]] = {}

    for record in records:
        if not isinstance(record, dict):
            continue
        website = _clean(record.get("website"))
        if not website:
            continue
        state = _clean(record.get("state"))
        state = state.upper() if state else None

        keys = {normalize_org_name(_clean(record.get(tag))) for tag in _NAME_TAGS}
        for key in filter(None, keys):
            key_sites.setdefault(key, set()).add(website)
            if state:
                key_state_sites.setdefault((key, state), set()).add(website)

    by_name = {k: next(iter(v)) for k, v in key_sites.items() if len(v) == 1}
    by_name_state = {k: next(iter(v)) for k, v in key_state_sites.items() if len(v) == 1}
    return OsmHospitalIndex(by_name=by_name, by_name_state=by_name_state)


def fetch_osm_hospital_index(cache_path: str | None) -> OsmHospitalIndex:
    """Load the local OSM hospital cache built by :func:`refresh_osm_cache`.

    Returns an empty index (never raises, never makes a network call) if
    ``cache_path`` is falsy or the file doesn't exist yet - this source is
    entirely optional, same graceful-skip contract as VA when no
    ``va_api_key`` is configured.
    """
    if not cache_path:
        return OsmHospitalIndex()
    path = Path(cache_path)
    if not path.exists():
        return OsmHospitalIndex()
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return OsmHospitalIndex()
    if not isinstance(records, list):
        return OsmHospitalIndex()
    return _build_osm_index(records)


def extract_hospital_records(pbf_path: str) -> list[dict[str, object]]:
    """Stream a Geofabrik ``.osm.pbf`` extract with pyosmium, pulling out
    every node/way tagged ``amenity=hospital`` or ``healthcare=hospital``
    that carries a website tag.

    Not unit-tested against a real extract (impractical at ~12GB) - same
    "exercised only via manual/live verification" treatment this project
    already gives every other network/bulk-parse function
    (``fetch_hifld_hospital_index``, ``fetch_wikidata_hospital_websites``,
    ...); :func:`_build_osm_index`, which this feeds, carries the offline
    test coverage instead.
    """
    import osmium  # type: ignore[import-not-found]  # optional extra, see pyproject.toml's [osm]

    records: list[dict[str, object]] = []

    class _Handler(osmium.SimpleHandler):
        def _collect(self, tags: object) -> None:
            amenity = tags.get("amenity")  # type: ignore[attr-defined]
            healthcare = tags.get("healthcare")  # type: ignore[attr-defined]
            if amenity != "hospital" and healthcare != "hospital":
                return
            website = None
            for tag in _WEBSITE_TAGS:
                website = tags.get(tag)  # type: ignore[attr-defined]
                if website:
                    break
            if not website:
                return
            records.append(
                {
                    "name": tags.get("name"),  # type: ignore[attr-defined]
                    "alt_name": tags.get("alt_name"),  # type: ignore[attr-defined]
                    "official_name": tags.get("official_name"),  # type: ignore[attr-defined]
                    "website": website,
                    "state": tags.get("addr:state"),  # type: ignore[attr-defined]
                }
            )

        def node(self, n: object) -> None:
            self._collect(n.tags)  # type: ignore[attr-defined]

        def way(self, w: object) -> None:
            self._collect(w.tags)  # type: ignore[attr-defined]

    _Handler().apply_file(pbf_path)
    return records


def _remote_content_length(url: str, timeout_seconds: float) -> int | None:
    """HEAD (following redirects) to learn the remote file's total size, for
    both the "already fully downloaded" short-circuit and resume logic.
    Returns None (never raises) if the server doesn't report one.
    """
    try:
        resp = httpx.head(url, timeout=timeout_seconds, follow_redirects=True)
        resp.raise_for_status()
        length = resp.headers.get("content-length")
        return int(length) if length is not None else None
    except (httpx.HTTPError, ValueError):
        return None


def download_pbf(url: str, dest_path: str, timeout_seconds: float = 600.0) -> None:
    """Stream ``url`` (a multi-GB Geofabrik extract) to ``dest_path`` in
    chunks - never loaded into memory whole.

    Resumable: if a partial file already exists at ``dest_path``, continues
    via an HTTP Range request rather than restarting from zero (Geofabrik's
    server confirmed ``Accept-Ranges: bytes`` support). If ``dest_path``
    already matches the remote file's full size, this is a no-op - safe to
    call repeatedly (e.g. after an interrupted run) without re-downloading
    anything. No automatic retry beyond that: a genuinely failed request
    should be visible and re-run explicitly, not silently patched over
    (unlike the small per-request sources, this is a deliberate, infrequent,
    human-triggered operation).
    """
    dest = Path(dest_path)
    existing_size = dest.stat().st_size if dest.exists() else 0
    remote_size = _remote_content_length(url, timeout_seconds)

    if remote_size is not None and existing_size == remote_size:
        return  # already fully downloaded - nothing to do

    headers: dict[str, str] = {}
    mode = "wb"
    if existing_size and (remote_size is None or existing_size < remote_size):
        headers["Range"] = f"bytes={existing_size}-"
        mode = "ab"

    with httpx.stream(
        "GET", url, timeout=timeout_seconds, follow_redirects=True, headers=headers
    ) as resp:
        if existing_size and resp.status_code == 200:
            # Server ignored the Range request (e.g. a caching proxy) and is
            # sending the whole file again - start over rather than append
            # a second copy onto the partial file.
            mode = "wb"
        else:
            resp.raise_for_status()
        with open(dest_path, mode) as f:
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)


def _fetch_expected_md5(pbf_url: str, timeout_seconds: float = 30.0) -> str | None:
    """Geofabrik publishes a ``<file>.md5`` alongside every extract (format:
    ``<hex digest>  <filename>``) - fetch and parse it. Returns None (never
    raises) if unavailable, so callers can treat "no checksum published" as
    "verification skipped", not "verification failed".
    """
    try:
        resp = httpx.get(f"{pbf_url}.md5", timeout=timeout_seconds, follow_redirects=True)
        resp.raise_for_status()
        return resp.text.strip().split()[0].lower()
    except (httpx.HTTPError, ValueError, IndexError):
        return None


def _compute_md5(path: str, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def verify_pbf_checksum(pbf_path: str, pbf_url: str) -> bool | None:
    """Verify ``pbf_path`` against Geofabrik's published MD5 for ``pbf_url``.

    Returns ``True``/``False`` if a checksum was published and compared, or
    ``None`` if Geofabrik didn't publish one for this URL (nothing to check
    against - not the same as a failure).
    """
    expected = _fetch_expected_md5(pbf_url)
    if expected is None:
        return None
    return _compute_md5(pbf_path) == expected


def refresh_osm_cache(pbf_url: str, cache_path: str, pbf_download_path: str) -> int:
    """The heavy, explicit, periodic refresh: (resumably) download the
    Geofabrik extract to ``pbf_download_path``, verify it against Geofabrik's
    published MD5 checksum, extract every hospital record, and write them to
    ``cache_path`` as JSON.

    Raises ``RuntimeError`` if the downloaded file fails checksum
    verification, rather than silently building an index from a possibly
    corrupt/truncated file - safe to just re-run afterward, since
    :func:`download_pbf` resumes rather than restarting. Returns the number
    of hospital records written. Meant to be run via ``providermap
    refresh-osm-cache``, on whatever cadence the operator chooses
    (weeks/months) - never invoked automatically by ``enrich-organizations``,
    which only ever reads the resulting cache.
    """
    download_pbf(pbf_url, pbf_download_path)

    verified = verify_pbf_checksum(pbf_download_path, pbf_url)
    if verified is False:
        raise RuntimeError(
            f"{pbf_download_path} failed MD5 verification against {pbf_url}.md5 - "
            "the download may be corrupt or incomplete. Re-run this command "
            "(the download resumes automatically) or delete the file to start fresh."
        )

    records = extract_hospital_records(pbf_download_path)
    Path(cache_path).write_text(json.dumps(records), encoding="utf-8")
    return len(records)


def fetch_live_state_hospitals(
    state: str, user_agent: str, timeout_seconds: float = 120.0, max_attempts: int = 3
) -> list[dict[str, object]]:
    """Optional, isolated live Overpass lookup for one US state - NOT used
    by :func:`fetch_osm_hospital_index` or the enrichment cascade. Useful
    for ad-hoc spot-checks or refreshing a single state without a full PBF
    re-parse. Carries its own retry/backoff since Phase 6.0's measurement
    found the public Overpass API unreliable under load; still returns an
    empty list (never raises) if every attempt fails, since this is a
    convenience helper, not a dependency anything else relies on.
    """
    query = f"""
    [out:json][timeout:90];
    area["ISO3166-2"="US-{state}"][admin_level=4]->.s;
    ( nwr["amenity"="hospital"](area.s); nwr["healthcare"="hospital"](area.s); );
    out tags;
    """
    for attempt in range(max_attempts):
        try:
            resp = httpx.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": query},
                headers={"User-Agent": user_agent},
                timeout=timeout_seconds,
            )
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
            return [
                {
                    "name": el.get("tags", {}).get("name"),
                    "alt_name": el.get("tags", {}).get("alt_name"),
                    "official_name": el.get("tags", {}).get("official_name"),
                    "website": el.get("tags", {}).get("website")
                    or el.get("tags", {}).get("contact:website"),
                    "state": state,
                }
                for el in elements
                if isinstance(el, dict)
            ]
        except (httpx.HTTPError, ValueError):
            if attempt < max_attempts - 1:
                time.sleep(5 * (attempt + 1))
    return []
