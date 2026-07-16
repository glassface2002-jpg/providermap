"""Site analysis and data-quality investigation.

Run this before a full scrape. It answers, empirically and against the live
site, the questions the full run depends on:

  1. What does robots.txt actually permit?
  2. Does the configured pagination parameter actually advance?
  3. Is there a JSON/API endpoint that should be used instead of HTML?
  4. Of the first N records: how many are real people vs. facilities?
  5. Do duplicates or false positives exist?

Findings are written to ``logs/investigation_report.md``; nothing is written
to the provider database.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from adapters.base import SiteAdapter

from . import parser_utils as P
from . import validator as V
from .config import Config
from .models import Provider
from .net import AsyncFetcher, Cache, Fetcher
from .nppes import NppesClient

log = logging.getLogger(__name__)

# Endpoints worth probing before committing to HTML scraping. If any returns
# JSON, prefer it - faster and gentler on the site than parsing thousands of
# rendered pages.
_API_PROBES = [
    "/jsonapi",
    "/jsonapi/node/physician",
    "/api/physicians",
    "/api/providers",
    "/graphql",
    "/search/api/physicians",
    "/views/ajax",
    "/sitemap.xml",
]


async def probe_apis(fetcher: AsyncFetcher, base: str) -> list[dict[str, str]]:
    findings = []
    for path in _API_PROBES:
        body = await fetcher.get(urljoin(base, path), use_cache=False)
        if body is None:
            findings.append({"path": path, "result": "no response / 404"})
            continue
        head = body.lstrip()[:200]
        if head.startswith(("{", "[")):
            try:
                json.loads(body)
                findings.append(
                    {"path": path, "result": "JSON  <-- INVESTIGATE", "preview": head[:160]}
                )
                continue
            except json.JSONDecodeError:
                pass
        if head.startswith("<?xml") or "<urlset" in head:
            findings.append(
                {"path": path, "result": "XML sitemap <-- INVESTIGATE", "preview": head[:160]}
            )
            continue
        findings.append({"path": path, "result": f"HTML ({len(body)} bytes)"})
    return findings


async def probe_pagination(
    fetcher: AsyncFetcher, adapter: SiteAdapter, config: Config, probe_pages: int = 4
) -> dict[str, Any]:
    """Verify the configured pagination parameter actually advances."""
    seen_sets: list[set[str]] = []
    result: dict[str, Any] = {
        "param": config.site.page_param,
        "works": False,
        "notes": [],
        "per_page": None,
    }

    for page in range(config.site.page_start, config.site.page_start + probe_pages):
        html = await fetcher.get(adapter.listing_url(page), use_cache=False)
        if html is None:
            result["notes"].append(f"page={page} returned nothing")
            break
        links = set(P.extract_matches(html, adapter.profile_link_pattern()))
        seen_sets.append(links)
        result["notes"].append(f"page={page}: {len(links)} profile links")

    if len(seen_sets) >= 2:
        if seen_sets[0] and seen_sets[0] == seen_sets[1]:
            result["notes"].append(
                "PROBLEM: consecutive pages returned identical links. The "
                "pagination parameter is likely wrong - check site.page_param."
            )
        elif seen_sets[0] and seen_sets[1] and not (seen_sets[0] & seen_sets[1]):
            result["works"] = True
            result["per_page"] = len(seen_sets[0])
            result["notes"].append("Pagination advances correctly (no overlap).")
        else:
            result["works"] = True
            result["per_page"] = len(seen_sets[0]) if seen_sets[0] else None
            result["notes"].append("Pages differ but overlap - check sort stability.")
    return result


async def sample_records(
    fetcher: AsyncFetcher,
    nppes: NppesClient,
    adapter: SiteAdapter,
    config: Config,
    sample_size: int,
) -> dict[str, Any]:
    """Classify a sample of records without writing to the provider database."""
    urls: list[str] = []
    page = config.site.page_start
    while len(urls) < sample_size and page < config.site.page_start + 200:
        html = await fetcher.get(adapter.listing_url(page))
        if not html:
            break
        found = [
            urljoin(config.site.base_url, u)
            for u in P.extract_matches(html, adapter.profile_link_pattern())
        ]
        for u in found:
            if u not in urls:
                urls.append(u)
        if not found:
            break
        page += 1
    urls = urls[:sample_size]
    log.info("Sampling %s records across %s listing pages", len(urls), page)

    labels: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    npis_seen: Counter[str] = Counter()
    urls_seen: Counter[str] = Counter()
    incomplete = bad_npi = no_nppes = 0
    rows: list[dict[str, str]] = []

    semaphore = asyncio.Semaphore(config.politeness.max_concurrent)

    async def one(url: str) -> None:
        nonlocal incomplete, bad_npi, no_nppes
        async with semaphore:
            npi = adapter.extract_npi(url)
            if not npi:
                labels["Unknown"] += 1
                return
            npi_valid = P.npi_checksum_valid(npi)
            if not npi_valid:
                bad_npi += 1

            rec = await nppes.lookup(npi) if npi_valid else None
            if rec is None:
                no_nppes += 1

            vcard_url = adapter.vcard_url(npi)
            vcard_text = await fetcher.get(vcard_url) if vcard_url else None
            vc = adapter.parse_vcard(vcard_text) if vcard_text else None
            html = await fetcher.get(url)
            pg = adapter.parse_profile_html(html) if html else None

            display = (
                (pg.display_name if pg else None) or (vc.organization if vc else None) or ""
            ).strip()
            name_only, creds = P.split_name_and_credentials(display)
            result = V.classify(display or None, creds, rec, adapter.organization_name_patterns())

            labels[result.provider_type.value] += 1
            if result.reason:
                reasons[result.reason[:80]] += 1
            npis_seen[npi] += 1
            urls_seen[url] += 1

            candidate = Provider(
                full_name=name_only,
                npi=npi,
                npi_valid=npi_valid,
                address=vc.address if vc else None,
                city=vc.city if vc else None,
                state=vc.state if vc else None,
                profile_url=url,
                specialty=pg.specialty if pg else None,
                practice_name=vc.organization if vc else None,
                phone=vc.phone if vc else None,
            )
            ok, problems = V.validate_provider(candidate)
            if result.is_individual and not ok:
                incomplete += 1
                for p in problems:
                    reasons[f"incomplete: {p}"] += 1

            rows.append(
                {
                    "url": url,
                    "name": display,
                    "label": result.provider_type.value,
                    "basis": result.basis,
                    "confidence": result.confidence.value,
                }
            )

    batch = 25
    for i in range(0, len(urls), batch):
        await asyncio.gather(*(one(u) for u in urls[i : i + batch]))
        log.info("sampled %s/%s", min(i + batch, len(urls)), len(urls))

    dup_npis = {n: c for n, c in npis_seen.items() if c > 1}
    dup_urls = {u: c for u, c in urls_seen.items() if c > 1}

    return {
        "sampled": len(urls),
        "labels": dict(labels),
        "reasons": dict(reasons.most_common(20)),
        "incomplete_individuals": incomplete,
        "invalid_npi_checksums": bad_npi,
        "no_nppes_record": no_nppes,
        "duplicate_npis_in_sample": len(dup_npis),
        "duplicate_urls_in_sample": len(dup_urls),
        "nppes_hits": nppes.hits,
        "nppes_misses": nppes.misses,
        "rows": rows,
    }


def write_report(
    path: Path,
    robots_note: str,
    apis: list[dict[str, str]],
    pagination: dict[str, Any],
    sample: dict[str, Any],
    fetch_stats: dict[str, int],
) -> None:
    labels: dict[str, int] = sample.get("labels", {})
    total = sample.get("sampled", 0) or 1
    individuals = sum(
        v for k, v in labels.items() if k in {"Physician", "Advanced Practice Provider"}
    )

    lines = [
        "# Provider Directory - Investigation Report",
        f"\nGenerated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "\n## 1. robots.txt\n",
        robots_note,
        "\n## 2. Structured data source probe\n",
        "| Endpoint | Result |",
        "| --- | --- |",
    ]
    lines += [f"| `{a['path']}` | {a['result']} |" for a in apis]
    lines += [
        "\nIf any row above says INVESTIGATE, look at it before running the "
        "full scrape - an API is always preferable to thousands of page fetches.",
        "\n## 3. Pagination\n",
        f"- Parameter tested: `?{pagination.get('param')}=N`",
        f"- Works: **{pagination.get('works')}**",
        f"- Links per page: {pagination.get('per_page')}",
    ]
    lines += [f"- {n}" for n in pagination.get("notes", [])]
    lines += [
        f"\n## 4. Data quality sample (n={sample.get('sampled')})\n",
        "### Classification breakdown\n",
        "| Classification | Count | % |",
        "| --- | ---: | ---: |",
    ]
    for label, count in sorted(labels.items(), key=lambda x: -x[1]):
        lines.append(f"| {label} | {count} | {count / total * 100:.1f}% |")
    lines += [
        f"\n**Individual providers: {individuals} / {total} "
        f"({individuals / total * 100:.1f}%)**",
        f"\n**Non-providers: {total - individuals} "
        f"({(total - individuals) / total * 100:.1f}%)**",
        "\n### Record quality\n",
        f"- Individuals that were incomplete (excluded): {sample.get('incomplete_individuals')}",
        f"- NPIs failing the CMS checksum: {sample.get('invalid_npi_checksums')}",
        f"- NPIs with no NPPES record: {sample.get('no_nppes_record')}",
        f"- Duplicate NPIs within the sample: {sample.get('duplicate_npis_in_sample')}",
        f"- NPPES lookups: {sample.get('nppes_hits')} hits / {sample.get('nppes_misses')} misses",
        "\n### Exclusion / flag reasons\n",
        "| Reason | Count |",
        "| --- | ---: |",
    ]
    for reason, count in sample.get("reasons", {}).items():
        lines.append(f"| {reason} | {count} |")
    lines += [
        "\n## 5. Fetch statistics\n",
        f"- Requests made: {fetch_stats.get('requests')}",
        f"- Cache hits: {fetch_stats.get('cache_hits')}",
        f"- Errors: {fetch_stats.get('errors')}",
        f"- Blocked by robots.txt: {fetch_stats.get('robots_blocked')}",
        "\n## 6. Records classified as non-providers (first 40)\n",
        "| Name | Classification | Basis | URL |",
        "| --- | --- | --- | --- |",
    ]
    shown = 0
    for r in sample.get("rows", []):
        if r["label"] not in {"Physician", "Advanced Practice Provider"} and shown < 40:
            lines.append(f"| {r['name']} | {r['label']} | {r['basis']} | {r['url']} |")
            shown += 1
    lines.append(
        "\n---\n\nReview the table above by hand - it is the cheapest possible "
        "check on the classifier before it runs across the full directory."
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote %s", path)


async def run(
    config: Config,
    adapter: SiteAdapter,
    sample_size: int | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    sample_size = sample_size or config.investigation.sample_size
    cache = Cache(config.cache.dir, config.cache.enabled, config.cache.ttl_days)
    report_path = report_path or Path(config.logging.dir) / "investigation_report.md"

    async with Fetcher(config, cache) as fetcher:
        listing = adapter.listing_url(config.site.page_start)
        allowed = fetcher.allowed(listing)
        robots_note = (
            f"- Listing URL: `{listing}`\n- Allowed for our User-Agent: **{allowed}**\n"
            f"- User-Agent: `{config.politeness.user_agent}`\n"
        )
        if not allowed:
            robots_note += (
                "\n**robots.txt disallows this path.** The pipeline will refuse "
                "to run. Do not override it - contact the site operator for a "
                "data feed or written permission instead.\n"
            )
            write_report(report_path, robots_note, [], {}, {}, fetcher.stats)
            return {"blocked": True}

        nppes = NppesClient(fetcher, config)
        log.info("Probing for structured data endpoints...")
        apis = await probe_apis(fetcher, config.site.base_url)
        log.info("Testing pagination...")
        pagination = await probe_pagination(fetcher, adapter, config)
        log.info("Sampling %s records for data quality...", sample_size)
        sample = await sample_records(fetcher, nppes, adapter, config, sample_size)

        write_report(report_path, robots_note, apis, pagination, sample, fetcher.stats)

    return {"apis": apis, "pagination": pagination, "sample": sample}
