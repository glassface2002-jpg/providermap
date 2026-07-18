"""Adversarial fixture rows for the CMS hospitals adapter's offline tests.

Shaped exactly like CMS's real CSV headers, so ``extract()`` is exercised
against the actual column names rather than a simplified stand-in for them.
Covers, by construction:

  * a complete row with every field present
  * CMS's "Not Available" placeholder in a normally-populated column
  * blank-string cells (the other way CMS represents "no value")
  * a name with irregular internal whitespace, to prove normalization
  * two distinct facilities that happen to share a city/state (must not
    collide - dedupe is keyed on Facility ID, not on name/location)
"""

from __future__ import annotations

from typing import Any

ROWS: list[dict[str, Any]] = [
    {
        "Facility ID": "030001",
        "Facility Name": "Banner Desert Medical Center",
        "Address": "1400 S Dobson Rd",
        "City/Town": "Mesa",
        "State": "AZ",
        "ZIP Code": "85202",
        "Phone Number": "(480) 412-3000",
    },
    {
        "Facility ID": "030002",
        "Facility Name": "  Chandler   Regional Medical Center  ",
        "Address": "1955 W Frye Rd",
        "City/Town": "Chandler",
        "State": "AZ",
        "ZIP Code": "85224",
        "Phone Number": "Not Available",
    },
    {
        "Facility ID": "030003",
        "Facility Name": "Mesa General Hospital",
        "Address": "515 N Mesa Dr",
        "City/Town": "Mesa",
        "State": "AZ",
        "ZIP Code": "",
        "Phone Number": "(480) 555-0100",
    },
]

EXPECTED = {
    "row_count": len(ROWS),
    "distinct_source_ids": {r["Facility ID"] for r in ROWS},
}
