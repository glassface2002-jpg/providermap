# 0002. Reject national discovery for production use; retain per-location state-scoped discovery as the sole production architecture

## Status

Approved. Supersedes
[0001](0001-national-discovery-strategy.md). See
`adapters/providers/cigna_fhir/NATIONAL_DISCOVERY_DESIGN.md` (status notice at
the top) for the full historical design this closes, and
`adapters/providers/cigna_fhir/evidence/PHASE_E_CAPABILITY_PROBES.md`'s
Finding 4 for the live evidence this decision is grounded in.

## Context

ADR-0001 approved, and Stages 4-5 subsequently **built and offline-tested**,
a national unscoped `PractitionerRole` traversal (walking the whole dataset
via `_include`s instead of enumerating `Location` per state):
`adapters/providers/cigna_fhir/discovery.py`'s `NationalDiscovery` and
`ResourceSpool` behind a `DiscoveryStrategy` abstraction, opt-in via
`cigna_fhir_discovery_mode: "national"`, validated against thirteen
criteria (V1-V13) including a full export-equivalence test — all passing,
per Stage 4's completed work. That code exists in the repository today and
is not being removed by this decision (see Decision #3).

All of that validation, however, ran against **offline synthetic fixtures**,
not Cigna's real server at production pagination scale. The live evidence
approving national discovery in the first place was Finding 1 in
`PHASE_E_CAPABILITY_PROBES.md`: a single-page, five-role probe (`_count=5`,
one request, no pagination) that returned a complete, untruncated graph and
was read as proof the national/`_include` approach scales cleanly against
the real API.

That probe was later re-run at the scale a real ingestion run actually
operates at: the full Rhode Island scope (42,108 locations — the same
universe the shipped state-scoped strategy enumerates, chosen specifically
so the two are directly comparable), walked end to end through `next` links
against the live `fhir.cigna.com` server, rather than read one page and
stopped. The result reversed the earlier finding: **33 pages came back as
silent failures** — `HTTP 200`, bundle `total` present and equal to `0`,
indistinguishable from a genuinely empty page without checking for that
specific signature. Only **40,555 of the expected 42,108 locations** were
recovered — **228 role-bearing locations** and **831 role ids** that the
shipped state-scoped strategy successfully finds were silently missing, with
no error, retry signal, or non-200 status anywhere in the run to indicate the
loss. No offline fixture-based test (V1-V13 included) could have caught this
— it is a real-server behavior under real pagination volume, not a logic
defect in the `NationalDiscovery`/`ResourceSpool` code itself.

This is a materially worse failure profile than state-scoped's own,
already-known truncation (ADR-0001's Findings 2-3: a `location=` filter
combined with a location `_include` returns an empty bundle; a comma-joined
`_id` OR-list returns at most one resource). That truncation is understood,
bounded, and was itself independently live-validated to be zero gaps against
the real `address-state`/`address-postalcode` filters state-scoped actually
uses in production (a separate live probe run this same investigation).
National's failure mode, by contrast, was silent and undetected until this
investigation built dedicated instrumentation
(`DiscoveryStats.suspected_silent_failure`, the `total:0`-vs-omitted-`total`
distinction) specifically to surface it.

OR-list batching as a targeted fix for state-scoped's own truncation was
already ruled out under ADR-0001 (Finding 3 — no OR-list support on
`Location._id`, ~doubles request count) and remains closed for the same
reason. An `Organization`-anchored `_revinclude` walk, considered as an
alternate anchor during Phase D, was already ruled out there too (networks
are not geo-scoped; `Organization?...&_revinclude=PractitionerRole:organization`
returns HTTP 400 — `PHASE_D_METRICS.md`). No new evidence reopens either of
those.

## Decision

1. **National discovery is rejected for production use, permanently.** The
   `cigna_fhir_discovery_mode: "national"` mode must never be selected
   against the real Cigna API. This is not a deferral pending future
   evidence — Finding 4 is a disqualifying correctness defect (silent,
   undetectable data loss), not a scalability tradeoff further engineering
   could work around. ADR-0001's step 5/6 migration gate (bring a
   production-quality national ingestion to completion, then separately
   decide whether to switch the default) will never be reached — it is
   foreclosed by this decision, not merely unscheduled.
2. **Per-location state-scoped discovery — the strategy already shipped and
   already the default — is the correct and only production discovery
   architecture for this adapter**, now and for any future national-scale
   ingestion. A future national run means running state-scoped discovery
   once per state, not adopting a different traversal shape.
3. **The `NationalDiscovery`/`ResourceSpool`/`DiscoveryStrategy` code is not
   being removed by this decision.** It remains in the repository, opt-in,
   offline-tested against V1-V13, and explicitly disqualified from
   production use by Finding 4 — a rejected mode kept for its historical/
   test value, not an active production option. `resource_spool_path`
   defaults to `null`, which already keeps `national` mode unusable without
   deliberate, explicit configuration. Deleting this code, if ever desired,
   is separate cleanup work this decision does not authorize or require.
4. **OR-list batching and alternate anchors remain closed**, per ADR-0001's
   Findings 2-3 and Phase D's Organization-anchor probe — no new evidence
   reopens either.
5. **Remaining work is production-hardening of the existing per-location
   architecture**, not new traversal design: streaming/memory optimization,
   checkpoint payload and commit-frequency tuning, and instrumentation to
   keep the `total:0` silent-failure signature visible on every run (already
   built, per `DiscoveryStats`) — never a replacement traversal.

## Consequences

**Easier:**
- The discovery-mode decision is closed and final: no dual-mode
  output-equivalence question hangs over future work, and no one needs to
  evaluate switching the default again — Finding 4 forecloses it outright
  rather than leaving it as an open, periodically-revisited question.
- The correctness risk this decision avoids is severe: national discovery's
  silent per-page data loss would have been essentially invisible in
  production without exactly the instrumentation this investigation had to
  build first to even detect it. Catching this before any real national run
  shipped is the direct payoff of validating Finding 1 at real scale instead
  of trusting the original single-page probe.

**Harder / accepted cost:**
- Per-location discovery's request volume and runtime remain what they are
  — one `Location` enumeration plus one `PractitionerRole` batch query per
  location, per state. A full national ingestion means running this once
  per state; there is no lower-request-count alternative available on this
  server today.
- Large states will continue to take substantial wall-clock time and
  memory unless the per-location architecture itself is optimized (streaming
  instead of accumulate-then-drain, checkpoint tuning) — this is exactly the
  work item this decision routes remaining effort toward.
- The `NationalDiscovery`/`ResourceSpool` code (Stages 4-5's work) is now
  dead weight from a production standpoint — built, tested, and
  permanently unusable against the real API. That investigation was not
  wasted (it is what made Finding 4's validation possible, and the shared
  `fetch_batches_concurrently` streaming infrastructure it prompted
  benefits state-scoped too), but the code itself has no path to
  production use.

**Explicitly not resolved by this decision:**
- Whether some other, not-yet-considered traversal strategy could someday
  avoid per-location enumeration's cost without the silent-failure defect.
  Nothing observed here rules that out in principle; it is simply not
  something any currently-investigated approach achieves. Reopening this
  requires new live evidence, not a retry of any approach closed here.
- Whether the dead `NationalDiscovery`/`ResourceSpool` code should
  eventually be deleted outright rather than kept as a disqualified opt-in.
  Deferred as unrelated cleanup, not a discovery-architecture decision.
- How far per-location discovery's own memory/runtime can be improved.
  That is active, separate work (streaming fetch processing, checkpoint
  payload/commit tuning) tracked independently of this decision.
