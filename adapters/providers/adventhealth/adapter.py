"""AdventHealth provider directory adapter.

This is the first (and reference) implementation of
:class:`providermap.adapters.base.SiteAdapter`, encoding everything specific
to ``adventhealth.com`` that was learned by reverse-engineering it:

* Profile URLs end in the provider's 10-digit NPI, in one of two path shapes:
  ``/doctors/{slug}-{npi}`` and ``/find-doctor/doctor/{slug}-{npi}``.
* A structured, per-NPI vCard endpoint exists at ``/physician/vcard/{npi}``
  and is the best source of practice name, address, and phone - far more
  reliable than parsing the rendered page.
* Profile pages carry one appointment link per practice location
  (``?location=<id>``); the count of distinct ids is a structural proxy for
  how many sites a provider practises at, used to set ``site_confidence``.
* The site is Drupal 11. No GraphQL or Algolia was found during
  investigation; JSON:API and the views AJAX endpoint are worth probing but
  were not confirmed enabled.
* Organization-branded pages (hospitals, urgent care, "AdventHealth Medical
  Group") appear in the doctor directory alongside individual providers and
  must be excluded - see ``organization_name_patterns`` for the brand-name
  fallback used when NPPES can't settle it.
"""

from __future__ import annotations

import re
from re import Pattern
from typing import Any
from urllib.parse import urljoin

from providermap.models import ProfilePage, VCardData
from providermap.parser_utils import parse_jsonld_physician
from providermap.parser_utils import parse_vcard as _parse_vcard

from ...base import SiteAdapter

_PROFILE_URL_RE = re.compile(
    r"^(?:https?://[^/]+)?"
    r"(?P<path>/(?:doctors|find-doctor/doctor)/(?P<slug>[a-z0-9\-]+?)-(?P<npi>\d{10}))/?$",
    re.IGNORECASE,
)

_PROFILE_HREF_RE = re.compile(
    r"""["'](/(?:doctors|find-doctor/doctor)/[a-z0-9\-]+-\d{10})/?["']""",
    re.IGNORECASE,
)

# Distinct appointment-link location ids on a profile page - a structural,
# selector-free proxy for "how many sites does this provider practise at".
_LOCATION_ID_RE = re.compile(r"[?&]location=(\d+)", re.IGNORECASE)

# Organization-branded pages that appear in the doctor directory but are not
# people. Used only when NPPES cannot classify the record. "Centra Care" is
# AdventHealth's urgent-care brand name - it doesn't contain the generic
# "urgent care" keyword the core validator already looks for, so it has to be
# supplied here rather than relying on the generic pattern set alone.
_ORG_NAME_PATTERNS: list[tuple[Pattern[str], str]] = [
    (re.compile(r"\bcentra care\b", re.IGNORECASE), "Clinic"),
    (re.compile(r"^AdventHealth\b", re.IGNORECASE), "Facility"),
]


def _text_or_name(value: Any) -> str | None:
    """A schema.org value that might be a plain string or an object carrying
    a ``name`` property (e.g. ``{"@type": "Organization", "name": "..."}``)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    return None


def _as_str_list(value: Any) -> list[str]:
    """Normalize a schema.org value that may be a single string, a list of
    strings, or a list of ``{"name": ...}``-shaped objects into a flat list."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        text = _text_or_name(item)
        if text:
            out.append(text)
    return out


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "1"}:
            return True
        if low in {"false", "no", "0"}:
            return False
    return None


def _apply_jsonld(out: ProfilePage, node: dict[str, Any]) -> None:
    """Map a schema.org Physician-shaped JSON-LD node onto a ``ProfilePage``.

    Property names below are candidate keys, not confirmed against a live
    page - Akamai bot-management blocked every attempt to inspect real
    AdventHealth JSON-LD during the investigation that motivated this
    adapter change (see ``PROJECT_STATE.md``). Each field checks a short
    list of plausible schema.org property names rather than one assumed-
    correct one, and every branch leaves the corresponding ``ProfilePage``
    field at its default instead of raising if the shape doesn't match.
    Adjust the candidate keys here, not the extraction shape in
    ``providermap.parser_utils.parse_jsonld_physician``, once real markup
    has been inspected via the Playwright fetcher.
    """
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        out.display_name = name.strip()

    specialty = _as_str_list(node.get("medicalSpecialty"))
    if specialty:
        out.specialty = ", ".join(specialty)

    for key in ("hospitalAffiliation", "affiliation", "memberOf"):
        affiliation = _text_or_name(node.get(key))
        if affiliation:
            out.hospital_affiliation = affiliation
            break

    for key in ("acceptingNewPatients", "isAcceptingNewPatients"):
        if key in node:
            accepting = _as_bool(node.get(key))
            if accepting is not None:
                out.accepting_new_patients = accepting
                break

    rating_node = node.get("aggregateRating")
    if isinstance(rating_node, dict):
        try:
            value = rating_node.get("ratingValue")
            if value is not None:
                out.rating = float(value)
        except (TypeError, ValueError):
            pass
        try:
            count = rating_node.get("reviewCount") or rating_node.get("ratingCount")
            if count is not None:
                out.rating_count = int(count)
        except (TypeError, ValueError):
            pass

    languages = _as_str_list(node.get("knowsLanguage") or node.get("availableLanguage"))
    if languages:
        out.languages = languages

    for key in ("acceptedInsurance", "insuranceAccepted"):
        insurance = _as_str_list(node.get(key))
        if insurance:
            out.insurance_accepted = insurance
            break


class AdventHealthAdapter(SiteAdapter):
    name = "adventhealth"

    def profile_url_pattern(self) -> Pattern[str]:
        return _PROFILE_URL_RE

    def profile_link_pattern(self) -> Pattern[str]:
        return _PROFILE_HREF_RE

    def vcard_url(self, npi: str) -> str:
        return urljoin(self.config.site.base_url, self.config.site.vcard_path.format(npi=npi))

    def jsonapi_type_candidates(self) -> list[str]:
        return [
            "node/physician",
            "node/provider",
            "node/doctor",
            "node/physician_profile",
            "node/practitioner",
        ]

    def parse_vcard(self, text: str) -> VCardData:
        return _parse_vcard(text)

    def parse_profile_html(self, html: str) -> ProfilePage:
        """Extract display name, specialty, location count, and the fields
        only JSON-LD carries (hospital affiliation, accepting-new-patients,
        rating, languages, insurance).

        schema.org JSON-LD (via :func:`~providermap.parser_utils.
        parse_jsonld_physician`) is tried first - a structured, site-
        published source is strictly more reliable than scraping rendered
        title text. The og:title/``<title>``-based scraping below is kept as
        a fallback for whatever JSON-LD doesn't supply, including pages that
        don't publish it at all: built from *extracted page text*, not a
        confirmed live DOM inspection - see the "Known limitations" section
        of the README. Both paths fail soft: every field independently
        returns ``None``/empty rather than raising, so neither a missing
        JSON-LD block nor a site redesign crashes the run.
        """
        out = ProfilePage()
        if not html:
            return out

        out.location_ids = sorted(set(_LOCATION_ID_RE.findall(html)))

        jsonld_node = parse_jsonld_physician(html)
        if jsonld_node:
            _apply_jsonld(out, jsonld_node)

        if out.display_name is None or out.specialty is None:
            try:
                from selectolax.parser import HTMLParser

                tree = HTMLParser(html)
            except Exception:
                tree = None

            if tree is not None:
                if out.display_name is None:
                    for meta in tree.css('meta[property="og:title"]'):
                        content = meta.attributes.get("content")
                        if content:
                            out.display_name = content.strip()
                            break
                    if out.display_name is None:
                        h1 = tree.css_first("h1")
                        if h1:
                            out.display_name = h1.text(strip=True) or None

                if out.specialty is None:
                    # This site's <title> is "Name | Specialty | City, ST | AdventHealth".
                    title = tree.css_first("title")
                    if title:
                        segs = [s.strip() for s in title.text(strip=True).split("|")]
                        if len(segs) >= 2 and segs[1] and segs[1].lower() != "adventhealth":
                            out.specialty = segs[1]

        lowered = html[:4000].lower()
        if "page not found" in lowered or "404" in (out.display_name or "").lower():
            out.is_error_page = True
        return out

    def organization_name_patterns(self) -> list[tuple[Pattern[str], str]]:
        return _ORG_NAME_PATTERNS

    def listing_url(self, page: int) -> str:
        listing = urljoin(self.config.site.base_url, self.config.site.listing_path)
        return f"{listing}?{self.config.site.page_param}={page}"
