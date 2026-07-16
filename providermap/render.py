"""A browser-rendered fetch backend, for sites whose bot-management blocks
plain HTTP clients but not a normal browser.

This is exactly what AdventHealth turned out to need: its robots.txt and
``/sitemap.xml`` both answer plain HTTP fine, but Akamai returns "Access
Denied" for the doctor-profile pages themselves even from a real interactive
browser session that never touched robots.txt-disallowed paths - see
``PROJECT_STATE.md``. :class:`PlaywrightFetcher` does nothing more clever
than a normal browser: navigate, wait for the page to settle, read the
rendered HTML. No stealth plugins, no fingerprint spoofing, no CAPTCHA
handling - if a site's bot-management blocks this too, that's the honest
answer, and the next step is the one the README's "Legal & ethical use"
section already recommends (a data feed or written permission), not more
technique.

:class:`PlaywrightFetcher` implements the same :class:`~providermap.net.
AsyncFetcher` protocol as :class:`~providermap.net.Fetcher`, so
``pipeline.py`` needs no changes at all to use it - it composes a plain
:class:`~providermap.net.Fetcher` internally and delegates anything outside
``config.site.base_url`` (NPPES lookups, robots.txt) to it unchanged, since
those already work over plain HTTP even on this site. Only requests to the
target site are actually rendered in a browser.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .config import Config
from .net import Cache, Fetcher, RateLimiter, compute_backoff

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright

log = logging.getLogger(__name__)


class PlaywrightFetcher:
    """AsyncFetcher backed by a real browser via Playwright.

    Selected instead of :class:`~providermap.net.Fetcher` when
    ``config.site.fetch_mode == "playwright"`` (see ``cli.py::make_fetcher``).
    Requires the optional ``render`` extra (``pip install -e ".[render]"``)
    and a one-time ``playwright install chromium`` - importing
    ``playwright.async_api`` is deferred to :meth:`__aenter__` so the rest of
    the pipeline never needs Playwright installed to run.
    """

    def __init__(self, config: Config, cache: Cache):
        self.config = config
        self.cache = cache
        self.base_url = config.site.base_url
        self.user_agent = config.politeness.user_agent
        self.timeout = config.politeness.timeout_seconds
        self.retries = config.retries
        self.limiter = RateLimiter(config.politeness.requests_per_second)
        self.abort_after = config.politeness.abort_after_consecutive_errors
        self._consecutive_errors = 0

        # Everything off the target site (NPPES, robots.txt) goes through a
        # plain Fetcher unchanged - no reason to pay for a browser render of
        # an unrelated, unblocked API.
        self._http = Fetcher(config, cache)
        self.stats = self._http.stats

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    @property
    def nppes_limiter(self) -> RateLimiter | None:
        return self._http.nppes_limiter

    async def __aenter__(self) -> PlaywrightFetcher:
        await self._http.__aenter__()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "site.fetch_mode is 'playwright' but the playwright package "
                'isn\'t installed. Run: pip install -e ".[render]" && '
                "playwright install chromium"
            ) from exc

        self._playwright = await async_playwright().start()
        browser_type = getattr(self._playwright, self.config.rendering.browser)
        self._browser = await browser_type.launch(headless=self.config.rendering.headless)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        await self._http.__aexit__(*exc)

    def allowed(self, url: str) -> bool:
        return self._http.allowed(url)

    def _is_target_site(self, url: str) -> bool:
        return urlparse(url).netloc.endswith(urlparse(self.base_url).netloc)

    async def get(
        self,
        url: str,
        limiter: RateLimiter | None = None,
        use_cache: bool = True,
    ) -> str | None:
        if not self._is_target_site(url):
            return await self._http.get(url, limiter, use_cache)

        if use_cache:
            hit = self.cache.get(url)
            if hit is not None:
                self.stats["cache_hits"] += 1
                return hit

        if not self.allowed(url):
            self.stats["robots_blocked"] += 1
            log.warning("robots.txt disallows %s - skipping", url)
            return None

        from playwright.async_api import Error as PlaywrightError

        lim = limiter or self.limiter
        assert self._browser is not None

        for attempt in range(1, self.retries.max_attempts + 1):
            await lim.wait()
            page = await self._browser.new_page(user_agent=self.user_agent)
            status: int | None = None
            try:
                self.stats["requests"] += 1
                response = await page.goto(
                    url, wait_until="networkidle", timeout=self.timeout * 1000
                )
                status = response.status if response else None
                if status == 200:
                    wait_s = self.config.rendering.wait_after_load_seconds
                    if wait_s:
                        await page.wait_for_timeout(wait_s * 1000)
                    html = await page.content()
                    self._consecutive_errors = 0
                    if use_cache:
                        self.cache.put(url, html)
                    return html
            except PlaywrightError as exc:
                log.debug("playwright error on %s: %s", url, exc)
            finally:
                await page.close()

            if status == 404:
                self._consecutive_errors = 0
                return None
            if status is not None and status not in self.retries.retry_on_status:
                log.warning("HTTP %s on %s (playwright) - not retrying", status, url)
                return None

            delay = compute_backoff(attempt, self.retries)
            log.warning(
                "%s on %s (playwright) - retry %s/%s in %.1fs",
                f"HTTP {status}" if status else "no response",
                url,
                attempt,
                self.retries.max_attempts,
                delay,
            )
            await asyncio.sleep(delay)

        self.stats["errors"] += 1
        self._consecutive_errors += 1
        if self._consecutive_errors >= self.abort_after:
            raise RuntimeError(
                f"Aborting: {self._consecutive_errors} consecutive failures. "
                f"The site may be rate-limiting or down. Do not retry immediately."
            )
        return None
