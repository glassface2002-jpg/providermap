# Phase E — capability probe evidence

Preserved per the same "do not discard evidence" discipline as
`PHASE_D_METRICS.md`. This is a record of what was *measured* against
`fhir.cigna.com` during the Phase E architecture review, not a design - see
`../NATIONAL_DISCOVERY_DESIGN.md` for what these findings drove. Every probe
is a read-only GET; commands are reproducible verbatim.

Base URL for all probes: `https://fhir.cigna.com/ProviderDirectory/v1`.
User-Agent: `ProviderMapBot/1.0 (+contact: glassface2002@gmail.com)`.

## Finding 1 — unscoped `PractitionerRole` with all three `_include`s works and returns the complete graph

```python
import httpx
H = {"User-Agent": "ProviderMapBot/1.0 (+contact: glassface2002@gmail.com)"}
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

## Summary: what these three findings rule in and out

| Approach considered | Verdict | Why |
|---|---|---|
| Fix state-scoped by adding `_include=PractitionerRole:location` to the batch query | **Ruled out** | Finding 2 - silently empty |
| Fix state-scoped by batch-fetching missing locations via `Location?_id=` | **Ruled out** | Finding 3 - no OR-list support, ~doubles request count |
| National unscoped `PractitionerRole` walk with all three `_include`s | **Confirmed viable** | Finding 1 - works, complete graph, one request per page |

These three results, taken together, are why the Phase E design accepts that
state-scoped and national discovery will differ in completeness rather than
trying to force them to match - see `NATIONAL_DISCOVERY_DESIGN.md`'s revised
invariant and Risk R1.
