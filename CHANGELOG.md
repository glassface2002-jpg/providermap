# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **CMS hospital organization adapter** (`adapters/organizations/cms_hospitals/`)
  - the first concrete `OrganizationAdapter` (ROADMAP.md stage 3). Ingests
  CMS's public "Hospital General Information" CSV - a manual bulk download,
  not a live fetch (see the adapter's module docstring for why) - mapping its
  real published columns (`Facility ID`, `Facility Name`, `Address`, ...)
  onto `Organization` rows.
  - `Database.upsert_organization()` dedupes on `(source, source_id)`,
    mirroring `upsert_provider`'s approach without the change-log machinery
    (organization fields aren't tracked field-by-field yet).
  - New `providermap ingest-organizations [--org-adapter NAME] [--dry-run]`
    CLI command; `--org-adapter cms_hospitals` is the default.
  - New `providermap.parser_utils.normalize_org_name()`, reusable by any
    future organization adapter.
  - New `organizations.cms_hospitals_csv_path` config setting.
  - `adapters/providers/adventhealth/` relocation (previously
    `adapters/adventhealth/`) so the tree is symmetric with
    `adapters/organizations/` - purely a Python import path move, no
    behavior change (`AdventHealthAdapter.name` is still `"adventhealth"`).
- **Organization architecture foundation** (schema v4) - the first step of
  growing ProviderMap into a provider → organization → location platform
  (see `ROADMAP.md`). Additive only, nothing scraped or imported yet:
  - `Organization` dataclass in `providermap/models.py`.
  - Three new tables via `CREATE TABLE IF NOT EXISTS` (safe on existing
    databases): `organizations`, `provider_organizations`, and a seeded
    `sources` provenance lookup. `SCHEMA_VERSION` 3 → 4.
  - `adapters.organizations.base.OrganizationAdapter` contract
    (`discover` / `extract` / `normalize`) + a parallel
    `ORGANIZATION_ADAPTERS` registry. Empty until the first concrete source
    ships.
  - The provider pipeline, the AdventHealth adapter, and every existing test
    are untouched; `tests/test_organizations.py` verifies the new schema is
    additive and the v3 → v4 migration loses no provider data.
- **Optional Playwright-rendered fetch backend** (`providermap/render.py`,
  `site.fetch_mode: "playwright"`, `pip install -e ".[render]"`) for sites
  whose bot-management blocks plain HTTP clients. Implements the same
  `AsyncFetcher` protocol as `net.py`'s `Fetcher`, so nothing in
  `pipeline.py` changed. No bypass/evasion techniques - a normal page load,
  same as an interactive browser.
- **JSON-LD-primary profile parsing**
  (`parser_utils.parse_jsonld_physician`, wired into the AdventHealth
  adapter's `parse_profile_html`) - reads schema.org `Physician` markup when
  a site publishes it, falling back to the existing HTML title-scraping only
  when it doesn't. Adds six new `Provider` fields: `hospital_affiliation`,
  `accepting_new_patients`, `rating`, `rating_count`, `languages`,
  `insurance_accepted` (additive schema migration, v2 -> v3).
- **`providermap trial [--n 10]`** - a live, single-command rehearsal
  (discover real URLs, enrich a handful for real, auto-export) distinct from
  the offline `providermap test` and from the `scrape --limit N` + `export`
  two-step flow.
- `PROJECT_STATE.md` - a living summary of what's actually verified working
  against the live AdventHealth site.

### Fixed

- **`SitemapSource` was silently discovering zero URLs from sitemaps whose
  `<sitemapindex>` children have no naming convention.** AdventHealth's
  `sitemap.xml` splits into 15 generically-named children (`?page=1`..`15`);
  the doctor directory turned out to live entirely in child #8. The previous
  keyword/child-count heuristic in `_expand()` never recursed into it. Now
  expands every child unconditionally (bounded by a safety-valve cap, not a
  targeting heuristic). See `tests/test_sources.py`.
- Bumped `selectolax` from `0.3.21` to `0.4.10`. The old pin predates Python
  3.14 and has no prebuilt Windows wheel for it, so `pip install` fell back
  to compiling from source and failed without Microsoft's C++ Build Tools
  installed. `0.4.10` ships a prebuilt `cp314-win_amd64` wheel. Verified as a
  drop-in replacement: full test suite passes unchanged against it.
- `playwright==1.48.0` pins `greenlet==3.1.1` exactly, which has no
  prebuilt Python 3.14 wheel and fails to build without MSVC Build Tools.
  Bumped to `playwright==1.61.0`, whose `greenlet` requirement is a range
  satisfied by the prebuilt `3.5.3` wheel.

### Known limitations

- AdventHealth's rendered profile page is currently blocked by Akamai
  bot-management for both plain HTTP and a non-evasive Playwright browser
  (verified directly - see `PROJECT_STATE.md`). JSON-LD, the HTML-scraping
  fallback, and location-count/site-confidence accuracy are all affected;
  discovery and vCard-sourced enrichment are not.

## [1.0.0] - 2026-07-16

Initial public release. ProviderMap generalizes what began as a
single-purpose AdventHealth scraper into a reusable, adapter-based
provider-directory ingestion pipeline.

### Added

- **Adapter framework** (`adapters/base.py`) - site-specific knowledge (URL
  shapes, HTML structure, available endpoints) is isolated behind a
  `SiteAdapter` interface. AdventHealth ships as the reference adapter
  (`adapters/providers/adventhealth/`).
- **Source discovery**, cheapest-first: XML sitemap → Drupal JSON:API →
  Drupal views AJAX → HTML listing scrape. Auto-probed on each run, or
  pinned via `site.force_source` in config.
- **NPPES integration** for authoritative individual-vs-organization
  classification, with a configurable policy (`nppes.enabled`,
  `npi.checksum`, `npi.require_nppes`) and a circuit breaker so a failing
  registry degrades the run to heuristic classification instead of stalling
  it.
- **Incremental updates**: content-hash change detection, a field-level
  `provider_changes` audit trail, `is_active` soft-deactivation (providers
  who leave a directory are marked inactive, never deleted), and automatic,
  idempotent schema migrations.
- **Dry-run mode** (`--dry-run`) - a genuine rehearsal that reads and dedupes
  exactly as a live run would, then rolls back every write.
- **Offline test mode** (`providermap test`) - a deterministic, adversarial
  100-record fixture dataset exercising every classification and validation
  rule with zero network access.
- SQLite schema with dedupe enforced by `UNIQUE` constraints (not
  application code), foreign keys with `ON DELETE CASCADE`, and indexes on
  every column used in a `WHERE` clause.
- Excel export: `providers.xlsx` (with a "Departed" tab), `excluded_records.xlsx`,
  and `summary.xlsx` (run history, recent changes, duplicate-review queue).
- CLI commands: `test`, `investigate`, `discover`, `scrape`, `refresh`,
  `export`, `status`, `changes`, `query`.
- Full type hints across `providermap/` and `adapters/`, dataclass models
  (`Provider`, `Location`, `ExcludedRecord`, `RunRecord`, ...) in place of
  untyped dicts, and a clean `mypy --strict`-adjacent pass.
- Windows support: `setup.ps1`, `run.bat`, UTF-8 console handling, and the
  Selector event loop policy (avoids a known Proactor/httpx shutdown issue).
- pytest suite (100 tests) covering parsing, classification, database dedupe,
  incremental change detection, schema migration, dry-run rollback, and a
  full offline pipeline integration test.
- `pyproject.toml` with Black, Ruff (including import sorting), and mypy
  configuration; a GitHub Actions workflow running lint, type-check, and
  tests on every push and PR across Python 3.10-3.12; a `Dockerfile` and
  `docker-compose.yml`.

### Changed

- Project renamed from a single-purpose "AdventHealth scraper" to
  **ProviderMap**, a general-purpose pipeline with AdventHealth as its first
  adapter, in anticipation of supporting additional health systems.

### Known limitations

See the README's "Known limitations" section - notably, the AdventHealth
adapter's specialty-field extraction is inferred from `<title>`/`og:title`
rather than confirmed against a live DOM inspection, and secondary practice
locations are counted (for confidence scoring) but not yet individually
captured.
