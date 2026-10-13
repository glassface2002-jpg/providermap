# Cigna FHIR adapter — concurrent fetching design

## Status: implemented (Phase D.3)

This design was approved and then implemented as described, with one
deliberate deviation from the illustrative config example below:
`cigna_fhir_max_concurrent_requests` **defaults to `1`** (fully sequential -
the original behavior, byte-for-byte), not `10`. "Preserve all existing
adapter interfaces and observable behavior" was a hard constraint on this
phase; defaulting to `1` means every existing run, test, and pilot report is
completely unaffected unless someone explicitly opts into a higher value.
Everything else below - scope (PractitionerRole batches only), the
`asyncio.run()`-at-the-boundary approach, the semaphore + token-bucket
combination, retry/backoff reusing `RetryConfig`, and the determinism/error-
aggregation reasoning - was implemented exactly as designed. See
`adapters/providers/cigna_fhir/adapter.py`'s `DiscoveryStats`,
`_RateLimiter`, `_fetch_with_retry`, and `_fetch_batches_concurrently`, and
`tests/test_cigna_fhir_adapter.py::TestConcurrency` for the concurrency-on-
vs-off output-identity regression test and the retry/limit tests.

## Problem, restated from measured evidence

The Rhode Island pilot (real, live, two runs) measured:

- 1,686 total HTTP requests: 843 `Location` (sequential pagination) + 843
  `PractitionerRole` (batch searches).
- Average request latency: 1.740s.
- Total runtime: ~2,935s, of which `1,686 × 1.74s ≈ 2,934s` — **essentially
  100% of wall-clock time is spent waiting on network round trips**, not
  local computation.
- The 843 `PractitionerRole` batch requests are **mutually independent** -
  each is `PractitionerRole?location=<50 bare ids>&...`, unrelated to every
  other batch's contents. Nothing in `extract()`/`normalize()`/the
  per-practitioner grouping step depends on which batch a resource came from
  or the order batches were fetched in - `discover()` already drains
  everything into `resources`/`roles` *before* grouping (the existing "do not
  trust FHIR page boundaries" design). That property is what makes
  concurrency safe to introduce here without touching grouping, extraction,
  or normalization at all.
- The 843 `Location` requests are **not** independent - each page's URL comes
  from the previous page's opaque `Bundle.link[relation=next]` token
  (confirmed live: Cigna uses a `_getpages=<uuid>` continuation token, not an
  offset/page-number parameter), so this phase cannot be parallelized without
  the server supporting a different pagination style. It stays sequential.

So the scope of this design is narrow and specific: **concurrent fetching of
the `PractitionerRole` batch phase only.** The `Location` phase is unchanged.

## Architectural approach: `asyncio.run()` at the boundary, not a fully async adapter

Two ways to get real concurrent I/O into `discover()`:

1. **Make `discover()` (and everything above it) async.** More idiomatic
   long-term - the scraped-website pipeline (`AsyncFetcher`, `cmd_scrape` via
   `asyncio.run(_run_pipeline(...))`) already works this way. But it's a
   larger surface change: `ProviderDatasetAdapter.discover()`'s contract
   would change for every current and future dataset adapter, and
   `cmd_ingest_providers` (currently a plain sync function, unlike
   `cmd_enrich_organizations`/`cmd_scrape` which are already `async def`)
   would need to become async too.
2. **Keep `discover()` a plain sync generator; run one `asyncio.run(...)`
   internally, only around the `PractitionerRole` batch phase.** Everything
   outside `CignaFhirAdapter` - the `ProviderDatasetAdapter` contract,
   `cmd_ingest_providers`'s `for raw in adapter.discover():` loop, every
   existing test - stays exactly as it is today. Concurrency is an
   implementation detail of one method.

**Recommendation: option 2.** It's strictly additive (satisfies "preserve all
existing adapter interfaces and observable behavior"), and the FHIR-batch
phase is the only part of the whole two-step flow that benefits from it. If a
future maintainer later decides the entire provider-dataset-adapter layer
should be async (e.g. because a second dataset adapter has a much slower,
more concurrency-hungry server), that's a bigger, separate decision - this
design doesn't foreclose it, it just doesn't force it now.

Concretely: `_walk_practitioner_role_pages` (currently sync, called once per
batch in a loop) gets a concurrent sibling used only when
`cigna_fhir_max_concurrent_requests > 1`; the existing sync path remains the
fallback/default-off behavior and what every current offline test exercises
unchanged.

## Fetcher contract: a new async path alongside the existing sync one

`PageFetcher = Callable[[str], dict[str, Any]]` stays exactly as it is - it's
what the `Location` phase uses, what every existing offline test
(`make_fetcher`) constructs, and what `CignaFhirAdapter.__init__`'s
`page_fetcher` parameter still defaults to.

A new, separate type is added for the concurrent phase only:

```python
AsyncPageFetcher = Callable[[str], Awaitable[dict[str, Any]]]
```

`CignaFhirAdapter.__init__` gains an optional `async_page_fetcher` parameter,
defaulting to an `httpx.AsyncClient`-backed implementation (mirroring
`providermap.net.AsyncFetcher`'s existing pattern, not inventing a new one).
Tests inject a fake async fetcher the same way `make_fetcher` fakes the sync
one today - no live network in offline tests, same as now.

## Maximum concurrency limits

New config field, parallel to `politeness.max_concurrent` (which already
exists for the scraped-website pipeline):

```yaml
providers:
  cigna_fhir_max_concurrent_requests: 10   # default; 1 = fully sequential (today's behavior)
```

Enforced with an `asyncio.Semaphore(max_concurrent_requests)` around each
batch fetch. `1` is a legitimate value and reproduces exactly today's
sequential behavior - this is how the feature stays off by default until
someone opts in, and how a regression test proves "concurrency enabled with
max=1 produces byte-identical output to the old sequential code path."

## Configurable throttling

Concurrency alone doesn't bound *rate* if individual requests are fast - a
second, independent knob is needed, matching the existing
`website_enrichment_requests_per_second` / `politeness.requests_per_second`
convention (every other multi-request ProviderMap source already has its own
requests-per-second budget, separate from the scraped-directory one):

```yaml
providers:
  cigna_fhir_requests_per_second: 5.0
```

Implemented as a simple token-bucket shared across the whole batch phase (one
bucket, not one per worker) so `max_concurrent_requests` bounds *how many
requests can be in flight at once* and `requests_per_second` bounds *how fast
new ones start* - the two knobs answer different questions and both are
needed under real server behavior (a server can rate-limit on requests/sec
even when concurrency is low, and vice versa).

## Rate-limit handling

On an HTTP 429: read `Retry-After` if the server sends it and sleep exactly
that long before the retry; if absent, fall back to the existing
exponential-backoff schedule (see below). A run of 429s across *multiple
different batches* in a short window (not just one batch retrying) pauses the
whole token bucket - i.e. temporarily drops the effective rate toward zero -
rather than only backing off the one request that got rate-limited, since a
429 is the server telling the whole client to slow down, not just one
in-flight request.

## Retry / backoff strategy

Reuse the existing `RetryConfig` dataclass and its fields
(`max_attempts`, `backoff_base_seconds`, `backoff_max_seconds`,
`retry_on_status`) rather than inventing Cigna-specific retry semantics - the
Phase B pilot had **zero errors and zero need for retries**, so there's no
evidence yet that Cigna needs different tuning than what `config.retries`
already expresses for the scraped-website pipeline. Only retry on
`retry_on_status` (429/500/502/503/504); a 400 (e.g. a malformed batch query)
is never retried since retrying won't fix a bad request. If a batch
permanently exhausts `max_attempts`, it becomes a **failed batch** (see error
aggregation below), not a fatal error for the whole run.

## Request ordering guarantees

**None required, and this is by design, not a gap.** `discover()` already
buffers every resource from every request into `resources`/`roles` *before*
grouping by practitioner (the existing "do not trust FHIR page boundaries"
architecture) - nothing downstream cares which batch's response arrived
first, second, or last. Concurrency changes *when* responses arrive, never
*what* the final `resources`/`roles` collections contain.

## Deterministic output regardless of execution order

Two places order could theoretically matter, both already safe or made
explicitly safe by this design:

- **Resource caching** (`resources[(resourceType, id)] = resource`): if two
  concurrent batches both return the same resource id (measured: this
  happens constantly for `Organization`, 79% duplicate rate), the rule is
  **first-write-wins, matching values assumed identical** - the same FHIR
  resource id from the same server should be byte-identical regardless of
  which batch's response introduced it first, so it does not matter *which*
  concurrent response's copy ends up cached; only *that* one does. The
  existing `DiscoveryStats.duplicate_resources_by_type` counter still counts
  correctly under concurrency (whichever occurrence processes second is the
  "duplicate," regardless of wall-clock arrival order).
- **Python's cooperative concurrency model**: implemented via `asyncio` (not
  `threading`), so only one coroutine's code runs at any instant between
  `await` points - dict/list mutations in `resources`/`roles` are never
  actually run in parallel, only *interleaved* at well-defined `await`
  boundaries. No locks are needed for the in-memory caches; this is the same
  reason `providermap.net.AsyncFetcher` doesn't need them either.

The final yielded set of `NormalizedProvider` records is therefore identical
- same practitioners, same locations, same organizations - regardless of
concurrency level or arrival order. This is exactly what a regression test
(concurrency on vs. off, same fixtures, assert identical output) verifies.

## Cancellation behavior

If a circuit-breaker threshold trips - consecutive batch failures across the
pool exceeding a limit (mirroring `politeness.abort_after_consecutive_errors`,
which already exists for the scraped-website pipeline) - every in-flight and
pending task in that `discover()` call's batch phase is cancelled
(`asyncio.gather(..., return_exceptions=True)` plus explicit
`task.cancel()` on the remainder), and `discover()` raises a clear exception
rather than silently yielding a partial result. A pilot or production run
that can't reach the server reliably should fail loudly, not quietly under-
report - the same principle the validator already follows (exclude and
record, never guess).

## Error aggregation

A single batch permanently failing (retries exhausted) does **not** abort the
whole run - it's recorded (which location-id batch, final exception) in a new
`DiscoveryStats.failed_batches: list[FailedBatch]` field and `discover()`
continues with the remaining batches. `cmd_ingest_providers`'s completion
report gains a line for this (e.g. `failed batches: 2 of 843 (≈100 locations
not queried)`), so a partial-but-mostly-successful run is visible and
quantified rather than either crashing entirely or silently missing data
with no signal. This only turns into a full abort if the circuit breaker
above trips.

## Progress reporting

`discover()` remains a plain generator with no return value mid-flight, so
progress needs an explicit, optional hook rather than print statements buried
in the adapter: `CignaFhirAdapter.__init__` gains an optional
`on_progress: Callable[[ProgressEvent], None] | None = None`, called at
well-defined points - one per `Location` page fetched (sequential phase) and
one per `PractitionerRole` batch completed (concurrent phase), each carrying
enough state to print something like `Location page 40 (≈2,000 ids so far)`
or `PractitionerRole batch 210/843 (12 in flight)`. Defaults to a no-op, so
every existing caller (tests, the current CLI) is unaffected; `cmd_ingest_providers`
can optionally wire a simple print-based reporter for interactive runs.

## Interaction with future adapters with different server characteristics

None of the concurrency/throttling/retry logic should live inside
`CignaFhirAdapter` itself - it's factored into a small, reusable helper
(proposed: `adapters/providers/concurrency.py`,
`fetch_concurrently(urls, fetch_one, *, max_concurrent, requests_per_second,
retry) -> ConcurrentFetchResult`) parametrized entirely by the caller's own
config values. A future provider-dataset adapter with a slower server, a
stricter rate limit, or a different retry policy reuses the same helper with
its own numbers, the same way `OrganizationsConfig`'s
`website_enrichment_requests_per_second` already coexists with
`politeness.requests_per_second` as two independently-tuned budgets for two
different sources, rather than one adapter's tuning leaking into another's
behavior.

## Config additions (additive; defaults reproduce today's sequential behavior)

```yaml
providers:
  cigna_fhir_max_concurrent_requests: 10   # 1 = sequential (today's behavior, and the regression-test baseline)
  cigna_fhir_requests_per_second: 5.0
```

No existing config field changes meaning. `retries:` (the existing top-level
`RetryConfig`) is reused as-is rather than duplicated per-adapter, until
evidence (not assumption) shows Cigna needs different tuning.

## What this design does not change

`ProviderDatasetAdapter.discover()`'s contract, `NormalizedProvider`,
`extract()`/`normalize()`, the CLI's `for raw in adapter.discover():` loop,
every existing offline test, and the sync `PageFetcher` type all stay exactly
as they are. This is scoped to *how* `CignaFhirAdapter` fetches
`PractitionerRole` batches, nothing else.
