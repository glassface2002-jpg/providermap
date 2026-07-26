"""Discovery strategy abstraction for the Cigna FHIR adapter (Phase E - see
``NATIONAL_DISCOVERY_DESIGN.md`` and
``docs/adr/0001-national-discovery-strategy.md``).

``CignaFhirAdapter.discover()`` used to be one method containing the entire
state-scoped fetch. It now builds a :class:`DiscoveryContext` and delegates
to whichever :class:`DiscoveryStrategy` ``config.providers.cigna_fhir_discovery_mode``
selects:

- :class:`StateScopedDiscovery` (Stage 1, the default) - an *extraction*,
  not a rewrite: its ``run()`` is ``CignaFhirAdapter.discover()``'s previous
  body, moved here verbatim, with every ``self.X`` reference replaced by the
  equivalent ``context.X``. No fetch logic, no checkpoint semantics, no
  concurrency behavior changed.
- :class:`NationalDiscovery` (Stage 2) - the design doc's spool-then-
  materialize strategy. Not the default; opt in via
  ``cigna_fhir_discovery_mode: "national"``.

The dependency direction is deliberately one-way:
``CignaFhirAdapter -> DiscoveryContext -> DiscoveryStrategy``. This module
imports pure helpers and the :class:`~adapters.providers.cigna_fhir.adapter.DiscoveryStats`
type from ``adapter.py`` (one-directional); ``adapter.py`` never imports from
here at module level - only inside method bodies, right before use - so a
strategy never needs to reach back into ``CignaFhirAdapter`` internals, and
adding a second adapter's own strategies later never has to import this
adapter's module to do it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from providermap.checkpoint import CheckpointStore
from providermap.resource_spool import ResourceSpool

from .adapter import (
    DiscoveryStats,
    _location_search_url,
    _national_window_url,
    _parse_reference,
    _practitioner_role_by_location_url,
    _unscoped_search_url,
)

log = logging.getLogger(__name__)

FetchPageEntries = Callable[[str | None, DiscoveryStats], list[dict[str, Any]]]
#: Same shape as FetchPageEntries, plus an optional per-page callback (this
#: page's entries, the next page's cursor or None) - lets a strategy persist
#: incremental progress for resumability. Only fetch_location_page_entries
#: uses the callback; fetch_role_page_entries' own per-location granularity
#: already makes it resumable without one (see _handle_batch_result).
FetchLocationPageEntries = Callable[
    [str | None, DiscoveryStats, Callable[[list[dict[str, Any]], str | None], None] | None],
    list[dict[str, Any]],
]
ApplyLocationEntries = Callable[
    [list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]], list[str], DiscoveryStats], None
]
ApplyRoleEntries = Callable[
    [list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]], DiscoveryStats],
    None,
]
FetchBatchesConcurrently = Callable[
    [list[tuple[str, str]], DiscoveryStats], Awaitable[dict[str, list[dict[str, Any]] | Exception]]
]


@dataclass
class DiscoveryContext:
    """Everything a :class:`DiscoveryStrategy` needs, and nothing else -
    deliberately thin (see the approved Phase E design). No reference back to
    the owning adapter: a strategy only ever sees plain config values, the
    shared :class:`DiscoveryStats` instance, and bound fetch/apply callables
    the adapter already implements. This is what keeps the strategy layer
    decoupled from ``CignaFhirAdapter``'s own internals (its ``PageFetcher``,
    ``httpx`` usage, retry/rate-limit wiring - none of that is visible here).

    Built fresh per :meth:`CignaFhirAdapter.discover` call by
    ``CignaFhirAdapter._build_discovery_context`` - never constructed
    directly by a strategy.
    """

    adapter_name: str
    base_url: str
    state: str | None
    page_size: int
    max_concurrent: int
    checkpoint_store: CheckpointStore | None
    stats: DiscoveryStats

    #: Bound to the owning adapter's fetch/apply implementations - a
    #: strategy calls these without knowing how fetching actually happens.
    fetch_location_page_entries: FetchLocationPageEntries
    apply_location_entries: ApplyLocationEntries
    fetch_role_page_entries: FetchPageEntries
    fetch_batches_concurrently: FetchBatchesConcurrently
    apply_role_entries: ApplyRoleEntries

    #: Only used by :class:`NationalDiscovery` - ``None`` for
    #: :class:`StateScopedDiscovery`, which never touches these three.
    resource_spool: ResourceSpool | None = None
    window_days: int = 30
    lookback_days: int = 3650


class DiscoveryStrategy(ABC):
    """One way of discovering practitioners from the Cigna FHIR Provider
    Directory. See ``adapters/providers/base_dataset.py``'s
    ``ProviderDatasetAdapter.discover()`` contract - a strategy's ``run()``
    must satisfy the exact same yielded-dict shape (bare-id keyed
    ``locations``/``organizations``, required ``practitioner``/``roles``).
    """

    @abstractmethod
    def run(self, context: DiscoveryContext) -> Iterator[dict[str, Any]]:
        """Yield one complete raw record per practitioner - see
        ``ProviderDatasetAdapter.discover()``'s docstring for what "complete"
        requires (grouping across every page before yielding, never a
        partial record)."""


class StateScopedDiscovery(DiscoveryStrategy):
    """The original (and still default) discovery behavior: a two-step
    ``Location`` enumeration -> batched ``PractitionerRole`` fetch when
    ``context.state`` is set, or a single unscoped ``PractitionerRole`` walk
    when it isn't. This is ``CignaFhirAdapter.discover()``'s previous body,
    moved here unchanged - see ``adapter.py``'s module docstring and
    ``CONCURRENCY_DESIGN.md`` for the reasoning behind every design choice
    inside it (checkpoint granularity per phase, why out-of-state locations
    are dropped, why concurrency only applies to the batch phase).

    Known, documented limitation carried over unchanged (see
    ``NATIONAL_DISCOVERY_DESIGN.md``'s Risk R1): when ``context.state`` is
    set, a practitioner's locations outside that state are silently dropped
    (counted in ``context.stats.reference_misses``) rather than fetched -
    this is why a future national strategy's output can be a superset of
    this one's, never the reverse.
    """

    def _run_location_enumeration(
        self,
        context: DiscoveryContext,
        stats: DiscoveryStats,
        cp: CheckpointStore | None,
        checkpoint_id: str,
        resources: dict[tuple[str, str], dict[str, Any]],
        location_ids: list[str],
    ) -> None:
        """Fetch every Location for this run's state, applying results into
        ``resources``/``location_ids`` in place (matching
        ``apply_location_entries``'s existing convention). Resumable at PAGE
        granularity: an interruption mid-walk leaves per-page checkpoint
        items behind (see ``on_page`` below), and a later call picks up from
        the last saved FHIR continuation cursor instead of re-walking from
        page 1.

        That saved cursor may be stale by the time we resume - Cigna's own
        `_getpages=<uuid>` cursors carry roughly a 17-minute IDLE TTL
        (measured live; a cursor under CONTINUOUS use does not expire, but
        one parked across a long interruption can). An expired cursor
        returns HTTP 200 with an empty bundle and the confirmed total:0
        failure signature (see ``DiscoveryStats.record_bundle_health``) -
        indistinguishable from genuine completion by entry count alone, but
        exactly what that signature exists to catch. If resuming shows it,
        the partial progress is discarded and the walk restarts cleanly
        from the beginning, exactly as it always has - correctness never
        depends on the resumed cursor still being valid, only benefits from
        it when it is.
        """
        resume_url = _location_search_url(context.base_url, context.state, context.page_size)
        reloaded_entries: list[dict[str, Any]] = []
        next_page_index = 0
        resuming = False

        if cp is not None:
            cursor_payload = cp.get_payload(checkpoint_id, "location_enumeration_cursor", "current")
            if cursor_payload is not None and cursor_payload.get("next_url"):
                for _key, page_payload in sorted(
                    cp.done_payloads(checkpoint_id, "location_enumeration_pages").items(),
                    key=lambda kv: int(kv[0]),
                ):
                    reloaded_entries.extend(page_payload["entries"])
                resume_url = cursor_payload["next_url"]
                next_page_index = cursor_payload["next_page_index"]
                resuming = True
                log.info(
                    "Resuming location enumeration from a saved cursor (%d "
                    "locations already checkpointed from before the interruption)",
                    len(reloaded_entries),
                )

        total_zero_before = stats.bundles_with_total_zero_by_endpoint.get("Location", 0)
        next_index = [next_page_index]  # boxed: mutated by the nested closure below

        def _on_page(page_entries: list[dict[str, Any]], next_url: str | None) -> None:
            if cp is not None:
                cp.mark_done(
                    checkpoint_id,
                    "location_enumeration_pages",
                    str(next_index[0]),
                    {"entries": page_entries},
                )
                cp.mark_done(
                    checkpoint_id,
                    "location_enumeration_cursor",
                    "current",
                    {"next_url": next_url, "next_page_index": next_index[0] + 1},
                )
            next_index[0] += 1

        new_entries = context.fetch_location_page_entries(resume_url, stats, _on_page)

        if resuming and stats.bundles_with_total_zero_by_endpoint.get("Location", 0) > total_zero_before:
            log.warning(
                "Resumed location-enumeration cursor was stale (total:0 "
                "signature seen) - discarding saved progress and restarting "
                "the walk from scratch."
            )
            if cp is not None:
                cp.clear_phase(checkpoint_id, "location_enumeration_pages")
                cp.clear_phase(checkpoint_id, "location_enumeration_cursor")
            self._run_location_enumeration(
                context, stats, cp, checkpoint_id, resources, location_ids
            )
            return

        context.apply_location_entries(reloaded_entries + new_entries, resources, location_ids, stats)

    def run(self, context: DiscoveryContext) -> Iterator[dict[str, Any]]:
        resources: dict[tuple[str, str], dict[str, Any]] = {}
        roles: list[dict[str, Any]] = []
        stats = context.stats
        started_at = time.monotonic()

        # Optional durable resume (see providermap/checkpoint.py). A run is
        # identified by adapter name + state scope, so re-running the same
        # scope after a completed run starts fresh, and re-running an
        # interrupted one resumes - see CheckpointStore.start_or_resume.
        cp = context.checkpoint_store
        checkpoint_id = f"{context.adapter_name}:{context.state or 'unscoped'}"
        if cp is not None and cp.start_or_resume(checkpoint_id):
            log.info("Resuming checkpoint %s", checkpoint_id)
        # Only a run with zero failed batches gets marked complete (see the
        # end of this method) - a run that limped through with errors must
        # still look resumable next time, not get its checkpoint state wiped
        # by start_or_resume() treating it as a finished, restartable scope.
        had_errors = False

        if context.state:
            # Two-step scoped flow (see _location_search_url /
            # _practitioner_role_by_location_url): find every Location in
            # the state first, seed the resource cache with them, then batch
            # them into PractitionerRole searches.
            #
            # Location enumeration is resumable at PAGE granularity (see
            # _run_location_enumeration below) - once it completes, the
            # whole-phase "location_enumeration"/"all" item below is what a
            # later, separate run checks first, exactly as before. The
            # per-page checkpoint phases underneath it exist only to survive
            # an interruption DURING this walk; they're cleared once the
            # whole-phase item is written, so they never accumulate. Ordinary
            # PractitionerRole batches remain independently checkpointed per
            # location, unchanged.
            location_ids: list[str] = []
            if cp is not None and cp.status(checkpoint_id, "location_enumeration", "all") == "done":
                payload = cp.get_payload(checkpoint_id, "location_enumeration", "all")
                assert payload is not None
                location_ids = payload["location_ids"]
                for resource in payload["entries"]:
                    resources[("Location", resource["id"])] = resource
                log.info(
                    "Checkpoint hit: location_enumeration already done (%d locations)",
                    len(location_ids),
                )
            else:
                self._run_location_enumeration(context, stats, cp, checkpoint_id, resources, location_ids)
                if cp is not None:
                    final_entries = [resources[("Location", lid)] for lid in location_ids]
                    cp.mark_done(
                        checkpoint_id,
                        "location_enumeration",
                        "all",
                        {"location_ids": location_ids, "entries": final_entries},
                    )
                    # No longer needed once the whole-phase item exists -
                    # drop the per-page bookkeeping so it never accumulates
                    # across repeated runs of the same scope.
                    cp.clear_phase(checkpoint_id, "location_enumeration_pages")
                    cp.clear_phase(checkpoint_id, "location_enumeration_cursor")

            # Collect the batches that still need fetching (skip anything
            # already checkpointed as done) before deciding sequential vs.
            # concurrent - both paths process this same list, so which one
            # runs doesn't change what gets fetched, only how it overlaps.
            # ONE request per location id - not a batch of page_size ids.
            # This server silently honors only one id from a comma-joined
            # list, so batching discarded ~98% of intended coverage with no
            # detectable signal; see _practitioner_role_by_location_url's
            # docstring for the live measurement. The checkpoint phase is
            # named "practitioner_role_by_location" (not the previous
            # "practitioner_role_batches") specifically so that a checkpoint
            # written by the old, batching implementation can never be
            # mistaken for completed work here - its keys counted batches of
            # 50, these count individual locations, and silently reusing
            # them would skip real work.
            pending_batches: list[tuple[str, str]] = []
            for location_index, location_id in enumerate(location_ids):
                batch_key = str(location_index)
                if cp is not None and cp.status(checkpoint_id, "practitioner_role_by_location", batch_key) == "done":
                    payload = cp.get_payload(checkpoint_id, "practitioner_role_by_location", batch_key)
                    assert payload is not None
                    context.apply_role_entries(payload["entries"], resources, roles, stats)
                    continue
                url = _practitioner_role_by_location_url(context.base_url, location_id, context.page_size)
                pending_batches.append((batch_key, url))

            def _handle_batch_result(
                batch_key: str, result: list[dict[str, Any]] | Exception
            ) -> None:
                nonlocal had_errors
                stats.role_queries_attempted += 1
                if isinstance(result, Exception):
                    had_errors = True
                    stats.failed_batches.append((batch_key, repr(result)))
                    if cp is not None:
                        cp.mark_error(checkpoint_id, "practitioner_role_by_location", batch_key, repr(result))
                    log.error(
                        "PractitionerRole batch %s failed - continuing with remaining "
                        "batches (this batch's practitioners will be missing this run): %r",
                        batch_key,
                        result,
                    )
                    return
                if not result:
                    # No PractitionerRole resources for this location - the
                    # `_include`d Practitioner/Organization resources only
                    # ever appear alongside a matched role, so an empty
                    # `result` here always means zero roles, never a role
                    # with unresolved includes. Expected for the majority of
                    # locations (measured ~94% for RI) - not a failure signal
                    # by itself; see DiscoveryStats.suspected_silent_failure.
                    stats.role_queries_empty += 1
                context.apply_role_entries(result, resources, roles, stats)
                if cp is not None:
                    cp.mark_done(checkpoint_id, "practitioner_role_by_location", batch_key, {"entries": result})

            if context.max_concurrent > 1 and pending_batches:
                # Concurrent path: fetch all pending batches together, then
                # apply/checkpoint results one at a time, in pending_batches'
                # order - deterministic regardless of network arrival order.
                batch_results = asyncio.run(context.fetch_batches_concurrently(pending_batches, stats))
                for batch_key, _url in pending_batches:
                    _handle_batch_result(batch_key, batch_results[batch_key])
            else:
                # Sequential path (the default, max_concurrent=1) - byte-for-
                # byte the original behavior: one batch fetched, applied, and
                # checkpointed at a time, before the next one is requested.
                for batch_key, url in pending_batches:
                    try:
                        entries: list[dict[str, Any]] | Exception = context.fetch_role_page_entries(url, stats)
                    except Exception as exc:
                        entries = exc
                    _handle_batch_result(batch_key, entries)
        else:
            if cp is not None and cp.status(checkpoint_id, "practitioner_role_pages", "all") == "done":
                payload = cp.get_payload(checkpoint_id, "practitioner_role_pages", "all")
                assert payload is not None
                context.apply_role_entries(payload["entries"], resources, roles, stats)
            else:
                start_url = _unscoped_search_url(context.base_url, context.page_size)
                entries = context.fetch_role_page_entries(start_url, stats)
                context.apply_role_entries(entries, resources, roles, stats)
                if cp is not None:
                    cp.mark_done(checkpoint_id, "practitioner_role_pages", "all", {"entries": entries})

        # Group every PractitionerRole by its practitioner, across ALL pages,
        # before yielding anything - see the module docstring's "do not trust
        # FHIR page boundaries" note.
        roles_by_practitioner: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for role in roles:
            practitioner_ref = _parse_reference(role.get("practitioner"))
            if practitioner_ref is None:
                log.warning("PractitionerRole %s has no resolvable practitioner reference", role.get("id"))
                continue
            roles_by_practitioner.setdefault(practitioner_ref, []).append(role)

        for practitioner_ref, practitioner_roles in roles_by_practitioner.items():
            practitioner = resources.get(practitioner_ref)
            if practitioner is None:
                stats.reference_misses += 1
                log.warning(
                    "PractitionerRole(s) reference practitioner %s but it was never "
                    "returned by the search - skipping",
                    practitioner_ref,
                )
                continue
            stats.reference_hits += 1

            locations: dict[str, dict[str, Any]] = {}
            organizations: dict[str, dict[str, Any]] = {}
            for role in practitioner_roles:
                for loc_ref in role.get("location", []) or []:
                    parsed = _parse_reference(loc_ref)
                    if not parsed:
                        continue
                    if parsed in resources:
                        stats.reference_hits += 1
                        locations[parsed[1]] = resources[parsed]
                    else:
                        stats.reference_misses += 1
                org_ref = _parse_reference(role.get("organization"))
                if org_ref:
                    if org_ref in resources:
                        stats.reference_hits += 1
                        organizations[org_ref[1]] = resources[org_ref]
                    else:
                        stats.reference_misses += 1

            stats.providers_yielded += 1
            # Discovery-time relationship count, pre-normalization - NOT the
            # same as the final provider_locations row count, which depends
            # on normalize()'s address-based dedup and is a database-layer
            # concern out of scope here (see DiscoveryStats's docstring).
            stats.provider_location_relationships_discovered += len(locations)
            yield {
                "practitioner": practitioner,
                "roles": practitioner_roles,
                "locations": locations,
                "organizations": organizations,
            }

        stats.runtime_seconds = time.monotonic() - started_at
        if cp is not None and not had_errors:
            cp.mark_complete(checkpoint_id)


def _generate_lastupdated_windows(
    window_days: int, lookback_days: int, now: datetime
) -> list[tuple[str | None, str | None]]:
    """Partition time into a finite, ordered set of ``_lastUpdated`` windows
    (oldest first), each a ``(ge, le)`` pair of FHIR date strings (either may
    be ``None`` for an open edge):

    - **First window: open on the low end** (``ge=None``) - catches
      everything older than ``lookback_days``, however old, without having
      to guess or hardcode an earliest possible date Cigna's data might go
      back to.
    - **Middle windows:** fixed ``window_days``-sized ranges walking forward
      from ``now - lookback_days`` to ``now``.
    - **Last window: open on the high end** (``le=None``) - catches
      everything from the final fixed window's edge through "now" and
      beyond, so a resource updated after this function ran (or a small
      clock skew) is never missed.

    Both open edges exist for the same reason: no resource is silently
    dropped because we guessed a bound instead of measuring one. The
    calibration probe (a later stage, not this one) is what turns
    ``window_days``/``lookback_days`` from a documented default into a
    measured, right-sized value - see ``NATIONAL_DISCOVERY_DESIGN.md``'s
    migration step 3.
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")
    if lookback_days <= 0:
        raise ValueError(f"lookback_days must be positive, got {lookback_days}")

    def fmt(dt: datetime) -> str:
        return dt.date().isoformat()

    start = now - timedelta(days=lookback_days)
    windows: list[tuple[str | None, str | None]] = [(None, fmt(start))]

    cursor = start
    while cursor < now:
        next_edge = min(cursor + timedelta(days=window_days), now)
        windows.append((fmt(cursor), fmt(next_edge)))
        cursor = next_edge

    # Re-open the final window's upper edge rather than leaving it at `now`
    # - see the docstring's "last window" note.
    last_ge, _ = windows[-1]
    windows[-1] = (last_ge, None)

    return windows


class NationalDiscovery(DiscoveryStrategy):
    """Unscoped national ``PractitionerRole`` traversal - see
    ``NATIONAL_DISCOVERY_DESIGN.md``'s national sequence diagram. Two
    stages:

    1. **Spool** - fetch every ``_lastUpdated`` window (independent of every
       other window, so this reuses the exact same concurrency/rate-limit/
       retry machinery :class:`StateScopedDiscovery` already uses for its
       batch phase - a "window" is just another kind of "batch" from
       ``fetch_batches_concurrently``'s perspective), writing every raw
       resource straight to ``context.resource_spool`` instead of an
       in-memory dict. This is what keeps memory at O(one page) regardless
       of how many practitioners exist nationally.
    2. **Materialize** - group by practitioner from the spool (an indexed
       lookup per practitioner, not a full scan) and yield the same
       per-practitioner dict shape :class:`StateScopedDiscovery` does. No
       locations are dropped here (finding 1 in
       ``evidence/PHASE_E_CAPABILITY_PROBES.md``: the national query
       natively ``_include``s every location a role references), which is
       the whole reason this strategy's output is a superset of the
       state-scoped one, never a truncated subset.

    Checkpointing is lighter than :class:`StateScopedDiscovery`'s: the spool
    itself is the durable record of what was fetched, so a window's
    checkpoint payload is empty - resuming only needs to know a window is
    ``"done"``, not replay its contents.
    """

    def run(self, context: DiscoveryContext) -> Iterator[dict[str, Any]]:
        if context.resource_spool is None:
            raise ValueError(
                "NationalDiscovery requires a resource_spool - set "
                "providers.resource_spool_path in config before selecting "
                "cigna_fhir_discovery_mode: national."
            )
        spool = context.resource_spool
        stats = context.stats
        started_at = time.monotonic()

        cp = context.checkpoint_store
        checkpoint_id = f"{context.adapter_name}:national"
        if cp is not None and cp.start_or_resume(checkpoint_id):
            log.info("Resuming checkpoint %s", checkpoint_id)
        had_errors = False

        # --- Stage 1: spool -------------------------------------------- #
        windows = _generate_lastupdated_windows(
            context.window_days, context.lookback_days, datetime.now(timezone.utc)
        )

        pending_windows: list[tuple[str, str]] = []
        for window_index, (ge, le) in enumerate(windows):
            window_key = str(window_index)
            if cp is not None and cp.status(checkpoint_id, "national_windows", window_key) == "done":
                continue
            url = _national_window_url(context.base_url, ge, le, context.page_size)
            pending_windows.append((window_key, url))

        def _spool_entries(entries: list[dict[str, Any]]) -> None:
            for resource in entries:
                resource_type = resource.get("resourceType")
                resource_id = resource.get("id")
                if not resource_type or not resource_id:
                    log.warning("Skipping malformed spooled entry with no type/id: %r", resource)
                    continue
                is_duplicate = spool.get(resource_type, resource_id) is not None
                stats.record_resource(resource_type, is_duplicate=is_duplicate)
                practitioner_ref: str | None = None
                if resource_type == "PractitionerRole":
                    parsed = _parse_reference(resource.get("practitioner"))
                    practitioner_ref = parsed[1] if parsed else None
                spool.upsert(resource_type, resource_id, resource, practitioner_ref=practitioner_ref)

        def _handle_window_result(window_key: str, result: list[dict[str, Any]] | Exception) -> None:
            nonlocal had_errors
            if isinstance(result, Exception):
                had_errors = True
                stats.failed_batches.append((window_key, repr(result)))
                if cp is not None:
                    cp.mark_error(checkpoint_id, "national_windows", window_key, repr(result))
                log.error(
                    "National window %s failed - continuing with remaining windows "
                    "(practitioners updated only in this window will be missing this run): %r",
                    window_key,
                    result,
                )
                return
            _spool_entries(result)
            if cp is not None:
                cp.mark_done(checkpoint_id, "national_windows", window_key, {})

        if context.max_concurrent > 1 and pending_windows:
            window_results = asyncio.run(context.fetch_batches_concurrently(pending_windows, stats))
            for window_key, _url in pending_windows:
                _handle_window_result(window_key, window_results[window_key])
        else:
            for window_key, url in pending_windows:
                try:
                    entries: list[dict[str, Any]] | Exception = context.fetch_role_page_entries(url, stats)
                except Exception as exc:
                    entries = exc
                _handle_window_result(window_key, entries)

        # --- Stage 2: materialize ---------------------------------------- #
        # O(one practitioner) memory: distinct_practitioner_refs() is bounded
        # by the number of practitioners, not the number of spooled
        # resources, and each iteration below only ever holds one
        # practitioner's own roles/locations/organizations at a time.
        for practitioner_ref in spool.distinct_practitioner_refs():
            practitioner = spool.get("Practitioner", practitioner_ref)
            if practitioner is None:
                stats.reference_misses += 1
                log.warning(
                    "PractitionerRole(s) reference practitioner %s but it was never "
                    "spooled - skipping",
                    practitioner_ref,
                )
                continue
            stats.reference_hits += 1

            practitioner_roles = spool.roles_for_practitioner(practitioner_ref)
            locations: dict[str, dict[str, Any]] = {}
            organizations: dict[str, dict[str, Any]] = {}
            for role in practitioner_roles:
                for loc_ref in role.get("location", []) or []:
                    parsed = _parse_reference(loc_ref)
                    if not parsed:
                        continue
                    loc = spool.get(*parsed)
                    if loc is not None:
                        stats.reference_hits += 1
                        locations[parsed[1]] = loc
                    else:
                        stats.reference_misses += 1
                org_ref = _parse_reference(role.get("organization"))
                if org_ref:
                    org = spool.get(*org_ref)
                    if org is not None:
                        stats.reference_hits += 1
                        organizations[org_ref[1]] = org
                    else:
                        stats.reference_misses += 1

            stats.providers_yielded += 1
            yield {
                "practitioner": practitioner,
                "roles": practitioner_roles,
                "locations": locations,
                "organizations": organizations,
            }

        stats.runtime_seconds = time.monotonic() - started_at
        if cp is not None and not had_errors:
            cp.mark_complete(checkpoint_id)


def get_discovery_strategy(mode: str) -> DiscoveryStrategy:
    """Look up a discovery strategy by ``config.providers.cigna_fhir_discovery_mode``.

    Mirrors the ``get_*_adapter_class`` lookup convention in
    ``adapters/__init__.py`` - ``ValueError`` listing the available names
    rather than a bare ``KeyError``.
    """
    strategies: dict[str, DiscoveryStrategy] = {
        "state_scoped": StateScopedDiscovery(),
        "national": NationalDiscovery(),
    }
    try:
        return strategies[mode]
    except KeyError:
        available = ", ".join(sorted(strategies)) or "(none registered)"
        raise ValueError(f"Unknown discovery mode '{mode}'. Available: {available}") from None
