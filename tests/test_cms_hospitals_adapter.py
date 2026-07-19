"""Tests for the CMS hospitals organization adapter - the first concrete
:class:`~adapters.organizations.base.OrganizationAdapter` (ROADMAP.md stage 3).
"""

from __future__ import annotations

import csv

import pytest

import adapters.organizations.cms_hospitals.adapter as cms_adapter_module
from adapters.organizations.cms_hospitals.adapter import CMSHospitalsAdapter, _clean
from adapters.organizations.cms_hospitals.fixtures import EXPECTED, ROWS
from providermap.config import Config
from providermap.models import Organization


class _FakeResponse:
    """Stands in for httpx.Response - just enough surface for the two calls
    this adapter's auto-fetch path makes (.raise_for_status(), .json(),
    .text)."""

    def __init__(
        self, json_data: object = None, text_data: str = "", status_code: int = 200
    ) -> None:
        self._json = json_data
        self.text = text_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise cms_adapter_module.httpx.HTTPStatusError(
                "error", request=None, response=self  # type: ignore[arg-type]
            )

    def json(self) -> object:
        return self._json


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


class TestCurrentCsvDownloadUrl:
    """`_current_csv_download_url` - parsing CMS's metastore API response.
    Network access is monkeypatched; no real request is made."""

    def test_extracts_download_url_from_metastore_response(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        def fake_get(url: str, headers: dict, timeout: float):  # type: ignore[no-untyped-def]
            assert url == cms_adapter_module._METASTORE_URL
            return _FakeResponse(
                json_data={"distribution": [{"downloadURL": "https://x/data.csv"}]}
            )

        monkeypatch.setattr(cms_adapter_module.httpx, "get", fake_get)
        result = cms_adapter_module._current_csv_download_url("test-agent", 30.0)
        assert result == "https://x/data.csv"

    def test_raises_a_clear_error_on_unexpected_response_shape(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        def fake_get(url: str, headers: dict, timeout: float):  # type: ignore[no-untyped-def]
            return _FakeResponse(json_data={"unexpected": "shape"})

        monkeypatch.setattr(cms_adapter_module.httpx, "get", fake_get)
        with pytest.raises(RuntimeError, match="xubh-q36u"):
            cms_adapter_module._current_csv_download_url("test-agent", 30.0)


class TestFetchDatasetCsv:
    """`_fetch_dataset_csv` - metastore lookup, then CSV download."""

    def test_fetches_metastore_then_the_csv_it_points_to(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        calls: list[str] = []

        def fake_get(url: str, headers: dict, timeout: float):  # type: ignore[no-untyped-def]
            calls.append(url)
            if url == cms_adapter_module._METASTORE_URL:
                return _FakeResponse(
                    json_data={"distribution": [{"downloadURL": "https://x/data.csv"}]}
                )
            return _FakeResponse(text_data="Facility ID,Facility Name\n1,Test Hospital\n")

        monkeypatch.setattr(cms_adapter_module.httpx, "get", fake_get)
        text = cms_adapter_module._fetch_dataset_csv("test-agent", 30.0)
        assert "Test Hospital" in text
        assert calls == [cms_adapter_module._METASTORE_URL, "https://x/data.csv"]


class TestDiscoverAutoFetch:
    """`discover()` auto-fetches when no local path is configured -
    ROADMAP.md follow-on: no manual CSV upload required."""

    def test_auto_fetches_when_no_path_configured(
        self, cms_adapter: CMSHospitalsAdapter, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        cms_adapter.config.organizations.cms_hospitals_csv_path = None

        def fake_get(url: str, headers: dict, timeout: float):  # type: ignore[no-untyped-def]
            if url == cms_adapter_module._METASTORE_URL:
                return _FakeResponse(
                    json_data={"distribution": [{"downloadURL": "https://x/data.csv"}]}
                )
            return _FakeResponse(text_data="Facility ID,Facility Name\n1,Test Hospital\n")

        monkeypatch.setattr(cms_adapter_module.httpx, "get", fake_get)
        rows = list(cms_adapter.discover())
        assert rows == [{"Facility ID": "1", "Facility Name": "Test Hospital"}]

    def test_explicit_path_takes_precedence_over_auto_fetch(
        self, cms_adapter: CMSHospitalsAdapter, monkeypatch, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        csv_path = tmp_path / "pinned.csv"
        csv_path.write_text("Facility ID,Facility Name\n2,Pinned Hospital\n", encoding="utf-8")
        cms_adapter.config.organizations.cms_hospitals_csv_path = str(csv_path)

        def fake_get(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("should not fetch the network when a path is configured")

        monkeypatch.setattr(cms_adapter_module.httpx, "get", fake_get)
        rows = list(cms_adapter.discover())
        assert rows == [{"Facility ID": "2", "Facility Name": "Pinned Hospital"}]
