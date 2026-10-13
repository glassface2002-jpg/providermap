# Phase E — capability probe evidence

Preserved per the same "do not discard evidence" discipline as
`PHASE_D_METRICS.md`. This is a record of what was *measured* against
`fhir.cigna.com` during the Phase E architecture review, not a design - see
`../NATIONAL_DISCOVERY_DESIGN.md` for what these findings drove. Every probe
is a read-only GET; commands are reproducible verbatim.

Base URL for all probes: `https://fhir.cigna.com/ProviderDirectory/v1`.
User-Agent: `ProviderMapBot/1.0 (+contact: you@example.com)`.

## Finding 1 — unscoped `PractitionerRole` with all three `_include`s works and returns the complete graph

```python
import httpx
H = {"User-Agent": "ProviderMapBot/1.0 (+contact: you@example.com)"}
B = "https://fhir.cigna.com/ProviderDirectory/v1"
url = (f"{B}/PractitionerRole?_count=5"
       f"&_include=PractitionerRole:practitioner"
       f"&_include=PractitionerRole:location"
       f"&_include=PractitionerRole:organization")
r = httpx.get(url, headers=H, timeout=45)
```

**Result:** `200`. Bundle contained **5 PractitionerRole + 13 Location + 5
Practitioner + 5 Organization** resources - i.e. the unscoped, fully-included
query natively returns *every* location a role references, not a
scope-truncated subset. This is what makes the national strategy's Stage 1
correct without any follow-up fetch.

## Finding 2 — `location=<ids>` combined with a location `_include` silently returns an empty bundle

```python
sc,d=g(f"{B}/Location?address-state=RI&_count=10")
ids=[e["resource"]["id"] for e in d["entry"] if e["resource"]["resourceType"]=="Location"]
j=",".join(ids)

base=f"{B}/PractitionerRole?location={j}&_include=PractitionerRole:practitioner&_include=PractitionerRole:organization&_count=50"
httpx.get(base, headers=H)                                   # -> 200, real data (3 roles, 1 practitioner, 3 orgs)
httpx.get(base + "&_include=PractitionerRole:location", headers=H)  # -> 200, EMPTY bundle
```

**Result:** the base 2-`_include` query (practitioner + organization)
returns real data. Adding a **third** `_include=PractitionerRole:location`
to a `location=`-filtered query returns **HTTP 200 with zero entries** - no
error, no `OperationOutcome`, just silently nothing. Reproduced across three
variants tested in the same session: `location` alone, `practitioner+location`,
and `practitioner+location+organization` were all empty; only combinations
**without** the location `_include` returned data. This is a server-side
defect (silent, not documented), not a client mistake - confirmed by the
same filtered `location=` query working perfectly for `practitioner`/
`organization` includes individually and together.

**Consequence:** this blocks the most direct fix for state-scoped's location
truncation (simply adding the missing `_include`). Never combine a
`location=` filter with a location `_include` in this adapter - it fails
silently, which is worse than an error.

## Finding 3 — `Location?_id=<comma-list>` returns at most one resource regardless of list length

```python
ids = [...]  # 13 real location ids collected from a PractitionerRole+_include=location response
for k in (1, 2, 3, 5):
    r = httpx.get(f"{B}/Location?_id={','.join(ids[:k])}&_count=50", headers=H)
    # count Location resources in r.json()["entry"]
```

**Result:**

| ids requested | Locations returned |
|---|---|
| 1 | 1 (correct) |
| 2 | 1 (truncated) |
| 3 | 1 (truncated) |
| 5 | 1 (truncated) |

Each id individually resolves fine (`Location?_id=<one id>` → 1 result, 3/3
tested). The server does not honor a comma-joined OR-list on `Location._id`
the way `PractitionerRole.location` does - this is a genuine
inconsistency between the two search parameters on the same server.

**Consequence:** completing a truncated location set by reference (fetching
each missing id individually) costs **one HTTP request per missing
location**, not one batched request. At RI's measured 27% miss rate (3,033
of 11,377 reference resolutions), this would roughly double that run's
request count - not viable as a targeted fix.

## Finding 4 — Finding 1 does not hold at production pagination scale; the `_include`/`_revinclude` national walk fails silently on a real fraction of pages

Finding 1 was a five-role, single-page probe (`_count=5`, one request, no
pagination). It was later validated at real scale - the full Rhode Island
scope (the same 42,108-location universe the shipped state-scoped strategy
enumerates, so the two are directly comparable) - by actually paginating the
national `_include`-based walk end to end via `next` links, the way a real
ingestion run would, rather than reading one page and stopping.

**Result:** of the pages walked, **33 came back as silent failures** - `HTTP
200` with the bundle's `total` field present and equal to `0`, indistinguishable
from a genuinely empty page without checking for that specific signature (see
`DiscoveryStats.suspected_silent_failure` and the `total:0` vs. omitted-`total`
distinction it encodes, added specifically because of this finding). Only
**40,555 of the expected 42,108 locations** were recovered through the
national/`_include` walk - **228 role-bearing locations** and **831 role ids**
that the existing per-location state-scoped strategy successfully finds were
silently missing from the national walk's output, with no error, retry
signal, or non-200 status anywhere in the run to indicate the loss.

**Consequence:** Finding 1's "confirmed viable" verdict does not survive at
the scale a real ingestion run operates at. A single 5-role, single-page
probe cannot exercise whatever server-side condition triggers the `total:0`
silent-failure pages - only full pagination over a real state's worth of data
surfaced it. This closes the national/`_include`/`_revinclude` traversal
approach outright, not merely as "differs in completeness" (the framing
Findings 1-3 supported) but as **unreliable in a way indistinguishable from
success without dedicated instrumentation** - a materially worse property
than the state-scoped strategy's understood, bounded truncation (Findings 2-3
above). See `docs/adr/0002-close-alternate-discovery-traversals.md` for the
resulting decision and `NATIONAL_DISCOVERY_DESIGN.md`'s status line for what
this supersedes.

## Summary: what these findings rule in and out

| Approach considered | Verdict | Why |
|---|---|---|
| Fix state-scoped by adding `_include=PractitionerRole:location` to the batch query | **Ruled out** | Finding 2 - silently empty |
| Fix state-scoped by batch-fetching missing locations via `Location?_id=` | **Ruled out** | Finding 3 - no OR-list support, ~doubles request count |
| National unscoped `PractitionerRole` walk with all three `_include`s (or `_revinclude` equivalents) | **Ruled out** - reverses Finding 1 | Finding 4 - 33 silent `total:0` pages at production pagination scale; 228 role-bearing locations and 831 role ids silently lost, undetectable without dedicated instrumentation |

These four results, taken together, are why per-location state-scoped
discovery - not national/`_include`/`_revinclude` traversal - is the
production architecture. State-scoped's truncation (Findings 2-3) is
understood, bounded, and was itself independently live-validated to be zero
gaps against the actual `address-state`/`address-postalcode` filter (see the
location-enumeration state-filter completeness probe); national's failure
mode (Finding 4) is silent, undetected before this investigation, and
unbounded until instrumented. See `NATIONAL_DISCOVERY_DESIGN.md`'s status line
and `docs/adr/0002-close-alternate-discovery-traversals.md` for the decision
this evidence drove.
