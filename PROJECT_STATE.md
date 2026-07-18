# Project state

A living status summary of what's actually shipped and verified against the
live AdventHealth site, as of this writing. Not a design doc - see the
README for architecture and usage, `CHANGELOG.md` for release history. This
file exists to answer "what actually works against the real site right now,
and why," which the README's "Known limitations" section covers only
partially.

> **Organization support is foundation-only.** Schema v4 adds the
> `organizations` / `provider_organizations` / `sources` tables and the
> `OrganizationAdapter` contract, but no adapter populates them yet and
> nothing organization-related is scraped or imported. Everything verified
> below concerns the **provider** pipeline, which is unchanged. See
> `ROADMAP.md` for the organization direction and its staged plan.

## AdventHealth access, verified

| Path | Plain HTTP (`httpx`) | Playwright (headless, no evasion) |
| --- | --- | --- |
| `/robots.txt` | 200 | 200 |
| `/sitemap.xml` (and its 15 child sitemaps) | 200 | 200 |
| `/physician/vcard/{npi}` | 200 | 200 |
| `/doctors` (listing) | 403 "Access Denied" | 403 "Access Denied" |
| `/doctors/{slug}-{npi}` (profile page) | 403 "Access Denied" | 403 "Access Denied" |

The 403s come from Akamai (`errors.edgesuite.net`), not from `robots.txt` -
`robots.txt` explicitly permits both `/doctors` paths. A vanilla, headless
Playwright browser with a normal, self-identifying `User-Agent`
(`providermap/render.py`, no stealth plugins, no fingerprint spoofing, no
session/cookie tricks beyond what a browser does automatically) gets the
identical block. This was verified directly, not assumed - see the spike
described below.

**Practical effect:** discovery (via the sitemap, once the traversal bug
below was fixed) and vCard-sourced enrichment (name, practice name, phone,
full address) work today over plain HTTP, no browser needed. Anything that
requires the rendered profile page - JSON-LD, the `<title>`/`og:title` HTML
fallback, and the appointment-link location-id count that drives
`location_count`/`site_confidence` - does not currently work for live
AdventHealth data. Every affected record still gets stored (the pipeline is
designed to degrade gracefully on a missing page, not exclude the record),
just with those fields at their defaults (`location_count=1`,
`hospital_affiliation`/`accepting_new_patients`/`rating`/`languages`/
`insurance_accepted` all null/empty).

**If those fields matter enough to pursue further:** per the README's
"Legal & ethical use" section, the next step is contacting AdventHealth for
a data feed or written permission - not further fetching technique. No
bypass/evasion was attempted or is planned.

## Fixed: sitemap discovery

`providermap/sources.py`'s `SitemapSource._expand()` used to skip recursing
into a `<sitemapindex>` child sitemap unless its URL contained
`doctor|physician|provider|find`, or the index had ≤12 children.
AdventHealth's `sitemap.xml` is an index of 15 children named generically
(`?page=1`..`?page=15`); the doctor directory turned out to live entirely in
child #8, discovered only by checking each one. The old heuristic recursed
into zero of them, so `providermap investigate`/`scrape` previously reported
"0 URLs seen" via the sitemap source despite the sitemap genuinely
containing the full directory. `_expand()` now expands every child
unconditionally (bounded by `_MAX_SITEMAP_FILES = 200` as a safety valve for
a pathological index, not a targeting heuristic). See
`tests/test_sources.py` for the regression test.

## New: Playwright rendering path

`providermap/render.py`'s `PlaywrightFetcher` implements the same
`AsyncFetcher` protocol as `net.py`'s `Fetcher`, selected via
`site.fetch_mode: "playwright"` in config. It composes a plain `Fetcher`
internally for everything off the target site (NPPES, robots.txt), and only
renders target-site URLs in a real (headless by default) browser. Requires
the optional `render` extra: `pip install -e ".[render]"` then
`playwright install chromium`.

**For AdventHealth specifically, this currently provides no additional
access** - see the table above. It's shipped as real, working, reusable
adapter-framework infrastructure (any future adapter for a site with
weaker/no bot-management gets it for free), not as a fix for this site's
Akamai configuration. `config.example.yaml`'s `site.fetch_mode` stays at the
default `"http"` for the AdventHealth adapter, since switching it to
`"playwright"` would add real overhead (a browser launch per request) for
zero benefit here.

## New: JSON-LD-primary parsing

`providermap/parser_utils.parse_jsonld_physician()` extracts a schema.org
`Physician`-shaped JSON-LD node from a page, if present; `adapters/
providers/adventhealth/adapter.py`'s `parse_profile_html()` tries it before falling
back to the existing `og:title`/`<title>` HTML scraping. Maps onto six new
`ProfilePage`/`Provider` fields: `hospital_affiliation`,
`accepting_new_patients`, `rating`, `rating_count`, `languages`,
`insurance_accepted`. Fully implemented and tested against the offline
fixture (`adapters/providers/adventhealth/fixtures.py` carries full/partial/malformed
JSON-LD variants), but **currently unreachable against live AdventHealth
data** for the reason above - the profile page it would read from is
blocked. It will start working automatically, no code changes needed, if
that access situation changes.

The exact schema.org property names checked (`hospitalAffiliation`,
`acceptingNewPatients`, `aggregateRating.ratingValue`/`reviewCount`,
`knowsLanguage`, `acceptedInsurance`, and a couple of synonyms for each) are
**candidates, not confirmed against real AdventHealth markup** - no live
profile page has actually been inspected, for the same reason. If/when one
becomes reachable, check `_apply_jsonld`'s candidate key lists in
`adapter.py` against the real shape before trusting the mapped values.

## New: additive database schema (v2 -> v3)

Six new nullable `providers` columns via the existing additive
`_migrate()` pattern (never drops/renames, safe on an old database):
`hospital_affiliation`, `accepting_new_patients`, `rating`, `rating_count`,
`languages`, `insurance_accepted` (the last two are list[str] on `Provider`,
stored as a single comma-joined TEXT column). `rating`/`rating_count` are
deliberately excluded from `TRACKED_FIELDS` (own-fluctuation would spam
`provider_changes` on every refresh); `hospital_affiliation`/
`accepting_new_patients` are tracked. `exporter.py`'s `providers.xlsx` gains
the corresponding six columns.

## New: `providermap trial` command

`providermap trial [--n 10]` - a live, single-command rehearsal: discover
real URLs, enrich up to `--n` of them for real, auto-export to
`exports/output/trial/`. Distinct from the offline `providermap test`
(fixture-only, zero network) and from `scrape --limit N` + `export` (same
underlying behavior, but two steps and no separated export directory). Run
today, it produces real vCard-sourced name/phone/address data for real
providers, with the JSON-LD-only fields empty per the limitation above - not
a broken run, an honest one.

## Verification performed

- Full `pytest` suite (123 tests) passing; zero regressions from the 100-test
  baseline.
- `providermap test` (offline fixture) still reports "Pipeline is healthy."
- The Akamai-blocking table above was produced by direct, live requests
  (`httpx` and Playwright both, headless and headed where the environment
  allowed it) against real AdventHealth URLs - not inferred from
  documentation or assumed.
- **`providermap trial --n 10` run live end-to-end.** Sitemap discovery found
  50 real URLs in ~1 request round-trip; all 10 profile-page fetches got the
  expected 403; 4 providers were stored with real name, NPI, NPPES-sourced
  specialty, and vCard-sourced address/phone (`site_confidence: high`); 6
  were correctly excluded rather than mis-stored, for a reason worth noting
  separately from the Akamai block: with no HTML page to read credentials
  from, an individual NPI-1 record whose NPPES taxonomy description doesn't
  read as physician-like (e.g. some nurse-practitioner/specialty phrasings)
  and whose vCard `ORG`/`FN` doesn't carry a credential string classifies as
  `Unknown` (`validator.py`'s "NPI is an individual but provider type could
  not be determined" path) and is excluded rather than guessed. This is a
  real completeness gap specific to the vCard-only regime, not a bug -
  distinct from (and in addition to) the Akamai access limitation above.
