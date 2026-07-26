# 0001. National discovery strategy for the Cigna FHIR adapter

## Status

Approved (design only — not yet implemented). See
`adapters/providers/cigna_fhir/NATIONAL_DISCOVERY_DESIGN.md` for the full
design this ADR summarizes, and
`adapters/providers/cigna_fhir/evidence/PHASE_E_CAPABILITY_PROBES.md` for the
live evidence it's grounded in.

**Promotion note:** this ADR covers a package-local `DiscoveryStrategy`
abstraction scoped to `adapters/providers/cigna_fhir/`. If that abstraction
is later promoted into shared cross-adapter infrastructure, that promotion
gets its own ADR that supersedes this one — see the design doc's "Promotion
criteria" section for the conditions under which that's warranted.

## Context

The shipped Cigna FHIR adapter discovers providers via a **state-scoped**
strategy: enumerate every `Location` in a target state, then batch-query
`PractitionerRole` against those location ids. This is correct — proven
through Phases A–D with verified inserts, enrichment-only updates,
idempotency, checkpointing, and bounded concurrency — but Phase D measured it
failing to scale past a single mid-size state. A Florida staging run ran for
5 hours without finishing Location enumeration and consumed 3.87 GB of
memory, still climbing. The root cause, measured directly: 98% of enumerated
Locations have no practitioner attached (832 of 42,108 in Rhode Island) — the
entire enumeration cost exists only because `PractitionerRole` has no
geographic search parameter on Cigna's server.

The natural alternative — walk `PractitionerRole` unscoped, nationally,
skipping Location enumeration entirely — was investigated live rather than
assumed. Two live findings changed the shape of the decision:

- Adding the missing `_include=PractitionerRole:location` to the existing
  state-scoped batch query, so both discovery paths would produce an
  identical location graph, **fails silently** (HTTP 200, empty bundle) —
  a server-side defect, confirmed reproducible three ways.
- Recovering the resulting truncated locations by a batched
  `Location?_id=` reference fetch is **not viable** either — the server
  returns at most one resource regardless of how many ids are requested.

So state-scoped and national discovery cannot be made to produce identical
output by any means available on this server today. A decision was required
on what invariant to hold instead.

## Decision

1. **Add a national unscoped `PractitionerRole` discovery strategy**,
   alongside the existing state-scoped one — not replacing it — behind a
   `DiscoveryStrategy` abstraction scoped to the adapter package
   (`adapters/providers/cigna_fhir/discovery.py`).
2. **Redefine "canonical" as a property of completeness, not of which
   strategy ran.** The canonical provider graph is the most complete
   normalized representation obtainable from the source data. National
   discovery is currently the only mechanism that achieves that
   completeness, but its output is canonical *because it is complete*, not
   because it is national — any future strategy attaining the same
   completeness is equally canonical, and a partial or aborted national run
   is not.
3. **Discovery modes may differ in completeness but must never contradict.**
   State-scoped's location graph is an accepted subset of national's, not a
   defect to be eliminated by force — relationships are an additive union
   across all runs and all modes, and provider scalar fields are seeded from
   whichever run holds a complete relationship set, to avoid the one real
   correctness hazard this introduces (a truncated run selecting a different
   primary location for the same NPI than a complete run would).
4. **The shipped default stays `state_scoped`** even once every validation
   criterion for national discovery passes in staging. Switching the default
   requires its own, later, separately reviewed decision, made after a
   production-quality national ingestion has actually completed and its
   operational characteristics — memory, request volume, runtime, retry
   behavior, resume across a real restart — have been verified directly.

## Consequences

**Easier:**
- National-scale ingestion becomes possible at all — the current strategy
  cannot complete even a single mid-size state.
- Memory is bounded (target under 500 MB via an on-disk resource spool,
  versus 3.87 GB measured) regardless of how much of the country is walked.
- Discovery genuinely becomes an implementation detail: both strategies
  yield the same dict shape into the same unchanged `extract`/`normalize`/
  `cmd_ingest_providers` path, so no other part of ProviderMap needs to know
  which one ran.
- State-scoped is preserved as the correct tool for a single-state refresh
  or troubleshooting session — it is not deprecated, only demoted from "the
  only option" to "the targeted-scope option."

**Harder / accepted cost:**
- Two discovery strategies now exist to maintain and test instead of one,
  and their outputs are validated against each other (13 criteria,
  including a full export-equivalence test) rather than trivially identical.
- The completeness gap between the two modes is a permanent, documented
  property of the system, not a bug to eventually close — a location present
  in a national export but absent from a state-scoped export of the same
  data is expected behavior, and every consumer of these exports needs to
  know that.
- A new operational component (the resource spool) is introduced, with its
  own disk-growth and cleanup characteristics to monitor.

**Explicitly not resolved by this decision:**
- Whether a later complete observation should ever *refine* a scalar value
  seeded earlier by a truncated run. Deferred; would require a per-record
  completeness qualifier and a change to the enrichment-only contract, and
  is out of scope unless measured drift later justifies revisiting it.
- Whether the shipped default ever changes to national. Deliberately
  deferred to its own future decision, gated on real production-scale
  operational evidence rather than the projections in this ADR.
- Whether `DiscoveryStrategy` is promoted beyond this one adapter. Deferred
  until a second adapter demonstrates the same traversal requirements in
  working code — see the design doc's promotion criteria.
