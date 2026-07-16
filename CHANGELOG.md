# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Bumped `selectolax` from `0.3.21` to `0.4.10`. The old pin predates Python
  3.14 and has no prebuilt Windows wheel for it, so `pip install` fell back
  to compiling from source and failed without Microsoft's C++ Build Tools
  installed. `0.4.10` ships a prebuilt `cp314-win_amd64` wheel. Verified as a
  drop-in replacement: full test suite passes unchanged against it.

## [1.0.0] - 2026-07-16

Initial public release. ProviderMap generalizes what began as a
single-purpose AdventHealth scraper into a reusable, adapter-based
provider-directory ingestion pipeline.

### Added

- **Adapter framework** (`adapters/base.py`) - site-specific knowledge (URL
  shapes, HTML structure, available endpoints) is isolated behind a
  `SiteAdapter` interface. AdventHealth ships as the reference adapter
  (`adapters/adventhealth/`).
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
