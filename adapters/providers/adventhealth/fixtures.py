"""A self-contained 100-record test dataset for the AdventHealth adapter, and
an :class:`OfflineFetcher` that serves it with zero network access.

Why this exists
----------------
The full pipeline - discovery, vCard parsing, NPPES classification,
validation, dedupe, location counting, incremental change detection, Excel
export - can be exercised end to end with zero network access and zero load
on the real site. That means the pipeline can be proven correct before a
single request is sent, and a regression in the classifier is caught here
instead of partway through an 11-hour live crawl.

The dataset is deliberately adversarial. It contains, by construction:

  * physicians (MD, DO, DPM, DDS) and advanced practice providers
    (APRN, PA-C, CNM, CRNA)
  * facility/hospital/clinic/organization pages that must be excluded
  * an organization whose NAME contains "MD"  <- defeats credential-only
    heuristics; only NPPES (or the org-name fallback pattern) catches it
  * a provider with no address                <- must be excluded, never guessed
  * a record with only a phone number          <- must be excluded
  * an invalid NPI checksum                    <- must be caught
  * two different people with the same name    <- must NOT be auto-merged
  * the same provider at two URLs              <- must be deduped to one
  * a 404 profile
  * a provider with four locations             <- must get site_confidence=low

Every NPI is generated with a valid CMS Luhn checksum (except the one
deliberately invalid one), so the fixture exercises the real validation path,
not a simplified stand-in for it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from providermap.config import Config
from providermap.net import Cache

log = logging.getLogger(__name__)

BASE = "https://www.adventhealth.com"


def make_npi(seed: int) -> str:
    """Generate a syntactically valid NPI (9 digits + CMS Luhn check digit)."""
    body = f"{seed:09d}"
    digits = [int(c) for c in "80840" + body]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return body + str((10 - total % 10) % 10)


@dataclass
class FixtureRecord:
    """One synthetic directory entry. Loosely typed on purpose - this models
    heterogeneous raw site data, which is exactly what a real adapter has to
    cope with, not the clean :mod:`providermap.models` types those parse into.
    """

    kind: str  # "person" | "org" | "missing" | "alias"
    npi: str
    first: str = ""
    last: str = ""
    cred: str = ""
    specialty: str | None = None
    taxonomy: str | None = None
    taxonomy_desc: str | None = None
    site: tuple[str | None, ...] | None = None
    locations: int = 1
    org_name: str = ""
    label: str = ""
    edge: str | None = None
    nppes: bool = True
    of: int | None = None
    url: str = ""
    name: str = ""
    # Raw <script type="application/ld+json"> body text to inject into this
    # record's rendered profile page, if any. None (the default, and true for
    # nearly every record in this dataset) means no JSON-LD block at all -
    # exercising the pure-HTML-fallback path, which is the common case on
    # sites that don't publish schema.org markup.
    jsonld: str | None = None


_FIRST = [
    "Justin",
    "Maria",
    "David",
    "Aisha",
    "Robert",
    "Priya",
    "Michael",
    "Elena",
    "James",
    "Sofia",
    "Daniel",
    "Grace",
    "Thomas",
    "Nina",
    "Andrew",
    "Rosa",
    "Kevin",
    "Leah",
    "Paul",
    "Chen",
]
_LAST = [
    "Menezes",
    "Alvarez",
    "Chen",
    "Okafor",
    "Whitfield",
    "Nair",
    "Brennan",
    "Petrov",
    "Sullivan",
    "Marchetti",
    "Kowalski",
    "Adeyemi",
    "Lindqvist",
    "Raymond",
    "Dubois",
    "Santiago",
    "Yamada",
    "Fitzgerald",
    "Novak",
    "Wu",
]

_PHYS = [
    ("MD", "Family Medicine", "207Q00000X", "Family Medicine Physician"),
    ("MD", "Cardiovascular Disease", "207RC0000X", "Cardiovascular Disease Physician"),
    ("DO", "Internal Medicine", "207R00000X", "Internal Medicine Physician"),
    ("MD", "Orthopedic Surgery", "207X00000X", "Orthopaedic Surgery Physician"),
    ("DPM", "Podiatry", "213E00000X", "Podiatrist"),
    ("MD", "Pediatrics", "208000000X", "Pediatrics Physician"),
    ("DDS", "Oral and Maxillofacial Surgery", "1223S0112X", "Oral Surgeon"),
    ("DO", "Gastroenterology", "207RG0100X", "Gastroenterology Physician"),
]

_APPS = [
    ("APRN, FNP-C", "Family Medicine", "363L00000X", "Nurse Practitioner"),
    ("PA-C", "Orthopedic Surgery", "363A00000X", "Physician Assistant"),
    ("APRN, CNM", "OBGYN", "367A00000X", "Advanced Practice Midwife"),
    ("CRNA", "Anesthesiology", "367500000X", "Nurse Anesthetist"),
    ("APRN, DNP", "Cardiovascular Disease", "363L00000X", "Nurse Practitioner"),
]

_SITES: list[tuple[str, str, str, str, str, str]] = [
    (
        "AdventHealth Medical Group Family Medicine at Winter Park",
        "133 Benmore Drive Suite 200",
        "Winter Park",
        "FL",
        "32792",
        "407-646-7070",
    ),
    (
        "AdventHealth Medical Group Cardiology at East Orlando",
        "258 South Chickasaw Trail Suite 203",
        "Orlando",
        "FL",
        "32825",
        "407-303-6588",
    ),
    (
        "AdventHealth Medical Group Internal Medicine at Bruce B Downs",
        "13601 Bruce B Downs Blvd Suite 160",
        "Tampa",
        "FL",
        "33613",
        "813-588-3516",
    ),
    (
        "AdventHealth Medical Group Family Medicine at Castle Pines",
        "250 Max Drive Suite 102",
        "Castle Pines",
        "CO",
        "80108",
        "303-649-3350",
    ),
    (
        "UChicago Medicine AdventHealth Medical Group Primary Care at Lombard",
        "2050 Finley Road Suite 50",
        "Lombard",
        "IL",
        "60148",
        "630-819-5600",
    ),
    (
        "AdventHealth Medical Group Audiology at Hendersonville",
        "80 Doctors Drive Suite 1",
        "Hendersonville",
        "NC",
        "28792",
        "828-650-8048",
    ),
    (
        "AdventHealth Medical Group Gynecologic Oncology at Shawnee Mission",
        "9100 West 74th Street",
        "Merriam",
        "KS",
        "66204",
        "913-632-9100",
    ),
]

_ORGS: list[tuple[str, str, str, str, str, str, str]] = [
    (
        "AdventHealth Centra Care Ocala",
        "Clinic",
        "3708 Southwest College Road",
        "Ocala",
        "FL",
        "34474",
        "352-401-8401",
    ),
    (
        "AdventHealth Hendersonville",
        "Hospital",
        "100 Hospital Drive",
        "Hendersonville",
        "NC",
        "28792",
        "828-684-8501",
    ),
    (
        "AdventHealth Medical Group",
        "Organization",
        "601 East Rollins Street",
        "Orlando",
        "FL",
        "32803",
        "407-303-5600",
    ),
    (
        "AdventHealth Centra Care Winter Garden",
        "Clinic",
        "13750 W Colonial Dr",
        "Winter Garden",
        "FL",
        "34787",
        "407-905-8850",
    ),
    (
        "AdventHealth Imaging at Altamonte",
        "Facility",
        "601 E Altamonte Dr",
        "Altamonte Springs",
        "FL",
        "32701",
        "407-303-2200",
    ),
    (
        "AdventHealth Orlando",
        "Hospital",
        "601 East Rollins Street",
        "Orlando",
        "FL",
        "32803",
        "407-303-5600",
    ),
    (
        "Whitfield and Brennan MD PA",
        "Organization",
        "1200 Oak Ridge Rd",
        "Orlando",
        "FL",
        "32809",
        "407-555-0142",
    ),
]


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", name.lower())
    return re.sub(r"[\s-]+", "-", s).strip("-")


def build_dataset() -> dict[str, Any]:
    """Deterministically build the 100-record fixture."""
    records: list[FixtureRecord] = []
    seed = 100_000_000

    def nxt() -> str:
        nonlocal seed
        seed += 7
        return make_npi(seed)

    for i in range(60):  # physicians
        cred, spec, tax, taxdesc = _PHYS[i % len(_PHYS)]
        records.append(
            FixtureRecord(
                kind="person",
                npi=nxt(),
                first=_FIRST[i % len(_FIRST)],
                last=_LAST[(i * 3) % len(_LAST)],
                cred=cred,
                specialty=spec,
                taxonomy=tax,
                taxonomy_desc=taxdesc,
                site=_SITES[i % len(_SITES)],
            )
        )

    for i in range(20):  # advanced practice providers
        cred, spec, tax, taxdesc = _APPS[i % len(_APPS)]
        records.append(
            FixtureRecord(
                kind="person",
                npi=nxt(),
                first=_FIRST[(i * 5 + 1) % len(_FIRST)],
                last=_LAST[(i * 7 + 2) % len(_LAST)],
                cred=cred,
                specialty=spec,
                taxonomy=tax,
                taxonomy_desc=taxdesc,
                site=_SITES[(i + 2) % len(_SITES)],
            )
        )

    for i in range(12):  # facilities/organizations masquerading as providers
        org_name, label, addr, city, st, zp, phone = _ORGS[i % len(_ORGS)]
        suffix = "" if i < len(_ORGS) else f" {i}"
        records.append(
            FixtureRecord(
                kind="org",
                npi=nxt(),
                org_name=org_name + suffix,
                label=label,
                specialty="Family Medicine, Pediatrics",
                locations=1,
                site=(org_name + suffix, addr, city, st, zp, phone),
            )
        )

    edge_site = _SITES[0]
    records.append(
        FixtureRecord(  # no address at all
            kind="person",
            npi=nxt(),
            first="Marcus",
            last="Halloway",
            cred="MD",
            specialty="Neurology",
            taxonomy="2084N0400X",
            taxonomy_desc="Neurology Physician",
            site=None,
            locations=0,
            edge="no_address",
        )
    )
    records.append(
        FixtureRecord(  # phone only
            kind="person",
            npi=nxt(),
            specialty=None,
            site=(None, None, None, None, None, "407-555-0199"),
            locations=0,
            edge="phone_only",
        )
    )
    records.append(
        FixtureRecord(  # invalid NPI checksum
            kind="person",
            npi="1234567890",
            first="Iris",
            last="Calloway",
            cred="MD",
            specialty="Dermatology",
            taxonomy="207N00000X",
            taxonomy_desc="Dermatology Physician",
            site=edge_site,
            locations=1,
            edge="bad_npi",
        )
    )
    for _ in range(2):  # two DIFFERENT people, same name, same city
        records.append(
            FixtureRecord(
                kind="person",
                npi=nxt(),
                first="David",
                last="Chen",
                cred="MD",
                specialty="Cardiovascular Disease",
                taxonomy="207RC0000X",
                taxonomy_desc="Cardiovascular Disease Physician",
                site=_SITES[1],
                locations=1,
                edge="same_name",
            )
        )
    records.append(
        FixtureRecord(  # one provider, four locations
            kind="person",
            npi=nxt(),
            first="Amara",
            last="Osei",
            cred="MD",
            specialty="Cardiovascular Disease",
            taxonomy="207RC0000X",
            taxonomy_desc="Cardiovascular Disease Physician",
            site=_SITES[1],
            locations=4,
            edge="multi_location",
        )
    )
    records.append(FixtureRecord(kind="missing", npi=nxt(), edge="404"))  # 404
    records.append(
        FixtureRecord(  # NPI with no NPPES record
            kind="person",
            npi=nxt(),
            first="Elena",
            last="Vasquez",
            cred="DO",
            specialty="Internal Medicine",
            taxonomy=None,
            taxonomy_desc=None,
            site=_SITES[2],
            locations=1,
            edge="no_nppes",
            nppes=False,
        )
    )

    dup_of_index = 0
    records.append(
        FixtureRecord(  # duplicate URL for record 0: same NPI
            kind="alias",
            npi=records[dup_of_index].npi,
            of=dup_of_index,
            locations=records[dup_of_index].locations,
            edge="duplicate_url",
        )
    )

    for r in records:
        if r.kind == "org":
            r.url = f"{BASE}/doctors/{_slug(r.org_name)}-{r.npi}"
            r.name = r.org_name
        elif r.kind == "alias":
            assert r.of is not None
            src = records[r.of]
            r.url = (
                f"{BASE}/find-doctor/doctor/"
                f"{_slug(src.first + '-' + src.last + '-' + src.cred)}-{r.npi}"
            )
            r.name = f"{src.first} {src.last}"
        elif r.kind == "missing":
            r.url = f"{BASE}/doctors/deleted-profile-{r.npi}"
            r.name = ""
        else:
            nm = f"{r.first} {r.last}".strip() or "unknown"
            r.url = f"{BASE}/doctors/{_slug(nm + '-' + r.cred)}-{r.npi}"
            r.name = f"{nm}, {r.cred}" if r.cred else nm

    # A few already-existing physician records also carry schema.org JSON-LD
    # on their profile page, to exercise the JSON-LD-primary/HTML-fallback
    # path (see providermap.parser_utils.parse_jsonld_physician and
    # adapters.providers.adventhealth.adapter._apply_jsonld). Attached here rather than
    # via new records, so EXPECTED's fixed counts are unaffected.
    records[0].jsonld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Physician",
            "name": records[0].name,
            "medicalSpecialty": [records[0].specialty],
            "hospitalAffiliation": {"@type": "Hospital", "name": "AdventHealth Orlando"},
            "acceptingNewPatients": True,
            "aggregateRating": {
                "@type": "AggregateRating",
                "ratingValue": 4.8,
                "reviewCount": 132,
            },
            "knowsLanguage": ["English", "Spanish"],
            "acceptedInsurance": ["Aetna", "Cigna", "UnitedHealthcare"],
        }
    )
    records[1].jsonld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Physician",
            "name": records[1].name,
            "medicalSpecialty": [records[1].specialty],
            # Deliberately no hospitalAffiliation/aggregateRating/
            # knowsLanguage/acceptedInsurance - each ProfilePage field they'd
            # populate must independently stay at its default, not raise.
        }
    )
    # Malformed/truncated JSON - must be skipped cleanly, falling back to the
    # og:title/<title> HTML scraping already present on the same page.
    records[2].jsonld = '{"@context": "https://schema.org", "@type": "Physician", "name": '

    return {
        "records": records,
        "by_npi": {r.npi: r for r in records},
        "by_url": {r.url: r for r in records},
    }


DATASET = build_dataset()

EXPECTED = {
    "total_urls": len(DATASET["records"]),
    "individuals": 60 + 20 + 5,  # 60 MD/DO + 20 APP + same_name x2 + multi + no_nppes
    "organizations": 12,
    "excluded_edge_cases": 4,  # no_address, phone_only, bad_npi, 404
    "duplicate_url_aliases": 1,
    "same_name_groups": 1,
}


# ---------------------------------------------------------------------- #
# Response rendering
# ---------------------------------------------------------------------- #


def render_vcard(rec: FixtureRecord) -> str | None:
    site = rec.site
    if rec.kind == "missing" or not site:
        return None
    label, addr, city, st, zp, phone = site
    if not any([label, addr, phone]):
        return None
    return (
        "BEGIN:VCARD\r\nVERSION:3.0\r\n"
        f"FN: {rec.name}\r\nORG: {label or ''}\r\nTEL;WORK;VOICE:{phone or ''}\r\n"
        f"ADR;WORK;PREF:;;{label or ''};{addr or ''} ;{city or ''};{st or ''};{zp or ''};US\r\n"
        f"TITLE:{rec.name}\r\nEND:VCARD\r\n"
    )


def render_profile_html(rec: FixtureRecord) -> str | None:
    if rec.kind == "missing":
        return None
    spec = rec.specialty or ""
    site = rec.site
    city = site[2] if site and site[2] else ""
    st = site[3] if site and site[3] else ""

    # One appointment link per practice location, mirroring the real site's
    # `?npi=...&location=<id>` pattern.
    appt = "".join(
        f'<a href="/request-appointment-0?npi={rec.npi}&location={15050100 + i}">Request</a>'
        for i in range(rec.locations)
    )
    jsonld_block = f'<script type="application/ld+json">{rec.jsonld}</script>' if rec.jsonld else ""
    return (
        "<!DOCTYPE html><html><head>"
        '<meta name="Generator" content="Drupal 11 (https://www.drupal.org)" />'
        f'<meta property="og:title" content="{rec.name}" />'
        f"<title>{rec.name} | {spec} | {city}, {st} | AdventHealth</title>"
        f"{jsonld_block}"
        f"</head><body><h1>{rec.name}</h1>"
        f'<a href="{rec.url.replace(BASE, "")}">profile</a>{appt}</body></html>'
    )


def render_nppes(rec: FixtureRecord) -> str:
    if not rec.nppes or rec.kind == "missing":
        return json.dumps({"result_count": 0, "results": []})

    body: dict[str, Any]
    if rec.kind == "org":
        site = rec.site
        assert site is not None
        body = {
            "enumeration_type": "NPI-2",
            "number": rec.npi,
            "basic": {"organization_name": rec.org_name.upper(), "organizational_subpart": "N"},
            "taxonomies": [{"code": "261Q00000X", "primary": True, "desc": "Clinic/Center"}],
            "addresses": [
                {"address_1": site[1], "city": site[2], "state": site[3], "postal_code": site[4]}
            ],
        }
    else:
        basic = {
            "first_name": rec.first.upper(),
            "last_name": rec.last.upper(),
            "credential": rec.cred,
            "status": "A",
        }
        taxonomies = (
            [{"code": rec.taxonomy, "primary": True, "desc": rec.taxonomy_desc}]
            if rec.taxonomy
            else []
        )
        body = {
            "enumeration_type": "NPI-1",
            "number": rec.npi,
            "basic": basic,
            "taxonomies": taxonomies,
            "addresses": [],
        }
    return json.dumps({"result_count": 1, "results": [body]})


def render_sitemap() -> str:
    locs = "".join(f"<url><loc>{r.url}</loc></url>" for r in DATASET["records"])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</urlset>"
    )


def render_listing(page: int, per_page: int = 10) -> str:
    recs: list[FixtureRecord] = DATASET["records"][page * per_page : (page + 1) * per_page]
    links = "".join(
        f'<div class="result"><a href="{r.url.replace(BASE, "")}">View Profile for {r.name}</a>'
        f"</div>"
        for r in recs
    )
    return (
        "<!DOCTYPE html><html><head>"
        '<meta name="Generator" content="Drupal 11" /><title>Find Doctors</title>'
        f"</head><body>{links}</body></html>"
    )


# ---------------------------------------------------------------------- #
# Offline fetcher - same interface as providermap.net.Fetcher
# ---------------------------------------------------------------------- #


class OfflineFetcher:
    """Drop-in replacement for :class:`providermap.net.Fetcher` that serves
    :data:`DATASET`. No network access of any kind."""

    def __init__(self, config: Config, cache: Cache, *, sitemap: bool = True, **_: object):
        self.config = config
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "robots_blocked": 0}
        self.nppes_limiter = None
        self.limiter = None
        self._sitemap = sitemap
        self.client = None

    async def __aenter__(self) -> OfflineFetcher:
        log.info(
            "OFFLINE MODE: serving %s fixture records; no network requests " "will be made.",
            len(DATASET["records"]),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def allowed(self, url: str) -> bool:
        return True

    async def get(self, url: str, limiter: object = None, use_cache: bool = True) -> str | None:
        self.stats["requests"] += 1
        parsed = urlparse(url)
        path, qs = parsed.path, parse_qs(parsed.query)

        if "npiregistry.cms.hhs.gov" in parsed.netloc:
            npi = (qs.get("number") or [""])[0]
            rec = DATASET["by_npi"].get(npi)
            return render_nppes(rec) if rec else json.dumps({"result_count": 0, "results": []})

        if path.rstrip("/") in ("/sitemap.xml", "/sitemap_index.xml"):
            return render_sitemap() if self._sitemap else None

        # Deliberately absent, so the source-probing order is exercised as it
        # would be against a real site that lacks these endpoints.
        if path.startswith("/jsonapi") or path.startswith("/views/ajax"):
            return None

        m = re.match(r"^/physician/vcard/(\d{10})$", path)
        if m:
            rec = DATASET["by_npi"].get(m.group(1))
            if rec and rec.kind == "alias":
                assert rec.of is not None
                rec = DATASET["records"][rec.of]
            return render_vcard(rec) if rec else None

        if path.rstrip("/") == "/doctors" and not re.search(r"-\d{10}$", path):
            page = int((qs.get("page") or ["0"])[0])
            return render_listing(page)

        canonical = BASE + unquote(path).rstrip("/")
        rec = DATASET["by_url"].get(canonical)
        return render_profile_html(rec) if rec else None
