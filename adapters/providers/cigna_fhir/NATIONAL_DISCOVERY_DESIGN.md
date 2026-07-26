# Cigna FHIR adapter — national discovery architecture

## Status: proposal approved (design only — not implemented)

This document **supersedes the original Phase E draft**. That draft assumed
Cigna's `_revinclude` optimization was a viable half-measure and assumed
state-scoped discovery could be brought into alignment with national
discovery by adding one missing `_include`. Both assumptions were tested live
against `fhir.cigna.com` during architecture review and **both were wrong** —
see "Decisive new evidence" below. Nothing in this document is implemented.
It is a reviewed and approved proposal; implementation begins only when a
follow-up phase is explicitly authorized.

## Context

The Cigna FHIR adapter's shipped **state-scoped** discovery
(`Location?address-state=X` → `PractitionerRole?location=<batch>`) is correct
and proven: Phases A–D verified inserts/updates, enrichment-only behavior,
idempotency, checkpointing, and bounded concurrency, all under a
365/365-green test suite. But Phase D measured it hitting two real walls at
merely one mid-size state:

- **Runtime:** the Florida staging run ran **5h02m** and never finished even
  Location enumeration.
- **Memory:** the same run reached **3.87 GB RSS and was still climbing** —
  the adapter buffers every fetched resource in memory before yielding
  (documented in the adapter's own module docstring as a pilot-only design).

Root cause, measured in Rhode Island: **98% of enumerated Locations have no
practitioner attached** (832 of 42,108). We only walk them because
`PractitionerRole` has no geographic search parameter on this server — the
entire enumeration cost is a scoping artifact, not an inherent cost of the
data.

This document designs a **national unscoped `PractitionerRole` traversal**
as an *additional* discovery mode, retaining state-scoped for targeted
refresh. Scope is strictly discovery efficiency and scalability — schema,
models, exports, and the enrichment-first update model are unchanged.

---

## Decisive new evidence (live probes, not assumption)

Three findings, all reproducible — see
`evidence/PHASE_E_CAPABILITY_PROBES.md` for exact commands and full output.

| # | Probe | Result | Consequence |
|---|---|---|---|
| 1 | Unscoped `PractitionerRole?_count=5` with all three `_include`s (practitioner, location, organization) | **200** — 5 roles + **13 Locations** + 5 Practitioners + 5 Organizations | National query is valid and natively returns *every* location a role references — the complete graph, no truncation |
| 2 | `PractitionerRole?location=<ids>` **plus** `_include=PractitionerRole:location` | **200 with an empty bundle** — silent, reproduced across three `_include` combinations | Blocks the obvious fix (add the missing include to state-scoped) — this is a server defect, not a client mistake |
| 3 | `Location?_id=a,b,c` (comma-joined OR-list) | Returns **1** resource regardless of list length; single ids resolve fine | Recovering missing locations by reference costs one request *per location*, not one batched request — not viable as a patch |

### The truncation these findings expose

`_practitioner_role_by_locations_url` (`adapters/providers/cigna_fhir/adapter.py:290`)
deliberately omits the location `_include` — it only asks for practitioner
and organization. In the grouping loop (`adapter.py:727-735`), any location a
role references that isn't already cached is counted a `reference_miss` and
**silently dropped**. Because state-scoped only ever caches in-state
locations, **multi-state practitioners lose every out-of-state location**.
Rhode Island measured **3,033 reference misses against 8,344 hits (~27%)** —
one live role observed during this investigation referenced 8 locations, 7
of them outside the state being scanned.

**Rhode Island's reported "832 locations" is therefore the in-state subset of
those 481 providers' locations, not the complete graph.** This was not known
before this investigation.

---

## Revised invariant

The architecturally clean option — add the missing `_include` to
state-scoped so both modes match exactly — is **blocked by finding 2**, and
the fallback (per-location reference completion) is **ruled out by finding
3**. The invariant is therefore defined around what the evidence actually
supports:

> **Discovery maximizes correctness. Operational scope is a view concern —
> applied at export, report, or query time — never a discovery concern.
> Discovery modes may differ in completeness, but must never produce
> contradictory provider identities or relationships. The goal is one
> canonical graph with scope-aware views, not multiple definitions of
> truth.**

Operationalized as three testable rules:

1. **Identity is absolute.** For the same source data, every discovery mode
   yields the identical set of provider NPIs, and identical values for every
   field derived from practitioner-level data (name, credentials, languages,
   NPI validity).
2. **Relationships are monotonic, never contradictory.** A location or
   organization emitted by state-scoped must also be emitted by national.
   National may emit more. Neither mode ever invents a relationship the
   other contradicts.
3. **Scope-derived scalars must never silently disagree.** This is the one
   place the relaxed invariant is genuinely at risk — see Risk R1.

---

## The canonical provider graph — explicit definition

"Canonical" needs a definition or Risk R1's mitigation has nothing to anchor
to.

> **The canonical provider graph is the most complete normalized
> representation obtainable from the source data.**

**Canonicality is a property of completeness, not of the mechanism that
produced it.** National discovery is *currently the only mechanism able to
construct the complete graph* — but its output is canonical *because it is
complete*, not because it is national. Two consequences follow:

- Any future strategy that attains the same completeness is **equally
  canonical**. The property is never reserved to a named mode.
- A national run that is **partial or aborted is not canonical** — running
  in "national mode" never by itself confers the property.

This keeps discovery a genuine implementation detail rather than smuggling a
mode name into the definition of truth — a provider dataset adapter should
be indifferent to *how* a record was discovered, and this definition is what
makes that indifference precise rather than aspirational.

| Element | Rule |
|---|---|
| **Identity** | Keyed by NPI (`providers.npi` `UNIQUE`). Exactly one row per NPI, ever, across all modes and all runs. |
| **Provider scalar fields** (name, credentials, specialty, taxonomy, languages, primary address) | Set by the **first writer**, never overwritten — the existing enrichment-only contract (`Database.upsert_provider`). Scalars should be *seeded* by a run operating on a **complete relationship set**, because a truncated set can select a different primary location (Risk R1). Completeness — not discovery mode — is the qualifying condition. |
| **Relationships** (`provider_locations`, `provider_organizations`) | **Additive union** across every run and every mode. A mode may add; no mode ever deletes or prunes a relationship another mode established. |
| **Completeness** | **Monotonic** — the graph only ever gains relationships over time, never loses them. |
| **`is_primary`** | Set by the first writer; re-evaluated only from a complete location set, never downgraded by a subsequently-run truncated one. |
| **Scope** | **Never persisted.** No run's state scope becomes a stored property of a provider, location, or organization. Scope is applied exclusively at export, report, or query time. |

**What canonical does not mean:** it does not require any single run to
produce everything in one pass. It requires that all runs, run in any order,
**converge on one non-contradictory graph** — exactly what invariant rules 1
and 2 test.

**Deliberate open item, not resolved here:** for a later, complete
observation to *refine* scalars already seeded by an earlier truncated one,
the graph would need a per-record completeness qualifier. That's achievable
without a schema change (e.g. inside the existing `Provider.source_notes`),
but it is a behavior change to the enrichment-only contract, which is out of
this phase's scope to alter unilaterally. **Default position: do not build
automatic refinement.** Rely on enrichment-only plus operational sequencing
(seed provider rows from a complete-set run first), and raise refinement as
its own reviewed proposal only if measured drift later justifies it.

---

## Risk R1 — primary-location divergence (the one real correctness hazard)

`normalize()` selects the primary location as "tied to the most roles,
tie-broken first-seen" (`adapter.py:847` onward), and that primary populates
`Provider.address/city/state/zip/phone` plus `location_count`. Given a
**truncated** location set, state-scoped can select a **different primary**
than national would for the exact same NPI — the same practitioner could
carry a different stored address depending only on which discovery mode ran
first. That is *contradiction*, not mere incompleteness, and it would violate
even the relaxed invariant above if left unmitigated.

**Mitigation, three layers, all already available or cheap to add:**

1. `Database.upsert_provider` is already enrichment-only — an existing NPI's
   row is never overwritten (`providermap/cli.py:578-586`, proven in Phase C
   with a live mutation test). The database itself never flip-flops; only
   which run writes *first* matters.
2. **Seed scalars from a complete relationship set.** Operational rule: a run
   working from a complete set (currently, only national) establishes
   provider rows; a scope-truncated run (state-scoped) contributes
   relationships and periodic refresh only. The qualifying condition is
   completeness, not the mode's name.
3. **Pin it with a test (V5).** Both strategies run over one shared fixture
   containing a deliberately multi-state practitioner; the test asserts
   invariant rules 1–2 hold, and that any primary-location difference between
   the two runs is *detected and reported*, never silent.

---

## Architecture — `DiscoveryStrategy`, scoped to the adapter package

**Introduce the abstraction — as a package-local seam, not a fourth global
registry.**

Justification: `discover()` is already a ~180-line method with exactly one
mode; a second mode as a branch inside it would be unmaintainable. The two
strategies share low-level FHIR helpers (URL builders, reference parsing,
retry/rate-limiting) but differ completely in traversal shape. Splitting them
behind a common `run(ctx) -> Iterator[dict]` interface is what makes "same
normalized output regardless of strategy" *structurally* testable (V1–V3),
not just asserted in a docstring.

**Kept package-local** (`adapters/providers/cigna_fhir/discovery.py`)
because both strategies are FHIR/Plan-Net-specific; a future non-FHIR
provider dataset adapter reuses neither.

### Promotion criteria — explicit, so this isn't re-litigated later

**Keeping the abstraction package-local is the correct decision today.**
Promote it into shared infrastructure (a new `adapters/providers/discovery.py`)
**only after at least one additional adapter has demonstrated the same
traversal requirements** — demonstrated in working code, not anticipated.
This is the project's standing measure-first-generalize-later discipline
(the same reasoning behind the OSM cache and the concurrency default of `1`)
applied to abstraction design, not just to features.

Concretely, promotion requires all three:

1. **A second adapter genuinely needs multiple strategies of its own** — not
   merely a second adapter that happens to have one strategy.
2. **Two or more strategies across *different* adapters share substantive
   traversal logic** — real shared behavior (windowing, spooling, reference
   resolution), demonstrated in code, not merely a matching ABC signature.
   A matching signature alone is not reuse.
3. **That shared logic is genuinely source-agnostic** — no FHIR, Plan-Net, or
   payer-specific concept leaks into the promoted layer.

**Explicit anti-criterion:** a second *Plan-Net payer* adapter alone
(Aetna, UnitedHealthcare, and Humana all publish equivalent APIs under the
same CMS-9115-F mandate Cigna's is built on) is **not sufficient by itself**.
If two payers share FHIR-specific traversal logic, the correct refactor is a
shared **FHIR/Plan-Net base**, not a generic cross-source
`DiscoveryStrategy` layer — generalizing along the wrong axis is worse than
not generalizing at all. Whenever promotion does happen, record it as a new
ADR that supersedes ADR-0001 (see `docs/adr/0001-national-discovery-strategy.md`).

### Component diagram

```
                    cmd_ingest_providers          (providermap/cli.py — UNCHANGED)
                             │  for raw in adapter.discover():
                             ▼
                    CignaFhirAdapter              (ProviderDatasetAdapter)
                    ├── discover()  ──────────────► delegates to strategy
                    ├── extract()   ── PURE, unchanged
                    └── normalize() ── PURE, unchanged
                             │
                             ▼
                    DiscoveryStrategy (ABC)       ← NEW, package-local
                    │   run(ctx) -> Iterator[dict]
                    ├── StateScopedDiscovery      ← existing logic, MOVED verbatim
                    └── NationalDiscovery         ← NEW
                             │
              ┌──────────────┼──────────────┬──────────────┐
              ▼              ▼              ▼              ▼
        PageFetcher    CheckpointStore  ResourceSpool  DiscoveryStats
        (existing)      (existing)       (NEW, disk)    (existing)
```

**The seam is exact and already clean — nothing about it needs to change.**
Every strategy, current and future, must yield:

```python
{"practitioner": <raw Practitioner dict>,          # required, non-None
 "roles":        [<raw PractitionerRole dict>, …], # required, non-empty
 "locations":    {bare_id: <raw Location dict>},   # may be empty
 "organizations":{bare_id: <raw Organization dict>}}
```

**Keys must be bare ids** — `_parse_reference(...)[1]`, never the
`Location/<id>` reference form. `extract()` re-parses each role's own
references independently and joins against these dicts by that same bare
key (`adapter.py:791-803`, `858` onward); a strategy that keyed these
dictionaries differently would silently produce providers with no address
and no primary location. This is mandated as a contract test (V1), not left
as an implicit assumption.

### Sequence — state-scoped (existing, moved verbatim, not changed)

```
run(ctx)
 └─ phase "location_enumeration" (one whole-phase checkpoint item)
     └─ GET Location?address-state=ST&_count=50 ──► follow next ──► ids + bodies
 └─ phase "practitioner_role_batches" (one checkpoint item PER batch)
     └─ for each 50-id chunk:  GET PractitionerRole?location=<bare ids>
                                   &_include=practitioner&_include=organization
        (concurrent, semaphore + token bucket — reuses Phase D.3 infrastructure)
 └─ group roles by practitioner ──► yield   [out-of-state locations DROPPED — Risk R1]
```

### Sequence — national (new)

```
run(ctx)
 STAGE 1 — spool          O(one page) memory
 └─ partition the walk by _lastUpdated date windows (one checkpoint item per window)
     └─ GET PractitionerRole?_lastUpdated=ge<start>&le<end>
            &_include=practitioner&_include=location&_include=organization
            &_count=50                          ← verified live to work (finding 1)
        └─ follow next; per page: INSERT OR REPLACE into ResourceSpool
                                  (dedup on (resourceType,id) at the DB, not RAM)
 STAGE 2 — materialize    O(one practitioner) memory
 └─ SELECT DISTINCT practitioner refs from the spool
     └─ per practitioner: indexed spool lookup of its roles + referenced loc/org
        └─ yield the SAME dict shape                [complete locations — no drop]
 STAGE 3 — write          UNCHANGED cmd_ingest_providers
```

`ResourceSpool` is a new, ephemeral on-disk SQLite file — operational state,
discardable, no main-schema change, the same conceptual sibling
`CheckpointStore` already is. It is what converts the measured 3.87 GB
in-memory cache into bounded disk, and its `(resourceType, id)` primary key
absorbs the measured 79% duplicate-Organization re-includes as cheap
`INSERT OR REPLACE`s instead of unbounded dict growth.

`_lastUpdated` was chosen as the windowing axis because it is a documented,
stable search parameter on every resource type in Cigna's own
CapabilityStatement — unlike page-number pagination, a date window is
independently re-runnable, which is exactly what checkpointed resume needs.

---

## Three-approach comparison

| Dimension | 1. State `Location→PR` (shipped) | 2. State `_revinclude` | 3. National unscoped |
|---|---|---|---|
| Implementation complexity | Done | Small | Medium (spool + windowing; reuses checkpoint/concurrency infrastructure) |
| Requests, per state | 1,686 (Rhode Island, measured) | ~half (~860, projected) | n/a (not state-scoped) |
| Requests, national | Σ of 50 full-state enumerations — unusable | ÷2 of that — still unusable | **~1–2 orders of magnitude lower** |
| Memory | O(all resources) — **3.87 GB at one state, measured** | O(all) — same wall | **O(one page)** via disk spool |
| Location completeness | **Truncated — ~27% of references dropped, measured** | Truncated the same way | **Complete** (finding 1) |
| Scalability ceiling | Fails at a mid-size state | Higher, but hits the same memory wall | National-viable |
| Operational risk | Lowest — proven correct in production-scale staging | Low, but `_revinclude` cannot carry any `_include` (finding 2) — would need its own separate practitioner/organization fetch | Moderate — new spool component; window sizing needs one calibration probe |
| Long-term fit | Retained as the targeted-refresh / troubleshooting mode | Not recommended — see below | Canonical graph builder |

**Approach 2 is weaker than the original Phase E draft assumed.** Finding 2
proves `_revinclude` cannot be combined with any `_include` on this server,
so it would need its own follow-up practitioner/organization fetch to be
useful at all — closing most of its apparent simplicity advantage.
**Recommendation: defer approach 2 indefinitely** rather than build it as an
interim step; it does not resolve the location-completeness truncation
either way, so it doesn't advance the invariant, only the request count.

---

## Files (all additive — nothing existing is removed)

**New**
- `adapters/providers/cigna_fhir/discovery.py` — `DiscoveryStrategy` ABC,
  a `DiscoveryContext` carrying the shared collaborators (fetcher, checkpoint
  store, stats, config), `StateScopedDiscovery` (the existing logic, moved
  verbatim, not rewritten), `NationalDiscovery`.
- `providermap/resource_spool.py` — `ResourceSpool`, mirroring
  `providermap/checkpoint.py`'s shape and conventions exactly (dedicated
  SQLite file, immediate commits, `(resourceType, id)` primary key).
- `tests/test_discovery_strategies.py`, `tests/test_resource_spool.py`.

**Modified, additive only**
- `adapters/providers/cigna_fhir/adapter.py` — `discover()` becomes a thin
  delegation to whichever strategy `config.providers.cigna_fhir_discovery_mode`
  selects; `extract()` and `normalize()` are untouched.
- `providermap/config.py` and `config.example.yaml` — new
  `cigna_fhir_discovery_mode` (default `"state_scoped"`, today's behavior
  exactly), `resource_spool_path`, `cigna_fhir_window_days`. Must land in
  **both** files, since `tests/conftest.py`'s `config` fixture loads the real
  `config.example.yaml`.
- `providermap/cli.py` — an optional `--discovery-mode` flag mirroring the
  existing `--provider-adapter`.

**Reused, unchanged:** `CheckpointStore`, `_RateLimiter`, `_fetch_with_retry`,
`_fetch_batches_concurrently`, `DiscoveryStats`, `_parse_reference`, every
existing URL builder, `Database.*`, `validate_provider`, both exporters, and
the full schema.

---

## Validation plan

Correctness and efficiency are both required; neither substitutes for the
other.

| # | Criterion | How |
|---|---|---|
| V1 | Yielded-dict contract: bare-id keys, required keys present | Both strategies must pass one shared contract test (guards the silent no-address failure mode) |
| V2 | Identical provider identity set (invariant rule 1) | Both strategies over one shared fixture → identical NPI set + identical practitioner-derived fields |
| V3 | Relationships monotonic, never contradictory (invariant rule 2) | Assert state-scoped output ⊆ national output; report the delta explicitly |
| V4 | Zero duplicate providers | `SELECT npi FROM providers GROUP BY npi HAVING COUNT(*) > 1` = 0 after a staging run |
| V5 | Primary-location divergence detected, never silent (Risk R1) | Fixture with a deliberately multi-state practitioner; assert the divergence is surfaced/logged |
| V6 | Enrichment-only behavior preserved | Repeat Phase C's live mutation test verbatim, under national mode |
| V7 | Deterministic and idempotent | Same fixture run twice → identical yields; a second real pass writes zero new rows |
| V8 | Checkpoint recovery | Kill mid-window, resume, assert no re-fetch of already-done windows |
| V9 | **Bounded memory** | Staging run peak RSS **under 500 MB** (measured baseline to beat: 3.87 GB) |
| V10 | Reduced requests and runtime | Windowed national probe compared against the Rhode Island / Florida baselines |
| V11 | Zero export regression | `providermap export` and `export-organizations` run clean, same columns/sheets/row shape |
| V12 | Full suite green | 365/365 plus every new test; zero existing tests modified |
| V13 | **Export equivalence across discovery modes** | See below — the decisive end-to-end proof |

### V13 — export equivalence, the decisive end-to-end test

V2 and V3 prove the *yielded records* agree at the unit level. V13 proves the
**shipped artifact** agrees — what downstream consumers of ProviderMap
actually see.

1. From one shared offline fixture representing equivalent source data, run a
   full ingest **twice, into two separate disposable databases** — once
   under `state_scoped`, once under `national`.
2. Run `providermap export` and `export-organizations` against both
   resulting databases.
3. Assert the two exported datasets are **structurally identical except for
   one explicitly documented location-completeness delta**, defined as a
   closed set — the test fails on any difference outside it:
   - **Identical** provider row set by NPI, and identical values for every
     identity/practitioner-derived column (name, credentials, specialty,
     taxonomy, languages, NPI validity).
   - **Identical** column set, sheet names, and column ordering — no schema
     drift between the two exports.
   - **The one allowed delta:** location/organization relationship rows
     present in the national export but absent from the state-scoped one,
     and the `location_count` those extra relationships imply — strictly in
     the ⊆ direction. Every relationship present in the state-scoped export
     must also be present in the national export; never the reverse, never
     contradictory.
   - Any `primary address` difference between the two exports is
     **enumerated and reported** by the test, never silently tolerated —
     this is Risk R1's end-to-end tripwire.

This is what actually protects downstream consumers: it proves that
discovery-mode optimization cannot change ProviderMap's observable output
beyond the one delta that's been consciously accepted and written down here.

**Success = V1 through V8 and V11 through V13 all pass, and V9/V10 both
improve on the measured baseline.** Reduced requests or runtime alone, without
the correctness criteria, is explicitly not success — and V13 failing is a
hard stop regardless of how much performance improved.

## Performance projections

These are estimates, not measurements — Cigna's server rejects
`_summary=count` with HTTP 400, so an exact national total is unobtainable
without actually walking the data.

| | Current state-scoped | National (projected) |
|---|---|---|
| Requests | Rhode Island 1,686; Florida 5h+ and unfinished | ~(total roles ÷ 50); if roughly 1M roles nationally, **~20,000 requests total** |
| Memory | 3.87 GB at one state, measured | **O(one page)**, target under 500 MB |
| Coverage | One state per run, and truncated even for that state | All 50 states in one run, complete |

Gated on a **calibration probe** — roles observed per `_lastUpdated` window —
before committing to any real national run, so window sizing is measured
rather than guessed.

## Migration and rollback

**Migration** is additive throughout; the mode defaults to `state_scoped`, so
merging this work changes zero existing behavior. Sequence:

1. Land the strategy refactor with **state-scoped only** implemented behind
   it; prove the full 365-test suite is unchanged (this step is a pure move,
   with no behavior delta).
2. Land `NationalDiscovery` behind the config flag; default remains
   `state_scoped`.
3. Run the calibration probe (roles observed per `_lastUpdated` window).
4. Run a two-window staging dry-run; execute the full V1–V13 validation
   plan against it.
5. Bring a **production-quality national ingestion** to a reviewed decision,
   run it to successful completion, and **verify its operational
   characteristics directly** — peak memory, total request volume, total
   runtime, retry/429 behavior under sustained load, spool disk growth,
   resume behavior across a real restart, and failed-window count.
6. **Only after step 5 completes successfully and those characteristics are
   verified**, open a separate reviewed decision on whether to change the
   shipped default discovery mode — reassessed on that operational evidence,
   never on the projections in this document. The default switch is not
   bundled into any earlier step.

**Rollback** at any point is a one-line config change:
`cigna_fhir_discovery_mode: state_scoped`, no deploy required. The spool and
checkpoint files are freely deletable with zero effect on `providermap.db`.
Step 1 (the strategy refactor itself) is independently revertable, since it
is a pure code move with no behavior delta to undo.

## Risk assessment

| Risk | Severity | Mitigation |
|---|---|---|
| **R1 — primary-location divergence** | High | Enrichment-only upsert + seed-from-complete-set rule + V5 |
| Silent empty bundles on bad `_include`/filter combinations (findings 2 and 3) | High | Never combine a `location=` filter with a location `_include`; assert non-empty results where data is expected |
| Resource spool disk growth | Medium | Bounded by distinct-resource count, not request count; monitored; freely deletable |
| `_lastUpdated` window sizing wrong | Medium | Calibration probe required before any national run |
| Refactor regression during the strategy extraction | Medium | Step 1 is a pure move; the full 365-test suite must stay green throughout |
| Sustained request volume against a live third-party payer API | Medium | Rate limiting already built (Phase D.3); advance coordination with Cigna recommended before any full national run |

---

## Recommendation

**Yes — national unscoped traversal should become ProviderMap's national
ingestion strategy, with state-scoped retained as a first-class targeted
mode, not replaced.** It is the only approach that is memory-bounded,
request-efficient, and produces the complete location graph. State-scoped
remains genuinely the better choice for a single-state refresh or
troubleshooting session, and it is the only discovery mode with any
server-side scoping available at all.

**The default discovery mode is deliberately not switched by this phase.**
Ship national as an opt-in `cigna_fhir_discovery_mode`, and keep
`state_scoped` as the shipped default **even after every validation
criterion in this document passes** — until national discovery has completed
at least one production-quality national ingestion and its operational
characteristics have been verified directly, not projected. Staging
validation (V1–V13) authorizes the national *mode*; it does not by itself
authorize the default *switch*. These are two separate reviewed decisions,
because staging fixtures cannot surface the failure classes that only appear
at genuine national scale — sustained rate-limit behavior, spool growth over
millions of resources, multi-day resume, real Cigna-side throttling. Phase D
is the concrete precedent for why this caution is warranted rather than
excessive: Rhode Island looked entirely healthy, and Florida alone still
exposed the 3.87 GB memory wall that motivated this whole redesign.

**Also recommended, separately and smaller in scope:** document the
state-scoped truncation directly in the adapter's module docstring, and
surface `DiscoveryStats.reference_misses` in the CLI's printed report — right
now it is invisible to an operator, and it is the reason Rhode Island's
location count understates the true relationship graph.
