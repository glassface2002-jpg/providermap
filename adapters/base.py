"""The adapter contract.

ProviderMap's pipeline (discovery, classification, validation, storage,
export) has no knowledge of any particular healthcare system's website. Site
-specific knowledge - URL shapes, HTML structure, which endpoints exist, what
an organization's name looks like on that platform - is isolated behind this
one interface. Supporting a new health system means writing a new
:class:`SiteAdapter` subclass; nothing in ``providermap/`` changes.

See ``adapters/adventhealth/adapter.py`` for a complete implementation, and
its module docstring for the reverse-engineering notes that motivated each
method.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from re import Pattern

from providermap.config import Config
from providermap.models import ProfilePage, VCardData


class SiteAdapter(ABC):
    """Everything the generic pipeline needs to know about one directory site.

    An adapter is deliberately *not* responsible for HTTP, rate limiting,
    caching, retries, or robots.txt - that's ``providermap.net.Fetcher``,
    shared by every adapter. An adapter only answers "what does this site's
    data look like", not "how do I politely fetch it".
    """

    #: Short, stable identifier used in logs, run records, and the CLI
    #: (``--adapter adventhealth``). Lowercase, no spaces.
    name: str

    def __init__(self, config: Config):
        self.config = config

    # ------------------------------------------------------------------ #
    # URL recognition
    # ------------------------------------------------------------------ #

    @abstractmethod
    def profile_url_pattern(self) -> Pattern[str]:
        """A compiled regex matching a provider profile URL, with a named
        group ``npi`` capturing the 10-digit NPI if the site encodes one in
        the URL (most directory platforms do). If the site does not encode
        the NPI in the URL, the adapter must still return a pattern that
        identifies profile URLs, and ``extract_npi`` should look elsewhere
        (e.g. fetch the page and read it from structured data).
        """

    @abstractmethod
    def profile_link_pattern(self) -> Pattern[str]:
        """A compiled regex for pulling profile-page hrefs out of listing-page
        HTML, used only by the HTML-listing fallback source."""

    def extract_npi(self, profile_url: str) -> str | None:
        """Pull the NPI out of a profile URL using :meth:`profile_url_pattern`.

        Overridden only if a site does not encode the NPI in the URL.
        """
        m = self.profile_url_pattern().match(profile_url.strip())
        return m.group("npi") if m and "npi" in m.groupdict() else None

    # ------------------------------------------------------------------ #
    # Endpoints
    # ------------------------------------------------------------------ #

    @abstractmethod
    def vcard_url(self, npi: str) -> str | None:
        """URL of a structured contact-card endpoint for this NPI, or
        ``None`` if the site has no such endpoint (the pipeline then relies
        on :meth:`parse_profile_html` alone for address data)."""

    def sitemap_candidates(self) -> list[str]:
        """Paths worth probing for an XML sitemap, cheapest source first."""
        return ["/sitemap.xml", "/sitemap_index.xml"]

    def jsonapi_type_candidates(self) -> list[str]:
        """Plausible Drupal JSON:API collection paths to probe. Return an
        empty list if the site is known not to be Drupal."""
        return []

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

    @abstractmethod
    def parse_vcard(self, text: str) -> VCardData:
        """Parse the response body of :meth:`vcard_url` into structured
        fields. Most adapters can delegate to
        :func:`providermap.parser_utils.parse_vcard`."""

    @abstractmethod
    def parse_profile_html(self, html: str) -> ProfilePage:
        """Extract whatever the vCard doesn't carry: display name (as a
        fallback), specialty, and a count of distinct practice locations."""

    def organization_name_patterns(self) -> list[tuple[Pattern[str], str]]:
        """Extra ``(pattern, ProviderType-label)`` pairs specific to this
        site's branding, layered on top of the generic hospital/clinic/LLC
        keyword patterns in :mod:`providermap.validator`. Used only as a
        fallback when NPPES cannot classify a record.

        Example: an AdventHealth-branded facility page whose name starts
        with the health system's own name and carries no other keyword.
        """
        return []

    # ------------------------------------------------------------------ #
    # Listing
    # ------------------------------------------------------------------ #

    @abstractmethod
    def listing_url(self, page: int) -> str:
        """URL of one page of the paginated HTML directory listing (used only
        by the html_listing fallback source)."""
