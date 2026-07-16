"""The pipeline orchestrator.

Ties together a :class:`~providermap.adapters.base.SiteAdapter`, the shared
HTTP layer (:mod:`providermap.net`), NPPES lookups
(:mod:`providermap.nppes`), classification (:mod:`providermap.validator`),
and storage (:mod:`providermap.database`) into the two operations the CLI
exposes:

    discover_urls()  - find every provider profile URL
    enrich_all()      - fetch, classify, validate, and store each one

Nothing in this module knows about AdventHealth specifically - it only calls
methods on whatever adapter it was given.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from adapters.base import SiteAdapter

from . import parser_utils as P
from . import sources as S
from . import validator as V
from .config import Config
from .database import Database
from .models import (
    ClassificationResult,
    ExcludedRecord,
    Location,
    PipelineTally,
    ProfilePage,
    Provider,
    UpsertOutcome,
    VCardData,
)
from .net import AsyncFetcher
from .nppes import NppesClient

log = logging.getLogger(__name__)


class Pipeline:
    """Runs discovery and enrichment for one adapter against one database."""

    def __init__(self, config: Config, db: Database, adapter: SiteAdapter, dry_run: bool = False):
        self.config = config
        self.db = db
        self.adapter = adapter
        self.dry_run = dry_run
        self.npi_policy = config.npi
        self.source: S.Source | None = None

    # ------------------------------------------------------------------ #
    # phase 1: discovery
    # ------------------------------------------------------------------ #

    async def discover_urls(self, fetcher: AsyncFetcher, max_items: int | None = None) -> int:
        """Enumerate provider URLs using the cheapest source that works.

        Tries, in order: XML sitemap, JSON:API, views AJAX, and only then
        scraping rendered listing pages. Whichever wins is recorded on the
        current run.
        """
        self.source = await S.select_source(
            fetcher,
            self.config,
            self.adapter,
            forced=self.config.site.force_source,
        )
        if self.source is None:
            raise RuntimeError(
                "No usable source found. Every strategy failed to return "
                "profile URLs - the site structure has probably changed. Run "
                "`providermap investigate` for a diagnosis."
            )

        if self.db.run_id:
            self.db.set_run_source(self.source.name)

        batch: list[str] = []
        total_new = 0
        seen = 0

        async for url in self.source.urls(max_items=max_items):
            batch.append(url)
            seen += 1
            if len(batch) >= 200:
                total_new += self.db.enqueue(batch)
                log.info("discovered %s URLs (%s new) via %s", seen, total_new, self.source.name)
                batch = []
        if batch:
            total_new += self.db.enqueue(batch)

        log.info(
            "Discovery complete via %s: %s URLs seen, %s new.", self.source.name, seen, total_new
        )
        self.db.set_stat("source_used", self.source.name)
        self.db.set_stat("urls_discovered", seen)
        self.db.set_stat(
            "discovery_completed_at",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        return total_new

    # ------------------------------------------------------------------ #
    # phase 2: enrich one record
    # ------------------------------------------------------------------ #

    async def process_url(
        self, fetcher: AsyncFetcher, nppes: NppesClient, url: str
    ) -> UpsertOutcome:
        """Fetch, classify, validate, and store one directory record."""
        npi = self.adapter.extract_npi(url)
        if npi is None:
            self.db.exclude(
                ExcludedRecord(
                    name="",
                    url=url,
                    classification="Unknown",
                    reason_excluded="URL did not match the profile pattern",
                )
            )
            return UpsertOutcome.EXCLUDED

        npi_valid = P.npi_checksum_valid(npi)
        check_ok = npi_valid or self.npi_policy.checksum == "off"

        nppes_rec = await nppes.lookup(npi) if check_ok else None

        vcard_url = self.adapter.vcard_url(npi)
        vcard_text = await fetcher.get(vcard_url) if vcard_url else None
        vcard = self.adapter.parse_vcard(vcard_text) if vcard_text else None

        html = await fetcher.get(url)
        page = self.adapter.parse_profile_html(html) if html else None

        if html is None and vcard is None:
            self.db.exclude(
                ExcludedRecord(
                    name="",
                    url=url,
                    classification="Unknown",
                    reason_excluded="Profile page and vCard both unavailable (404?)",
                    npi=npi,
                )
            )
            return UpsertOutcome.EXCLUDED

        if page and page.is_error_page:
            self.db.exclude(
                ExcludedRecord(
                    name="",
                    url=url,
                    classification="Unknown",
                    reason_excluded="Profile page returned an error page",
                    npi=npi,
                )
            )
            return UpsertOutcome.EXCLUDED

        display = (
            (page.display_name if page else None)
            or (vcard.organization if vcard else None)
            or (vcard.full_name if vcard else None)
            or ""
        ).strip()
        if not display and not nppes_rec:
            self.db.exclude(
                ExcludedRecord(
                    name="",
                    url=url,
                    classification="Unknown",
                    reason_excluded="No name available from page, vCard or NPPES",
                    npi=npi,
                )
            )
            return UpsertOutcome.EXCLUDED

        name_only, creds = P.split_name_and_credentials(display)
        result = V.classify(
            display or None, creds, nppes_rec, self.adapter.organization_name_patterns()
        )

        if not result.is_individual:
            basic = (nppes_rec or {}).get("basic") or {}
            org_name = basic.get("organization_name", "")
            self.db.exclude(
                ExcludedRecord(
                    name=display or org_name,
                    url=url,
                    classification=result.provider_type.value,
                    reason_excluded=result.reason
                    or f"Classified as {result.provider_type.value} " f"({result.basis})",
                    npi=npi,
                )
            )
            return UpsertOutcome.EXCLUDED

        provider = self._build_provider(
            url, npi, npi_valid, name_only, creds, display, vcard, page, nppes_rec, result
        )

        ok, problems = V.validate_provider(provider, self.npi_policy)
        if not ok:
            self.db.exclude(
                ExcludedRecord(
                    name=display,
                    url=url,
                    classification=result.provider_type.value,
                    reason_excluded="Incomplete record: " + "; ".join(problems),
                    npi=npi,
                )
            )
            return UpsertOutcome.EXCLUDED

        pid, outcome = self.db.upsert_provider(provider)
        if pid is None:
            return UpsertOutcome.ERROR

        self._store_location(pid, provider)
        return outcome

    def _build_provider(
        self,
        url: str,
        npi: str,
        npi_valid: bool,
        name_only: str,
        creds: str,
        display: str,
        vcard: VCardData | None,
        page: ProfilePage | None,
        nppes_rec: dict[str, Any] | None,
        result: ClassificationResult,
    ) -> Provider:
        vc = vcard or VCardData()
        pg = page or ProfilePage()

        basic = (nppes_rec or {}).get("basic") or {}
        if basic.get("first_name") or basic.get("last_name"):
            first = (basic.get("first_name") or "").title() or None
            last = (basic.get("last_name") or "").title() or None
        else:
            first, last = P.split_person_name(name_only)

        name_match = V.names_match(name_only, nppes_rec)

        tax_code = tax_desc = None
        for t in (nppes_rec or {}).get("taxonomies") or []:
            if t.get("primary"):
                tax_code, tax_desc = t.get("code"), t.get("desc")
                break

        practice_name = vc.organization or vc.facility_label
        site_of_care = vc.facility_label or vc.organization
        # Counted from distinct appointment-link location ids on the page - a
        # structural signal, not a guessed CSS selector. A provider at four
        # sites has no single meaningful "primary" site.
        location_count = max(1, len(pg.location_ids))

        provider = Provider(
            full_name=name_only or None,
            first_name=first,
            last_name=last,
            credentials=creds or (basic.get("credential") or None),
            provider_type=result.provider_type,
            specialty=pg.specialty or tax_desc,
            primary_site_of_care=site_of_care,
            practice_name=practice_name,
            address=vc.address,
            city=vc.city,
            state=vc.state,
            zip=vc.zip,
            phone=vc.phone,
            profile_url=url,
            npi=npi,
            npi_valid=npi_valid,
            nppes_enumeration=(nppes_rec or {}).get("enumeration_type") or "not_found",
            nppes_name_match=name_match,
            taxonomy_code=tax_code,
            taxonomy_desc=tax_desc,
            location_count=location_count,
        )
        provider.site_confidence = V.site_confidence(
            location_count,
            nppes_rec is not None,
            name_match,
            result.confidence,
        )
        notes = [f"classified via {result.basis} (confidence: {result.confidence.value})"]
        notes += V.validation_notes(provider, self.npi_policy)
        provider.source_notes = "; ".join(notes)
        return provider

    def _store_location(self, provider_id: int, provider: Provider) -> None:
        key = P.normalize_address_key(provider.address, provider.city, provider.state, provider.zip)
        if not key:
            return
        location_id = self.db.upsert_location(
            Location(
                facility_name=provider.practice_name,
                address=provider.address,
                city=provider.city,
                state=provider.state,
                zip=provider.zip,
                phone=provider.phone,
                address_key=key,
            )
        )
        if location_id:
            self.db.link(provider_id, location_id, is_primary=True, rank=0)

    # ------------------------------------------------------------------ #
    # phase 2 driver
    # ------------------------------------------------------------------ #

    async def enrich_all(
        self,
        fetcher: AsyncFetcher,
        nppes: NppesClient,
        limit: int | None = None,
        urls: list[str] | None = None,
    ) -> PipelineTally:
        target_urls = urls if urls is not None else self.db.pending_urls(limit=limit)
        tally = PipelineTally()
        if not target_urls:
            log.info("Nothing pending. Database is up to date.")
            return tally

        log.info("Processing %s URLs...", len(target_urls))
        semaphore = asyncio.Semaphore(self.config.politeness.max_concurrent)

        async def worker(u: str) -> None:
            async with semaphore:
                try:
                    outcome = await self.process_url(fetcher, nppes, u)
                    tally.record(outcome)
                    self.db.mark(u, "done" if outcome != UpsertOutcome.ERROR else "error")
                except RuntimeError:
                    raise
                except (ValueError, KeyError, TypeError) as exc:
                    log.exception("Failed on %s", u)
                    tally.record(UpsertOutcome.ERROR)
                    self.db.mark(u, "error", str(exc)[:500])

        batch_size = 50
        for i in range(0, len(target_urls), batch_size):
            await asyncio.gather(*(worker(u) for u in target_urls[i : i + batch_size]))
            log.info(
                "progress %s/%s | new=%s changed=%s unchanged=%s excluded=%s errors=%s",
                min(i + batch_size, len(target_urls)),
                len(target_urls),
                tally.new,
                tally.changed,
                tally.unchanged,
                tally.excluded,
                tally.error,
            )
        return tally
