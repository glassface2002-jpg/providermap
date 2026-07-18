# Roadmap: from provider scraper to healthcare data platform

ProviderMap began as a single-purpose tool: turn one health system's public
provider directory into a queryable database of *provider → primary site of
care*. It is now growing into a broader **healthcare data ingestion
platform** that also understands the organizations providers work for and the
locations those organizations occupy.

This document explains **why** that expansion is happening, the
**architecture decisions** behind it, and the **staged plan** to get there
safely. It is a direction document, not a status report — for what is
actually working today, see [`PROJECT_STATE.md`](PROJECT_STATE.md).

## Why organizations

The original question ProviderMap answers is *where does a provider actually
practice?* A provider practises **at an organization** (a hospital, health
system, clinic, or medical group), and that organization **occupies a
location**. Modelling only providers flattens that structure and loses
information a consumer of the data usually wants:

```
Provider            Dr. John Smith
   │
   ▼
Organization        Banner Desert Medical Center
   │
   ▼
Location            Mesa, Arizona
```

Adding organizations as a first-class entity — rather than a free-text
`practice_name` string on a provider — lets the same real hospital be
deduplicated, enriched (website, type, address), and linked to every provider
who works there, from any number of data sources.

## Architecture decisions

The expansion follows the design principle that already makes ProviderMap
extensible: **site/source-specific knowledge lives behind an adapter; the
core engine stays generic.**

- **Two adapter families, two registries.** Provider directories keep using
  `adapters.base.SiteAdapter` (registry: `ADAPTERS`). Organizations get a
  parallel `adapters.organizations.base.OrganizationAdapter` (registry:
  `ORGANIZATION_ADAPTERS`). The two are intentionally symmetric so the CLI
  and pipeline can eventually treat "which adapter" uniformly.
- **A different adapter shape for a different kind of source.** Provider
  adapters are built around fetching and parsing a *website*. Organization
  data typically comes from a *dataset* (a CMS bulk download, a registry
  export), so `OrganizationAdapter` exposes `discover() → extract() →
  normalize()` rather than a website-shaped interface. Same spirit, honest
  fit.
- **Additive schema, never destructive.** New tables arrive via
  `CREATE TABLE IF NOT EXISTS`; new columns via the existing additive
  `_migrate()`. An older database is only ever *added to* — no table or
  column is dropped or renamed, and no existing row is touched.
- **Provenance is explicit.** A `sources` table records where every piece of
  information originated (CMS, NPPES, a specific directory, manual review),
  and organizations carry `source` / `source_id`, so a mixed-source dataset
  stays auditable.

## Data model

Three tables form the organization foundation (schema v4):

- **`organizations`** — one row per real organization, deduplicated on
  `(source, source_id)`. Carries name, `normalized_name`, type, address,
  phone, website + `website_confidence`, and provenance.
- **`provider_organizations`** — the many-to-many link between a provider and
  the organizations they practise at.
- **`sources`** — the provenance lookup, seeded with CMS / NPPES /
  AdventHealth / Manual Review.

The existing `providers`, `locations`, and `provider_locations` tables are
unchanged.

## Staged plan

Each stage is a small, independently reviewable milestone. Existing provider
functionality is preserved and re-verified at every stage.

1. **Organization architecture foundation** *(done — schema v4)* — the
   `Organization` model, the three tables, the `OrganizationAdapter` contract
   and registry, and this document. No data is imported and nothing is
   scraped; the provider pipeline is untouched.
2. **Provider-adapter relocation** *(done)* — moved the working AdventHealth
   adapter to `adapters/providers/adventhealth/` so the tree is symmetric
   (`adapters/providers/…`, `adapters/organizations/…`).
3. **CMS hospital organization adapter** *(done)* — the first concrete
   `OrganizationAdapter` (`adapters/organizations/cms_hospitals/`), mapping
   CMS's public "Hospital General Information" CSV onto `Organization` rows.
   `Database.upsert_organization()` dedupes on `(source, source_id)`; the
   `providermap ingest-organizations` command runs it. The CSV is a manual
   download (see the adapter's module docstring for why), not a live fetch.
4. **Hospital website enrichment** *(done)* — `providermap/website_enrichment.py`
   discovers a candidate website from an organization's name and verifies it
   actually resolves before recording it, via `providermap enrich-organizations`.
   There is no free, reliable API for this, and a real health system's
   website frequently doesn't match the facility's legal name at all, so
   this is explicitly a **guess, never authoritative**: results are only
   ever `Confidence.LOW` or `Confidence.MEDIUM`, never `HIGH`.
   `Database.set_organization_website()` is a method deliberately separate
   from `upsert_organization()`, so a later CMS re-import (which has no
   website field) can never silently erase a discovered one.
5. **Provider ↔ organization linking** *(done — completes this staged plan)* —
   `providermap/organization_linking.py` populates `provider_organizations`,
   completing the provider → organization → location chain, via
   `providermap link-organizations`. Matching is **exact normalized-name
   only, deliberately never fuzzy**: unlike stage 4's website guess, a wrong
   link here corrupts the answer to the actual question this project
   exists to answer (*where does this provider actually practice?*), so a
   provider with no exact match is left unlinked rather than guessed at.
   `Database.link_provider_organization()` relies on the existing
   `UNIQUE(provider_id, organization_id)` constraint for idempotent dedupe.

All five stages above are now shipped. What's *not* yet covered - and would
be natural follow-on work, not part of this plan - includes a second
organization adapter (CMS's dataset only covers hospitals, not clinics or
medical groups), fuzzy/probabilistic linking with human review for the cases
exact-match deliberately leaves unlinked, and exporting the organization side
of the data (`exporter.py` still only writes provider workbooks).

## Target structure

```
adapters/
├── providers/
│   └── adventhealth/        relocated in stage 2 ✓
└── organizations/
    ├── base.py              OrganizationAdapter contract  ✓
    └── cms_hospitals/       first concrete adapter - stage 3 ✓

providermap/
├── website_enrichment.py    best-effort website discovery - stage 4 ✓
└── organization_linking.py  exact-match provider<->org linking - stage 5 ✓
```
