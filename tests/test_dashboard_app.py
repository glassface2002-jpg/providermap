"""Smoke tests for dashboard/app.py, run headlessly with Streamlit's AppTest.

The snapshot is built from ``SNAPSHOT_COLUMNS`` itself, so if the app starts
using a column the builder does not produce (or the builder drops one the app
needs), these tests fail instead of the live dashboard.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from providermap.map_data import NOT_ASSESSED, SNAPSHOT_COLUMNS, UNKNOWN, write_snapshot

APP = str(Path(__file__).resolve().parent.parent / "dashboard" / "app.py")


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict.fromkeys(SNAPSHOT_COLUMNS)
    base.update(
        dataset="AdventHealth",
        name="Justin Menezes, MD",
        credentials="MD",
        specialty="Family Medicine",
        provider_type="Physician",
        primary_site="AdventHealth Family Medicine at Winter Park",
        address="133 Benmore Drive Suite 200",
        city="Winter Park",
        state="FL",
        zip="32792",
        npi="1194013169",
        profile_url="https://example.org/menezes",
        site_confidence="high",
        accepting_new_patients=UNKNOWN,
        lat=28.5958,
        lon=-81.3271,
        county_fips="12095",
        geocode_precision="address",
    )
    base.update(overrides)
    return base


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    rows = [
        _row(),
        _row(name="Ana Lopez, NP", provider_type="Advanced Practice Provider", npi="1111111112"),
        _row(
            dataset="Rhode Island",
            name="CATHERINE A'VANT-FOWLER",
            specialty="Counselor",
            provider_type=NOT_ASSESSED,
            site_confidence=NOT_ASSESSED,
            profile_url=None,
            city="PROVIDENCE",
            state="RI",
            npi="1265446264",
            lat=41.8541,
            lon=-71.4378,
            geocode_precision="zip",
        ),
        _row(name="No Address", npi="1234567893", lat=None, lon=None, geocode_precision="none"),
    ]
    write_snapshot(pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS), tmp_path)
    monkeypatch.setenv("PROVIDERMAP_DATA_DIR", str(tmp_path))
    return AppTest.from_file(APP, default_timeout=60).run()


def _metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def test_renders_without_errors_and_counts_every_provider(app: AppTest):
    assert not app.exception
    metrics = _metrics(app)
    assert metrics["Providers"] == "4"
    assert metrics["Mapped locations"] == "2"  # the unmappable row is counted but not mapped
    assert metrics["High-confidence sites"] == "100%"  # only assessed providers count


def test_dataset_filter_narrows_everything(app: AppTest):
    dataset_filter = next(m for m in app.sidebar.multiselect if m.label == "Dataset")
    dataset_filter.select("Rhode Island").run()
    assert not app.exception
    metrics = _metrics(app)
    assert metrics["Providers"] == "1"
    assert metrics["High-confidence sites"] == "n/a"  # nothing assessed in that dataset


def test_filters_matching_nothing_do_not_crash(app: AppTest):
    next(m for m in app.sidebar.multiselect if m.label == "Dataset").select("Rhode Island").run()
    next(m for m in app.sidebar.multiselect if m.label == "State").select("FL").run()
    assert not app.exception
    assert _metrics(app)["Providers"] == "0"


def test_find_a_provider_shows_the_match(app: AppTest):
    app.text_input[0].set_value("menezes").run()
    assert not app.exception
    assert any(s.value == "Justin Menezes, MD" for s in app.subheader)


def test_selection_table_columns_exist_in_the_snapshot():
    """The click-to-list table only runs after a map click, which AppTest cannot simulate."""
    tree = ast.parse(Path(APP).read_text(encoding="utf-8"))
    table_columns = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "TABLE_COLUMNS" for t in node.targets)
    )
    assert set(table_columns) <= set(SNAPSHOT_COLUMNS)


@pytest.fixture(scope="module")
def dashboard() -> ModuleType:
    """The app module itself; importing it does not run the app (``main`` is guarded)."""
    spec = importlib.util.spec_from_file_location("dashboard_app", APP)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAggregateLocations:
    def test_one_row_per_point_with_counts_and_dominant_values(self, dashboard: ModuleType):
        mapped = pd.DataFrame(
            [
                _row(npi="1", specialty="Family Medicine"),
                _row(npi="2", specialty="Family Medicine"),
                _row(npi="3", specialty="Cardiology", primary_site=None),
                _row(npi="4", lat=41.85, lon=-71.43, primary_site=None, specialty=None),
            ]
        )
        out = dashboard.aggregate_locations(mapped).set_index("lat")

        assert len(out) == 2
        orlando = out.loc[28.5958]
        assert orlando["providers"] == 3
        assert orlando["site"] == "AdventHealth Family Medicine at Winter Park"
        assert orlando["specialties"] == "Family Medicine, Cardiology"
        providence = out.loc[41.85]
        assert providence["providers"] == 1
        assert providence["site"] == "Unnamed site"
        assert providence["specialties"] == ""
        assert sorted(out["location_id"]) == [0, 1]
        assert orlando["radius_m"] == pytest.approx(dashboard.RADIUS_M_PER_SQRT_PROVIDER * 3**0.5)

    def test_largest_first_and_dot_area_proportional_to_providers(self, dashboard: ModuleType):
        rows = [_row(npi=str(i), lat=28.0, lon=-81.0) for i in range(4)]  # 4 providers
        rows.append(_row(npi="9", lat=41.0, lon=-71.0))  # 1 provider
        out = dashboard.aggregate_locations(pd.DataFrame(rows))
        assert out["providers"].tolist() == [4, 1]
        big, small = out["radius_m"]
        assert (big / small) ** 2 == pytest.approx(4)  # area ratio equals provider ratio


class TestZoomFor:
    def test_wider_extent_zooms_out(self, dashboard: ModuleType):
        nationwide = pd.DataFrame({"lat": [26.0, 45.0], "lon": [-120.0, -70.0]})
        one_city = pd.DataFrame({"lat": [41.80, 41.90], "lon": [-71.50, -71.40]})
        assert dashboard.zoom_for(nationwide) < dashboard.zoom_for(one_city)

    def test_stays_within_sane_bounds(self, dashboard: ModuleType):
        single_point = pd.DataFrame({"lat": [41.8], "lon": [-71.4]})
        world = pd.DataFrame({"lat": [-60.0, 70.0], "lon": [-170.0, 170.0]})
        assert dashboard.zoom_for(single_point) == 12
        assert dashboard.zoom_for(world) == 3


class TestColors:
    def test_hex_to_rgba(self, dashboard: ModuleType):
        assert dashboard.hex_to_rgba("#1f77b4") == [31, 119, 180, dashboard.DOT_ALPHA]
        assert dashboard.hex_to_rgba("#000000", alpha=10) == [0, 0, 0, 10]

    def test_every_palette_colour_converts(self, dashboard: ModuleType):
        for colour in dashboard.COLORS.values():
            rgba = dashboard.hex_to_rgba(colour)
            assert len(rgba) == 4
            assert all(0 <= channel <= 255 for channel in rgba)

    def test_unknown_label_gets_the_neutral_colour(self, dashboard: ModuleType):
        assert dashboard.color_for("Something new") == dashboard.DEFAULT_COLOR


class TestLegend:
    def test_known_labels_in_palette_order_then_unknown(self, dashboard: ModuleType):
        text = dashboard.legend_markdown(
            ["Not assessed", "Zzz", "Physician", "Advanced Practice Provider"]
        )
        order = [
            text.index(label)
            for label in ("Physician", "Advanced Practice Provider", "Not assessed", "Zzz")
        ]
        assert order == sorted(order)

    def test_labels_are_html_escaped(self, dashboard: ModuleType):
        text = dashboard.legend_markdown(["<script>alert(1)</script>"])
        assert "<script>" not in text
        assert "&lt;script&gt;" in text


class TestSelectedLocationIds:
    def test_reads_ids_from_clicked_dots(self, dashboard: ModuleType):
        event = SimpleNamespace(
            selection=SimpleNamespace(
                objects={"locations": [{"location_id": 4}, {"location_id": 9}]}
            )
        )
        assert dashboard.selected_location_ids(event) == [4, 9]

    @pytest.mark.parametrize(
        "event",
        [None, SimpleNamespace(selection=SimpleNamespace(objects={}))],
    )
    def test_nothing_selected(self, dashboard: ModuleType, event):
        assert dashboard.selected_location_ids(event) == []

    def test_other_layers_are_ignored(self, dashboard: ModuleType):
        event = SimpleNamespace(
            selection=SimpleNamespace(objects={"another-layer": [{"location_id": 1}]})
        )
        assert dashboard.selected_location_ids(event) == []


def test_map_layer_sizes_dots_in_metres_with_pixel_limits(app: AppTest):
    """Dots grow with zoom because the radius is in metres, clamped to a pixel range."""
    deck = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    (layer,) = deck["layers"]
    assert layer["@@type"] == "ScatterplotLayer"
    assert layer["id"] == "locations"
    assert layer["pickable"] is True
    assert layer["radiusMinPixels"] < layer["radiusMaxPixels"]
    # pydeck would turn radius_units into a JS expression; the metres default must be left alone.
    assert "radiusUnits" not in layer

    rows = layer["data"]
    assert len(rows) == 2  # two distinct mapped points; the unmappable row is not drawn
    assert sorted(r["location_id"] for r in rows) == [0, 1]
    assert all(len(r["color"]) == 4 and r["radius_m"] > 0 for r in rows)


def test_deploy_requirements_are_pinned_like_the_dashboard_extra():
    """Streamlit Cloud installs requirements.txt; local installs use the pyproject extra."""
    dashboard_dir = Path(APP).parent
    pyproject = (dashboard_dir.parent / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^dashboard = \[(.*?)\]", pyproject, re.DOTALL | re.MULTILINE)
    assert block
    extra = set(re.findall(r'"([^"]+==[^"]+)"', block.group(1)))
    deployed = {
        line.strip()
        for line in (dashboard_dir / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert deployed
    assert deployed <= extra


def test_a_rebuilt_snapshot_is_not_hidden_by_the_cache(app: AppTest, tmp_path: Path, monkeypatch):
    """A running server must pick up new data even though the app code did not change."""
    assert _metrics(app)["Providers"] == "4"

    write_snapshot(pd.DataFrame([_row()], columns=SNAPSHOT_COLUMNS), tmp_path)
    bumped = (tmp_path / "providers.parquet").stat().st_mtime_ns + 5_000_000_000
    os.utime(tmp_path / "providers.parquet", ns=(bumped, bumped))

    refreshed = AppTest.from_file(APP, default_timeout=60).run()
    assert not refreshed.exception
    assert _metrics(refreshed)["Providers"] == "1"
