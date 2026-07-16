"""URL discovery strategies, ordered cheapest-first.

The pipeline should never crawl hundreds of listing pages if the site will
simply hand over the list. On startup it probes, in order:

    1. SitemapSource    ~5 requests    XML sitemap - the whole directory
    2. JsonApiSource    ~N/50 requests Drupal JSON:API, if enabled
    3. ViewsAjaxSource  ~N requests    Drupal views AJAX endpoint, JSON-wrapped
    4. HtmlListSource   ~N requests    scraping rendered listing pages

...and uses the first one that actually works. Every strategy is generic:
site-specific knowledge (URL patterns, candidate paths) comes entirely from
the :class:`~providermap.adapters.base.SiteAdapter` passed to each source, so
a new adapter gets all four strategies for free.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from providermap.parser_utils import extract_matches

from .config import Config
from .net import AsyncFetcher

if TYPE_CHECKING:
    from adapters.base import SiteAdapter

log = logging.getLogger(__name__)

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


class Source(ABC):
    """A strategy for enumerating provider profile URLs on one site."""

    name: str = "abstract"
    cost: int = 999  # rough request count; lower is preferred
    description: str = ""

    def __init__(self, fetcher: AsyncFetcher, config: Config, adapter: SiteAdapter):
        self.fetcher = fetcher
        self.config = config
        self.adapter = adapter
        self.base_url = config.site.base_url

    @abstractmethod
    async def available(self) -> bool:
        """Cheap probe. Must not raise; return False on any doubt."""

    @abstractmethod
    def urls(self, max_items: int | None = None) -> AsyncIterator[str]:
        """Yield profile URLs."""


class SitemapSource(Source):
    name = "sitemap"
    cost = 5
    description = "XML sitemap - the entire directory in a handful of requests"

    def __init__(self, fetcher: AsyncFetcher, config: Config, adapter: SiteAdapter):
        super().__init__(fetcher, config, adapter)
        self._roots: list[str] = []

    async def available(self) -> bool:
        """A sitemap being present isn't enough - it has to actually lead to
        profile URLs. Some sites' `/sitemap.xml` is a generic, site-wide
        sitemap (blog posts, location pages, everything) with no
        directory-specific content on its first page; naively accepting any
        file with a `<loc>` tag caused a real, silent failure (the source got
        selected, "found" nothing, and the run reported zero results instead
        of falling through to the next strategy).

        A `<sitemapindex>`'s own `<loc>` entries point to other sitemap
        *files*, not content pages, so they're checked differently: peek into
        the first couple of children rather than expecting the index itself
        to contain profile URLs.
        """
        pattern = self.adapter.profile_url_pattern()

        for path in self.adapter.sitemap_candidates():
            url = urljoin(self.base_url, path)
            body = await self.fetcher.get(url, use_cache=False)
            if not body or "<loc>" not in body.lower():
                continue

            locs = _LOC_RE.findall(body)
            is_index = "<sitemapindex" in body[:2000].lower()

            if is_index:
                found = False
                for child in locs[:2]:
                    child_body = await self.fetcher.get(child, use_cache=False)
                    if child_body and any(
                        pattern.match(loc) for loc in _LOC_RE.findall(child_body)[:500]
                    ):
                        found = True
                        break
            else:
                found = any(pattern.match(loc) for loc in locs[:500])

            if not found:
                log.info(
                    "%s has <loc> entries but none lead to URLs matching this "
                    "adapter's profile pattern - not using it as the discovery "
                    "source.",
                    url,
                )
                continue

            self._roots.append(url)
            log.info("Found sitemap: %s", url)
            return True
        return False

    async def _expand(self, url: str, seen: set[str]) -> list[str]:
        if url in seen:
            return []
        seen.add(url)
        body = await self.fetcher.get(url)
        if not body:
            return []
        locs = _LOC_RE.findall(body)
        if "<sitemapindex" not in body[:2000].lower():
            return locs
        out: list[str] = []
        for child in locs:
            if re.search(r"doctor|physician|provider|find", child, re.I) or len(locs) <= 12:
                out.extend(await self._expand(child, seen))
        return out

    async def urls(self, max_items: int | None = None) -> AsyncIterator[str]:
        seen_maps: set[str] = set()
        emitted: set[str] = set()
        pattern = self.adapter.profile_url_pattern()
        for root in self._roots:
            for loc in await self._expand(root, seen_maps):
                if not pattern.match(loc) or loc in emitted:
                    continue
                emitted.add(loc)
                yield loc
                if max_items and len(emitted) >= max_items:
                    return


class JsonApiSource(Source):
    name = "jsonapi"
    cost = 100
    description = "Drupal JSON:API - structured JSON, ~50 records per request"

    def __init__(self, fetcher: AsyncFetcher, config: Config, adapter: SiteAdapter):
        super().__init__(fetcher, config, adapter)
        self._collection: str | None = None

    async def available(self) -> bool:
        types = self.adapter.jsonapi_type_candidates()
        if not types:
            return False

        root_doc = None
        for root in ("/jsonapi", "/jsonapi/index"):
            body = await self.fetcher.get(urljoin(self.base_url, root), use_cache=False)
            if not body:
                continue
            try:
                root_doc = json.loads(body)
                break
            except json.JSONDecodeError:
                continue
        if root_doc is None:
            return False

        links = root_doc.get("links") or {}
        for key, val in links.items():
            if re.search(r"physician|provider|doctor", key, re.I):
                href = val.get("href") if isinstance(val, dict) else val
                if href and await self._works(href):
                    self._collection = href
                    return True

        for t in types:
            href = urljoin(self.base_url, f"/jsonapi/{t}")
            if await self._works(href):
                self._collection = href
                return True
        return False

    async def _works(self, href: str) -> bool:
        body = await self.fetcher.get(f"{href}?page[limit]=1", use_cache=False)
        if not body:
            return False
        try:
            doc = json.loads(body)
        except json.JSONDecodeError:
            return False
        return isinstance(doc.get("data"), list) and not doc.get("errors")

    async def urls(self, max_items: int | None = None) -> AsyncIterator[str]:
        if not self._collection:
            return
        url = f"{self._collection}?page[limit]=50"
        count = 0
        while url:
            body = await self.fetcher.get(url)
            if not body:
                return
            try:
                doc = json.loads(body)
            except json.JSONDecodeError:
                return
            for item in doc.get("data") or []:
                attrs = item.get("attributes") or {}
                path = attrs.get("path")
                alias = path.get("alias") if isinstance(path, dict) else None
                candidate = urljoin(self.base_url, alias) if alias else None
                if candidate and self.adapter.profile_url_pattern().match(candidate):
                    count += 1
                    yield candidate
                    if max_items and count >= max_items:
                        return
            url = (doc.get("links") or {}).get("next", {}).get("href")


class ViewsAjaxSource(Source):
    name = "views_ajax"
    cost = 900
    description = "Drupal views AJAX endpoint - JSON-wrapped rendered rows"

    def __init__(self, fetcher: AsyncFetcher, config: Config, adapter: SiteAdapter):
        super().__init__(fetcher, config, adapter)
        self._view: dict[str, str] = {}

    async def available(self) -> bool:
        listing = self.adapter.listing_url(self.config.site.page_start)
        html = await self.fetcher.get(listing)
        if not html:
            return False

        m = re.search(r'"view_name"\s*:\s*"([a-z0-9_]+)"', html, re.I)
        d = re.search(r'"view_display_id"\s*:\s*"([a-z0-9_]+)"', html, re.I)
        if not (m and d):
            return False

        self._view = {"view_name": m.group(1), "view_display_id": d.group(1)}
        for key, rx in (
            ("view_args", r'"view_args"\s*:\s*"([^"]*)"'),
            ("view_dom_id", r'"view_dom_id"\s*:\s*"([a-f0-9]+)"'),
        ):
            mm = re.search(rx, html, re.I)
            if mm:
                self._view[key] = mm.group(1)

        return bool(await self._page(0))

    async def _page(self, page: int) -> list[str]:
        params = dict(self._view)
        params["page"] = str(page)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        body = await self.fetcher.get(urljoin(self.base_url, f"/views/ajax?{qs}"))
        if not body:
            return []
        try:
            commands = json.loads(body)
        except json.JSONDecodeError:
            return []
        blob = "".join(
            c.get("data", "")
            for c in commands
            if isinstance(c, dict) and isinstance(c.get("data"), str)
        )
        return extract_matches(blob, self.adapter.profile_link_pattern())

    async def urls(self, max_items: int | None = None) -> AsyncIterator[str]:
        page = 0
        emitted: set[str] = set()
        empty = 0
        while True:
            found = [urljoin(self.base_url, u) for u in await self._page(page)]
            new = [u for u in found if u not in emitted]
            if not new:
                empty += 1
                if empty >= 2:
                    return
            else:
                empty = 0
            for u in new:
                emitted.add(u)
                yield u
                if max_items and len(emitted) >= max_items:
                    return
            page += 1


class HtmlListSource(Source):
    name = "html_listing"
    cost = 970
    description = "Scraping rendered listing pages (fallback)"

    async def available(self) -> bool:
        html = await self.fetcher.get(self.adapter.listing_url(self.config.site.page_start))
        return bool(html and extract_matches(html, self.adapter.profile_link_pattern()))

    async def urls(self, max_items: int | None = None) -> AsyncIterator[str]:
        page = self.config.site.page_start
        emitted: set[str] = set()
        empty = 0

        while True:
            html = await self.fetcher.get(self.adapter.listing_url(page))
            if html is None:
                return
            found = [
                urljoin(self.base_url, u)
                for u in extract_matches(html, self.adapter.profile_link_pattern())
            ]
            new = [u for u in found if u not in emitted]

            if not found:
                empty += 1
                if empty >= 2:
                    return
            elif not new:
                log.warning(
                    "page=%s returned only URLs already seen; pagination may not "
                    "be advancing (check site.page_param in config.yaml).",
                    page,
                )
                empty += 1
                if empty >= 2:
                    return
            else:
                empty = 0

            for u in new:
                emitted.add(u)
                yield u
                if max_items and len(emitted) >= max_items:
                    return
            page += 1


ALL_SOURCES: list[type[Source]] = [SitemapSource, JsonApiSource, ViewsAjaxSource, HtmlListSource]


async def select_source(
    fetcher: AsyncFetcher,
    config: Config,
    adapter: SiteAdapter,
    forced: str | None = None,
) -> Source | None:
    """Probe each strategy cheapest-first; return the first that works.

    ``forced`` pins a specific strategy by name (``site.force_source`` in
    config), for when the answer is already known and re-probing on every run
    would just waste requests.
    """
    candidates = ALL_SOURCES
    if forced:
        candidates = [s for s in ALL_SOURCES if s.name == forced]
        if not candidates:
            names = ", ".join(s.name for s in ALL_SOURCES)
            raise ValueError(f"Unknown source '{forced}'. Options: {names}")

    for cls in candidates:
        src = cls(fetcher, config, adapter)
        log.info("Probing source: %-12s (%s)", src.name, src.description)
        try:
            if await src.available():
                log.info("SELECTED source: %s (~%s requests expected)", src.name, src.cost)
                return src
            log.info("  not available")
        except (RuntimeError, ValueError) as exc:
            log.warning("  probe failed for %s: %s", src.name, exc)
    return None
