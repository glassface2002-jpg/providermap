"""Smoke tests for dashboard/app.py, run headlessly with Streamlit's AppTest.

The snapshots are produced by ``providermap.map_data`` itself, so if the app
starts using a column the builder does not write (or the builder drops one the
app needs), these tests fail instead of the live dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from providermap.map_data import (
    HOSPITAL_COLUMNS,
    NOT_ASSESSED,
    NOT_STATED,
    ROW_COLUMNS,
    Snapshot,
    aggregate_locations,
    summarize,
    write_hospital_snapshot,
    write_snapshot,
)

REPO = Path(__file__).resolve().parent.parent
APP = str(REPO / "dashboard" / "app.py")


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "dataset": "AdventHealth",
        "specialty": "Family Medicine",
        "provider_type": "Physician",
        "primary_site": "AdventHealth Family Medicine at Winter Park",
        "address": "133 Benmore Drive Suite 200",
        "city": "Winter Park",
        "state": "FL",
        "zip": "32792",
        "site_confidence": "high",
        "provider_type_source": "Source",
        "lat": 28.5958,
        "lon": -81.3271,
        "geocode_precision": "address",
    }
    return base | overrides


def _provider_rows() -> list[dict[str, object]]:
    return [
        *[_row() for _ in range(6)],  # one busy, named site
        _row(
            provider_type="Advanced Practice Provider",
            specialty="Cardiology",
            primary_site="A Private Practice",
            lat=28.7,
            lon=-81.4,
        ),
        _row(
            dataset="Rhode Island",
            specialty="Counselor",
            provider_type="Other clinician",
            provider_type_source="Inferred from specialty",
            site_confidence=NOT_ASSESSED,
            city="Providence",
            state="RI",
            lat=41.8541,
            lon=-71.4378,
            geocode_precision="zip",
        ),
        _row(lat=None, lon=None, geocode_precision="none"),  # counted, but not on the map
    ]


def _snapshot(rows: list[dict[str, object]], states_done: dict[str, list[str]]) -> Snapshot:
    return summarize(pd.DataFrame(rows, columns=ROW_COLUMNS), states_done)


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    write_snapshot(
        _snapshot(_provider_rows(), {"AdventHealth": ["FL"], "Rhode Island": ["RI"]}), tmp_path
    )
    monkeypatch.setenv("PROVIDERMAP_DATA_DIR", str(tmp_path))
    return AppTest.from_file(APP, default_timeout=60).run()


def _hospital(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict.fromkeys(HOSPITAL_COLUMNS)
    base.update(
        kind="trauma",
        name="Rhode Island Hospital",
        address="593 Eddy St",
        city="Providence",
        state="RI",
        zip="02903",
        level="I",
        acs_verified="Yes",
        lat=41.81,
        lon=-71.41,
        geocode_precision="address",
    )
    base.update(overrides)
    return base


def _hospital_rows() -> list[dict[str, object]]:
    return [
        _hospital(),
        _hospital(
            name="Phoenix Children's",
            city="Phoenix",
            state="AZ",
            zip="85016",
            address=None,
            level=NOT_STATED,
            acs_verified="No",
            lat=33.47,
            lon=-112.06,
            geocode_precision="zip",
        ),
        _hospital(name="Not Placed", state="AZ", lat=None, lon=None, geocode_precision="none"),
        _hospital(
            kind="nicu",
            name="Banner Desert",
            city="Mesa",
            state="AZ",
            zip="85202",
            address="1400 S Dobson Rd",
            level="III",
            acs_verified=None,
            nicu_beds=40,
            lat=33.39,
            lon=-111.88,
        ),
    ]


@pytest.fixture()
def app_with_hospitals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    write_snapshot(
        _snapshot(_provider_rows(), {"AdventHealth": ["FL"], "Rhode Island": ["RI"]}), tmp_path
    )
    frame = pd.DataFrame(_hospital_rows(), columns=HOSPITAL_COLUMNS)
    frame["nicu_beds"] = frame["nicu_beds"].astype("Int64")
    write_hospital_snapshot(frame, tmp_path)
    monkeypatch.setenv("PROVIDERMAP_DATA_DIR", str(tmp_path))
    return AppTest.from_file(APP, default_timeout=60).run()


def _big_snapshot(points: int) -> Snapshot:
    """Arizona with ``points`` one-provider points, plus one Rhode Island point."""
    rng = np.random.default_rng(0)
    az = pd.DataFrame([_row(dataset="Cigna", state="AZ", city="Phoenix")] * points)
    az["lat"] = 33 + rng.random(points)
    az["lon"] = -112 + rng.random(points)
    ri = pd.DataFrame([_row(dataset="Cigna", state="RI", city="Providence", lat=41.8, lon=-71.4)])
    rows = pd.concat([az, ri], ignore_index=True)[ROW_COLUMNS]
    return summarize(rows, {"Cigna": ["AZ", "RI"]})


def _layers(at: AppTest) -> list[dict]:
    return json.loads(at.get("deck_gl_json_chart")[0].proto.json)["layers"]


def _pick(at: AppTest, label: str, *values: str, sidebar: bool = False) -> AppTest:
    widgets = at.sidebar.multiselect if sidebar else at.multiselect
    control = next(m for m in widgets if m.label == label)
    for value in values:
        control.select(value)
    return at.run()


def _metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def test_renders_without_errors_and_counts_every_provider(app: AppTest):
    assert not app.exception
    metrics = _metrics(app)
    assert metrics["Providers"] == "8"  # includes the unmappable row (the overview tab has its own)
    assert metrics["Mapped locations"] == "2"
    assert metrics["High-confidence sites"] == "100%"  # only assessed providers count
    assert metrics["Specialties"] == "2"


def test_dataset_choice_narrows_everything(app: AppTest):
    app.sidebar.selectbox[0].select("Rhode Island").run()
    assert not app.exception
    metrics = _metrics(app)
    assert metrics["Providers"] == "1"
    assert metrics["High-confidence sites"] == "n/a"  # nothing assessed in that dataset


def test_the_default_dataset_is_the_one_with_the_most_providers(app: AppTest):
    assert app.sidebar.selectbox[0].value == "AdventHealth"  # 9 providers vs Rhode Island's 1


def test_a_state_with_no_providers_in_the_dataset_does_not_crash(app_with_hospitals: AppTest):
    at = _pick(app_with_hospitals, "State", "AZ", sidebar=True)  # AZ only has hospitals
    assert not at.exception
    assert _metrics(at)["Providers"] == "0"
    assert _metrics(at)["High-confidence sites"] == "n/a"


def test_no_find_a_provider_tab_and_no_row_level_inputs(app: AppTest):
    assert [t.label for t in app.tabs] == ["National overview", "Map", "Insights", "Data quality"]
    assert not app.text_input
    labels = [m.label for m in app.sidebar.multiselect]
    assert labels == ["State"]  # no specialty or accepting-new-patients filters any more


def test_overview_reports_how_much_of_the_country_is_covered(app: AppTest):
    metrics = _metrics(app)
    assert metrics["States covered"] == "1 of 51"
    assert metrics["As of"] == pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    assert any("not every provider in a state" in c.value for c in app.caption)


@pytest.fixture(scope="module")
def dashboard() -> ModuleType:
    """The app module itself; importing it does not run the app (``main`` is guarded)."""
    spec = importlib.util.spec_from_file_location("dashboard_app", APP)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestOverviewFigure:
    def test_finished_states_are_coloured_and_the_rest_are_grey(self, dashboard: ModuleType):
        counts = pd.DataFrame({"state": ["AZ", "RI"], "providers": [34_000, 8_000]})
        fig = dashboard.overview_figure(counts, ["AZ", "RI"])
        done, pending = fig.data
        assert sorted(done.locations) == ["AZ", "RI"]
        assert sorted(done.customdata) == [8_000, 34_000]  # hover shows real counts, not logs
        assert set(pending.locations) == set(dashboard.ALL_STATES) - {"AZ", "RI"}
        assert len(pending.locations) == 49
        assert {c[1] for c in pending.colorscale} == {dashboard.PENDING_COLOR}

    def test_a_finished_state_with_no_providers_is_not_grey(self, dashboard: ModuleType):
        counts = pd.DataFrame({"state": ["AZ"], "providers": [5]})
        fig = dashboard.overview_figure(counts, ["AZ", "WY"])
        assert "WY" not in fig.data[1].locations  # finished (just empty), so not "pending"

    def test_codes_that_are_not_states_are_left_off_the_map(self, dashboard: ModuleType):
        counts = pd.DataFrame({"state": ["AZ", "Unknown"], "providers": [5, 3]})
        assert list(dashboard.overview_figure(counts, ["AZ"]).data[0].locations) == ["AZ"]


class TestDatasetOrder:
    def test_most_providers_first(self, dashboard: ModuleType):
        info = {
            "datasets": {
                "small": {"providers": 5, "states_done": ["AZ", "RI", "FL"]},
                "big": {"providers": 50, "states_done": ["AZ"]},
                "mid": {"providers": 9, "states_done": ["AZ"]},
            }
        }
        assert dashboard.dataset_order(info) == ["big", "mid", "small"]


class TestPreparePoints:
    @pytest.fixture()
    def locations(self) -> pd.DataFrame:
        rows = pd.DataFrame(_provider_rows(), columns=ROW_COLUMNS)
        return aggregate_locations(rows[rows["dataset"] == "AdventHealth"])

    def test_busiest_first_with_ids_sizes_and_colours(self, dashboard: ModuleType, locations):
        points, total = dashboard.prepare_points(locations, "Provider type")
        assert total == 2
        assert points["providers"].tolist() == [6, 1]
        assert points["location_id"].tolist() == [0, 1]
        big, small = points["radius_m"]
        assert (big / small) ** 2 == pytest.approx(6)  # dot area is proportional to providers
        assert all(len(c) == 4 for c in points["color"])
        physician = dashboard.hex_to_rgba(dashboard.COLORS["Physician"])
        assert points["color"].iloc[0] == physician

    def test_colour_by_confidence_changes_the_colour(self, dashboard: ModuleType, locations):
        points, _ = dashboard.prepare_points(locations, "Site confidence")
        assert points["color"].iloc[0] == dashboard.hex_to_rgba(dashboard.COLORS["high"])

    def test_only_big_sites_are_named_in_the_hover_text(self, dashboard: ModuleType, locations):
        points, _ = dashboard.prepare_points(locations, "Provider type")
        big, small = points["title"]
        assert big == "AdventHealth Family Medicine at Winter Park"
        assert small == "Small practice site"  # not the private practice's name
        assert points["place"].iloc[0] == "133 Benmore Drive Suite 200, Winter Park"
        assert points["place"].iloc[1] == "Winter Park"  # no street for a small point
        assert "A Private Practice" not in points.to_json()

    def test_approximate_points_say_so(self, dashboard: ModuleType):
        rows = pd.DataFrame(_provider_rows(), columns=ROW_COLUMNS)
        points, _ = dashboard.prepare_points(
            aggregate_locations(rows[rows["dataset"] == "Rhode Island"]), "Provider type"
        )
        assert points["title"].iloc[0] == "Approximate location (centre of the ZIP code)"

    def test_hover_text_gives_the_split_and_specialties(self, dashboard: ModuleType, locations):
        points, _ = dashboard.prepare_points(locations, "Provider type")
        assert points["mix"].iloc[0] == "Physician 6"
        assert points["detail"].iloc[0] == "6 providers - Family Medicine (6)"
        assert points["detail"].iloc[1] == "1 provider - Cardiology (1)"
        assert points["mix"].iloc[1] == "Advanced practice 1"

    def test_a_huge_dataset_keeps_only_the_busiest_points(self, dashboard: ModuleType):
        locations = _big_snapshot(dashboard.MAX_MAP_POINTS + 10).locations
        locations.loc[locations.index[0], "providers"] = 99  # one clearly busiest point
        points, total = dashboard.prepare_points(locations, "Provider type")
        assert total == dashboard.MAX_MAP_POINTS + 11
        assert len(points) == dashboard.MAX_MAP_POINTS
        assert points["providers"].iloc[0] == 99


class TestBigDatasets:
    @pytest.fixture()
    def big(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dashboard: ModuleType):
        write_snapshot(_big_snapshot(dashboard.MAX_MAP_POINTS), tmp_path)  # + 1 RI point: too many
        monkeypatch.setenv("PROVIDERMAP_DATA_DIR", str(tmp_path))
        return AppTest.from_file(APP, default_timeout=120).run()

    def test_starts_on_the_biggest_state_so_the_map_is_not_empty(self, big: AppTest):
        assert not big.exception
        assert big.sidebar.multiselect[0].value == ["AZ"]
        (layer,) = _layers(big)
        assert len(layer["data"]) > 20_000

    def test_clearing_the_state_asks_for_one_instead_of_drawing_everything(self, big: AppTest):
        big.sidebar.multiselect[0].set_value([]).run()
        assert not big.exception
        assert not big.get("deck_gl_json_chart")
        assert any("too many to draw" in i.value for i in big.info)


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
    (layer,) = _layers(app)
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


def test_map_layer_carries_counts_only(app: AppTest):
    """Nothing that identifies a person is ever put into the page."""
    (layer,) = _layers(app)
    assert set(layer["data"][0]) == {
        "lat",
        "lon",
        "location_id",
        "radius_m",
        "color",
        "title",
        "place",
        "detail",
        "mix",
    }


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


def test_a_rebuilt_snapshot_is_not_hidden_by_the_cache(app: AppTest, tmp_path: Path):
    """A running server must pick up new data even though the app code did not change."""
    assert _metrics(app)["Providers"] == "8"

    write_snapshot(_snapshot([_row()], {"AdventHealth": ["FL"]}), tmp_path)
    bumped = (tmp_path / "by_state.csv").stat().st_mtime_ns + 5_000_000_000
    os.utime(tmp_path / "by_state.csv", ns=(bumped, bumped))

    refreshed = AppTest.from_file(APP, default_timeout=60).run()
    assert not refreshed.exception
    assert _metrics(refreshed)["Providers"] == "1"


def test_no_hospital_control_without_a_hospital_snapshot(app: AppTest):
    assert "Hospitals" not in [m.label for m in app.multiselect]


class TestHospitalLayers:
    def test_control_is_offered_and_starts_empty(self, app_with_hospitals: AppTest):
        assert not app_with_hospitals.exception
        control = next(m for m in app_with_hospitals.multiselect if m.label == "Hospitals")
        assert control.options == ["Trauma centers", "NICUs"]
        assert [layer["id"] for layer in _layers(app_with_hospitals)] == ["locations"]

    def test_rings_sit_beneath_the_provider_dots(self, app_with_hospitals: AppTest):
        at = _pick(app_with_hospitals, "Hospitals", "Trauma centers", "NICUs")
        assert not at.exception
        assert [layer["id"] for layer in _layers(at)] == [
            "trauma-centers",
            "nicu-centers",
            "locations",  # last = drawn on top, so provider dots stay clickable
        ]

    def test_trauma_rings_default_to_acs_verified_only(self, app_with_hospitals: AppTest):
        at = _pick(app_with_hospitals, "Hospitals", "Trauma centers")
        trauma = next(layer for layer in _layers(at) if layer["id"] == "trauma-centers")
        assert [row["title"] for row in trauma["data"]] == ["Rhode Island Hospital"]
        assert next(c for c in at.checkbox if "ACS-verified" in c.label).value is True

    def test_unticking_shows_every_placed_trauma_center(self, app_with_hospitals: AppTest):
        at = _pick(app_with_hospitals, "Hospitals", "Trauma centers")
        next(c for c in at.checkbox if "ACS-verified" in c.label).uncheck().run()
        trauma = next(layer for layer in _layers(at) if layer["id"] == "trauma-centers")
        assert sorted(row["title"] for row in trauma["data"]) == [
            "Phoenix Children's",
            "Rhode Island Hospital",
        ]

    def test_verified_checkbox_only_appears_with_the_trauma_layer(self, app_with_hospitals):
        assert not [c for c in app_with_hospitals.checkbox if "ACS-verified" in c.label]
        at = _pick(app_with_hospitals, "Hospitals", "NICUs")
        assert not [c for c in at.checkbox if "ACS-verified" in c.label]

    def test_ring_style_and_data(self, app_with_hospitals: AppTest):
        at = _pick(app_with_hospitals, "Hospitals", "Trauma centers")
        next(c for c in at.checkbox if "ACS-verified" in c.label).uncheck().run()
        trauma = next(layer for layer in _layers(at) if layer["id"] == "trauma-centers")
        assert trauma["stroked"] is True and trauma["pickable"] is True
        assert trauma["radiusMinPixels"] < trauma["radiusMaxPixels"]
        assert trauma["lineWidthMinPixels"] == 3
        names = sorted(row["title"] for row in trauma["data"])
        assert names == ["Phoenix Children's", "Rhode Island Hospital"]  # unplaced one is dropped
        assert all(len(row["color"]) == 4 and len(row["fill"]) == 4 for row in trauma["data"])

    def test_rings_follow_the_state_filter(self, app_with_hospitals: AppTest):
        at = _pick(app_with_hospitals, "Hospitals", "Trauma centers")
        at = _pick(at, "State", "RI", sidebar=True)
        trauma = next(layer for layer in _layers(at) if layer["id"] == "trauma-centers")
        assert [row["title"] for row in trauma["data"]] == ["Rhode Island Hospital"]

    def test_a_state_with_hospitals_but_no_providers_still_draws_a_map(
        self, app_with_hospitals: AppTest
    ):
        """Arizona before any Arizona provider run: hospitals alone should still show."""
        at = _pick(app_with_hospitals, "Hospitals", "Trauma centers", "NICUs")
        next(c for c in at.checkbox if "ACS-verified" in c.label).uncheck()
        at = _pick(at, "State", "AZ", sidebar=True)
        assert not at.exception
        assert _metrics(at)["Providers"] == "0"
        assert [layer["id"] for layer in _layers(at)] == ["trauma-centers", "nicu-centers"]

    def test_state_filter_also_offers_states_that_only_have_hospitals(
        self, app_with_hospitals: AppTest
    ):
        states = next(m for m in app_with_hospitals.sidebar.multiselect if m.label == "State")
        assert "AZ" in states.options  # no AZ providers in the fixture, only AZ hospitals

    def test_hover_text_uses_the_shared_tooltip_fields(self, app_with_hospitals: AppTest):
        at = _pick(app_with_hospitals, "Hospitals", "NICUs")
        deck = json.loads(at.get("deck_gl_json_chart")[0].proto.json)
        nicu = next(layer for layer in deck["layers"] if layer["id"] == "nicu-centers")
        (row,) = nicu["data"]
        assert row["detail"] == "NICU level III - 40 beds"
        assert row["place"] == "1400 S Dobson Rd, Mesa, AZ 85202"

    def test_data_quality_tab_describes_the_hospital_layers(self, app_with_hospitals: AppTest):
        assert "Hospital layers" in [s.value for s in app_with_hospitals.subheader]


class TestHospitalPoints:
    @pytest.fixture()
    def hospitals(self) -> pd.DataFrame:
        frame = pd.DataFrame(_hospital_rows(), columns=HOSPITAL_COLUMNS)
        frame["nicu_beds"] = frame["nicu_beds"].astype("Int64")
        return frame

    def test_only_placed_hospitals_of_the_kind(self, dashboard: ModuleType, hospitals):
        points = dashboard.hospital_points(hospitals, "trauma", [])
        assert sorted(points["title"]) == ["Phoenix Children's", "Rhode Island Hospital"]

    def test_verified_only_applies_to_trauma_but_not_nicu(self, dashboard: ModuleType, hospitals):
        trauma = dashboard.hospital_points(hospitals, "trauma", [], verified_only=True)
        assert trauma["title"].tolist() == ["Rhode Island Hospital"]
        nicu = dashboard.hospital_points(hospitals, "nicu", [], verified_only=True)
        assert nicu["title"].tolist() == ["Banner Desert"]  # NICUs carry no verification flag

    def test_state_filter(self, dashboard: ModuleType, hospitals):
        assert dashboard.hospital_points(hospitals, "trauma", ["AZ"])["title"].tolist() == [
            "Phoenix Children's"
        ]
        assert dashboard.hospital_points(hospitals, "trauma", ["TX"]).empty

    def test_detail_text(self, dashboard: ModuleType, hospitals):
        by_site = dashboard.hospital_points(hospitals, "trauma", []).set_index("title")["detail"]
        assert by_site["Rhode Island Hospital"] == "Trauma center, level I - ACS verified"
        assert by_site["Phoenix Children's"] == "Trauma center, level not stated"

    def test_ring_colours_come_from_the_level(self, dashboard: ModuleType, hospitals):
        points = dashboard.hospital_points(hospitals, "trauma", []).set_index("title")
        palette = dashboard.HOSPITAL_LAYERS["Trauma centers"]["palette"]
        expected = dashboard.hex_to_rgba(palette["I"], 255)
        assert points.loc["Rhode Island Hospital", "color"] == expected
        assert points.loc["Rhode Island Hospital", "fill"][3] < 255  # tinted, not solid

    def test_address_line_skips_missing_parts(self, dashboard: ModuleType):
        row = pd.Series({"address": None, "city": "Phoenix", "state": "AZ", "zip": None})
        assert dashboard.address_line(row) == "Phoenix, AZ"
        row = pd.Series({"address": "1 Main St", "city": "Phoenix", "state": "AZ", "zip": "85016"})
        assert dashboard.address_line(row) == "1 Main St, Phoenix, AZ 85016"


def test_hospital_palettes_are_ordered_by_level_and_convert(dashboard: ModuleType):
    for layer in dashboard.HOSPITAL_LAYERS.values():
        for colour in layer["palette"].values():
            assert len(dashboard.hex_to_rgba(colour)) == 4
    assert list(dashboard.HOSPITAL_LAYERS["Trauma centers"]["palette"])[:2] == ["I", "II"]


def test_snapshot_labels_and_columns_match_the_snapshot_builder(dashboard: ModuleType):
    """The app cannot import the pipeline, so its copies of these names are pinned here."""
    from providermap import map_data

    assert dashboard.TYPE_COLUMNS == map_data.TYPE_COLUMNS
    assert dashboard.CONFIDENCE_COLUMNS == map_data.CONFIDENCE_COLUMNS
    assert dashboard.PRECISION_COLUMNS == map_data.PRECISION_COLUMNS
    assert dashboard.LOCATIONS_FILE == map_data.LOCATIONS_NAME
    assert dashboard.BY_STATE_FILE == map_data.BY_STATE_NAME
    assert dashboard.BY_SPECIALTY_FILE == map_data.BY_SPECIALTY_NAME
    assert dashboard.INFO_FILE == map_data.INFO_NAME
    assert dashboard.MAP_STYLE  # used by the density view
    for provider_type in (map_data.TYPE_PHYSICIAN, map_data.TYPE_APP, map_data.TYPE_OTHER):
        assert provider_type in dashboard.COLORS
        assert dashboard.color_for(provider_type) != dashboard.DEFAULT_COLOR


def test_every_column_the_app_reads_is_written_by_the_builder(dashboard: ModuleType):
    snap = _snapshot(_provider_rows(), {"AdventHealth": ["FL"], "Rhode Island": ["RI"]})
    assert set(dashboard.LOCATION_COLUMNS) <= set(snap.locations.columns)
    needed = {"n_type_inferred", "map_points", "providers", *dashboard.PRECISION_COLUMNS.values()}
    assert needed <= set(snap.by_state.columns)
    assert {"dataset", "state", "specialty", "providers"} == set(snap.by_specialty.columns)


def test_map_legend_lists_the_inferred_type_with_its_own_colour(app: AppTest):
    app.sidebar.selectbox[0].select("Rhode Island").run()
    legend = next(m.value for m in app.markdown if "&#9679;" in m.value)
    assert "Other clinician" in legend


def test_data_quality_tab_says_counts_only_and_explains_provider_type(app: AppTest):
    text = " ".join(m.value for m in app.markdown) + " ".join(i.value for i in app.info)
    assert "counts only" in text
    assert "five or more providers" in text
    assert "does not prove a credential" in text
    assert {"Provider type", "Names on the map"} <= {s.value for s in app.subheader}


def test_the_committed_snapshot_holds_no_row_level_data():
    """Only counts are published: no names, NPIs, profile links or phone numbers, and no database."""
    data = REPO / "dashboard" / "data"
    allowed = {
        "locations.parquet",
        "by_state.csv",
        "by_specialty.csv",
        "build_info.json",
        "hospitals.parquet",
    }
    assert {p.name for p in data.iterdir()} <= allowed
    for name in ("locations.parquet",):
        columns = set(pd.read_parquet(data / name).columns)
        assert not columns & {"name", "full_name", "npi", "profile_url", "phone", "credentials"}
    assert not (data / "providers.parquet").exists()


def test_no_database_or_export_file_is_tracked_by_git():
    """Collected data stays out of the repository: no database, and no providers.xlsx export."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.split("\n")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    forbidden = (".db", ".sqlite", ".sqlite3", ".xlsx")
    assert not [f for f in tracked if f.lower().endswith(forbidden) or ".db-" in f or ".db." in f]
