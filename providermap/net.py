"""HTTP access layer: rate limiting, disk caching, retries, and robots.txt.

Nothing in this module knows about providers, NPIs, or any particular site -
it is a generic, polite async HTTP client that every
:class:`~providermap.adapters.base.SiteAdapter` shares. Site-specific URL
construction happens in the adapter; site-specific *fetching policy*
(requests/sec, concurrency, timeouts, retries) comes entirely from
:class:`~providermap.config.Config`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import urllib.robotparser
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx

from .config import Config

log = logging.getLogger(__name__)


class AsyncFetcher(Protocol):
    """Structural interface shared by :class:`Fetcher` and the offline test
    fetcher (``adapters.adventhealth.fixtures.OfflineFetcher``).

    Lets call sites - the CLI, the pipeline, source discovery - be typed
    against "something that fetches URLs" without importing test-only
    fixture code into production modules just to satisfy the type checker.
    """

    stats: dict[str, int]

    @property
    def nppes_limiter(self) -> RateLimiter | None: ...

    async def __aenter__(self) -> AsyncFetcher: ...

    async def __aexit__(self, *exc: object) -> None: ...

    async def get(
        self, url: str, limiter: RateLimiter | None = None, use_cache: bool = True
    ) -> str | None: ...

    def allowed(self, url: str) -> bool: ...


class RateLimiter:
    """Async token-spacing limiter. One shared instance per traffic class
    (e.g. one for the target site, a separate one for NPPES)."""

    def __init__(self, requests_per_second: float):
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval


class Cache:
    """Content-addressed response cache, so a re-run costs nothing for URLs
    already fetched within ``ttl_days``."""

    def __init__(self, directory: str, enabled: bool = True, ttl_days: int = 30):
        self.enabled = enabled
        self.dir = Path(directory)
        self.ttl = ttl_days * 86400
        if enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        h = hashlib.sha256(key.encode()).hexdigest()
        return self.dir / h[:2] / f"{h}.txt"

    def get(self, key: str) -> str | None:
        if not self.enabled:
            return None
        p = self._path(key)
        if not p.exists():
            return None
        if self.ttl and (time.time() - p.stat().st_mtime) > self.ttl:
            return None
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    def put(self, key: str, value: str) -> None:
        if not self.enabled:
            return
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(value, encoding="utf-8")
        except OSError as exc:
            log.debug("cache write failed for %s: %s", key, exc)


class Fetcher:
    """Polite async HTTP client: robots.txt enforcement, rate limiting,
    retries with backoff, and a hard abort on sustained failure."""

    def __init__(self, config: Config, cache: Cache):
        self.config = config
        self.cache = cache
        pol = config.politeness
        self.base_url = config.site.base_url
        self.user_agent = pol.user_agent
        self.limiter = RateLimiter(pol.requests_per_second)
        self.nppes_limiter = RateLimiter(config.nppes.requests_per_second)
        self.timeout = pol.timeout_seconds
        self.retries = config.retries
        self.abort_after = pol.abort_after_consecutive_errors
        self._consecutive_errors = 0
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self.client: httpx.AsyncClient | None = None
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "robots_blocked": 0}

    async def __aenter__(self) -> Fetcher:
        limits = httpx.Limits(
            max_connections=self.config.politeness.max_concurrent,
            max_keepalive_connections=self.config.politeness.max_concurrent,
        )
        self.client = httpx.AsyncClient(
            headers={"User-Agent": self.user_agent, "Accept-Language": "en-US,en;q=0.9"},
            timeout=self.timeout,
            follow_redirects=True,
            limits=limits,
            http2=True,
        )
        if self.config.politeness.respect_robots_txt:
            await self._load_robots()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.client:
            await self.client.aclose()

    async def _load_robots(self) -> None:
        url = urljoin(self.base_url, "/robots.txt")
        rp = urllib.robotparser.RobotFileParser()
        assert self.client is not None
        try:
            resp = await self.client.get(url)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                log.info("Loaded robots.txt from %s", url)
                delay = rp.crawl_delay(self.user_agent) or rp.crawl_delay("*")
                if delay:
                    log.warning(
                        "robots.txt declares Crawl-delay: %ss - honouring it "
                        "(overrides politeness.requests_per_second).",
                        delay,
                    )
                    self.limiter = RateLimiter(1.0 / float(delay))
            else:
                log.warning(
                    "robots.txt returned HTTP %s; treating as 'no rules published' "
                    "and proceeding at the configured rate.",
                    resp.status_code,
                )
                rp.parse([])
        except httpx.HTTPError as exc:
            log.warning("Could not fetch robots.txt (%s); proceeding cautiously.", exc)
            rp.parse([])
        self._robots = rp

    def allowed(self, url: str) -> bool:
        if not self._robots or not self.config.politeness.respect_robots_txt:
            return True
        try:
            return self._robots.can_fetch(self.user_agent, url)
        except ValueError:
            return True

    async def get(
        self,
        url: str,
        limiter: RateLimiter | None = None,
        use_cache: bool = True,
    ) -> str | None:
        """Fetch a URL. Returns body text, or ``None`` on permanent failure
        (404, disallowed by robots.txt, or retries exhausted)."""
        if use_cache:
            hit = self.cache.get(url)
            if hit is not None:
                self.stats["cache_hits"] += 1
                return hit

        if urlparse(url).netloc.endswith(urlparse(self.base_url).netloc) and not self.allowed(url):
            self.stats["robots_blocked"] += 1
            log.warning("robots.txt disallows %s - skipping", url)
            return None

        lim = limiter or self.limiter
        assert self.client is not None

        for attempt in range(1, self.retries.max_attempts + 1):
            await lim.wait()
            try:
                self.stats["requests"] += 1
                resp = await self.client.get(url)

                if resp.status_code == 200:
                    self._consecutive_errors = 0
                    if use_cache:
                        self.cache.put(url, resp.text)
                    return resp.text

                if resp.status_code == 404:
                    self._consecutive_errors = 0
                    log.debug("404 %s", url)
                    return None

                if resp.status_code in self.retries.retry_on_status:
                    delay = self._backoff(attempt, resp)
                    log.warning(
                        "HTTP %s on %s - retry %s/%s in %.1fs",
                        resp.status_code,
                        url,
                        attempt,
                        self.retries.max_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                log.warning("HTTP %s on %s - not retrying", resp.status_code, url)
                return None

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                delay = self._backoff(attempt)
                log.warning(
                    "%s on %s - retry %s/%s in %.1fs",
                    type(exc).__name__,
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

    def _backoff(self, attempt: int, resp: httpx.Response | None = None) -> float:
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), self.retries.backoff_max_seconds)
                except ValueError:
                    pass
        return min(self.retries.backoff_base_seconds**attempt, self.retries.backoff_max_seconds)
