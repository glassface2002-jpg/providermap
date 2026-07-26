# Phase D — preserved scalability evidence

Operational measurements gathered while validating the Cigna FHIR adapter at
increasing scale. Preserved per the Phase D review instruction ("do not
discard any evidence gathered so far"). This is a record of what was
*measured*, not a design — see `../CONCURRENCY_DESIGN.md` and the Phase E
proposal for design work.

## Rhode Island (smallest-population state) — completed, multiple runs

Two-step state-scoped discovery (`Location?address-state=RI` →
`PractitionerRole?location=<batch>`), measured live against
`fhir.cigna.com`:

| Metric | Value |
|---|---|
| Total HTTP requests | 1,686 (843 Location enumeration + 843 PractitionerRole batches) |
| Locations enumerated | 42,108 |
| Locations with ≥1 practitioner | 832 (**~2%** — 98% of enumeration is "empty") |
| Practitioners discovered | 521 (481 stored, 40 excluded for genuine missing-address) |
| Avg request latency | ~1.7 s (dry-run) / ~0.9–1.7 s (real runs) |
| Total runtime | ~50 min (dry-run), ~53 min / ~30 min (real passes) |
| Duplicate Organization re-includes | 1,011 of 1,280 (**79%** — pure overhead) |
| Runtime attributable to network wait | ~100% (CPU-negligible) |

## Florida (mid-size state) — deliberately interrupted after architectural validation

Same two-step strategy, checkpointing + concurrency (max=5) enabled.
**Stopped intentionally**, not failed — it had served its purpose as a
scaling probe.

| Metric | Value at stop |
|---|---|
| Elapsed wall-clock | **5h 02m** |
| CPU seconds consumed | 2,421 (~8% of elapsed — confirms latency-bound, not compute-bound) |
| Process memory (working set) | **3,867 MB and still climbing** |
| Phase reached | **Still in Location enumeration** — never completed even its single whole-phase checkpoint item |
| Providers written | 0 (never reached the yield/upsert stage) |
| Checkpoint DB size | 20 KB (run row only) |

Staging DB (`database/staging_phase_c.db`) confirmed `integrity_check: ok`
after the mid-run kill; counts unchanged (899 providers), zero partial FL
writes — the yield/write loop never began. The interrupted run's
`start_run()` row remains in the `runs` table (started, no `finished_at`),
and the checkpoint row `cigna_fhir:FL` remains resumable (`completed_at`
NULL).

## Two decisive conclusions from this evidence

1. **Memory, not just requests, is a scaling wall.** The adapter buffers
   every fetched resource in memory before yielding (documented as
   pilot-only). At one mid-size state that reached ~3.9 GB and was still
   growing. A national run in this shape is not viable regardless of request
   optimization.
2. **The Location-enumeration cost is a per-state-scoping artifact.** 98% of
   enumerated Locations have no practitioner; the only reason to walk them is
   that `PractitionerRole` has no geographic search parameter. This is what
   motivates the Phase E national-unscoped-traversal proposal.

## FHIR capability findings (live, not spec assumption)

Verified against `fhir.cigna.com`'s CapabilityStatement + tested queries:

| Capability | Result |
|---|---|
| Bulk Export (`$export`) | Not supported (zero operations in CapabilityStatement) |
| `Group` resources | Not supported (not among the 8 resource types) |
| `_summary=count` | Rejected (HTTP 400) — no cheap totals |
| Network / Organization `_revinclude` traversal | Not better (networks not geo-scoped; `Organization?...&_revinclude=PractitionerRole:organization` → 400) |
| `Location?address-state=X&_revinclude=PractitionerRole:location` | **Works** — Locations + their roles in one bundle |
| `_revinclude` + any `_include` together | Rejected (HTTP 400) |
| Unscoped `PractitionerRole?_include=practitioner` | **Works** — 50 roles + 50 practitioners/page, clean pagination |
