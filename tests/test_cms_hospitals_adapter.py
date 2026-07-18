"""Tests for the CMS hospitals organization adapter - the first concrete
:class:`~adapters.organizations.base.OrganizationAdapter` (ROADMAP.md stage 3).
"""

from __future__ import annotations

import csv

import pytest

from adapters.organizations.cms_hospitals.adapter import CMSHospitalsAdapter, _clean
from adapters.organizations.cms_hospitals.fixtures import EXPECTED, ROWS
from providermap.config import Config
from providermap.models import Organization


@pytest.fixture()
def cms_adapter(config: Config) -> CMSHospitalsAdapter:
    return CMSHospitalsAdapter(config)


class TestClean:
    def test_strips_whitespace(self) -> None:
        assert _clean("  Mesa  ") == "Mesa"

    def test_not_available_becomes_none(self) -> None:
        assert _clean("Not Available") is None
        assert _clean("not available") is None

    def test_blank_becomes_none(self) -> None:
        assert _clean("") is None
        assert _clean("   ") is None

    def test_none_stays_none(self) -> None:
        assert _clean(None) is None


class TestExtract:
    def test_maps_real_cms_column_names(self, cms_adapter: CMSHospitalsAdapter) -> None:
        extracted = cms_adapter.extract(ROWS[0])
        assert extracted == {
            "source_id": "030001",
            "name": "Banner Desert Medical Center",
            "address": "1400 S Dobson Rd",
            "city": "Mesa",
            "state": "AZ",
            "zip": "85202",
            "phone": "(480) 412-3000",
        }

    def test_not_available_phone_becomes_none(self, cms_adapter: CMSHospitalsAdapter) -> None:
        extracted = cms_adapter.extract(ROWS[1])
        assert extracted["phone"] is None

    def test_blank_zip_becomes_none(self, cms_adapter: CMSHospitalsAdapter) -> None:
        extracted = cms_adapter.extract(ROWS[2])
        assert extracted["zip"] is None


class TestNormalize:
    def test_builds_a_hospital_organization(self, cms_adapter: CMSHospitalsAdapter) -> None:
        org = cms_adapter.normalize(cms_adapter.extract(ROWS[0]))
        assert isinstance(org, Organization)
        assert org.name == "Banner Desert Medical Center"
        assert org.organization_type == "hospital"
        assert org.source == "CMS"
        assert org.source_id == "030001"
        assert org.state == "AZ"

    def test_irregular_whitespace_is_collapsed_in_normalized_name(
        self, cms_adapter: CMSHospitalsAdapter
    ) -> None:
        org = cms_adapter.normalize(cms_adapter.extract(ROWS[1]))
        # extract()'s _clean() only strips leading/trailing whitespace, so
        # the display name still carries the internal double space;
        # normalized_name is where full whitespace collapsing happens.
        assert org.name == "Chandler   Regional Medical Center"
        assert org.normalized_name == "chandler regional medical center"


class TestDiscover:
    def test_streams_rows_from_a_real_csv_file(
        self, cms_adapter: CMSHospitalsAdapter, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        csv_path = tmp_path / "cms_hospitals.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(ROWS[0].keys()))
            writer.writeheader()
            writer.writerows(ROWS)
        cms_adapter.config.organizations.cms_hospitals_csv_path = str(csv_path)

        rows = list(cms_adapter.discover())
        assert len(rows) == EXPECTED["row_count"]
        assert {r["Facility ID"] for r in rows} == EXPECTED["distinct_source_ids"]

    def test_missing_file_raises_a_helpful_error(
        self, cms_adapter: CMSHospitalsAdapter, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        cms_adapter.config.organizations.cms_hospitals_csv_path = str(tmp_path / "missing.csv")
        with pytest.raises(FileNotFoundError, match="cms_hospitals_csv_path"):
            list(cms_adapter.discover())
