"""Client for the CMS NPPES NPI Registry.

This is what makes facility-vs-person classification authoritative rather
than a name-pattern guess: NPPES enumerates every NPI as either an individual
(NPI-1) or an organization (NPI-2). It's free, public, and needs no API key.

Usage of this client is entirely optional in the pipeline - see
``config.example.yaml``'s ``nppes.enabled`` and the circuit breaker below -
so a dead or disabled registry degrades classification quality rather than
blocking the run.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import Config
from .net import AsyncFetcher

log = logging.getLogger(__name__)


class NppesClient:
    """Looks up one NPI at a time against the NPPES registry API."""

    def __init__(self, fetcher: AsyncFetcher, config: Config):
        self.fetcher = fetcher
        self.config = config.nppes
        self.enabled = self.config.enabled
        self.hits = 0
        self.misses = 0

        # Circuit breaker: if NPPES starts failing mid-run, stop calling it
        # rather than spending hours timing out against a dead endpoint.
        # Affected records fall back to heuristic classification and are
        # marked nppes_enumeration='not_found', so the degradation is visible.
        self._consecutive_failures = 0
        self._trip_after = self.config.trip_after_consecutive_failures
        self.tripped = False

    async def lookup(self, npi: str) -> dict[str, Any] | None:
        """Return the raw NPPES result dict for one NPI, or ``None`` if
        disabled, tripped, not found, or the request failed."""
        if not self.enabled or not npi or self.tripped:
            return None

        url = f"{self.config.api_url}?version={self.config.version}&number={npi}"
        body = await self.fetcher.get(url, limiter=self.fetcher.nppes_limiter)
        if not body:
            self.misses += 1
            self._record_failure()
            return None

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log.warning("NPPES returned non-JSON for %s", npi)
            self.misses += 1
            self._record_failure()
            return None

        self._consecutive_failures = 0

        if data.get("Errors"):
            log.debug("NPPES error for %s: %s", npi, data["Errors"])
            self.misses += 1
            return None

        results = data.get("results") or []
        if not results:
            self.misses += 1
            return None
        self.hits += 1
        result: dict[str, Any] = results[0]
        return result

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._trip_after and not self.tripped:
            self.tripped = True
            log.error(
                "NPPES circuit breaker TRIPPED after %s consecutive failures. "
                "Continuing with heuristic classification only - affected "
                "records will be marked nppes_enumeration='not_found' and given "
                "lower confidence. Re-run later to fill them in.",
                self._consecutive_failures,
            )
