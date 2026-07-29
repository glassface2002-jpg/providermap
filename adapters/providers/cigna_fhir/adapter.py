"""Cigna FHIR Provider Directory dataset adapter.

Ingests Cigna's public, no-authentication FHIR R4 Provider Directory API
(Da Vinci PDex Plan-Net, mandated under the CMS Interoperability & Patient
Access final rule, CMS-9115-F) - not a website to scrape. This is exactly the
kind of source :class:`~adapters.providers.base_dataset.ProviderDatasetAdapter`
was shaped for.

``discover()`` walks ``PractitionerRole`` search results page by page,
following each ``Bundle.link[relation=next]`` URL (never a hardcoded page-URL
scheme - the same "don't hardcode what the source itself tells you how to
paginate" principle
``adapters/organizations/cms_hospitals/adapter.py`` applies to its metastore
lookup). Every ``Practitioner``/``Location``/``Organization``/
``HealthcareService`` resource returned via ``_include`` is cached by
``(resourceType, id)`` as pages are walked.

Do not trust FHIR page boundaries: a real Plan-Net server has no obligation to
keep one practitioner's ``PractitionerRole`` resources contiguous within a
single page, or to repeat an already-returned ``_include``d resource on a
later page. So ``discover()`` fully drains every page *before* grouping
``PractitionerRole``s by practitioner and yielding - grouping page-by-page
would silently drop roles/locations/organizations that happen to land on a
different page than the resource that first introduced them. Correctness is
more important than premature optimization here, and this part is
irreducible: yielding a practitioner requires having seen every role that
references them, which isn't knowable until every page has been walked.

This is a different, smaller memory footprint than it might sound like,
though - it holds only *deduplicated, normalized-shape* resources
(``resources``/``roles`` in ``StateScopedDiscovery.run()``), not raw HTTP
response bodies. The part of this adapter that used to buffer full raw
fetch results before consuming any of them -
``_fetch_batches_concurrently``'s concurrent-path result dict - measurably
contributed a 5.61 GB peak RSS on the smallest state tested and has since
been converted to a streaming async generator (see that method's own
docstring); this module-level grouping accumulator was never the source of
that measurement and remains unchanged.

Field mapping (verified against a live Plan-Net response shape during the
research phase; see the plan file for the full table):

* NPI: ``Practitioner.identifier[system=http://hl7.org/fhir/sid/us-npi].value``
* name: ``Practitioner.name[0]`` (``family``, ``given``, ``suffix`` ->
  credentials)
* languages: ``Practitioner.communication[].text`` (or first coding display)
* specialty/taxonomy: ``PractitionerRole.specialty[].coding[]`` - the entry
  whose ``system`` contains ``nucc.org`` is the NUCC taxonomy code/display;
  otherwise the first coding is used as a best-effort fallback
* accepting new patients: the Plan-Net ``newpatients`` extension on
  ``PractitionerRole``
* locations/organizations: ``PractitionerRole.location[]`` /
  ``.organization`` references, resolved against the page-walk resource cache

Fields with no ProviderMap column (``Practitioner.gender``, FHIR ``fax``
telecom entries) are read by nothing here and dropped - matching CMS
adapter's convention of silently ignoring source columns with no destination.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from providermap.checkpoint import CheckpointStore
from providermap.config import Config, RetryConfig
from providermap.models import Location, Organization, Provider
from providermap.parser_utils import normalize_address_key, normalize_org_name, npi_checksum_valid
from providermap.resource_spool import ResourceSpool

from ..base_dataset import NormalizedProvider, ProviderDatasetAdapter

if TYPE_CHECKING:
    from .discovery import DiscoveryContext

log = logging.getLogger(__name__)

_NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
_NUCC_SYSTEM_HINT = "nucc.org"
_NEWPATIENTS_EXTENSION_URL = (
    "http://hl7.org/fhir/us/davinci-pdex-plan-net/StructureDefinition/newpatients"
)

# Fetches a FHIR page (an already-built request URL) and returns the decoded
# Bundle. Swapped out in tests for a fixture-backed fake - see
# ``tests/test_cigna_fhir_adapter.py``.
PageFetcher = Callable[[str], dict[str, Any]]


@dataclass
class DiscoveryStats:
    """Instrumentation for one :meth:`CignaFhirAdapter.discover` call.

    Pure profiling - nothing here changes what ``discover()`` returns, only
    what's visible afterward via :attr:`CignaFhirAdapter.last_discovery_stats`
    (mirrors the existing, narrower ``last_request_count``). Built to answer
    the Phase B question: before optimizing anything, where does the two-step
    (or unscoped) fetch actually spend its requests and time?
    """

    total_requests: int = 0
    #: Keyed by which FHIR endpoint the request hit ("Location" or
    #: "PractitionerRole") - not the resource *types* a response contains,
    #: since one PractitionerRole search response bundles Practitioner and
    #: Organization resources too via ``_include``. See
    #: :attr:`resources_seen_by_type` for that breakdown.
    requests_by_endpoint: dict[str, int] = field(default_factory=dict)
    #: Requests that followed a `Bundle.link[relation=next]` (as opposed to
    #: the first page of a Location walk or a PractitionerRole batch).
    continuation_requests: int = 0
    #: Every resource entry encountered across all responses, including ones
    #: already cached from an earlier response (see
    #: :attr:`duplicate_resources_by_type` for how many of these were that).
    resources_seen_by_type: dict[str, int] = field(default_factory=dict)
    #: A resource already present in the cache, returned again by a later
    #: request (e.g. the same Organization included in more than one
    #: PractitionerRole batch's response) - pure overhead, no new data.
    duplicate_resources_by_type: dict[str, int] = field(default_factory=dict)
    #: Reference resolutions against the resource cache while grouping
    #: PractitionerRoles by practitioner (practitioner/location/organization
    #: references) - a "hit" resolved to a cached resource, a "miss" pointed
    #: at a resource never returned by any query in this run.
    reference_hits: int = 0
    reference_misses: int = 0
    #: Retry attempts consumed by the retry/backoff wrapper used on the
    #: concurrent PractitionerRole batch path (see ``_fetch_with_retry``) -
    #: 0 whenever concurrency is off (``cigna_fhir_max_concurrent_requests
    #: == 1``, the default) or every request succeeded first try, which is
    #: what every measured pilot run has shown so far.
    retry_count: int = 0
    #: (batch_key, error) for every PractitionerRole batch that ultimately
    #: failed (retries exhausted, or a non-retryable error) - error
    #: aggregation: one bad batch doesn't abort the whole run, but it also
    #: shouldn't vanish silently. See ``cmd_ingest_providers``'s report.
    failed_batches: list[tuple[str, str]] = field(default_factory=list)
    request_latencies_seconds: list[float] = field(default_factory=list)
    runtime_seconds: float = 0.0
    providers_yielded: int = 0

    # ------------------------------------------------------------------ #
    # Silent-failure instrumentation. This server can return HTTP 200 with
    # a bundle that looks superficially valid but silently lost data - see
    # NATIONAL_DISCOVERY_DESIGN.md and evidence/PHASE_E_CAPABILITY_PROBES.md.
    # The load-bearing signal, confirmed across this project's live
    # investigation: a bundle carrying an explicit ``"total": 0`` marks a
    # server-side failure, while a genuinely empty result OMITS ``total``
    # entirely. The two are indistinguishable by entry count alone.
    # ------------------------------------------------------------------ #

    #: Keyed by endpoint ("Location"/"PractitionerRole") - count of bundles
    #: where the server returned the explicit total:0 failure signature.
    #: Any nonzero value here means discovery data was silently lost.
    bundles_with_total_zero_by_endpoint: dict[str, int] = field(default_factory=dict)
    #: Keyed by endpoint - count of bundles with zero entries, REGARDLESS of
    #: whether total:0 was present. A genuinely empty page (no total field)
    #: is normal and expected here; this counter alone does not indicate a
    #: problem - see bundles_with_total_zero_by_endpoint for that.
    empty_bundles_by_endpoint: dict[str, int] = field(default_factory=dict)
    #: Pages walked during THIS run's location enumeration. 0 if the phase
    #: was skipped via checkpoint hit or never ran (unscoped mode).
    location_enumeration_pages: int = 0
    #: None = the phase did not run this session (checkpoint hit, or
    #: unscoped/no-state mode) - deliberately distinct from False, which
    #: would wrongly suggest a verified abnormal termination. True only
    #: when the walk actually ran and its `next` link ended the pagination
    #: naturally (as opposed to an exception propagating mid-walk, which is
    #: already a loud failure and out of scope for this flag).
    location_enumeration_completed_normally: bool | None = None
    #: One PractitionerRole query issued per enumerated location (see
    #: _practitioner_role_by_location_url) - how many were attempted this
    #: run, and how many of the SUCCESSFUL ones returned zero roles. A
    #: nonzero empty count is expected and NOT a failure signal on its own -
    #: many locations legitimately have no practitioner. The exact rate is
    #: NOT a stable constant to assert against, though: an earlier RI
    #: measurement (from the since-corrected OR-list-batching defect - see
    #: config.staging_ri_corrected.yaml) suggested ~94% empty, while a later
    #: live production-scale run (the post-streaming-fix release-gate
    #: validation) measured ~36% empty on the same state - Cigna's directory
    #: is a live, continuously-updated third-party dataset, and its
    #: location/role attachment rate is expected to drift over time. Treat
    #: any specific percentage cited in commit history or design docs as a
    #: point-in-time measurement, never a correctness invariant.
    role_queries_attempted: int = 0
    role_queries_empty: int = 0
    #: Discovery-time practitioner-location associations, counted before
    #: normalize()'s address-based dedup - NOT the same as the final
    #: provider_locations row count in the database, which is a downstream,
    #: post-normalization concern this stat deliberately does not track.
    provider_location_relationships_discovered: int = 0

    def record_bundle_health(self, bundle: dict[str, Any], endpoint: str) -> None:
        """Call once per fetched bundle, for every endpoint, both the
        sequential and concurrent fetch paths - see this class's docstring
        section above for why ``total: 0`` and "zero entries" are tracked
        as two separate signals rather than one."""
        if bundle.get("total") == 0:
            self.bundles_with_total_zero_by_endpoint[endpoint] = (
                self.bundles_with_total_zero_by_endpoint.get(endpoint, 0) + 1
            )
        if not bundle.get("entry"):
            self.empty_bundles_by_endpoint[endpoint] = (
                self.empty_bundles_by_endpoint.get(endpoint, 0) + 1
            )

    @property
    def suspected_silent_failure(self) -> bool:
        """True if this run shows a KNOWN silent-failure signature: any
        total:0 bundle, or a location enumeration that ran but did not
        terminate normally. Deliberately does NOT flag a high
        role_queries_empty count - that is expected on every healthy run,
        not a sign of trouble.
        """
        return bool(self.bundles_with_total_zero_by_endpoint) or (
            self.location_enumeration_completed_normally is False
        )

    def record_request(self, endpoint: str, latency_seconds: float, is_continuation: bool) -> None:
        self.total_requests += 1
        self.requests_by_endpoint[endpoint] = self.requests_by_endpoint.get(endpoint, 0) + 1
        if is_continuation:
            self.continuation_requests += 1
        self.request_latencies_seconds.append(latency_seconds)

    def record_resource(self, resource_type: str, is_duplicate: bool) -> None:
        self.resources_seen_by_type[resource_type] = (
            self.resources_seen_by_type.get(resource_type, 0) + 1
        )
        if is_duplicate:
            self.duplicate_resources_by_type[resource_type] = (
                self.duplicate_resources_by_type.get(resource_type, 0) + 1
            )

    @property
    def average_latency_seconds(self) -> float | None:
        if not self.request_latencies_seconds:
            return None
        return sum(self.request_latencies_seconds) / len(self.request_latencies_seconds)

    @property
    def distinct_resources_by_type(self) -> dict[str, int]:
        """Resources actually cached (post-dedup): :attr:`resources_seen_by_type`
        minus :attr:`duplicate_resources_by_type`."""
        return {
            rt: count - self.duplicate_resources_by_type.get(rt, 0)
            for rt, count in self.resources_seen_by_type.items()
        }


def _default_page_fetcher(user_agent: str, timeout_seconds: float) -> PageFetcher:
    def fetch(url: str) -> dict[str, Any]:
        resp = httpx.get(url, headers={"User-Agent": user_agent}, timeout=timeout_seconds)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    return fetch


# ---------------------------------------------------------------------- #
# Bounded concurrency for the PractitionerRole batch phase only - see
# adapters/providers/cigna_fhir/CONCURRENCY_DESIGN.md. The Location phase and
# the unscoped search stay on the sync PageFetcher above; both paginate via
# an opaque continuation token that can't be parallelized, so there's
# nothing to gain from an async path there.
# ---------------------------------------------------------------------- #

# Fetches a FHIR page asynchronously. Only used for the PractitionerRole
# batch phase when cigna_fhir_max_concurrent_requests > 1.
AsyncPageFetcher = Callable[[str], Awaitable[dict[str, Any]]]


def _default_async_page_fetcher(
    client: httpx.AsyncClient, user_agent: str, timeout_seconds: float
) -> AsyncPageFetcher:
    async def fetch(url: str) -> dict[str, Any]:
        resp = await client.get(url, headers={"User-Agent": user_agent}, timeout=timeout_seconds)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    return fetch


async def _fetch_with_retry(
    fetch: AsyncPageFetcher, url: str, retry_config: RetryConfig, stats: DiscoveryStats
) -> dict[str, Any]:
    """Retry/backoff wrapper reusing the project's existing ``RetryConfig``
    (``config.retries``) rather than inventing Cigna-specific retry
    semantics - see the concurrency design doc's "retry/backoff strategy"
    section for why. Only retries HTTP statuses ``retry_config`` already
    considers retryable (429/500/502/503/504 by default) plus transport-level
    failures (timeouts, connection errors); a non-retryable HTTP error (e.g.
    a malformed batch query) raises immediately - retrying won't fix it.
    """
    last_exc: Exception | None = None
    for attempt in range(retry_config.max_attempts):
        try:
            return await fetch(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in retry_config.retry_on_status:
                raise
            last_exc = exc
        except httpx.TransportError as exc:
            last_exc = exc
        if attempt < retry_config.max_attempts - 1:
            stats.retry_count += 1
            delay = min(
                retry_config.backoff_base_seconds * (2**attempt), retry_config.backoff_max_seconds
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _fetch_with_retry_sync(
    fetch: PageFetcher, url: str, retry_config: RetryConfig, stats: DiscoveryStats
) -> dict[str, Any]:
    """Sync counterpart to :func:`_fetch_with_retry`, same retry/backoff
    semantics, for ``_fetch_timed`` - the Location-enumeration and
    sequential (``max_concurrent=1``, the default) PractitionerRole fetch
    path, neither of which runs inside an event loop.

    This did not exist until a live production-scale run hit a transient
    ``502`` during Location enumeration and crashed the entire multi-hour
    ingestion instead of retrying - ``config.retries`` was always meant to
    cover every fetch path, but only the concurrent path actually used it.
    Applying the identical, already-configured retry policy here is a gap
    fix, not a new policy.
    """
    last_exc: Exception | None = None
    for attempt in range(retry_config.max_attempts):
        try:
            return fetch(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in retry_config.retry_on_status:
                raise
            last_exc = exc
        except httpx.TransportError as exc:
            last_exc = exc
        if attempt < retry_config.max_attempts - 1:
            stats.retry_count += 1
            delay = min(
                retry_config.backoff_base_seconds * (2**attempt), retry_config.backoff_max_seconds
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


class _RateLimiter:
    """A minimum-interval gate between successive request starts, safe under
    concurrent asyncio tasks (single event loop, cooperative scheduling - no
    thread lock needed, just an ``asyncio.Lock`` to serialize the
    read-modify-write of ``_last_dispatch``). Independent of
    ``asyncio.Semaphore``'s concurrency limit: the semaphore bounds how many
    requests are in flight at once, this bounds how fast new ones start."""

    def __init__(self, requests_per_second: float) -> None:
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._last_dispatch = 0.0

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            wait = self._last_dispatch + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_dispatch = loop.time()


def _unscoped_search_url(base_url: str, page_size: int) -> str:
    """Build the (unscoped, no state filter) first ``PractitionerRole``
    search URL - includes practitioner/location/organization directly.

    Cigna's real deployed server does not support any state/geography
    search parameter on ``PractitionerRole`` at all, despite the Da Vinci
    PDex Plan-Net implementation guide describing chained location searches
    in the abstract - confirmed live against ``fhir.cigna.com``'s own
    CapabilityStatement (only ``practitioner``/``organization``/``location``/
    ``service``/``network``/``role``/``specialty`` are listed, and a chained
    ``location.address-state`` search returns a fatal ``OperationOutcome``).
    See :func:`_location_search_url` for how one-state scoping is actually
    done, via a separate ``Location`` search.
    """
    return (
        f"{base_url.rstrip('/')}/PractitionerRole"
        f"?_include=PractitionerRole:practitioner"
        f"&_include=PractitionerRole:location"
        f"&_include=PractitionerRole:organization"
        f"&_count={page_size}"
    )


def _national_window_url(
    base_url: str, ge: str | None, le: str | None, page_size: int
) -> str:
    """Build a national (unscoped) ``PractitionerRole`` search for one
    ``_lastUpdated`` window - see
    ``NATIONAL_DISCOVERY_DESIGN.md``'s national sequence and finding 1 in
    ``evidence/PHASE_E_CAPABILITY_PROBES.md`` (confirmed live: this exact
    ``_count=50`` plus all three ``_include``s combination returns the
    complete graph - every location a role references, not a truncated
    subset).

    ``ge``/``le`` are FHIR date strings (e.g. ``"2020-01-01"``); either may
    be ``None`` for an open-ended window. The first and last windows a
    national walk generates are deliberately open-ended on their outer
    edge, so no resource is missed regardless of how far back Cigna's real
    data actually goes - see :class:`~adapters.providers.cigna_fhir.discovery.NationalDiscovery`
    for how the finite window set is generated.
    """
    params = [
        f"_count={page_size}",
        "_include=PractitionerRole:practitioner",
        "_include=PractitionerRole:location",
        "_include=PractitionerRole:organization",
    ]
    if ge:
        params.append(f"_lastUpdated=ge{ge}")
    if le:
        params.append(f"_lastUpdated=le{le}")
    return f"{base_url.rstrip('/')}/PractitionerRole?" + "&".join(params)


def _location_search_url(base_url: str, state: str, page_size: int) -> str:
    """Build the first ``Location`` search URL scoped to one state.

    Unlike ``PractitionerRole``, ``Location`` really does support
    ``address-state`` (confirmed live) - this is step 1 of the two-step
    one-state pilot flow: find every Location in the state, then ask
    ``PractitionerRole`` which practitioners are at those locations (see
    :func:`_practitioner_role_by_location_url`).
    """
    return f"{base_url.rstrip('/')}/Location?address-state={state}&_count={page_size}"


def _practitioner_role_by_location_url(base_url: str, location_id: str, page_size: int) -> str:
    """Build a ``PractitionerRole`` search scoped to exactly ONE Location id.

    Confirmed live: this server's ``location`` search parameter only matches
    a *bare* resource id (``location=<id>``), not the standard
    ``Location/<id>`` reference form the FHIR spec would suggest - the
    reference form silently returns zero matches here.

    **One id per request is mandatory, not a style choice.** This function
    previously accepted a list and comma-joined it, on the documented
    assumption that a comma-joined list acts as an OR filter (which is what
    the FHIR spec says it should do). That assumption is false on this
    server: a comma-joined list silently honors only ONE of the ids and
    discards the rest, returning HTTP 200 with a normal-looking bundle. It
    is undetectable from the response.

    Measured live against three Location ids that each individually return
    roles (union = 6 distinct roles): every comma-joined ordering returned
    exactly 3 - one id's worth. Reordering changed *which* id's roles came
    back, confirming the rest are dropped rather than merged. With the
    previous batch size of 50 this discarded 49 of every 50 locations, i.e.
    ~98% of intended coverage, silently.

    (The same defect affects ``Location?_id=a,b,c``, which returns at most
    one resource - see ``evidence/PHASE_E_CAPABILITY_PROBES.md`` finding 3.
    Repeated params (``location=a&location=b``) and ``location:in=`` were
    also tested and return nothing at all, so there is no working batch
    syntax to fall back to.)

    ``page_size`` is still used for ``_count`` - a single location can have
    more roles than fit on one page, and the caller follows ``next`` links.
    No ``_include=PractitionerRole:location`` is needed - the Location
    resources were already fetched in step 1 (see
    :func:`_location_search_url`), so the caller seeds the resource cache
    from that response directly.
    """
    return (
        f"{base_url.rstrip('/')}/PractitionerRole"
        f"?location={location_id}"
        f"&_include=PractitionerRole:practitioner"
        f"&_include=PractitionerRole:organization"
        f"&_count={page_size}"
    )


def _next_link(bundle: dict[str, Any]) -> str | None:
    for link in bundle.get("link", []) or []:
        if link.get("relation") == "next" and link.get("url"):
            return str(link["url"])
    return None


def _parse_reference(ref: dict[str, Any] | None) -> tuple[str, str] | None:
    """Normalize a FHIR ``Reference`` (relative, absolute, or contained) to
    ``(resourceType, id)``, or ``None`` if it can't be resolved (e.g. a
    contained ``#id`` reference, which this adapter doesn't support)."""
    if not ref:
        return None
    raw = ref.get("reference")
    if not raw or raw.startswith("#"):
        return None
    parts = raw.rstrip("/").split("/")
    if len(parts) < 2:
        return None
    resource_type, resource_id = parts[-2], parts[-1]
    return resource_type, resource_id


def _codeable_concept_text(cc: dict[str, Any] | None) -> str | None:
    if not cc:
        return None
    if cc.get("text"):
        return str(cc["text"])
    codings = cc.get("coding") or []
    if codings and codings[0].get("display"):
        return str(codings[0]["display"])
    return None


def _nucc_coding(specialties: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Return ``(taxonomy_code, taxonomy_desc)`` from a
    ``PractitionerRole.specialty`` list, preferring the coding whose system
    is the NUCC taxonomy, falling back to the first coding of the first
    specialty entry if no NUCC-tagged one is present."""
    for cc in specialties:
        for coding in cc.get("coding") or []:
            if _NUCC_SYSTEM_HINT in (coding.get("system") or ""):
                return coding.get("code"), coding.get("display")
    if specialties:
        codings = specialties[0].get("coding") or []
        if codings:
            return codings[0].get("code"), codings[0].get("display")
    return None, None


def _accepting_new_patients(role: dict[str, Any]) -> bool | None:
    for ext in role.get("extension", []) or []:
        if ext.get("url") != _NEWPATIENTS_EXTENSION_URL:
            continue
        for inner in ext.get("extension", []) or []:
            if inner.get("url") != "acceptingPatients":
                continue
            codings = (inner.get("valueCodeableConcept") or {}).get("coding") or []
            for coding in codings:
                code = coding.get("code")
                if code == "newpt":
                    return True
                if code in ("nopt", "existptonly"):
                    return False
    return None


class CignaFhirAdapter(ProviderDatasetAdapter):
    """Provider dataset adapter for Cigna's public FHIR Provider Directory."""

    name = "cigna_fhir"
    source = "Cigna"

    def __init__(
        self,
        config: Config,
        page_fetcher: PageFetcher | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        super().__init__(config, checkpoint_store)
        pc = config.providers
        self._base_url = pc.cigna_fhir_base_url
        self._state = pc.cigna_fhir_state
        self._page_size = pc.cigna_fhir_page_size
        self._page_fetcher = page_fetcher or _default_page_fetcher(
            config.politeness.user_agent, pc.cigna_fhir_fetch_timeout_seconds
        )
        self._fetch_timeout = pc.cigna_fhir_fetch_timeout_seconds
        self._user_agent = config.politeness.user_agent
        self._retry_config = config.retries
        # 1 (the default) is fully sequential - the exact original behavior.
        # See CONCURRENCY_DESIGN.md; only the PractitionerRole batch phase
        # ever uses these two.
        self._max_concurrent = max(1, pc.cigna_fhir_max_concurrent_requests)
        self._requests_per_second = pc.cigna_fhir_requests_per_second
        #: Only used by the ``"national"`` discovery strategy - built here
        #: (not injected) directly from ``providers.resource_spool_path``,
        #: the same "config path in, collaborator out" shape
        #: ``_default_page_fetcher`` already uses. Kept off the shared
        #: ``ProviderDatasetAdapter`` ABC deliberately: unlike
        #: ``checkpoint_store`` (genuinely source-agnostic resumable-work
        #: tracking), a resource spool's shape is still specific to this one
        #: strategy - see the design doc's ``DiscoveryStrategy`` promotion
        #: criteria for when that changes. ``None`` if unset, in which case
        #: selecting national mode raises rather than silently falling back
        #: to an in-memory cache (defeating the whole point of that
        #: strategy). Public (no leading underscore), like
        #: ``checkpoint_store``, so callers can inspect or close it.
        self.resource_spool: ResourceSpool | None = (
            ResourceSpool(pc.resource_spool_path) if pc.resource_spool_path else None
        )
        self._window_days = pc.cigna_fhir_window_days
        self._lookback_days = pc.cigna_fhir_lookback_days
        #: Number of page requests the most recent :meth:`discover` call
        #: made - read by ``cmd_ingest_providers`` for the pilot dry-run
        #: report. Not meaningful until ``discover()`` has been iterated.
        self.last_request_count = 0
        #: Full profiling breakdown for the most recent :meth:`discover`
        #: call - see :class:`DiscoveryStats`. Same object for the life of
        #: one ``discover()`` call, so it's readable mid-iteration, but
        #: ``runtime_seconds``/``providers_yielded`` are only final once the
        #: generator has been fully drained.
        self.last_discovery_stats: DiscoveryStats | None = None

    # ------------------------------------------------------------------ #
    # discover
    # ------------------------------------------------------------------ #

    def _fetch_timed(self, url: str, endpoint: str, stats: DiscoveryStats, is_continuation: bool) -> dict[str, Any]:
        started = time.monotonic()
        bundle = _fetch_with_retry_sync(self._page_fetcher, url, self._retry_config, stats)
        stats.record_bundle_health(bundle, endpoint)
        stats.record_request(endpoint, time.monotonic() - started, is_continuation)
        return bundle

    def _fetch_location_page_entries(
        self,
        start_url: str | None,
        stats: DiscoveryStats,
        on_page: Callable[[list[dict[str, Any]], str | None], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Follow ``next`` links starting at ``start_url``, returning every
        raw ``Location`` resource dict encountered - fetch only, no
        application to any shared cache. This is what gets checkpointed (see
        :meth:`_apply_location_entries`), so a resumed run can replay it
        without a second network request.

        ``on_page``, if given, is called once per fetched page with (this
        page's ``Location`` resources, the next page's cursor URL, or
        ``None`` if this was the last page) - lets a caller persist
        incremental per-page progress (see
        ``StateScopedDiscovery._run_location_enumeration``) without this
        method needing any awareness of checkpointing itself.
        """
        entries: list[dict[str, Any]] = []
        url = start_url
        is_continuation = False
        pages = 0
        while url:
            bundle = self._fetch_timed(url, "Location", stats, is_continuation)
            pages += 1
            page_entries = [
                entry.get("resource")
                for entry in bundle.get("entry", []) or []
                if entry.get("resource")
                and entry["resource"].get("resourceType") == "Location"
                and entry["resource"].get("id")
            ]
            entries.extend(page_entries)
            next_url = _next_link(bundle)
            if on_page is not None:
                on_page(page_entries, next_url)
            url = next_url
            is_continuation = True
        # Reached here only via the loop's natural exit (next link absent) -
        # an exception mid-walk propagates past this point instead, leaving
        # completed_normally at its default None. See DiscoveryStats's
        # docstring for why None/True/False are three distinct states. Note
        # this reflects only THIS call's page count - a resumed walk's own
        # earlier pages (before the interruption) are not re-counted here,
        # matching the existing convention that stats reflect this process's
        # activity, not cumulative history across resumes (e.g.
        # total_requests works the same way).
        stats.location_enumeration_pages = pages
        stats.location_enumeration_completed_normally = True
        return entries

    def _apply_location_entries(
        self,
        entries: list[dict[str, Any]],
        resources: dict[tuple[str, str], dict[str, Any]],
        location_ids: list[str],
        stats: DiscoveryStats,
    ) -> None:
        for resource in entries:
            key = ("Location", resource["id"])
            stats.record_resource("Location", is_duplicate=key in resources)
            resources[key] = resource
            location_ids.append(resource["id"])

    def _fetch_role_page_entries(
        self, start_url: str | None, stats: DiscoveryStats
    ) -> list[dict[str, Any]]:
        """Follow ``next`` links starting at ``start_url``, returning every
        raw resource dict (any type - PractitionerRole/Practitioner/
        Organization) encountered. Fetch only; see
        :meth:`_apply_role_entries` for what gets checkpointed."""
        entries: list[dict[str, Any]] = []
        url = start_url
        is_continuation = False
        while url:
            bundle = self._fetch_timed(url, "PractitionerRole", stats, is_continuation)
            for entry in bundle.get("entry", []) or []:
                resource = entry.get("resource")
                if resource and resource.get("resourceType") and resource.get("id"):
                    entries.append(resource)
                else:
                    log.warning("Skipping malformed Bundle entry with no resource/id: %r", entry)
            url = _next_link(bundle)
            is_continuation = True
        return entries

    async def _fetch_role_page_entries_async(
        self, client: httpx.AsyncClient, start_url: str, stats: DiscoveryStats
    ) -> list[dict[str, Any]]:
        """Async equivalent of :meth:`_fetch_role_page_entries`, used only by
        the concurrent batch phase - one batch's own pagination is still
        walked sequentially (a batch could in principle return more than one
        page), it's just that *multiple batches* run concurrently with each
        other via :meth:`_fetch_batches_concurrently`."""
        entries: list[dict[str, Any]] = []
        url: str | None = start_url
        is_continuation = False
        fetch = _default_async_page_fetcher(client, self._user_agent, self._fetch_timeout)
        while url:
            started = time.monotonic()
            bundle = await _fetch_with_retry(fetch, url, self._retry_config, stats)
            stats.record_bundle_health(bundle, "PractitionerRole")
            stats.record_request("PractitionerRole", time.monotonic() - started, is_continuation)
            for entry in bundle.get("entry", []) or []:
                resource = entry.get("resource")
                if resource and resource.get("resourceType") and resource.get("id"):
                    entries.append(resource)
                else:
                    log.warning("Skipping malformed Bundle entry with no resource/id: %r", entry)
            url = _next_link(bundle)
            is_continuation = True
        return entries

    async def _fetch_batches_concurrently(
        self,
        pending: list[tuple[str, str]],
        stats: DiscoveryStats,
        client: httpx.AsyncClient | None = None,
    ) -> AsyncIterator[tuple[str, list[dict[str, Any]] | Exception]]:
        """Fetch multiple independent PractitionerRole batch URLs
        concurrently, bounded by ``cigna_fhir_max_concurrent_requests`` and
        throttled by ``cigna_fhir_requests_per_second``.

        Yields ``(batch_key, entries_or_exception)`` one at a time, in
        ``pending``'s order - the caller applies and checkpoints each
        result as it arrives (see ``StateScopedDiscovery.run()``'s
        streaming apply loop) instead of waiting for every request to
        finish first. Ordering is still deterministic regardless of which
        request actually completed first over the network (see the design
        doc's "deterministic output" section): every task is scheduled
        immediately via ``asyncio.ensure_future`` so all of them progress
        concurrently in the background (bounded by the semaphore below),
        but this method ``await``s them back in ``pending``'s own order,
        not completion order - awaiting an already-finished task simply
        returns its cached result immediately.

        Previously returned a fully-materialized ``dict`` only after every
        request finished, which held all ~42,000 results in memory at once
        on a real state-scale run (measured contributing to a 5.61 GB peak
        RSS on the smallest state tested) before any of them were applied
        or checkpointed. This version holds at most ``max_concurrent``
        completed-but-not-yet-consumed results at a time, since every task
        beyond that is still blocked on the semaphore and hasn't fetched
        anything yet.

        ``client`` is an injectable seam for tests (an ``httpx.AsyncClient``
        built on ``httpx.MockTransport``, no real network) - production code
        always leaves it ``None`` and gets an owned, auto-closed client.
        """
        semaphore = asyncio.Semaphore(self._max_concurrent)
        limiter = _RateLimiter(self._requests_per_second)

        async def fetch_one(active_client: httpx.AsyncClient, url: str) -> list[dict[str, Any]] | Exception:
            async with semaphore:
                await limiter.acquire()
                try:
                    return await self._fetch_role_page_entries_async(active_client, url, stats)
                except Exception as exc:  # noqa: BLE001 - any failure is a per-batch result
                    return exc

        async def run_with(active_client: httpx.AsyncClient) -> AsyncIterator[tuple[str, list[dict[str, Any]] | Exception]]:
            tasks = [asyncio.ensure_future(fetch_one(active_client, url)) for _key, url in pending]
            for (batch_key, _url), task in zip(pending, tasks, strict=True):
                yield batch_key, await task

        if client is not None:
            async for item in run_with(client):
                yield item
        else:
            async with httpx.AsyncClient() as owned_client:
                async for item in run_with(owned_client):
                    yield item

    def _apply_role_entries(
        self,
        entries: list[dict[str, Any]],
        resources: dict[tuple[str, str], dict[str, Any]],
        roles: list[dict[str, Any]],
        stats: DiscoveryStats,
    ) -> None:
        for resource in entries:
            key = (resource["resourceType"], resource["id"])
            is_duplicate = key in resources
            stats.record_resource(resource["resourceType"], is_duplicate=is_duplicate)
            resources[key] = resource
            # Append each role ONCE, no matter how many batches return it.
            # The same PractitionerRole is re-fetched once per in-state
            # location it references (one query per location - see
            # _practitioner_role_by_location_url), so without this guard
            # `roles` carries duplicate occurrences (measured 65,256 seen
            # vs 27,171 distinct on Rhode Island, a non-uniform 2.4x
            # inflation) and normalize()'s primary-location scoring counts
            # a role's locations once per re-fetch - making the selected
            # primary depend on fetch multiplicity rather than the data.
            # The shared `resources` dict spans the whole run() - including
            # checkpoint-replayed batches, which call this same method - so
            # this is the one place that sees every occurrence.
            # (`resources[key]` is still refreshed on re-sight above; the
            # body is the same server resource either way.)
            if resource["resourceType"] == "PractitionerRole" and not is_duplicate:
                roles.append(resource)

    def _build_discovery_context(self, stats: DiscoveryStats) -> "DiscoveryContext":
        """Assemble the thin, adapter-independent context a
        :class:`~adapters.providers.cigna_fhir.discovery.DiscoveryStrategy`
        runs against - see ``discovery.py``'s module docstring for why this
        exists instead of passing ``self`` directly."""
        from .discovery import DiscoveryContext

        return DiscoveryContext(
            adapter_name=self.name,
            base_url=self._base_url,
            state=self._state,
            page_size=self._page_size,
            max_concurrent=self._max_concurrent,
            checkpoint_store=self.checkpoint_store,
            stats=stats,
            fetch_location_page_entries=self._fetch_location_page_entries,
            apply_location_entries=self._apply_location_entries,
            fetch_role_page_entries=self._fetch_role_page_entries,
            fetch_batches_concurrently=self._fetch_batches_concurrently,
            apply_role_entries=self._apply_role_entries,
            resource_spool=self.resource_spool,
            window_days=self._window_days,
            lookback_days=self._lookback_days,
        )

    def discover(self) -> Iterator[dict[str, Any]]:
        # Phase E, Stage 1: discover() is now selection + dispatch onto a
        # DiscoveryStrategy (see discovery.py) - StateScopedDiscovery.run()
        # is this method's entire previous body, moved there unchanged. The
        # import is deferred (not module-level) so adapter.py never depends
        # on discovery.py at import time - see discovery.py's module
        # docstring for the one-directional dependency this preserves.
        from .discovery import get_discovery_strategy

        stats = DiscoveryStats()
        self.last_discovery_stats = stats
        context = self._build_discovery_context(stats)
        strategy = get_discovery_strategy(self.config.providers.cigna_fhir_discovery_mode)
        yield from strategy.run(context)
        self.last_request_count = stats.total_requests

    # ------------------------------------------------------------------ #
    # extract
    # ------------------------------------------------------------------ #

    def extract(self, raw: dict[str, Any]) -> dict[str, Any]:
        practitioner = raw["practitioner"]

        npi = None
        for ident in practitioner.get("identifier", []) or []:
            if ident.get("system") == _NPI_SYSTEM and ident.get("value"):
                npi = str(ident["value"])
                break

        names = practitioner.get("name", []) or []
        first_name = last_name = full_name = credentials = None
        if names:
            name = names[0]
            given = name.get("given", []) or []
            family = name.get("family")
            first_name = given[0] if given else None
            last_name = family
            full_name = " ".join(p for p in [*given, family] if p) or None
            suffixes = name.get("suffix", []) or []
            credentials = ", ".join(suffixes) if suffixes else None

        languages = []
        for comm in practitioner.get("communication", []) or []:
            text = _codeable_concept_text(comm)
            if text:
                languages.append(text)

        roles_out = []
        for role in raw["roles"]:
            taxonomy_code, taxonomy_desc = _nucc_coding(role.get("specialty", []) or [])
            location_ids = []
            for loc_ref in role.get("location", []) or []:
                parsed = _parse_reference(loc_ref)
                if parsed:
                    location_ids.append(parsed[1])
            org_ref = _parse_reference(role.get("organization"))
            roles_out.append(
                {
                    "specialty": taxonomy_desc,
                    "taxonomy_code": taxonomy_code,
                    "taxonomy_desc": taxonomy_desc,
                    "accepting_new_patients": _accepting_new_patients(role),
                    "location_ids": location_ids,
                    "organization_id": org_ref[1] if org_ref else None,
                }
            )

        locations_out = {}
        for loc_id, loc in raw["locations"].items():
            address = loc.get("address") or {}
            lines = address.get("line", []) or []
            telecoms = loc.get("telecom", []) or []
            phone = next((t.get("value") for t in telecoms if t.get("system") == "phone"), None)
            addr_str = ", ".join(lines) if lines else None
            city = address.get("city")
            state = address.get("state")
            zip_code = address.get("postalCode")
            name = loc.get("name")
            if _location_name_is_generated_address(name, lines, city, state, zip_code):
                name = None
            locations_out[loc_id] = {
                "name": name,
                "address": addr_str,
                "city": city,
                "state": state,
                "zip": zip_code,
                "phone": phone,
            }

        organizations_out = {}
        for org_id, org in raw["organizations"].items():
            org_npi = None
            for ident in org.get("identifier", []) or []:
                if ident.get("system") == _NPI_SYSTEM and ident.get("value"):
                    org_npi = str(ident["value"])
                    break
            organizations_out[org_id] = {"name": org.get("name"), "npi": org_npi}

        return {
            "npi": npi,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "credentials": credentials,
            "languages": languages,
            "roles": roles_out,
            "locations": locations_out,
            "organizations": organizations_out,
        }

    # ------------------------------------------------------------------ #
    # normalize
    # ------------------------------------------------------------------ #

    def normalize(self, extracted: dict[str, Any]) -> NormalizedProvider:
        roles = extracted["roles"]
        locations = extracted["locations"]
        organizations = extracted["organizations"]

        # Deterministic primary-location selection: FHIR gives no ranking
        # among a practitioner's locations, so pick whichever location is
        # tied to the most DISTINCT roles (deduplicated upstream in
        # _apply_role_entries), tie-broken by lexicographically smallest
        # location id. Both the count and the tie-break are pure functions
        # of the practitioner's role set - fetch order, fetch multiplicity,
        # checkpoint replay order, and storage backend cannot change the
        # winner. (The previous first-appearance tie-break made the common
        # case - most locations tie at one role each - depend on traversal
        # order, and its .index() scan was O(n^2) in locations per
        # practitioner.)
        location_role_counts: dict[str, int] = {}
        for role in roles:
            for loc_id in role["location_ids"]:
                location_role_counts[loc_id] = location_role_counts.get(loc_id, 0) + 1

        primary_location_id = None
        if location_role_counts:
            primary_location_id = min(
                location_role_counts,
                key=lambda lid: (-location_role_counts[lid], lid),
            )

        # The role tied to the primary location is the one whose
        # specialty/taxonomy describe the practitioner's primary site of
        # care; fall back to the first role if no location was selected
        # (e.g. no role carries a location reference at all).
        primary_role = next(
            (r for r in roles if primary_location_id in r["location_ids"]),
            roles[0] if roles else {},
        )
        primary_org_name = None
        if primary_role.get("organization_id") in organizations:
            primary_org_name = organizations[primary_role["organization_id"]]["name"]

        npi = extracted["npi"]
        # Same value that answers "where does this practitioner primarily
        # practice" for practice_name (adapter.py:970-973's primary-role
        # selection) - populated for both columns so exports (see
        # exporter.py's "Primary Site of Care" column) don't come up blank
        # for Cigna-sourced providers the way they used to.
        primary_site_name = (
            locations.get(primary_location_id, {}).get("name") if primary_location_id else None
        ) or primary_org_name
        provider = Provider(
            full_name=extracted["full_name"],
            first_name=extracted["first_name"],
            last_name=extracted["last_name"],
            credentials=extracted["credentials"],
            specialty=primary_role.get("specialty"),
            taxonomy_code=primary_role.get("taxonomy_code"),
            taxonomy_desc=primary_role.get("taxonomy_desc"),
            practice_name=primary_site_name,
            primary_site_of_care=primary_site_name,
            npi=npi,
            npi_valid=npi_checksum_valid(npi) if npi else True,
            location_count=len(locations) or 1,
            accepting_new_patients=_any_or_none(
                [r["accepting_new_patients"] for r in roles]
            ),
            languages=extracted["languages"],
            source_notes="Cigna FHIR Provider Directory",
        )
        if primary_location_id and primary_location_id in locations:
            primary = locations[primary_location_id]
            provider.address = primary["address"]
            provider.city = primary["city"]
            provider.state = primary["state"]
            provider.zip = primary["zip"]
            provider.phone = primary["phone"]

        normalized_locations = []
        primary_key = None
        if primary_location_id and primary_location_id in locations:
            p = locations[primary_location_id]
            primary_key = normalize_address_key(p["address"], p["city"], p["state"], p["zip"])
        for loc in locations.values():
            key = normalize_address_key(loc["address"], loc["city"], loc["state"], loc["zip"])
            if not key:
                continue
            normalized_locations.append(_location_from_dict(loc, key))
        # Primary location first - cmd_ingest_providers uses list position
        # to decide which Location gets is_primary=True in provider_locations.
        if primary_key:
            normalized_locations.sort(key=lambda loc: loc.address_key != primary_key)

        normalized_organizations = []
        for org_id, org in organizations.items():
            org_name = org["name"]
            normalized_organizations.append(
                (
                    Organization(
                        name=org_name,
                        normalized_name=normalize_org_name(org_name),
                        source=self.source,
                        source_id=org["npi"] or org_id,
                    ),
                    "affiliated",
                )
            )

        return NormalizedProvider(
            provider=provider,
            locations=normalized_locations,
            organizations=normalized_organizations,
        )


def _any_or_none(values: list[bool | None]) -> bool | None:
    """``True`` if any role says accepting, ``False`` if any (non-True) role
    explicitly says not accepting, ``None`` if nothing said either way."""
    if any(v is True for v in values):
        return True
    if any(v is False for v in values):
        return False
    return None


def _location_name_is_generated_address(
    name: str | None,
    address_lines: list[str] | None,
    city: str | None,
    state: str | None,
    zip_code: str | None,
) -> bool:
    """True if a FHIR ``Location.name`` is really just that same Location's
    own address components auto-joined by dashes - EVERY line, not just the
    first (observed live: a two-line address like ``"70 KENYON AVE UNIT
    326"`` + ``"# 326"`` produces a name of ``"70 KENYON AVE UNIT 326-#
    326-WAKEFIELD-RI-02879"``) - rather than an actual facility name.
    ``address_lines`` must be the raw per-line list (``Location.address
    .line``), not a comma-joined display string - joining with ``", "``
    first (as the display ``address`` field does) doesn't match Cigna's own
    dash-joined generated name whenever a location has more than one
    address line, which is common (suite/unit/building lines) and was
    exactly the gap that let the majority of these slip past an earlier,
    narrower version of this check that only compared against a single
    joined address string. Treating a match as a name duplicates the
    address already stored separately in ``Provider.address``/``city``/
    ``state``/``zip`` and ``Location.address``/``city``/``state``/``zip`` -
    callers filter it out here so ``practice_name``/``primary_site_of_care``
    (``normalize()``) and ``Location.facility_name``
    (``_location_from_dict``) fall back to the organization name or stay
    blank instead of repeating the address a third time.
    """
    if not name or not address_lines:
        return False
    zip5 = (zip_code or "")[:5]
    candidate = "-".join(p for p in [*address_lines, city, state, zip5] if p)
    return name.strip().casefold() == candidate.strip().casefold()


def _location_from_dict(loc: dict[str, Any], address_key: str) -> Location:
    return Location(
        facility_name=loc["name"],
        address=loc["address"],
        city=loc["city"],
        state=loc["state"],
        zip=loc["zip"],
        phone=loc["phone"],
        address_key=address_key,
    )
