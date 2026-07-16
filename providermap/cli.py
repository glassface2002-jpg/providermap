"""ProviderMap command-line interface.

Every command is adapter-driven: ``--adapter adventhealth`` (the default, and
currently the only registered adapter) selects which
:class:`~adapters.base.SiteAdapter` the pipeline uses. Adding a second health
system's adapter makes it available here automatically via
:data:`adapters.ADAPTERS` - no CLI changes required.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from adapters import ADAPTERS, SiteAdapter, get_adapter_class

from . import discover as discovery
from .config import Config, load_config
from .database import Database
from .net import AsyncFetcher, Cache, Fetcher
from .nppes import NppesClient
from .pipeline import Pipeline


def _setup_windows_console() -> None:
    """Fixes specific to running on Windows, applied before anything else
    touches asyncio or stdout.

    The default Proactor event loop raises spurious "Event loop is closed"
    errors on shutdown with httpx; the selector loop is well within our
    2-connection concurrency cap. The Windows console defaults to cp1252,
    which cannot encode provider names like "Ruiz-Sánchez" and would crash
    mid-run without the UTF-8 reconfiguration below.
    """
    if sys.platform != "win32":
        return
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def setup_logging(config: Config) -> None:
    logdir = Path(config.logging.dir)
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    logging.basicConfig(
        level=getattr(logging, config.logging.level),
        format="%(asctime)s | %(levelname)-7s | %(name)-24s | %(message)s",
        handlers=[
            logging.FileHandler(logdir / f"run_{stamp}.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def check_user_agent(config: Config) -> None:
    if "REPLACE_WITH_YOUR_EMAIL" in config.politeness.user_agent:
        print(
            "\nSTOP: set a real contact address in config.yaml "
            "(politeness.user_agent), or export PROVIDERMAP_CONTACT_EMAIL.\n\n"
            "A pipeline that does not identify itself is one the site operator "
            "cannot contact when something goes wrong. Running thousands of "
            "requests anonymously against a healthcare system's site is a bad "
            "idea. Set a contact address first.\n\n"
            "(`providermap test` works offline and needs no contact address.)\n",
            file=sys.stderr,
        )
        sys.exit(2)


def make_adapter(config: Config, name: str) -> SiteAdapter:
    return get_adapter_class(name)(config)


def make_fetcher(config: Config, cache: Cache, test_mode: bool, adapter_name: str) -> AsyncFetcher:
    if test_mode:
        if adapter_name != "adventhealth":
            raise ValueError(
                f"--test mode is only implemented for the adventhealth "
                f"adapter's fixtures, got '{adapter_name}'"
            )
        from adapters.adventhealth.fixtures import OfflineFetcher

        return OfflineFetcher(config, cache)
    return Fetcher(config, cache)


def db_path(config: Config, test_mode: bool) -> Path:
    if test_mode:
        return Path("database") / "test_fixture.db"
    return Path(config.database.path)


# ---------------------------------------------------------------------- #
# commands
# ---------------------------------------------------------------------- #


async def cmd_investigate(config: Config, adapter: SiteAdapter, args: argparse.Namespace) -> None:
    result = await discovery.run(config, adapter, sample_size=args.sample)
    if result.get("blocked"):
        print("\nrobots.txt disallows the directory path. Stopping.\n")
        return
    report = Path(config.logging.dir) / "investigation_report.md"
    print(f"\nInvestigation complete. Read the report before scraping:\n  {report}\n")
    sample = result.get("sample", {})
    labels: dict[str, int] = sample.get("labels", {})
    total = sample.get("sampled", 0) or 1
    print(f"Sampled {total} records:")
    for label, count in sorted(labels.items(), key=lambda x: -x[1]):
        print(f"  {label:<32} {count:>5}  ({count / total * 100:5.1f}%)")


async def _run_pipeline(
    config: Config, adapter: SiteAdapter, args: argparse.Namespace, mode: str
) -> None:
    test = getattr(args, "test", False) or mode == "test"
    dry = getattr(args, "dry_run", False)

    db = Database(db_path(config, test), dry_run=dry)
    cache = Cache(config.cache.dir, config.cache.enabled and not test, config.cache.ttl_days)
    pipeline = Pipeline(config, db, adapter, dry_run=dry)
    run_id = db.start_run(mode="dry-run" if dry else mode)

    complete_discovery = False
    fetcher = make_fetcher(config, cache, test, adapter.name)
    async with fetcher as f:
        nppes = NppesClient(f, config)

        if mode == "refresh":
            days = (
                args.older_than
                if args.older_than is not None
                else config.incremental.refresh_older_than_days
            )
            urls = db.stale_urls(days, limit=args.limit)
            if not urls:
                print(f"\nNothing older than {days} days. Nothing to refresh.\n")
                db.finish_run(notes="nothing stale")
                db.close()
                return
            print(f"\nRefreshing {len(urls)} records not checked in {days}+ days.\n")
            db.requeue(urls)
            tally = await pipeline.enrich_all(f, nppes, urls=urls)
        else:
            if db.counts()["urls_total"] == 0 or getattr(args, "rediscover", False):
                await pipeline.discover_urls(f, max_items=getattr(args, "max_items", None))
                complete_discovery = getattr(args, "max_items", None) is None
            try:
                tally = await pipeline.enrich_all(f, nppes, limit=args.limit)
            except RuntimeError as exc:
                print(f"\n{exc}\n", file=sys.stderr)
                db.finish_run(notes=str(exc)[:200])
                db.close()
                sys.exit(1)

    partial = bool(args.limit) or not complete_discovery
    if config.incremental.deactivate_missing and not partial and mode in ("full", "test"):
        db.deactivate_missing(run_id)
    elif config.incremental.deactivate_missing and partial:
        logging.getLogger(__name__).info(
            "Skipping deactivate_missing: this was a partial run, so absence "
            "does not imply removal from the directory."
        )

    c = db.counts()
    t = tally.as_dict()
    db.finish_run(
        urls_discovered=c["urls_total"],
        providers_new=t.get("new", 0),
        providers_changed=t.get("changed", 0),
        providers_unchanged=t.get("unchanged", 0),
        excluded=t.get("excluded", 0),
        errors=t.get("error", 0),
        source_used=pipeline.source.name if pipeline.source else None,
    )

    banner = "DRY RUN - nothing was written" if dry else "Done"
    print(f"\n{banner}.")
    print(f"  source used     : {pipeline.source.name if pipeline.source else 'n/a'}")
    print(f"  new             : {t.get('new', 0)}")
    print(f"  changed         : {t.get('changed', 0)}")
    print(f"  unchanged       : {t.get('unchanged', 0)}")
    print(f"  excluded        : {t.get('excluded', 0)}")
    print(f"  errors          : {t.get('error', 0)}")
    tripped = " (CIRCUIT BREAKER TRIPPED)" if nppes.tripped else ""
    print(f"  NPPES           : {nppes.hits} hits / {nppes.misses} misses{tripped}")
    if dry:
        print(f"\n  {db.pending_writes} write operation(s) were rolled back.")
    elif c["urls_pending"]:
        print(f"\n  {c['urls_pending']} still pending - re-run to continue.")
    db.close()


async def cmd_discover(config: Config, adapter: SiteAdapter, args: argparse.Namespace) -> None:
    test = args.test
    db = Database(db_path(config, test), dry_run=args.dry_run)
    cache = Cache(config.cache.dir, config.cache.enabled and not test, config.cache.ttl_days)
    pipeline = Pipeline(config, db, adapter, dry_run=args.dry_run)
    db.start_run(mode="discover")
    async with make_fetcher(config, cache, test, adapter.name) as f:
        new = await pipeline.discover_urls(f, max_items=args.max_items)
    db.finish_run(
        urls_discovered=db.counts()["urls_total"],
        source_used=pipeline.source.name if pipeline.source else None,
    )
    print(f"\nSource used: {pipeline.source.name if pipeline.source else 'none'}")
    print(f"Queued {new} new profile URLs. Total queue: {db.counts()['urls_total']}\n")
    db.close()


def cmd_export(config: Config, args: argparse.Namespace) -> None:
    from .exporter import export_all

    db = Database(db_path(config, args.test))
    outdir = Path(config.exports.dir) / ("test" if args.test else "")
    export_all(db, outdir)
    print(f"\nWrote three workbooks to {outdir}\n")
    db.close()


def cmd_status(config: Config, args: argparse.Namespace) -> None:
    db = Database(db_path(config, args.test))
    c = db.counts()
    dupes = db.find_probable_duplicates()
    print(f"\nProviderMap database\n  {db.path}\n" + "-" * 52)
    for k, v in c.items():
        print(f"  {k.replace('_', ' '):<32} {v:>8,}")
    print(f"  {'probable duplicate groups':<32} {len(dupes):>8,}")
    if c["urls_total"]:
        print(f"\n  progress: {c['urls_done'] / c['urls_total'] * 100:.1f}%")

    runs = db.recent_runs(5)
    if runs:
        print("\n  recent runs:")
        print(
            f"    {'id':>3}  {'mode':<12} {'source':<14} {'new':>5} {'chg':>5} {'same':>5}  started"
        )
        for r in runs:
            print(
                f"    {r.run_id:>3}  {(r.mode or '-'):<12} {(r.source_used or '-'):<14} "
                f"{r.providers_new:>5} {r.providers_changed:>5} {r.providers_unchanged:>5}  "
                f"{r.started_at}"
            )
    print()
    db.close()


def cmd_changes(config: Config, args: argparse.Namespace) -> None:
    db = Database(db_path(config, args.test))
    last = db.last_run()
    since = args.since if args.since is not None else (last.run_id if last else 0)
    rows = db.changes_since(since or 0)
    if not rows:
        print(f"\nNo recorded changes since run {since}.\n")
        db.close()
        return
    print(f"\n{len(rows)} field change(s) since run {since}:\n")
    for r in rows[: args.limit]:
        print(f"  [{r.run_id}] {r.full_name or '?'} (NPI {r.npi or '?'})")
        print(f"        {r.field}: {r.old_value!r} -> {r.new_value!r}")
    if len(rows) > args.limit:
        print(f"\n  ... and {len(rows) - args.limit} more (use --limit to see them)")
    print()
    db.close()


def cmd_query(config: Config, args: argparse.Namespace) -> None:
    """The question this whole project exists to answer."""
    db = Database(db_path(config, args.test))
    rows = db.search_providers(args.name)
    if not rows:
        print(f"\nNo provider matching '{args.name}'.\n")
        db.close()
        return
    for p in rows:
        creds = f", {p.credentials}" if p.credentials else ""
        flag = "" if p.is_active else "   [NO LONGER IN DIRECTORY]"
        print(f"\n  {p.full_name}{creds}   (NPI {p.npi}){flag}")
        print(f"    Specialty:       {p.specialty or '-'}")
        print(f"    Primary site:    {p.primary_site_of_care or '-'}")
        print(
            f"    Address:         {p.address or '-'}, {p.city or '-'}, "
            f"{p.state or '-'} {p.zip or ''}"
        )
        print(f"    Locations:       {p.location_count}")
        print(f"    Confidence:      {p.site_confidence.value if p.site_confidence else '-'}")
        if p.site_confidence and p.site_confidence.value != "high":
            print(f"      ^ {p.source_notes or 'review before trusting this'}")
    print()
    db.close()


async def cmd_test(config: Config, adapter: SiteAdapter, args: argparse.Namespace) -> None:
    from adapters.adventhealth.fixtures import DATASET, EXPECTED

    path = db_path(config, True)
    if path.exists() and not args.keep:
        path.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(path) + suffix)
            if p.exists():
                p.unlink()
    print(f"\nOffline test: {len(DATASET['records'])} fixture records, no network.\n  db: {path}\n")
    args.limit = None
    args.max_items = None
    args.rediscover = True
    args.test = True
    await _run_pipeline(config, adapter, args, mode="test")

    if getattr(args, "dry_run", False):
        # Every write from the run above was rolled back by design, so the
        # database is empty - checking it against EXPECTED here would report
        # a false "regression" for behavior that is actually correct. The
        # dry-run's own tally (printed above, from before the rollback) is
        # the real signal.
        print(
            "\ndry-run complete - skipping the stored-data check because "
            "--dry-run rolls back every write by design (that's not a "
            "regression). Re-run `providermap test` without --dry-run to "
            "verify the stored data.\n"
        )
        return

    db = Database(path)
    c = db.counts()
    dupes = db.find_probable_duplicates()
    print("\nExpectations:")
    checks = [
        ("individual providers stored", c["providers"], EXPECTED["individuals"]),
        ("facilities/orgs excluded (>=)", c["excluded"], EXPECTED["organizations"]),
        ("same-name groups flagged, not merged", len(dupes), EXPECTED["same_name_groups"]),
    ]
    ok = True
    for label, got, want in checks:
        good = got >= want if ">=" in label else got == want
        ok &= good
        print(f"  [{'OK  ' if good else 'FAIL'}] {label:<40} got {got}, want {want}")
    low = db.conn.execute("SELECT COUNT(*) FROM providers WHERE site_confidence='low'").fetchone()[
        0
    ]
    ok &= low >= 1
    print(
        f"  [{'OK  ' if low >= 1 else 'FAIL'}] multi-location -> low confidence{'':<11} "
        f"got {low}, want >=1"
    )
    db.close()
    print("\n" + ("Pipeline is healthy.\n" if ok else "Something regressed - see above.\n"))
    if not ok:
        sys.exit(1)


async def cmd_trial(config: Config, adapter: SiteAdapter, args: argparse.Namespace) -> None:
    """A small, live, single-command rehearsal: discover real URLs, enrich a
    handful of them for real, and auto-export for hand review - distinct
    from the offline ``providermap test`` (fixture-only, zero network) and
    from the normal ``scrape [--limit N]`` + ``export`` two-step flow (this
    runs both automatically, capped at ``--n`` records). Writes to the real
    database, same as ``scrape --limit N`` does; only the export directory
    (``exports/output/trial/``) is separated out, so it never overwrites a
    full run's ``providers.xlsx``.
    """
    if args.test:
        print(
            "\n--test doesn't apply to `trial` - trial's entire point is a "
            "live run. Use `providermap test` for the offline fixture "
            "pipeline instead.\n",
            file=sys.stderr,
        )
        sys.exit(2)

    from .exporter import export_all

    n = args.n
    db = Database(db_path(config, False))
    cache = Cache(config.cache.dir, config.cache.enabled, config.cache.ttl_days)
    pipeline = Pipeline(config, db, adapter, dry_run=False)
    db.start_run(mode="trial")

    async with make_fetcher(config, cache, False, adapter.name) as f:
        nppes = NppesClient(f, config)
        # A handful more URLs than `n` are discovered up front, since some
        # will classify as facilities/organizations and get excluded before
        # ever counting toward the `n` individuals `enrich_all` stops at.
        await pipeline.discover_urls(f, max_items=max(n * 5, 50))
        try:
            tally = await pipeline.enrich_all(f, nppes, limit=n)
        except RuntimeError as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            db.finish_run(notes=str(exc)[:200])
            db.close()
            sys.exit(1)

    t = tally.as_dict()
    db.finish_run(
        urls_discovered=db.counts()["urls_total"],
        providers_new=t.get("new", 0),
        providers_changed=t.get("changed", 0),
        providers_unchanged=t.get("unchanged", 0),
        excluded=t.get("excluded", 0),
        errors=t.get("error", 0),
        source_used=pipeline.source.name if pipeline.source else None,
        notes="trial run - partial by design, deactivate_missing not run",
    )

    outdir = Path(config.exports.dir) / "trial"
    export_all(db, outdir)

    print(f"\nTrial run complete - requested {n} record(s).")
    print(f"  source used     : {pipeline.source.name if pipeline.source else 'n/a'}")
    print(f"  new             : {t.get('new', 0)}")
    print(f"  changed         : {t.get('changed', 0)}")
    print(f"  unchanged       : {t.get('unchanged', 0)}")
    print(f"  excluded        : {t.get('excluded', 0)}")
    print(f"  errors          : {t.get('error', 0)}")
    tripped = " (CIRCUIT BREAKER TRIPPED)" if nppes.tripped else ""
    print(f"  NPPES           : {nppes.hits} hits / {nppes.misses} misses{tripped}")
    print(f"\nWrote review workbooks to {outdir}\n")
    db.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="providermap",
        description=(
            "ProviderMap - a reusable provider-directory ingestion pipeline.\n\n"
            "Quick start (offline, no network, safe right now):\n"
            "    providermap test\n\n"
            "Against a live site:\n"
            "    providermap investigate\n"
            "    providermap trial            # live, 10 records, auto-exports\n"
            "    providermap scrape --dry-run --limit 50\n"
            "    providermap scrape\n"
            "    providermap export\n\n"
            "Keeping it current:\n"
            "    providermap refresh\n"
            "    providermap changes\n"
            "    providermap status\n"
            '    providermap query "Menezes"\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--adapter",
        default="adventhealth",
        choices=sorted(ADAPTERS),
        help="which site adapter to use (default: adventhealth)",
    )
    ap.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser, dry: bool = True) -> None:
        p.add_argument(
            "--test", action="store_true", help="use the offline fixture dataset, no network"
        )
        if dry:
            p.add_argument(
                "--dry-run", action="store_true", help="do everything, write nothing (rolled back)"
            )

    p = sub.add_parser("test", help="run the full pipeline offline against fixtures")
    p.add_argument("--keep", action="store_true", help="keep the existing test db")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("investigate", help="analyse the site + sample data quality")
    p.add_argument("--sample", type=int, default=None)

    p = sub.add_parser("discover", help="find every provider URL")
    p.add_argument("--max-items", type=int, default=None)
    common(p)

    p = sub.add_parser("scrape", help="process the queue (resumable)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--max-items", type=int, default=None)
    p.add_argument("--rediscover", action="store_true")
    common(p)

    p = sub.add_parser("refresh", help="re-check records that have gone stale")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--older-than", type=int, default=None, metavar="DAYS")
    common(p)

    p = sub.add_parser("trial", help="live rehearsal: discover + enrich N records + auto-export")
    p.add_argument("--n", type=int, default=10, help="how many records to enrich (default: 10)")
    common(p, dry=False)

    p = sub.add_parser("export", help="write providers/excluded/summary xlsx")
    common(p, dry=False)

    p = sub.add_parser("status", help="show progress and recent runs")
    common(p, dry=False)

    p = sub.add_parser("changes", help="what changed since the last run")
    p.add_argument("--since", type=int, default=None)
    p.add_argument("--limit", type=int, default=40)
    common(p, dry=False)

    p = sub.add_parser("query", help="look up a provider's primary site of care")
    p.add_argument("name")
    common(p, dry=False)

    return ap


def main(argv: list[str] | None = None) -> None:
    _setup_windows_console()
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        # `providermap test` is documented as "no config file needed" (see
        # the Quick start help text below) - it's fully offline and never
        # reads politeness/contact-email settings, so config.example.yaml's
        # placeholder values are fine here. Every other command still
        # requires a real config.yaml.
        if args.cmd == "test" and args.config == "config.yaml":
            config = load_config("config.example.yaml")
        else:
            print(f"\n{exc}\n", file=sys.stderr)
            sys.exit(2)
    setup_logging(config)
    adapter = make_adapter(config, args.adapter)

    live = args.cmd in {"investigate", "discover", "scrape", "refresh", "trial"} and not getattr(
        args, "test", False
    )
    if live:
        check_user_agent(config)

    if args.cmd == "test":
        asyncio.run(cmd_test(config, adapter, args))
    elif args.cmd == "investigate":
        asyncio.run(cmd_investigate(config, adapter, args))
    elif args.cmd == "discover":
        asyncio.run(cmd_discover(config, adapter, args))
    elif args.cmd == "scrape":
        asyncio.run(_run_pipeline(config, adapter, args, mode="full"))
    elif args.cmd == "refresh":
        asyncio.run(_run_pipeline(config, adapter, args, mode="refresh"))
    elif args.cmd == "trial":
        asyncio.run(cmd_trial(config, adapter, args))
    elif args.cmd == "export":
        cmd_export(config, args)
    elif args.cmd == "status":
        cmd_status(config, args)
    elif args.cmd == "changes":
        cmd_changes(config, args)
    elif args.cmd == "query":
        cmd_query(config, args)


if __name__ == "__main__":
    main()
