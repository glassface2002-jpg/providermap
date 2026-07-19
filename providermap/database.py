"""SQLite storage for the provider directory.

Schema decisions
----------------
* ``providers.npi`` and ``providers.profile_url`` are both ``UNIQUE``. That is
  the actual deduplication mechanism - not application code - so a crash
  mid-run cannot produce a duplicate row; the second insert simply becomes an
  update. ``upsert_provider`` checks NPI first, then profile_url, mirroring
  the dedupe priority in the original brief (NPI > URL > name+location; the
  third tier is human-reviewed, see ``find_probable_duplicates``, never
  auto-merged).
* ``provider_locations`` has ``FOREIGN KEY ... ON DELETE CASCADE`` to
  ``providers`` and ``locations``, and a ``UNIQUE(provider_id, location_id)``
  constraint so linking the same location twice is a no-op, not a duplicate
  row.
* Every write commits immediately (no batching transaction spanning multiple
  logical operations). The scraper is expected to be killed mid-run and
  restarted; nothing is held only in memory.
* ``scrape_log`` is the resume ledger, keyed by ``url UNIQUE``. A ``done`` URL
  is never re-fetched unless explicitly requeued or past its staleness
  window (see ``stale_urls``).

Incremental updates
--------------------
Refreshing on a schedule, rather than rebuilding, requires answering three
questions:

    What changed since last time?      -> content_hash + provider_changes
    What have I not looked at lately?  -> last_checked + stale_urls()
    What disappeared from the site?    -> is_active + last_seen_run_id

A provider who vanishes from the directory is marked ``is_active = 0``, never
deleted - historical billing data still refers to them.

Dry run
-------
``Database(path, dry_run=True)`` opens the real file - reads and dedupe
behave exactly as in a live run - but the whole session is rolled back on
close. It's a rehearsal, not a parallel code path that could drift from the
real one.

Migrations
----------
``schema_meta`` holds a single integer schema version. ``_migrate()`` runs on
every open and is idempotent: it only ever *adds* columns/tables, never drops
or renames, so opening an old database is always safe and never loses data.
Indexes are created in a separate step, after migration, specifically because
an index on a column that a migration is about to add would fail on an
old-schema database (this was a real bug caught by the migration test).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import (
    TRACKED_FIELDS,
    Confidence,
    ExcludedRecord,
    Location,
    Organization,
    Provider,
    ProviderChange,
    ProviderType,
    RunRecord,
    ScrapeStatus,
    UpsertOutcome,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 5

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY, value TEXT
);

-- Individual healthcare providers only. Facilities never land here.
CREATE TABLE IF NOT EXISTS providers (
    provider_id           INTEGER PRIMARY KEY,
    full_name             TEXT,
    first_name            TEXT,
    last_name             TEXT,
    credentials           TEXT,
    provider_type         TEXT,
    specialty             TEXT,
    primary_site_of_care  TEXT,
    practice_name         TEXT,
    address                TEXT,
    city                   TEXT,
    state                  TEXT,
    zip                    TEXT,
    phone                  TEXT,
    profile_url            TEXT UNIQUE,
    npi                    TEXT UNIQUE,
    created_date           DATETIME,
    updated_date           DATETIME,

    -- Trust signals: how much to believe primary_site_of_care.
    location_count         INTEGER DEFAULT 1,
    site_confidence        TEXT,
    nppes_enumeration      TEXT,
    nppes_name_match       INTEGER,
    taxonomy_code          TEXT,
    taxonomy_desc          TEXT,
    source_notes           TEXT,

    -- Incremental update bookkeeping.
    content_hash            TEXT,
    first_seen               DATETIME,
    last_seen                DATETIME,
    last_checked              DATETIME,
    last_seen_run_id           INTEGER,
    is_active                   INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS locations (
    location_id    INTEGER PRIMARY KEY,
    facility_name  TEXT,
    address        TEXT,
    city           TEXT,
    state          TEXT,
    zip            TEXT,
    phone          TEXT,
    address_key    TEXT UNIQUE,
    first_seen     DATETIME,
    last_seen      DATETIME
);

CREATE TABLE IF NOT EXISTS provider_locations (
    id           INTEGER PRIMARY KEY,
    provider_id  INTEGER REFERENCES providers(provider_id) ON DELETE CASCADE,
    location_id  INTEGER REFERENCES locations(location_id) ON DELETE CASCADE,
    is_primary   INTEGER DEFAULT 0,
    rank         INTEGER,
    is_active    INTEGER DEFAULT 1,
    UNIQUE(provider_id, location_id)
);

CREATE TABLE IF NOT EXISTS excluded_records (
    id               INTEGER PRIMARY KEY,
    name             TEXT,
    url              TEXT UNIQUE,
    classification   TEXT,
    reason_excluded  TEXT,
    npi              TEXT,
    date_found       DATETIME,
    last_seen        DATETIME
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id             INTEGER PRIMARY KEY,
    url            TEXT UNIQUE,
    status         TEXT,
    attempts       INTEGER DEFAULT 0,
    error_message  TEXT,
    timestamp      DATETIME,
    last_fetched   DATETIME,
    run_id         INTEGER
);

-- One row per invocation. Answers "what did last Tuesday's run actually do?"
CREATE TABLE IF NOT EXISTS runs (
    run_id              INTEGER PRIMARY KEY,
    started_at          DATETIME,
    finished_at         DATETIME,
    mode                TEXT,
    source_used         TEXT,
    urls_discovered     INTEGER DEFAULT 0,
    providers_new       INTEGER DEFAULT 0,
    providers_changed   INTEGER DEFAULT 0,
    providers_unchanged INTEGER DEFAULT 0,
    excluded            INTEGER DEFAULT 0,
    errors              INTEGER DEFAULT 0,
    notes               TEXT
);

-- Field-level audit trail: what changed, when, for whom.
CREATE TABLE IF NOT EXISTS provider_changes (
    id           INTEGER PRIMARY KEY,
    provider_id  INTEGER REFERENCES providers(provider_id) ON DELETE CASCADE,
    run_id       INTEGER,
    field        TEXT,
    old_value    TEXT,
    new_value    TEXT,
    changed_at   DATETIME
);

CREATE TABLE IF NOT EXISTS run_stats (
    key TEXT PRIMARY KEY, value TEXT
);

-- ------------------------------------------------------------------------ --
-- Organization foundation (schema v4).
--
-- These three tables lay the groundwork for the provider -> organization ->
-- location model (see ROADMAP.md). They are ADDITIVE: created here via
-- `CREATE TABLE IF NOT EXISTS` (which runs before _migrate on every open, on
-- both fresh and existing databases), touch nothing in `providers`, and are
-- not yet written to by any adapter. An old v3 database simply gains three
-- empty tables on its next open; no provider data is affected.
-- ------------------------------------------------------------------------ --

-- Hospitals, health systems, clinics, medical groups, facilities.
CREATE TABLE IF NOT EXISTS organizations (
    organization_id    INTEGER PRIMARY KEY,
    name               TEXT,
    normalized_name    TEXT,
    organization_type  TEXT,
    address            TEXT,
    city               TEXT,
    state              TEXT,
    zip                TEXT,
    phone              TEXT,
    website            TEXT,
    website_confidence TEXT,
    source             TEXT,
    source_id          TEXT,
    created_date       DATETIME,
    updated_date       DATETIME,

    -- Dedupe key: one row per (source, source_id). NULLs compare distinct in
    -- SQLite, so manually-entered orgs with no source_id are never collapsed.
    UNIQUE(source, source_id)
);

-- Many-to-many link between providers and the organizations they practise at.
CREATE TABLE IF NOT EXISTS provider_organizations (
    id               INTEGER PRIMARY KEY,
    provider_id      INTEGER REFERENCES providers(provider_id) ON DELETE CASCADE,
    organization_id  INTEGER REFERENCES organizations(organization_id) ON DELETE CASCADE,
    relationship     TEXT,      -- e.g. "employed" | "affiliated"; future use
    source           TEXT,
    is_active        INTEGER DEFAULT 1,
    UNIQUE(provider_id, organization_id)
);

-- Provenance: where a piece of information originated. Seeded idempotently
-- below so the concept is queryable from day one; adapters register their own.
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY,
    name          TEXT UNIQUE,
    description   TEXT,
    created_date  DATETIME
);

INSERT OR IGNORE INTO sources (name, description) VALUES
    ('NPPES',         'CMS National Plan & Provider Enumeration System'),
    ('CMS',           'Centers for Medicare & Medicaid Services datasets'),
    ('AdventHealth',  'AdventHealth provider directory (reference adapter)'),
    ('Manual Review', 'Human-entered or human-corrected data');
"""

# Applied AFTER _migrate(), never inside SCHEMA - see module docstring.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_providers_last_name ON providers(last_name);
CREATE INDEX IF NOT EXISTS idx_providers_full_name ON providers(full_name);
CREATE INDEX IF NOT EXISTS idx_providers_npi       ON providers(npi);
CREATE INDEX IF NOT EXISTS idx_providers_active    ON providers(is_active);
CREATE INDEX IF NOT EXISTS idx_providers_checked   ON providers(last_checked);
CREATE INDEX IF NOT EXISTS idx_scrape_log_status   ON scrape_log(status);
CREATE INDEX IF NOT EXISTS idx_scrape_log_fetched  ON scrape_log(last_fetched);
CREATE INDEX IF NOT EXISTS idx_changes_provider    ON provider_changes(provider_id);
CREATE INDEX IF NOT EXISTS idx_changes_run         ON provider_changes(run_id);

-- Organization foundation (schema v4).
CREATE INDEX IF NOT EXISTS idx_orgs_normalized_name ON organizations(normalized_name);
CREATE INDEX IF NOT EXISTS idx_orgs_state           ON organizations(state);
CREATE INDEX IF NOT EXISTS idx_orgs_source          ON organizations(source);
CREATE INDEX IF NOT EXISTS idx_prov_orgs_provider   ON provider_organizations(provider_id);
CREATE INDEX IF NOT EXISTS idx_prov_orgs_org        ON provider_organizations(organization_id);
"""

WRITE_COLS: tuple[str, ...] = (
    "full_name",
    "first_name",
    "last_name",
    "credentials",
    "provider_type",
    "specialty",
    "primary_site_of_care",
    "practice_name",
    "address",
    "city",
    "state",
    "zip",
    "phone",
    "profile_url",
    "npi",
    "location_count",
    "site_confidence",
    "nppes_enumeration",
    "nppes_name_match",
    "taxonomy_code",
    "taxonomy_desc",
    "source_notes",
    "hospital_affiliation",
    "accepting_new_patients",
    "rating",
    "rating_count",
    "languages",
    "insurance_accepted",
)

# languages/insurance_accepted are list[str] on Provider but stored as a
# single comma-joined TEXT column - simpler than a join table for data that's
# read as a whole, never queried by individual member.
_LIST_COLS = {"languages", "insurance_accepted"}

# Dedupe keys are never overwritten with NULL by a later pass - a re-scrape
# that fails to determine one keeps what was already proven correct.
_PROTECTED_COLS = {"npi", "profile_url"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_for_diff(value: object) -> str | None:
    """Coerce a value from either side of a before/after comparison to the
    same string form, so ``1`` (from SQLite) and ``"1"`` (from a freshly
    stringified :class:`~providermap.models.Provider`) compare equal instead
    of producing a spurious change entry. Empty string and ``None`` are also
    treated as equivalent - "" arriving from SQL and ``None`` from a dataclass
    default are the same absence of a value, not a change.
    """
    if value is None or value == "":
        return None
    return str(value)


def _provider_row_values(p: Provider) -> list[str | int | float | None]:
    """Serialize a Provider's WRITE_COLS to SQL-ready values (enums -> str)."""
    values: list[str | int | float | None] = []
    for col in WRITE_COLS:
        v = getattr(p, col)
        if isinstance(v, ProviderType | Confidence):
            v = v.value
        elif isinstance(v, bool):
            v = int(v)
        elif col in _LIST_COLS:
            v = ", ".join(v) if v else None
        values.append(v)
    return values


def content_hash(p: Provider) -> str:
    """Stable hash of the tracked fields, so unchanged records stay cheap."""
    payload = json.dumps(p.tracked_snapshot(), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _row_to_provider(row: sqlite3.Row) -> Provider:
    keys = row.keys()
    return Provider(
        provider_id=row["provider_id"],
        full_name=row["full_name"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        credentials=row["credentials"],
        provider_type=ProviderType(row["provider_type"]) if row["provider_type"] else None,
        specialty=row["specialty"],
        primary_site_of_care=row["primary_site_of_care"],
        practice_name=row["practice_name"],
        address=row["address"],
        city=row["city"],
        state=row["state"],
        zip=row["zip"],
        phone=row["phone"],
        profile_url=row["profile_url"],
        npi=row["npi"],
        location_count=row["location_count"] or 1,
        site_confidence=Confidence(row["site_confidence"]) if row["site_confidence"] else None,
        nppes_enumeration=row["nppes_enumeration"],
        nppes_name_match=(
            bool(row["nppes_name_match"]) if row["nppes_name_match"] is not None else None
        ),
        taxonomy_code=row["taxonomy_code"],
        taxonomy_desc=row["taxonomy_desc"],
        source_notes=row["source_notes"],
        hospital_affiliation=(
            row["hospital_affiliation"] if "hospital_affiliation" in keys else None
        ),
        accepting_new_patients=(
            bool(row["accepting_new_patients"])
            if "accepting_new_patients" in keys and row["accepting_new_patients"] is not None
            else None
        ),
        rating=row["rating"] if "rating" in keys else None,
        rating_count=row["rating_count"] if "rating_count" in keys else None,
        languages=(
            [s.strip() for s in row["languages"].split(",") if s.strip()]
            if "languages" in keys and row["languages"]
            else []
        ),
        insurance_accepted=(
            [s.strip() for s in row["insurance_accepted"].split(",") if s.strip()]
            if "insurance_accepted" in keys and row["insurance_accepted"]
            else []
        ),
        created_date=row["created_date"],
        updated_date=row["updated_date"],
        first_seen=row["first_seen"] if "first_seen" in keys else None,
        last_seen=row["last_seen"] if "last_seen" in keys else None,
        last_checked=row["last_checked"] if "last_checked" in keys else None,
        is_active=(
            bool(row["is_active"]) if "is_active" in keys and row["is_active"] is not None else True
        ),
    )


def _row_to_organization(row: sqlite3.Row) -> Organization:
    return Organization(
        organization_id=row["organization_id"],
        name=row["name"],
        normalized_name=row["normalized_name"],
        organization_type=row["organization_type"],
        address=row["address"],
        city=row["city"],
        state=row["state"],
        zip=row["zip"],
        phone=row["phone"],
        website=row["website"],
        website_confidence=(
            Confidence(row["website_confidence"]) if row["website_confidence"] else None
        ),
        source=row["source"],
        source_id=row["source_id"],
        created_date=row["created_date"],
        updated_date=row["updated_date"],
    )


class Database:
    def __init__(self, path: str | Path, dry_run: bool = False):
        self.path = Path(path)
        self.dry_run = dry_run
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()
        self.conn.executescript(INDEXES)
        self.conn.commit()
        self.run_id: int | None = None
        self.pending_writes = 0
        if dry_run:
            log.info("DRY RUN: reads are live, every write will be rolled back.")

    # ------------------------------------------------------------------ #
    # migrations
    # ------------------------------------------------------------------ #

    def _migrate(self) -> None:
        """Bring an older database up to the current schema, in place,
        preserving every existing row."""
        row = self.conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        version = int(row["value"]) if row else 0
        if version >= SCHEMA_VERSION:
            return

        additions = {
            "providers": [
                ("content_hash", "TEXT"),
                ("first_seen", "DATETIME"),
                ("last_seen", "DATETIME"),
                ("last_checked", "DATETIME"),
                ("last_seen_run_id", "INTEGER"),
                ("is_active", "INTEGER DEFAULT 1"),
                ("hospital_affiliation", "TEXT"),
                ("accepting_new_patients", "INTEGER"),
                ("rating", "REAL"),
                ("rating_count", "INTEGER"),
                ("languages", "TEXT"),
                ("insurance_accepted", "TEXT"),
            ],
            "locations": [("first_seen", "DATETIME"), ("last_seen", "DATETIME")],
            "provider_locations": [("is_active", "INTEGER DEFAULT 1")],
            "excluded_records": [("last_seen", "DATETIME")],
            "scrape_log": [("last_fetched", "DATETIME"), ("run_id", "INTEGER")],
            # v5: lets a failed website lookup be distinguished from a
            # never-attempted one - see organizations_missing_website().
            "organizations": [("website_checked_at", "DATETIME")],
        }
        for table, cols in additions.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    log.info("migration: added %s.%s", table, name)

        self.conn.execute("UPDATE providers SET is_active=1 WHERE is_active IS NULL")
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()
        if version:
            log.info("migrated database schema v%s -> v%s", version, SCHEMA_VERSION)

    def _commit(self) -> None:
        """No-op under dry-run so the whole session can be rolled back."""
        if self.dry_run:
            self.pending_writes += 1
            return
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # runs
    # ------------------------------------------------------------------ #

    def start_run(self, mode: str, source_used: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, mode, source_used) VALUES (?, ?, ?)",
            (_now(), mode, source_used),
        )
        self._commit()
        self.run_id = cur.lastrowid
        assert self.run_id is not None
        return self.run_id

    def set_run_source(self, source_name: str) -> None:
        """Record which discovery source the current run selected."""
        if not self.run_id:
            return
        self.conn.execute(
            "UPDATE runs SET source_used=? WHERE run_id=?", (source_name, self.run_id)
        )
        self._commit()

    def finish_run(self, **counts: object) -> None:
        if not self.run_id:
            return
        allowed = {
            "urls_discovered",
            "providers_new",
            "providers_changed",
            "providers_unchanged",
            "excluded",
            "errors",
            "notes",
            "source_used",
        }
        fields = {k: v for k, v in counts.items() if k in allowed}
        sets = ", ".join(f"{k}=?" for k in fields)
        sql = "UPDATE runs SET finished_at=?" + (f", {sets}" if sets else "") + " WHERE run_id=?"
        self.conn.execute(sql, [_now(), *fields.values(), self.run_id])
        self._commit()

    def last_run(self) -> RunRecord | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        return _row_to_run(row) if row else None

    def recent_runs(self, n: int = 10) -> list[RunRecord]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (n,))
        return [_row_to_run(r) for r in rows]

    # ------------------------------------------------------------------ #
    # resume ledger
    # ------------------------------------------------------------------ #

    def enqueue(self, urls: Iterable[str]) -> int:
        rows = [(u, ScrapeStatus.PENDING.value, _now(), self.run_id) for u in urls]
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO scrape_log (url, status, timestamp, run_id) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        self._commit()
        return cur.rowcount

    def pending_urls(self, limit: int | None = None) -> list[str]:
        sql = (
            "SELECT url FROM scrape_log "
            "WHERE status IN ('pending', 'error') AND attempts < 4 "
            "ORDER BY attempts ASC, id ASC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["url"] for r in self.conn.execute(sql)]

    def stale_urls(self, older_than_days: int, limit: int | None = None) -> list[str]:
        """Done URLs not re-checked in N days - the incremental refresh queue.

        ``older_than_days <= 0`` means "re-check everything", handled as a
        separate branch: timestamps are stored truncated to the second, so a
        strict ``<`` against the current second silently matches nothing.
        """
        if older_than_days <= 0:
            sql = "SELECT url FROM scrape_log WHERE status='done' ORDER BY id ASC"
            if limit:
                sql += f" LIMIT {int(limit)}"
            return [r["url"] for r in self.conn.execute(sql)]

        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(
            timespec="seconds"
        )
        sql = (
            "SELECT url FROM scrape_log WHERE status='done' "
            "AND (last_fetched IS NULL OR last_fetched < ?) ORDER BY last_fetched ASC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["url"] for r in self.conn.execute(sql, (cutoff,))]

    def mark(self, url: str, status: ScrapeStatus | str, error: str | None = None) -> None:
        status_val = status.value if isinstance(status, ScrapeStatus) else status
        self.conn.execute(
            "UPDATE scrape_log SET status=?, attempts=attempts+1, error_message=?, "
            "timestamp=?, last_fetched=?, run_id=? WHERE url=?",
            (status_val, error, _now(), _now(), self.run_id, url),
        )
        self._commit()

    def requeue(self, urls: Iterable[str]) -> int:
        """Force re-processing of URLs already marked done."""
        n = 0
        for u in urls:
            cur = self.conn.execute(
                "UPDATE scrape_log SET status='pending', attempts=0 WHERE url=?", (u,)
            )
            n += cur.rowcount
        self._commit()
        return n

    def is_done(self, url: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM scrape_log WHERE url=? AND status='done'", (url,)
            ).fetchone()
            is not None
        )

    # ------------------------------------------------------------------ #
    # providers
    # ------------------------------------------------------------------ #

    def upsert_provider(self, provider: Provider) -> tuple[int | None, UpsertOutcome]:
        """Insert or update one provider. Dedupes on NPI first, then
        ``profile_url`` - both ``UNIQUE`` columns, so the constraint (not
        application logic) is what prevents duplicates surviving a crash.

        Returns ``(provider_id, outcome)``. ``UNCHANGED`` means the content
        hash matched: the row is touched (``last_seen``/``last_checked``) but
        nothing is rewritten and no change rows are logged.
        """
        existing = None
        if provider.npi:
            existing = self.conn.execute(
                "SELECT * FROM providers WHERE npi=?", (provider.npi,)
            ).fetchone()
        if existing is None and provider.profile_url:
            existing = self.conn.execute(
                "SELECT * FROM providers WHERE profile_url=?", (provider.profile_url,)
            ).fetchone()

        h = content_hash(provider)
        values = _provider_row_values(provider)

        if existing is None:
            placeholders = ", ".join("?" for _ in WRITE_COLS)
            cur = self.conn.execute(
                f"INSERT INTO providers ({', '.join(WRITE_COLS)}, content_hash, "
                f"created_date, updated_date, first_seen, last_seen, last_checked, "
                f"last_seen_run_id, is_active) "
                f"VALUES ({placeholders}, ?, ?, ?, ?, ?, ?, ?, 1)",
                [*values, h, _now(), _now(), _now(), _now(), _now(), self.run_id],
            )
            self._commit()
            return cur.lastrowid, UpsertOutcome.NEW

        pid: int = existing["provider_id"]

        if existing["content_hash"] == h and existing["is_active"] == 1:
            self.conn.execute(
                "UPDATE providers SET last_seen=?, last_checked=?, last_seen_run_id=? "
                "WHERE provider_id=?",
                (_now(), _now(), self.run_id, pid),
            )
            self._commit()
            return pid, UpsertOutcome.UNCHANGED

        new_snapshot = provider.tracked_snapshot()
        for f in TRACKED_FIELDS:
            # Both sides must go through the same string normalization before
            # comparing - `existing[f]` comes back from SQLite as whatever
            # type the column stores (e.g. `int` for location_count), while
            # `new_snapshot` is pre-stringified. Comparing `1 != "1"` without
            # normalizing both sides logs a false "changed" entry on every
            # write to a numeric field, even when nothing moved.
            # sqlite3.Row is not a dict: `f in existing` would check membership
            # among the row's *values*, not its column names, so `.keys()` is
            # required here despite what a dict-oriented linter suggests.
            has_field = f in existing.keys()  # noqa: SIM118
            old = _normalize_for_diff(existing[f]) if has_field else None
            new = _normalize_for_diff(new_snapshot.get(f))
            if old != new:
                self.conn.execute(
                    "INSERT INTO provider_changes (provider_id, run_id, field, "
                    "old_value, new_value, changed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, self.run_id, f, old, new, _now()),
                )

        sets = ", ".join(
            f"{c}=COALESCE(?, {c})" if c in _PROTECTED_COLS else f"{c}=?" for c in WRITE_COLS
        )
        self.conn.execute(
            f"UPDATE providers SET {sets}, content_hash=?, updated_date=?, last_seen=?, "
            f"last_checked=?, last_seen_run_id=?, is_active=1 WHERE provider_id=?",
            [*values, h, _now(), _now(), _now(), self.run_id, pid],
        )
        self._commit()
        return pid, UpsertOutcome.CHANGED

    def get_provider(
        self, npi: str | None = None, profile_url: str | None = None
    ) -> Provider | None:
        if npi:
            row = self.conn.execute("SELECT * FROM providers WHERE npi=?", (npi,)).fetchone()
        elif profile_url:
            row = self.conn.execute(
                "SELECT * FROM providers WHERE profile_url=?", (profile_url,)
            ).fetchone()
        else:
            raise ValueError("get_provider requires npi or profile_url")
        return _row_to_provider(row) if row else None

    def search_providers(
        self, query: str, limit: int = 25, active_only: bool = False
    ) -> list[Provider]:
        sql = "SELECT * FROM providers WHERE (full_name LIKE ? OR last_name LIKE ? OR npi = ?)"
        if active_only:
            sql += " AND is_active = 1"
        sql += " ORDER BY is_active DESC, full_name LIMIT ?"
        rows = self.conn.execute(sql, (f"%{query}%", f"%{query}%", query, limit))
        return [_row_to_provider(r) for r in rows]

    def deactivate_missing(self, run_id: int) -> int:
        """Mark providers not seen in this run as inactive.

        Only safe after a COMPLETE discovery pass - calling it after a
        partial/``--limit`` run would deactivate everyone simply not reached.
        Never deletes: historical billing rows still point at these people.
        """
        cur = self.conn.execute(
            "UPDATE providers SET is_active=0, updated_date=? "
            "WHERE is_active=1 AND (last_seen_run_id IS NULL OR last_seen_run_id != ?)",
            (_now(), run_id),
        )
        self._commit()
        if cur.rowcount:
            log.warning(
                "%s providers were not seen in this run and are now marked "
                "inactive (not deleted).",
                cur.rowcount,
            )
        return cur.rowcount

    def changes_since(self, run_id: int) -> list[ProviderChange]:
        rows = self.conn.execute(
            "SELECT c.*, p.full_name, p.npi FROM provider_changes c "
            "LEFT JOIN providers p ON p.provider_id = c.provider_id "
            "WHERE c.run_id >= ? ORDER BY c.id DESC",
            (run_id,),
        )
        return [
            ProviderChange(
                provider_id=r["provider_id"],
                run_id=r["run_id"],
                field=r["field"],
                old_value=r["old_value"],
                new_value=r["new_value"],
                changed_at=r["changed_at"],
                full_name=r["full_name"],
                npi=r["npi"],
            )
            for r in rows
        ]

    def find_probable_duplicates(self) -> list[sqlite3.Row]:
        """Same name + same city, different NPI - reported, never auto-merged."""
        return list(
            self.conn.execute(
                """
            SELECT full_name, city, state, COUNT(*) AS n,
                   GROUP_CONCAT(npi, ' | ')         AS npis,
                   GROUP_CONCAT(profile_url, ' | ') AS urls
            FROM providers
            WHERE full_name IS NOT NULL AND full_name <> '' AND is_active = 1
            GROUP BY LOWER(full_name), LOWER(COALESCE(city, '')), COALESCE(state, '')
            HAVING n > 1
            ORDER BY n DESC, full_name
            """
            )
        )

    # ------------------------------------------------------------------ #
    # locations
    # ------------------------------------------------------------------ #

    def upsert_location(self, location: Location) -> int | None:
        key = location.address_key
        if not key:
            return None
        row = self.conn.execute(
            "SELECT location_id FROM locations WHERE address_key=?", (key,)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE locations SET last_seen=? WHERE location_id=?", (_now(), row["location_id"])
            )
            self._commit()
            loc_id: int = row["location_id"]
            return loc_id
        cur = self.conn.execute(
            "INSERT INTO locations (facility_name, address, city, state, zip, phone, "
            "address_key, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                location.facility_name,
                location.address,
                location.city,
                location.state,
                location.zip,
                location.phone,
                key,
                _now(),
                _now(),
            ),
        )
        self._commit()
        return cur.lastrowid

    def link(
        self, provider_id: int, location_id: int, is_primary: bool = False, rank: int = 0
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO provider_locations "
            "(provider_id, location_id, is_primary, rank, is_active) VALUES (?, ?, ?, ?, 1)",
            (provider_id, location_id, int(is_primary), rank),
        )
        self._commit()

    # ------------------------------------------------------------------ #
    # organizations
    # ------------------------------------------------------------------ #

    def upsert_organization(self, org: Organization) -> tuple[int | None, UpsertOutcome]:
        """Insert or update one organization. Dedupes on ``(source, source_id)``
        - the same ``UNIQUE`` constraint the schema enforces (see this
        module's docstring), not application logic, so a crash mid-import
        cannot produce a duplicate row.

        Unlike :meth:`upsert_provider`, there is no content hash or change
        log yet - organization fields aren't tracked field-by-field (see
        ROADMAP.md); a re-import simply refreshes the row. Returns
        ``(organization_id, outcome)``; the three outcomes mirror the
        provider ones so call sites can tally both the same way.
        """
        existing = None
        if org.source and org.source_id:
            existing = self.conn.execute(
                "SELECT * FROM organizations WHERE source=? AND source_id=?",
                (org.source, org.source_id),
            ).fetchone()

        website_confidence = org.website_confidence.value if org.website_confidence else None

        if existing is None:
            cur = self.conn.execute(
                "INSERT INTO organizations (name, normalized_name, organization_type, "
                "address, city, state, zip, phone, website, website_confidence, "
                "source, source_id, created_date, updated_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    org.name,
                    org.normalized_name,
                    org.organization_type,
                    org.address,
                    org.city,
                    org.state,
                    org.zip,
                    org.phone,
                    org.website,
                    website_confidence,
                    org.source,
                    org.source_id,
                    _now(),
                    _now(),
                ),
            )
            self._commit()
            return cur.lastrowid, UpsertOutcome.NEW

        oid: int = existing["organization_id"]
        tracked_cols = (
            "name",
            "normalized_name",
            "organization_type",
            "address",
            "city",
            "state",
            "zip",
            "phone",
        )
        new_values = (
            org.name,
            org.normalized_name,
            org.organization_type,
            org.address,
            org.city,
            org.state,
            org.zip,
            org.phone,
        )
        if all(existing[c] == v for c, v in zip(tracked_cols, new_values, strict=True)):
            self.conn.execute(
                "UPDATE organizations SET updated_date=? WHERE organization_id=?", (_now(), oid)
            )
            self._commit()
            return oid, UpsertOutcome.UNCHANGED

        self.conn.execute(
            "UPDATE organizations SET name=?, normalized_name=?, organization_type=?, "
            "address=?, city=?, state=?, zip=?, phone=?, updated_date=? WHERE organization_id=?",
            (*new_values, _now(), oid),
        )
        self._commit()
        return oid, UpsertOutcome.CHANGED

    def set_organization_website(
        self, organization_id: int, website: str, confidence: Confidence
    ) -> None:
        """Record a discovered website for an existing organization.

        Deliberately a separate method from :meth:`upsert_organization`
        rather than folded into its UPDATE: that method's SET clause is
        driven by an ingestion adapter's fields (name, address, ...), none
        of which currently include a website. If website/website_confidence
        were part of that UPDATE, a later re-import from an adapter with no
        website field (e.g. ``cms_hospitals``) would pass ``None`` for both
        and silently null out whatever this method previously found. This
        method only ever touches these two columns (plus ``website_checked_at``
        - see :meth:`mark_website_checked` for the failed-lookup counterpart).
        """
        self.conn.execute(
            "UPDATE organizations SET website=?, website_confidence=?, "
            "website_checked_at=?, updated_date=? WHERE organization_id=?",
            (website, confidence.value, _now(), _now(), organization_id),
        )
        self._commit()

    def mark_website_checked(self, organization_id: int) -> None:
        """Record that a website lookup was *attempted* for this
        organization, even though nothing was found.

        Without this, ``organizations_missing_website`` has no way to tell
        "never looked" apart from "looked and found nothing" - every
        ``enrich-organizations`` run would re-guess domains for every
        hospital that previously came up empty, forever. Touches only
        ``website_checked_at``; ``website``/``website_confidence`` stay
        ``NULL`` so a future run (past ``retry_after_days``) can still try
        again - a hospital's site can appear later, or Wikidata's data can
        improve.
        """
        self.conn.execute(
            "UPDATE organizations SET website_checked_at=? WHERE organization_id=?",
            (_now(), organization_id),
        )
        self._commit()

    def organizations_missing_website(
        self, limit: int | None = None, retry_after_days: int | None = None
    ) -> list[Organization]:
        """Organizations with no website yet - the input set for
        ``providermap enrich-organizations``.

        ``retry_after_days`` (default ``None``, meaning no filter - always
        include every organization with no website) excludes organizations
        whose last lookup attempt (:meth:`mark_website_checked` or
        :meth:`set_organization_website`) was more recent than that many
        days ago, mirroring :meth:`stale_urls`'s convention for the provider
        side: ``<= 0`` means "recheck everything, ignore how recently it was
        checked."
        """
        sql = "SELECT * FROM organizations WHERE website IS NULL"
        params: list[object] = []
        if retry_after_days is not None and retry_after_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=retry_after_days)).isoformat(
                timespec="seconds"
            )
            sql += " AND (website_checked_at IS NULL OR website_checked_at < ?)"
            params.append(cutoff)
        sql += " ORDER BY organization_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_organization(r) for r in rows]

    def link_provider_organization(
        self, provider_id: int, organization_id: int, source: str
    ) -> bool:
        """Link a provider to an organization. Idempotent - relies on the
        ``UNIQUE(provider_id, organization_id)`` constraint for dedupe, the
        same pattern :meth:`link` uses for ``provider_locations``. Returns
        ``True`` if a new link row was created, ``False`` if it already
        existed (``INSERT OR IGNORE`` silently no-ops on the constraint).
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO provider_organizations "
            "(provider_id, organization_id, source, is_active) VALUES (?, ?, ?, 1)",
            (provider_id, organization_id, source),
        )
        self._commit()
        return cur.rowcount > 0

    def providers_for_organization_linking(self, limit: int | None = None) -> list[Provider]:
        """Active providers with a ``practice_name`` or
        ``hospital_affiliation`` to match against, not yet linked to any
        organization - the input set for ``providermap link-organizations``.
        Excluding already-linked providers keeps re-running the command
        cheap and idempotent rather than re-checking everyone every time.
        """
        sql = (
            "SELECT * FROM providers WHERE is_active = 1 "
            "AND (practice_name IS NOT NULL OR hospital_affiliation IS NOT NULL) "
            "AND provider_id NOT IN (SELECT provider_id FROM provider_organizations) "
            "ORDER BY provider_id"
        )
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_provider(r) for r in rows]

    def organizations_by_normalized_name(self) -> dict[str, Organization]:
        """Every organization keyed by ``normalized_name``, for exact-match
        linking (see ``providermap/organization_linking.py``). Last-one-wins
        on a duplicate normalized name - two real organizations sharing one
        is rare, and exact-name matching alone can't disambiguate them
        anyway; that's a case for a future, more specific adapter, not a
        guess here.
        """
        rows = self.conn.execute(
            "SELECT * FROM organizations WHERE normalized_name IS NOT NULL"
        ).fetchall()
        return {r["normalized_name"]: _row_to_organization(r) for r in rows}

    # ------------------------------------------------------------------ #
    # exclusions
    # ------------------------------------------------------------------ #

    def exclude(self, record: ExcludedRecord) -> None:
        row = self.conn.execute(
            "SELECT date_found FROM excluded_records WHERE url=?", (record.url,)
        ).fetchone()
        first_seen = row["date_found"] if row else _now()
        self.conn.execute(
            "INSERT OR REPLACE INTO excluded_records "
            "(id, name, url, classification, reason_excluded, npi, date_found, last_seen) "
            "VALUES ((SELECT id FROM excluded_records WHERE url=?), ?, ?, ?, ?, ?, ?, ?)",
            (
                record.url,
                record.name,
                record.url,
                record.classification,
                record.reason_excluded,
                record.npi,
                first_seen,
                _now(),
            ),
        )
        self._commit()

    # ------------------------------------------------------------------ #
    # misc stats
    # ------------------------------------------------------------------ #

    def set_stat(self, key: str, value: object) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO run_stats (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        self._commit()

    def get_stat(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM run_stats WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            result: int = self.conn.execute(sql).fetchone()[0]
            return result

        return {
            "providers": one("SELECT COUNT(*) FROM providers WHERE is_active=1"),
            "providers_inactive": one("SELECT COUNT(*) FROM providers WHERE is_active=0"),
            "locations": one("SELECT COUNT(*) FROM locations"),
            "excluded": one("SELECT COUNT(*) FROM excluded_records"),
            "urls_total": one("SELECT COUNT(*) FROM scrape_log"),
            "urls_done": one("SELECT COUNT(*) FROM scrape_log WHERE status='done'"),
            "urls_error": one("SELECT COUNT(*) FROM scrape_log WHERE status='error'"),
            "urls_pending": one("SELECT COUNT(*) FROM scrape_log WHERE status='pending'"),
            "multi_location": one(
                "SELECT COUNT(*) FROM providers WHERE location_count > 1 AND is_active=1"
            ),
            "changes_logged": one("SELECT COUNT(*) FROM provider_changes"),
        }

    def close(self) -> None:
        if self.dry_run:
            self.conn.rollback()
            log.info(
                "DRY RUN: rolled back %s write operation(s). Database on disk is " "unchanged.",
                self.pending_writes,
            )
        self.conn.close()


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(**{k: row[k] for k in row.keys()})  # noqa: SIM118 - sqlite3.Row, not a dict


@contextmanager
def open_db(path: str | Path, dry_run: bool = False) -> Iterator[Database]:
    db = Database(path, dry_run=dry_run)
    try:
        yield db
    finally:
        db.close()
