"""CMS "Hospital General Information" organization adapter.

Ingests CMS's public Hospital General Information dataset - a periodic bulk
CSV published on the CMS Provider Data Catalog
(https://data.cms.gov/provider-data/dataset/xubh-q36u), not a website to
scrape. This is exactly the kind of source
:class:`~adapters.organizations.base.OrganizationAdapter` was shaped for.

By default, ``discover()`` fetches the dataset itself: CMS's metastore API
(``https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/xubh-q36u``)
returns the *current* CSV download URL keyed by the dataset's permanent
identifier (``xubh-q36u``) - that identifier is stable, but the download
URL's resource hash changes every time CMS republishes the file, so looking
it up via the metastore on each run (rather than hardcoding a URL that will
eventually 404) is what makes auto-fetch safe to rely on long-term. Setting
``organizations.cms_hospitals_csv_path`` in ``config.yaml`` overrides this
with a local file instead - useful for a pinned/offline copy, or for the
adversarial fixture this adapter's own tests use.

Column names below are the dataset's real, stable headers as CMS publishes
them (verified directly against a live download, not assumed):
``Facility ID``, ``Facility Name``, ``Address``, ``City/Town``, ``State``,
``ZIP Code``, ``Telephone Number``. CMS represents an unknown value as the
literal string ``"Not Available"`` in several columns; :func:`_clean` treats
that the same as an empty cell. This dataset has no website/URL field at
all - see ``providermap/wikidata_hospitals.py`` and
``providermap/website_enrichment.py`` for how that's discovered separately.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from providermap.models import Organization
from providermap.parser_utils import normalize_org_name

from ..base import OrganizationAdapter

_NOT_AVAILABLE = "not available"

_METASTORE_URL = (
    "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/xubh-q36u"
)


def _clean(value: str | None) -> str | None:
    """Collapse CMS's blank/placeholder cells to ``None``."""
    if value is None:
        return None
    v = value.strip()
    if not v or v.lower() == _NOT_AVAILABLE:
        return None
    return v


def _current_csv_download_url(user_agent: str, timeout_seconds: float) -> str:
    """Ask CMS's metastore API where the *current* CSV actually lives.

    Never hardcode the resource URL this returns - it embeds a content hash
    that changes on every CMS republish. The dataset identifier
    (``xubh-q36u``) in :data:`_METASTORE_URL` is the only part of this
    that's meant to be stable.
    """
    resp = httpx.get(_METASTORE_URL, headers={"User-Agent": user_agent}, timeout=timeout_seconds)
    resp.raise_for_status()
    data = resp.json()
    try:
        return str(data["distribution"][0]["downloadURL"])
    except (KeyError, IndexError) as exc:
        raise RuntimeError(
            f"CMS's metastore API response for dataset xubh-q36u didn't have "
            f"the expected distribution[0].downloadURL shape - CMS may have "
            f"changed their API. Response: {data!r}"
        ) from exc


def _fetch_dataset_csv(user_agent: str, timeout_seconds: float) -> str:
    """Auto-fetch: look up the current download URL, then fetch it."""
    url = _current_csv_download_url(user_agent, timeout_seconds)
    resp = httpx.get(url, headers={"User-Agent": user_agent}, timeout=timeout_seconds)
    resp.raise_for_status()
    return resp.text


class CMSHospitalsAdapter(OrganizationAdapter):
    """The first concrete organization adapter - see ROADMAP.md stage 3."""

    name = "cms_hospitals"
    source = "CMS"

    def discover(self) -> Iterator[dict[str, Any]]:
        path = self.config.organizations.cms_hospitals_csv_path
        if path:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(
                    f"organizations.cms_hospitals_csv_path is set to {p}, but "
                    f"that file doesn't exist. Leave it unset (null) in "
                    f"config.yaml to auto-fetch the current dataset instead."
                )
            # utf-8-sig: CMS's export carries a BOM; plain utf-8 would leave
            # it stuck to the first header name ("﻿Facility ID"),
            # silently breaking every extract() lookup on that column.
            with p.open(newline="", encoding="utf-8-sig") as fh:
                yield from csv.DictReader(fh)
            return

        text = _fetch_dataset_csv(
            self.config.politeness.user_agent,
            self.config.organizations.cms_hospitals_fetch_timeout_seconds,
        )
        # CMS's live export also carries a BOM; strip it the same way the
        # file-based path's utf-8-sig decoding does.
        yield from csv.DictReader(io.StringIO(text.lstrip("﻿")))

    def extract(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_id": _clean(raw.get("Facility ID")),
            "name": _clean(raw.get("Facility Name")),
            "address": _clean(raw.get("Address")),
            "city": _clean(raw.get("City/Town")),
            "state": _clean(raw.get("State")),
            "zip": _clean(raw.get("ZIP Code")),
            "phone": _clean(raw.get("Telephone Number")),
        }

    def normalize(self, extracted: dict[str, Any]) -> Organization:
        name = extracted.get("name")
        return Organization(
            name=name,
            normalized_name=normalize_org_name(name),
            # The dataset is CMS's hospital list end to end - every row is a
            # hospital by definition, not a guess from a field value.
            organization_type="hospital",
            address=extracted.get("address"),
            city=extracted.get("city"),
            state=extracted.get("state"),
            zip=extracted.get("zip"),
            phone=extracted.get("phone"),
            source=self.source,
            source_id=extracted.get("source_id"),
        )
