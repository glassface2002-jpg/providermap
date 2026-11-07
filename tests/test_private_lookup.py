"""Tests for dashboard/private_lookup.py - the private, per-click provider list (offline)."""

from __future__ import annotations

import io
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import private_lookup as pl  # noqa: E402

from providermap.map_data import PRIVATE_COLUMNS  # noqa: E402

TOKEN = "github_pat_SECRET123"


def _provider(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "dataset": "Cigna",
        "state": "NM",
        "lat": 35.08,
        "lon": -106.65,
        "name": "Ana Lopez",
        "credentials": "NP",
        "specialty": "Family Medicine",
        "provider_type": "Physician",
        "provider_type_source": "Source",
        "npi": "1111111112",
        "primary_site": "Presbyterian Hospital",
        "address": "1100 Central Ave SE",
        "city": "Albuquerque",
        "zip": "87106",
    }
    return base | overrides


def _table(rows: list[dict[str, object]]) -> pa.Table:
    return pa.Table.from_pandas(
        pd.DataFrame(rows, columns=pl.REQUIRED_COLUMNS), preserve_index=False
    )


def _points(*pairs: tuple[str, float, float]) -> pd.DataFrame:
    return pd.DataFrame(pairs, columns=["state", "lat", "lon"])


def test_required_columns_match_what_the_builder_writes():
    assert pl.REQUIRED_COLUMNS == PRIVATE_COLUMNS


class TestSourceFromSecrets:
    def test_absent_means_the_feature_is_off(self):
        assert pl.source_from_secrets({}) is None
        assert pl.source_from_secrets({"private_data": {}}) is None

    def test_a_complete_section(self):
        secrets = {"private_data": {"repo": "me/data", "path": "p.parquet", "token": TOKEN}}
        assert pl.source_from_secrets(secrets) == pl.Source("me/data", "p.parquet", TOKEN, "main")

    def test_ref_is_optional_and_honoured(self):
        secrets = {
            "private_data": {"repo": "me/data", "path": "p", "token": TOKEN, "ref": "release"}
        }
        source = pl.source_from_secrets(secrets)
        assert source is not None and source.ref == "release"

    @pytest.mark.parametrize("missing", ["repo", "path", "token"])
    def test_a_missing_field_is_an_error(self, missing: str):
        section = {"repo": "me/data", "path": "p", "token": TOKEN}
        del section[missing]
        with pytest.raises(pl.PrivateDataError, match="needs repo, path and token"):
            pl.source_from_secrets({"private_data": section})

    @pytest.mark.parametrize("repo", ["justaname", "a/b/c", "https://github.com/a/b", "a b/c"])
    def test_a_malformed_repo_is_refused(self, repo: str):
        secrets = {"private_data": {"repo": repo, "path": "p", "token": TOKEN}}
        with pytest.raises(pl.PrivateDataError, match="owner/name"):
            pl.source_from_secrets(secrets)

    def test_a_path_that_escapes_the_repository_is_refused(self):
        secrets = {"private_data": {"repo": "me/data", "path": "../x", "token": TOKEN}}
        with pytest.raises(pl.PrivateDataError, match="inside the repository"):
            pl.source_from_secrets(secrets)


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class TestDownload:
    SOURCE = pl.Source("me/data", "dir/providers private.parquet", TOKEN, "main")

    def test_asks_the_github_contents_api_for_the_raw_file(self, monkeypatch):
        seen: list[urllib.request.Request] = []

        def fake_urlopen(request, timeout):
            seen.append(request)
            assert timeout == pl.TIMEOUT_SECONDS
            return _Response(b"abc")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert pl.download(self.SOURCE) == b"abc"
        (request,) = seen
        assert request.full_url == (
            "https://api.github.com/repos/me/data/contents/dir/providers%20private.parquet"
            "?ref=main"
        )
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        assert request.get_header("Accept") == "application/vnd.github.raw+json"

    @pytest.mark.parametrize(
        ("code", "hint"), [(401, "rejected"), (403, "lack access"), (404, "not found")]
    )
    def test_http_errors_become_safe_messages(self, monkeypatch, code: int, hint: str):
        def fail(request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "no", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(urllib.request, "urlopen", fail)
        with pytest.raises(pl.PrivateDataError, match=hint) as err:
            pl.download(self.SOURCE)
        assert TOKEN not in str(err.value)

    def test_network_failure_is_a_safe_message(self, monkeypatch):
        def fail(request, timeout):
            raise urllib.error.URLError("dns")

        monkeypatch.setattr(urllib.request, "urlopen", fail)
        with pytest.raises(pl.PrivateDataError, match="Could not reach") as err:
            pl.download(self.SOURCE)
        assert TOKEN not in str(err.value)


class TestLoadTable:
    def _bytes(self, table: pa.Table) -> bytes:
        buffer = io.BytesIO()
        pq.write_table(table, buffer)
        return buffer.getvalue()

    def test_round_trip(self):
        table = pl.load_table(self._bytes(_table([_provider()])))
        assert table.column_names == pl.REQUIRED_COLUMNS
        assert table.num_rows == 1

    def test_a_file_missing_columns_is_refused(self):
        thin = pa.table({"dataset": ["x"], "state": ["NM"]})
        with pytest.raises(pl.PrivateDataError, match="lacks columns"):
            pl.load_table(self._bytes(thin))

    def test_garbage_is_refused(self):
        with pytest.raises(pl.PrivateDataError, match="not a readable Parquet"):
            pl.load_table(b"this is not parquet")


class TestProvidersAt:
    @pytest.fixture()
    def table(self) -> pa.Table:
        return _table(
            [
                _provider(name="Zed Last", npi="1"),
                _provider(name="Ana Lopez", npi="2", specialty="Cardiology"),
                _provider(name="Other Place", npi="3", lat=35.5),
                _provider(name="Other State", npi="4", state="AZ"),
                _provider(name="Other Dataset", npi="5", dataset="AdventHealth"),
            ]
        )

    def test_finds_exactly_the_providers_at_the_clicked_point_sorted_by_name(self, table):
        found, total = pl.providers_at(table, "Cigna", _points(("NM", 35.08, -106.65)))
        assert found["name"].tolist() == ["Ana Lopez", "Zed Last"]
        assert total == 2

    def test_the_match_tolerates_float_noise(self, table):
        found, _ = pl.providers_at(table, "Cigna", _points(("NM", 35.0800004, -106.6500004)))
        assert len(found) == 2

    def test_several_points_are_combined(self, table):
        points = _points(("NM", 35.08, -106.65), ("NM", 35.5, -106.65))
        found, total = pl.providers_at(table, "Cigna", points)
        assert total == 3
        assert found["name"].tolist() == sorted(found["name"])

    def test_no_points_no_providers(self, table):
        found, total = pl.providers_at(table, "Cigna", _points())
        assert found.empty and total == 0

    def test_text_narrows_by_name_specialty_or_npi_ignoring_case(self, table):
        point = _points(("NM", 35.08, -106.65))
        assert pl.providers_at(table, "Cigna", point, "LOPEZ")[0]["name"].tolist() == ["Ana Lopez"]
        assert pl.providers_at(table, "Cigna", point, "cardio")[1] == 1
        assert pl.providers_at(table, "Cigna", point, "1")[0]["npi"].tolist() == ["1"]
        assert pl.providers_at(table, "Cigna", point, "nobody")[1] == 0

    def test_text_is_not_a_regular_expression(self, table):
        assert pl.providers_at(table, "Cigna", _points(("NM", 35.08, -106.65)), ".*")[1] == 0

    def test_rows_are_capped_but_the_total_is_reported(self):
        many = _table([_provider(name=f"P{n:04d}", npi=str(n)) for n in range(pl.MAX_ROWS + 50)])
        found, total = pl.providers_at(many, "Cigna", _points(("NM", 35.08, -106.65)))
        assert len(found) == pl.MAX_ROWS
        assert total == pl.MAX_ROWS + 50
        assert found["name"].iloc[0] == "P0000"


class TestDisplayFrame:
    def test_shows_only_the_public_columns_and_marks_inferred_types(self):
        providers = pd.DataFrame(
            [
                _provider(provider_type="Physician", provider_type_source="Source"),
                _provider(provider_type="Other clinician", provider_type_source=pl.TYPE_INFERRED),
            ]
        )
        shown = pl.display_frame(providers)
        assert list(shown.columns) == ["name", "specialty", "provider type", "NPI", "practice site"]
        assert shown["provider type"].tolist() == ["Physician", "Other clinician (inferred)"]
