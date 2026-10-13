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
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from providermap.checkpoint import CheckpointStore
from providermap.resource_spool import ResourceSpool

from .adapter import (
    DiscoveryStats,
    SilentTotalZeroError,
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
#: 4th param is ``collect`` - ``False`` makes the walk genuinely streaming
#: (pages go to the callback and are dropped; the return value is empty).
#: Spool mode MUST pass ``False`` or the internal list re-materializes the
#: whole state in memory anyway - see the adapter method's docstring.
FetchLocationPageEntries = Callable[
    [str | None, DiscoveryStats, Callable[[list[dict[str, Any]], str | None], None] | None, bool],
    list[dict[str, Any]],
]
ApplyLocationEntries = Callable[
    [list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]], list[str], DiscoveryStats], None
]
ApplyRoleEntries = Callable[
    [
        list[dict[str, Any]],
        dict[tuple[str, str], dict[str, Any]],
        list[dict[str, Any]],
        DiscoveryStats,
    ],
    None,
]
#: Yields (batch_key, entries_or_exception) as each becomes available, in
#: `pending`'s order - not a dict returned only once everything finishes.
#: See CignaFhirAdapter._fetch_batches_concurrently's docstring for why:
#: materializing a dict of ~42,000 results before consuming any of them
#: measurably contributed to a 5.61 GB peak RSS on the smallest state
#: tested.
FetchBatchesConcurrently = Callable[
    [list[tuple[str, str]], DiscoveryStats],
    AsyncIterator[tuple[str, list[dict[str, Any]] | Exception]],
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

    #: Used by :class:`NationalDiscovery` (always) and by
    #: :class:`StateScopedDiscovery` when ``state_scoped_spool`` is set -
    #: ``None`` otherwise. ``window_days``/``lookback_days`` remain
    #: national-only.
    resource_spool: ResourceSpool | None = None
    window_days: int = 30
    lookback_days: int = 3650
    #: Route state-scoped discovery through the spool (see
    #: ``config.providers.cigna_fhir_state_scoped_spool``). ``False`` (the
    #: default) is the original in-memory behavior, byte for byte.
    state_scoped_spool: bool = False


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
        returns HTTP 200 with the confirmed total:0 failure signature,
        which the retry wrappers now surface as
        :class:`~adapters.providers.cigna_fhir.adapter.SilentTotalZeroError`
        after exhausting retries (retrying a truly-stale cursor is futile
        but cheap, and keeps one uniform total:0 rule for every fetch
        path). On a RESUMED walk that error means "stale cursor": the
        partial progress is discarded, the total:0 count is reclassified
        into ``stats.stale_cursor_restarts`` (a recovered restart is not a
        silent failure - without reclassification the completeness gate
        would fail a healthy run), and the walk restarts cleanly from the
        beginning. On a FRESH walk the same error is a genuine silent
        failure and propagates - failing the run loudly rather than
        recording an undercounted enumeration as complete.
        """
        if context.state is None:
            raise ValueError("state-scoped discovery requires context.state")
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

        try:
            new_entries = context.fetch_location_page_entries(resume_url, stats, _on_page, True)
        except SilentTotalZeroError:
            if not resuming:
                # Fresh walk: a persistent total:0 page is a genuine silent
                # failure - propagate so the run fails loudly (per-page
                # checkpoints written so far still allow a cursor resume).
                raise
            log.warning(
                "Resumed location-enumeration cursor was stale (total:0 "
                "signature persisted through retries) - discarding saved "
                "progress and restarting the walk from scratch."
            )
            # Reclassify: a stale cursor that a restart recovers is NOT a
            # silent failure. The exhaustion counter was incremented by the
            # retry wrapper before raising; move that count into
            # stale_cursor_restarts so suspected_silent_failure (and the
            # CLI's completeness gate) doesn't fail a healthy run. The key
            # is deleted at zero because suspected_silent_failure checks
            # dict truthiness, and {"Location": 0} is a non-empty dict.
            remaining = stats.bundles_with_total_zero_by_endpoint.get("Location", 0) - 1
            if remaining > 0:
                stats.bundles_with_total_zero_by_endpoint["Location"] = remaining
            else:
                stats.bundles_with_total_zero_by_endpoint.pop("Location", None)
            stats.stale_cursor_restarts += 1
            if cp is not None:
                cp.clear_phase(checkpoint_id, "location_enumeration_pages")
                cp.clear_phase(checkpoint_id, "location_enumeration_cursor")
            # Bounded: the recursed call finds no saved cursor (cleared
            # above), so resuming=False - a second persistent total:0
            # propagates as a genuine failure instead of looping.
            self._run_location_enumeration(
                context, stats, cp, checkpoint_id, resources, location_ids
            )
            return

        context.apply_location_entries(
            reloaded_entries + new_entries, resources, location_ids, stats
        )

    def _run_location_enumeration_spool(
        self,
        context: DiscoveryContext,
        stats: DiscoveryStats,
        cp: CheckpointStore | None,
        checkpoint_id: str,
        spool: ResourceSpool,
        location_ids: list[str],
    ) -> None:
        """Spool-mode counterpart to :meth:`_run_location_enumeration`: pages
        stream into ``spool`` via ``on_page`` with ``collect=False`` - no
        whole-state list of Location BODIES ever exists in memory - and
        every checkpoint payload here holds only the page's location IDS,
        never bodies. Resumability, stale-cursor detection (via
        :class:`~adapters.providers.cigna_fhir.adapter.SilentTotalZeroError`),
        and the recursive-restart bound are otherwise identical to the
        legacy method - see its docstring for the underlying cursor-TTL
        rationale; only the payload shape differs.
        """
        if context.state is None:
            raise ValueError("state-scoped discovery requires context.state")
        resume_url = _location_search_url(context.base_url, context.state, context.page_size)
        reloaded_ids: list[str] = []
        next_page_index = 0
        resuming = False

        if cp is not None:
            cursor_payload = cp.get_payload(
                checkpoint_id, "location_enumeration_spool_cursor", "current"
            )
            if cursor_payload is not None and cursor_payload.get("next_url"):
                for _key, page_payload in sorted(
                    cp.done_payloads(checkpoint_id, "location_enumeration_spool_pages").items(),
                    key=lambda kv: int(kv[0]),
                ):
                    reloaded_ids.extend(page_payload["location_ids"])
                resume_url = cursor_payload["next_url"]
                next_page_index = cursor_payload["next_page_index"]
                resuming = True
                log.info(
                    "Resuming spool-mode location enumeration from a saved cursor "
                    "(%d location ids already checkpointed from before the interruption)",
                    len(reloaded_ids),
                )

        next_index = [next_page_index]
        new_ids: list[str] = []

        def _on_page(page_entries: list[dict[str, Any]], next_url: str | None) -> None:
            _spool_entries(spool, page_entries, stats)
            page_ids = [e["id"] for e in page_entries if e.get("id")]
            new_ids.extend(page_ids)
            if cp is not None:
                cp.mark_done(
                    checkpoint_id,
                    "location_enumeration_spool_pages",
                    str(next_index[0]),
                    {"location_ids": page_ids},
                )
                # Durability ordering (see resource_spool.py's module
                # docstring): this page's bodies must reach disk before the
                # cursor marker that claims the page is done - the reverse
                # order would let a crash produce a marker whose bodies
                # never landed, and resume would silently skip them.
                spool.flush()
                cp.mark_done(
                    checkpoint_id,
                    "location_enumeration_spool_cursor",
                    "current",
                    {"next_url": next_url, "next_page_index": next_index[0] + 1},
                )
            next_index[0] += 1

        try:
            context.fetch_location_page_entries(resume_url, stats, _on_page, False)
        except SilentTotalZeroError:
            if not resuming:
                raise
            log.warning(
                "Resumed spool-mode location-enumeration cursor was stale "
                "(total:0 signature persisted through retries) - clearing "
                "the spool and restarting the walk from scratch."
            )
            remaining = stats.bundles_with_total_zero_by_endpoint.get("Location", 0) - 1
            if remaining > 0:
                stats.bundles_with_total_zero_by_endpoint["Location"] = remaining
            else:
                stats.bundles_with_total_zero_by_endpoint.pop("Location", None)
            stats.stale_cursor_restarts += 1
            if cp is not None:
                cp.clear_phase(checkpoint_id, "location_enumeration_spool_pages")
                cp.clear_phase(checkpoint_id, "location_enumeration_spool_cursor")
            # The doomed resume may have already spooled some of this
            # walk's locations before hitting the stale page - discard them
            # along with the checkpoint phases so the restart's fresh walk
            # (resuming=False on the recursed call) starts from a clean
            # slate, not a partially-populated one.
            spool.clear()
            self._run_location_enumeration_spool(
                context, stats, cp, checkpoint_id, spool, location_ids
            )
            return

        location_ids.extend(reloaded_ids + new_ids)

    def _run_spool(self, context: DiscoveryContext) -> Iterator[dict[str, Any]]:
        """Spool-backed counterpart to :meth:`run`: same observable
        contract (yielded dict shape, ``DiscoveryStats`` semantics,
        resumability guarantees) as the legacy in-memory path, but every
        fetched resource is written to ``context.resource_spool`` instead
        of an in-memory dict - memory stays O(one practitioner) regardless
        of state size, mirroring :class:`NationalDiscovery`'s
        spool-then-materialize pattern via the shared
        ``_spool_entries``/``_materialize_from_spool`` helpers.
        """
        stats = context.stats
        started_at = time.monotonic()
        spool = context.resource_spool
        if spool is None:
            raise ValueError(
                "cigna_fhir_state_scoped_spool requires providers.resource_spool_path "
                "to be set (same requirement national mode already has)."
            )

        cp = context.checkpoint_store
        # Distinct checkpoint id (":spool" suffix) - a flag flip mid-run is
        # always a clean fresh start in either direction, never a misread
        # of the other mode's checkpoint state or payload shapes.
        checkpoint_id = f"{context.adapter_name}:{context.state or 'unscoped'}:spool"
        if cp is not None and cp.start_or_resume(checkpoint_id):
            log.info("Resuming checkpoint %s", checkpoint_id)
        else:
            # Fresh start: the spool persists across runs so RESUMES can
            # reuse it, but rows left by a previous run/mode/scope must
            # never leak into this run's materialize pass (they'd
            # resurrect server-deleted resources and inflate yields).
            spool.clear()
        had_errors = False

        if context.state:
            location_ids: list[str] = []
            if (
                cp is not None
                and cp.status(checkpoint_id, "location_enumeration_spool", "all") == "done"
            ):
                payload = cp.get_payload(checkpoint_id, "location_enumeration_spool", "all")
                assert payload is not None
                location_ids = payload["location_ids"]
                log.info(
                    "Checkpoint hit: spool-mode location_enumeration already done "
                    "(%d locations)",
                    len(location_ids),
                )
            else:
                self._run_location_enumeration_spool(
                    context, stats, cp, checkpoint_id, spool, location_ids
                )
                if cp is not None:
                    cp.mark_done(
                        checkpoint_id,
                        "location_enumeration_spool",
                        "all",
                        {"location_ids": location_ids},
                    )
                    cp.clear_phase(checkpoint_id, "location_enumeration_spool_pages")
                    cp.clear_phase(checkpoint_id, "location_enumeration_spool_cursor")

            pending_batches: list[tuple[str, str]] = []
            for location_id in location_ids:
                # Keyed by LOCATION ID, not positional index: the spool
                # already durably holds a done batch's data, so - unlike
                # the legacy phase, which replays stored entries through
                # apply_role_entries - resuming needs no replay at all.
                if (
                    cp is not None
                    and cp.status(checkpoint_id, "role_by_location_spool", location_id) == "done"
                ):
                    continue
                url = _practitioner_role_by_location_url(
                    context.base_url, location_id, context.page_size
                )
                pending_batches.append((location_id, url))

            def _handle_batch_result(
                batch_key: str, result: list[dict[str, Any]] | Exception
            ) -> None:
                nonlocal had_errors
                stats.role_queries_attempted += 1
                if isinstance(result, Exception):
                    had_errors = True
                    stats.failed_batches.append((batch_key, repr(result)))
                    if cp is not None:
                        cp.mark_error(
                            checkpoint_id, "role_by_location_spool", batch_key, repr(result)
                        )
                    log.error(
                        "PractitionerRole batch %s failed - continuing with remaining "
                        "batches (this location's practitioners will be missing this "
                        "run): %r",
                        batch_key,
                        result,
                    )
                    return
                if not result:
                    stats.role_queries_empty += 1
                _spool_entries(spool, result, stats)
                if cp is not None:
                    # Durability ordering, same rule as location
                    # enumeration above: spool before marker.
                    spool.flush()
                    cp.mark_done(checkpoint_id, "role_by_location_spool", batch_key, {})

            if context.max_concurrent > 1 and pending_batches:

                async def _consume_batches() -> None:
                    async for batch_key, result in context.fetch_batches_concurrently(
                        pending_batches, stats
                    ):
                        _handle_batch_result(batch_key, result)

                asyncio.run(_consume_batches())
            else:
                for batch_key, url in pending_batches:
                    try:
                        entries: list[dict[str, Any]] | Exception = context.fetch_role_page_entries(
                            url, stats
                        )
                    except Exception as exc:
                        entries = exc
                    _handle_batch_result(batch_key, entries)
        else:
            if (
                cp is not None
                and cp.status(checkpoint_id, "practitioner_role_pages_spool", "all") == "done"
            ):
                pass  # already spooled by a prior run of this same scope
            else:
                start_url = _unscoped_search_url(context.base_url, context.page_size)
                try:
                    entries = context.fetch_role_page_entries(start_url, stats)
                except Exception as exc:  # noqa: BLE001
                    had_errors = True
                    stats.failed_batches.append(("unscoped", repr(exc)))
                    log.error(
                        "Unscoped PractitionerRole walk failed - this run "
                        "discovered no providers and will not be marked "
                        "complete: %r",
                        exc,
                    )
                else:
                    _spool_entries(spool, entries, stats)
                    if cp is not None:
                        spool.flush()
                        cp.mark_done(checkpoint_id, "practitioner_role_pages_spool", "all", {})

        yield from _materialize_from_spool(spool, stats, count_relationships=True)

        stats.runtime_seconds = time.monotonic() - started_at
        if cp is not None and not had_errors:
            cp.mark_complete(checkpoint_id)

    def run(self, context: DiscoveryContext) -> Iterator[dict[str, Any]]:
        # Spool mode is a separate, flag-gated implementation (see
        # _run_spool) so the legacy in-memory path below stays byte-for-
        # byte what it was - the existing suite is the regression guard
        # for flag-off behavior.
        if context.state_scoped_spool:
            yield from self._run_spool(context)
            return

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
                self._run_location_enumeration(
                    context, stats, cp, checkpoint_id, resources, location_ids
                )
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
                if (
                    cp is not None
                    and cp.status(checkpoint_id, "practitioner_role_by_location", batch_key)
                    == "done"
                ):
                    payload = cp.get_payload(
                        checkpoint_id, "practitioner_role_by_location", batch_key
                    )
                    assert payload is not None
                    context.apply_role_entries(payload["entries"], resources, roles, stats)
                    continue
                url = _practitioner_role_by_location_url(
                    context.base_url, location_id, context.page_size
                )
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
                        cp.mark_error(
                            checkpoint_id, "practitioner_role_by_location", batch_key, repr(result)
                        )
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
                    # with unresolved includes. A nonzero count here is
                    # expected and not a failure signal by itself - see
                    # DiscoveryStats.role_queries_empty's docstring for why
                    # the exact rate isn't a stable constant to assert
                    # against (it's measured to vary significantly over
                    # time on this live, continuously-updated third-party
                    # dataset). See DiscoveryStats.suspected_silent_failure
                    # for the actual failure signal.
                    stats.role_queries_empty += 1
                context.apply_role_entries(result, resources, roles, stats)
                if cp is not None:
                    cp.mark_done(
                        checkpoint_id,
                        "practitioner_role_by_location",
                        batch_key,
                        {"entries": result},
                    )

            if context.max_concurrent > 1 and pending_batches:
                # Concurrent path: apply/checkpoint each batch's result as
                # SOON AS IT ARRIVES, in pending_batches' order (still
                # deterministic regardless of network arrival order - see
                # fetch_batches_concurrently's docstring), rather than
                # waiting for every one of ~42,000 requests to finish
                # before applying or checkpointing any of them.
                async def _consume_batches() -> None:
                    async for batch_key, result in context.fetch_batches_concurrently(
                        pending_batches, stats
                    ):
                        _handle_batch_result(batch_key, result)

                asyncio.run(_consume_batches())
            else:
                # Sequential path (the default, max_concurrent=1) - byte-for-
                # byte the original behavior: one batch fetched, applied, and
                # checkpointed at a time, before the next one is requested.
                for batch_key, url in pending_batches:
                    try:
                        entries: list[dict[str, Any]] | Exception = context.fetch_role_page_entries(
                            url, stats
                        )
                    except Exception as exc:
                        entries = exc
                    _handle_batch_result(batch_key, entries)
        else:
            if (
                cp is not None
                and cp.status(checkpoint_id, "practitioner_role_pages", "all") == "done"
            ):
                payload = cp.get_payload(checkpoint_id, "practitioner_role_pages", "all")
                assert payload is not None
                context.apply_role_entries(payload["entries"], resources, roles, stats)
            else:
                start_url = _unscoped_search_url(context.base_url, context.page_size)
                # Same containment the per-location batch paths get from
                # _handle_batch_result: a failed unscoped walk (silent
                # total:0 or otherwise) is recorded as an error and blocks
                # mark_complete, instead of propagating a raw traceback out
                # of the generator with the run row left unfinished.
                try:
                    entries = context.fetch_role_page_entries(start_url, stats)
                except Exception as exc:  # noqa: BLE001
                    had_errors = True
                    stats.failed_batches.append(("unscoped", repr(exc)))
                    log.error(
                        "Unscoped PractitionerRole walk failed - this run "
                        "discovered no providers and will not be marked "
                        "complete: %r",
                        exc,
                    )
                else:
                    context.apply_role_entries(entries, resources, roles, stats)
                    if cp is not None:
                        cp.mark_done(
                            checkpoint_id, "practitioner_role_pages", "all", {"entries": entries}
                        )

        # Group every PractitionerRole by its practitioner, across ALL pages,
        # before yielding anything - see the module docstring's "do not trust
        # FHIR page boundaries" note.
        roles_by_practitioner: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for role in roles:
            practitioner_ref = _parse_reference(role.get("practitioner"))
            if practitioner_ref is None:
                log.warning(
                    "PractitionerRole %s has no resolvable practitioner reference", role.get("id")
                )
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


def _spool_entries(
    spool: ResourceSpool, entries: list[dict[str, Any]], stats: DiscoveryStats
) -> None:
    """Write one batch/window's fetched resources into the spool, computing
    the ``practitioner_ref`` index column and duplicate stats along the way.

    Module-level (not a closure) because it is the ONE spool write path,
    shared by :class:`NationalDiscovery`'s window handler and the
    spool-backed state-scoped batch path - the practitioner_ref extraction
    and duplicate accounting must never drift between the two. Uses
    ``exists()`` rather than ``get()`` for the duplicate check: this runs
    once per fetched resource (~80k+ per state), and deserializing a full
    FHIR body just to discard it is pure read amplification."""
    for resource in entries:
        resource_type = resource.get("resourceType")
        resource_id = resource.get("id")
        if not resource_type or not resource_id:
            log.warning("Skipping malformed spooled entry with no type/id: %r", resource)
            continue
        stats.record_resource(resource_type, is_duplicate=spool.exists(resource_type, resource_id))
        practitioner_ref: str | None = None
        if resource_type == "PractitionerRole":
            parsed = _parse_reference(resource.get("practitioner"))
            practitioner_ref = parsed[1] if parsed else None
        spool.upsert(resource_type, resource_id, resource, practitioner_ref=practitioner_ref)


def _materialize_from_spool(
    spool: ResourceSpool, stats: DiscoveryStats, *, count_relationships: bool
) -> Iterator[dict[str, Any]]:
    """Yield one complete per-practitioner record per distinct spooled
    practitioner, dereferencing locations/organizations from the spool -
    O(one practitioner) memory regardless of how many resources are
    spooled, since ``distinct_practitioner_refs()``/``roles_for_practitioner()``
    are both bounded and ordered (deterministic emission, see
    ResourceSpool's docstrings).

    Shared by :class:`NationalDiscovery` and :class:`StateScopedDiscovery`'s
    spool-mode path so the dereference/stats logic never drifts between
    them. ``count_relationships`` is False for national (its stats never
    tracked ``provider_location_relationships_discovered`` - preserving that
    exactly) and True for state-scoped spool mode, to match the legacy
    in-memory path's stat.
    """
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
        if count_relationships:
            stats.provider_location_relationships_discovered += len(locations)
        yield {
            "practitioner": practitioner,
            "roles": practitioner_roles,
            "locations": locations,
            "organizations": organizations,
        }


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
        else:
            # FRESH start: the spool file may hold rows from a previous
            # run, mode, or scope (it persists across runs by design so
            # RESUMES can reuse it). Left in place, stale rows would leak
            # into this run's materialize pass - resurrecting resources the
            # server has since deleted and inflating yields/counts. Only a
            # genuine resume may trust existing spool contents.
            spool.clear()
        had_errors = False

        # --- Stage 1: spool -------------------------------------------- #
        windows = _generate_lastupdated_windows(
            context.window_days, context.lookback_days, datetime.now(timezone.utc)
        )

        pending_windows: list[tuple[str, str]] = []
        for window_index, (ge, le) in enumerate(windows):
            window_key = str(window_index)
            if (
                cp is not None
                and cp.status(checkpoint_id, "national_windows", window_key) == "done"
            ):
                continue
            url = _national_window_url(context.base_url, ge, le, context.page_size)
            pending_windows.append((window_key, url))

        def _handle_window_result(
            window_key: str, result: list[dict[str, Any]] | Exception
        ) -> None:
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
            _spool_entries(spool, result, stats)
            if cp is not None:
                # Durability ordering (see resource_spool.py's module
                # docstring): this window's checkpoint payload is EMPTY -
                # the spool is the only durable record of its data - so the
                # spooled rows must reach disk before the "done" marker
                # does. The reverse order would let a crash produce a
                # marker whose data never landed, and resume would skip the
                # window forever.
                spool.flush()
                cp.mark_done(checkpoint_id, "national_windows", window_key, {})

        if context.max_concurrent > 1 and pending_windows:
            # Same streaming-consumption change as StateScopedDiscovery's
            # batch phase - see fetch_batches_concurrently's docstring.
            async def _consume_windows() -> None:
                async for window_key, result in context.fetch_batches_concurrently(
                    pending_windows, stats
                ):
                    _handle_window_result(window_key, result)

            asyncio.run(_consume_windows())
        else:
            for window_key, url in pending_windows:
                try:
                    entries: list[dict[str, Any]] | Exception = context.fetch_role_page_entries(
                        url, stats
                    )
                except Exception as exc:
                    entries = exc
                _handle_window_result(window_key, entries)

        # --- Stage 2: materialize ---------------------------------------- #
        yield from _materialize_from_spool(spool, stats, count_relationships=False)

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
