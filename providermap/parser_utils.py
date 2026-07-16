"""Generic parsing utilities with no knowledge of any particular directory site.

Everything here is either a real standard (the CMS NPI checksum, RFC 6350
vCard) or a pure text-transformation utility (address normalization, name
splitting) that any :class:`~providermap.adapters.base.SiteAdapter` can reuse.
Site-specific patterns - URL shapes, HTML structure, brand-name keyword lists
- live in the adapter module for that site, not here.
"""

from __future__ import annotations

import json
import re
from re import Pattern
from typing import Any

from .models import VCardData

_JSONLD_SCRIPT_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

# schema.org types that plausibly describe an individual healthcare provider.
# Sites vary in which one they pick (and sometimes assign more than one via a
# JSON-LD array on @type), so this is deliberately a set of candidates, not a
# single expected value.
_PHYSICIAN_JSONLD_TYPES = {"physician", "person", "medicalorganization", "medicalbusiness"}


def npi_checksum_valid(npi: str) -> bool:
    """Validate an NPI against the CMS Luhn check (ISO/IEC 7812 with an
    ``80840`` prefix, per the NPI Final Rule).

    This is what lets the pipeline reject a coincidental 10-digit number in a
    URL slug that is not actually an NPI, before it ever reaches NPPES or the
    dedupe key.
    """
    if not npi or len(npi) != 10 or not npi.isdigit():
        return False
    digits = [int(c) for c in "80840" + npi[:9]]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (total + int(npi[9])) % 10 == 0


def split_name_and_credentials(display: str) -> tuple[str, str]:
    """``'Justin Menezes, MD'`` -> ``('Justin Menezes', 'MD')``.

    ``'Ana Ruiz-Sanchez, APRN, FNP-C'`` -> ``('Ana Ruiz-Sanchez', 'APRN, FNP-C')``.

    Returns ``('', '')`` for empty input. Never guesses: with no comma, the
    whole string is treated as the name and credentials come back empty.
    """
    if not display:
        return "", ""
    display = re.sub(r"\s+", " ", display).strip()
    parts = [p.strip() for p in display.split(",")]
    if len(parts) == 1:
        return parts[0], ""
    name = parts[0]
    creds = [p for p in parts[1:] if p]
    return name, ", ".join(creds)


def credential_tokens(credentials: str) -> set[str]:
    """``'APRN, FNP-C'`` -> ``{'APRN', 'FNP-C'}``, upper-cased and trimmed."""
    if not credentials:
        return set()
    toks = re.split(r"[,\s/]+", credentials.upper())
    return {t.strip(". ") for t in toks if t.strip(". ")}


def split_person_name(name: str) -> tuple[str | None, str | None]:
    """Best-effort first/last split for an individual.

    Returns ``(None, None)`` rather than guessing when the shape is unclear.
    This is only a fallback - NPPES's own first/last fields are preferred
    wherever a lookup succeeded.
    """
    if not name:
        return None, None
    name = re.sub(r"\s+", " ", name).strip()
    parts = name.split(" ")
    if len(parts) < 2:
        return None, None
    first, last = parts[0], parts[-1]
    if last.upper().rstrip(".") in {"JR", "SR", "II", "III", "IV", "V"} and len(parts) >= 3:
        last = parts[-2]
    return first, last


def clean_phone(raw: str) -> str | None:
    """Normalize any phone-like string to ``NNN-NNN-NNNN``, or ``None``."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"


def parse_vcard(text: str) -> VCardData:
    """Parse an RFC 6350-ish vCard response into structured fields.

    Some directory platforms (this project's AdventHealth adapter among them)
    populate the ``ADR`` property non-standardly - putting a facility label in
    the "extended address" slot rather than a suite number. This function
    reads ``ADR`` positionally and does not assume strict RFC semantics,
    which makes it tolerant of that in either direction.
    """
    out = VCardData()
    if not text or "BEGIN:VCARD" not in text.upper():
        return out

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        prop, _, value = line.partition(":")
        name = prop.split(";")[0].strip().upper()
        value = value.strip()

        if name == "FN":
            out.full_name = value
        elif name == "ORG":
            out.organization = value
        elif name == "TEL":
            out.phone = clean_phone(value)
        elif name == "TITLE":
            out.title = value
        elif name == "ADR":
            parts = [p.strip() for p in value.split(";")]
            padded = parts + [""] * (8 - len(parts))  # pad so index access never raises
            out.facility_label = padded[2] or None
            out.address = padded[3] or None
            out.city = padded[4] or None
            out.state = padded[5] or None
            out.zip = padded[6] or None
            out.country = padded[7] or None
    return out


def _matches_physician_type(node: dict[str, Any]) -> bool:
    raw_type = node.get("@type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    return any(isinstance(t, str) and t.lower() in _PHYSICIAN_JSONLD_TYPES for t in types)


def _iter_jsonld_nodes(parsed: Any) -> list[dict[str, Any]]:
    """Flatten one decoded JSON-LD document into a list of candidate nodes.

    Handles the shapes actually seen in the wild: a single object, a bare
    array of objects, and an object wrapping its nodes in ``@graph``.
    """
    if isinstance(parsed, dict):
        graph = parsed.get("@graph")
        if isinstance(graph, list):
            return [n for n in graph if isinstance(n, dict)]
        return [parsed]
    if isinstance(parsed, list):
        return [n for n in parsed if isinstance(n, dict)]
    return []


def parse_jsonld_physician(html: str) -> dict[str, Any] | None:
    """Find and decode the schema.org ``Physician``-shaped JSON-LD block on a
    provider profile page, if the site publishes one.

    JSON-LD is a real, site-agnostic standard (unlike this project's
    per-adapter HTML scraping), so this lives here rather than in a specific
    adapter - any :class:`~providermap.adapters.base.SiteAdapter` can reuse
    it. It only extracts and returns the raw decoded node; mapping its
    properties onto :class:`~providermap.models.ProfilePage` is adapter-
    specific, since sites disagree on which schema.org properties they
    actually populate.

    Never raises: a missing script tag, malformed JSON, or a document with no
    node matching :data:`_PHYSICIAN_JSONLD_TYPES` all just return ``None``,
    so a caller can fall back to HTML scraping unconditionally.
    """
    if not html:
        return None
    for block in _JSONLD_SCRIPT_RE.findall(html):
        try:
            parsed = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _iter_jsonld_nodes(parsed):
            if _matches_physician_type(node):
                return node
    return None


# Longest-match-first street/direction abbreviations used by normalize_address_key.
_ADDRESS_ABBREVIATIONS = {
    r"\bstreet\b": "st",
    r"\bavenue\b": "ave",
    r"\bboulevard\b": "blvd",
    r"\bdrive\b": "dr",
    r"\broad\b": "rd",
    r"\blane\b": "ln",
    r"\bcourt\b": "ct",
    r"\bplace\b": "pl",
    r"\bparkway\b": "pkwy",
    r"\bhighway\b": "hwy",
    r"\bsuite\b": "ste",
    r"\bbuilding\b": "bldg",
    r"\bnortheast\b": "ne",
    r"\bnorthwest\b": "nw",
    r"\bsoutheast\b": "se",
    r"\bsouthwest\b": "sw",
    r"\bnorth\b": "n",
    r"\bsouth\b": "s",
    r"\beast\b": "e",
    r"\bwest\b": "w",
}


def normalize_address_key(
    address: str | None,
    city: str | None,
    state: str | None,
    zipcode: str | None,
) -> str | None:
    """Build a stable dedupe key so address-string variants collapse to one
    :class:`~providermap.models.Location` row.

    ``'3708 Southwest College Road'`` and ``'3708 SW College Rd.'`` produce
    the same key. Conservative on purpose: whitespace, case, punctuation, and
    a fixed abbreviation table, nothing more speculative than that.
    """
    if not address or not zipcode:
        return None

    s = re.sub(r"[.,#]", " ", address.lower())
    s = re.sub(r"\s+", " ", s).strip()
    for pattern in sorted(_ADDRESS_ABBREVIATIONS, key=len, reverse=True):
        s = re.sub(pattern, _ADDRESS_ABBREVIATIONS[pattern], s)
    s = re.sub(r"\s+", " ", s).strip()

    zip5 = re.sub(r"\D", "", zipcode)[:5]
    state2 = (state or "").strip().upper()[:2]
    return f"{s}|{state2}|{zip5}" if zip5 else None


def extract_matches(html: str, pattern: Pattern[str]) -> list[str]:
    """Run a precompiled regex against page text and return de-duplicated,
    order-preserving matches.

    A thin, generic wrapper so adapters don't each reimplement "find every
    match, keep first-seen order, drop dupes." The pattern itself - what a
    profile link or a location id looks like - is the adapter's business.
    """
    seen: dict[str, None] = {}
    for m in pattern.findall(html):
        seen.setdefault(m, None)
    return list(seen)
