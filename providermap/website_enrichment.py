"""Best-effort organization website discovery (ROADMAP.md stage 4).

Feeds ``Organization.website`` / ``website_confidence`` (schema v4) with a
*guess*, not an authoritative lookup - there is no free, reliable public API
mapping a hospital's legal name to its official domain, and real
health-system websites frequently don't match the facility's legal name at
all (e.g. "Banner Desert Medical Center" lives at bannerhealth.com, not
bannerdesert.com). Given that, this module deliberately:

  * never returns ``Confidence.HIGH`` - a guess, however plausible, is not
    confirmed data, and nothing downstream should ever treat it as one;
  * only ever reports a domain that actually resolved to a live page, never
    an unverified guess;
  * distinguishes ``Confidence.MEDIUM`` (the organization's own name shows up
    on the page it found) from ``Confidence.LOW`` (the domain resolves, but
    nothing on the page corroborates it's the right organization).

Network access is injected as a callable (:data:`DomainChecker`) rather than
performed directly with ``httpx`` in :func:`discover_website`, so the pure
candidate-generation and confidence-scoring logic - the part worth testing -
stays fully offline, matching the rest of this project's test suite.
:func:`http_domain_checker` is the real, network-backed implementation used
against a live run (see ``providermap/cli.py``'s ``enrich-organizations``
command).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

import httpx

from .models import Confidence, Organization
from .net import RateLimiter

DomainChecker = Callable[[str], Awaitable["str | None"]]

# Words common enough across hospital names/pages that matching on them
# proves nothing - either as a trailing word to strip while generating
# candidates, or as a token to ignore when scoring a name-match.
_GENERIC_WORDS = {
    "medical",
    "center",
    "centre",
    "hospital",
    "health",
    "healthcare",
    "system",
    "systems",
    "regional",
    "general",
    "clinic",
    "group",
    "medicine",
    "care",
    "community",
}

_TLDS = ("com", "org")


def candidate_domains(name: str | None) -> list[str]:
    """Ordered, most-specific-first candidate hostnames for ``name``.

    Progressively drops a trailing *generic* word ("Medical Center",
    "Regional", "Hospital", ...) and retries, since a health system's
    branded domain is often a prefix of the full legal facility name.
    Stops the moment the trailing word isn't generic, rather than guessing
    all the way down to a single word - a bare first name (e.g. "Banner"
    alone) is too likely to collide with an unrelated business to be worth
    trying.
    """
    if not name:
        return []
    words = re.findall(r"[a-z0-9]+", name.lower())
    if not words:
        return []

    slugs: list[str] = []
    trimmed = words[:]
    while trimmed:
        slug = "".join(trimmed)
        if slug not in slugs:
            slugs.append(slug)
        if trimmed[-1] in _GENERIC_WORDS:
            trimmed = trimmed[:-1]
        else:
            break

    return [f"{slug}.{tld}" for slug in slugs for tld in _TLDS]


def _distinctive_tokens(name: str | None) -> list[str]:
    """Words from ``name`` worth checking for on a candidate page - long
    enough and specific enough that finding one actually corroborates a
    match, unlike a generic word every hospital's site contains."""
    if not name:
        return []
    return [
        w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _GENERIC_WORDS and len(w) > 3
    ]


async def discover_website(
    org: Organization,
    checker: DomainChecker,
    requests_per_second: float = 1.0,
) -> tuple[str | None, Confidence | None]:
    """Try each candidate domain in turn; return the first that resolves.

    Returns ``(None, None)`` if nothing resolves - a website is left
    undiscovered rather than guessed at with no verification at all.
    """
    candidates = candidate_domains(org.normalized_name or org.name)
    if not candidates:
        return None, None

    limiter = RateLimiter(requests_per_second)
    distinctive = _distinctive_tokens(org.name)

    for domain in candidates:
        await limiter.wait()
        url = f"https://{domain}"
        html = await checker(url)
        if html is None:
            continue
        matched = any(tok in html.lower() for tok in distinctive)
        return url, (Confidence.MEDIUM if matched else Confidence.LOW)

    return None, None


async def http_domain_checker(
    url: str, user_agent: str, timeout_seconds: float = 8.0
) -> str | None:
    """The real, network-backed :data:`DomainChecker` used against a live
    run. A fresh client per call (rather than a shared, reused one) is
    deliberate: each check is an independent, low-volume existence probe of
    an unrelated domain, not a sustained session worth pooling connections
    for.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds) as client:
            resp = await client.get(url, headers={"User-Agent": user_agent})
    except httpx.HTTPError:
        return None
    return resp.text if resp.status_code == 200 else None
