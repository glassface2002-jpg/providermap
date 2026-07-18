"""CMS "Hospital General Information" organization adapter.

Ingests CMS's public Hospital General Information dataset - a periodic bulk
CSV published on the CMS Provider Data Catalog
(https://data.cms.gov/provider-data/dataset/xubh-q36u), not a website to
scrape. This is exactly the kind of source
:class:`~adapters.organizations.base.OrganizationAdapter` was shaped for:
``discover()`` streams rows from a file rather than fetching URLs.

The dataset is not downloaded automatically. CMS refreshes it periodically
and expects bulk consumers to pull the CSV themselves rather than hit a live
endpoint per request; pointing this adapter at a stale copy is also a
deliberate, visible operator choice rather than a silent one. Download the
CSV from the link above and set ``organizations.cms_hospitals_csv_path`` in
``config.yaml`` to its path.

Column names below are the dataset's real, stable headers as CMS publishes
them: ``Facility ID``, ``Facility Name``, ``Address``, ``City/Town``,
``State``, ``ZIP Code``, ``Phone Number``. CMS represents an unknown value as
the literal string ``"Not Available"`` in several columns; :func:`_clean`
treats that the same as an empty cell.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from providermap.models import Organization
from providermap.parser_utils import normalize_org_name

from ..base import OrganizationAdapter

_NOT_AVAILABLE = "not available"


def _clean(value: str | None) -> str | None:
    """Collapse CMS's blank/placeholder cells to ``None``."""
    if value is None:
        return None
    v = value.strip()
    if not v or v.lower() == _NOT_AVAILABLE:
        return None
    return v


class CMSHospitalsAdapter(OrganizationAdapter):
    """The first concrete organization adapter - see ROADMAP.md stage 3."""

    name = "cms_hospitals"
    source = "CMS"

    def discover(self) -> Iterator[dict[str, Any]]:
        path = Path(self.config.organizations.cms_hospitals_csv_path)
        if not path.exists():
            raise FileNotFoundError(
                f"CMS hospital dataset not found at {path}. Download "
                f"'Hospital_General_Information.csv' from "
                f"https://data.cms.gov/provider-data/dataset/xubh-q36u and "
                f"set organizations.cms_hospitals_csv_path in config.yaml to "
                f"its location."
            )
        # utf-8-sig: CMS's export carries a BOM; plain utf-8 would leave it
        # stuck to the first header name ("﻿Facility ID"), silently
        # breaking every extract() lookup on that column.
        with path.open(newline="", encoding="utf-8-sig") as fh:
            yield from csv.DictReader(fh)

    def extract(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_id": _clean(raw.get("Facility ID")),
            "name": _clean(raw.get("Facility Name")),
            "address": _clean(raw.get("Address")),
            "city": _clean(raw.get("City/Town")),
            "state": _clean(raw.get("State")),
            "zip": _clean(raw.get("ZIP Code")),
            "phone": _clean(raw.get("Phone Number")),
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
