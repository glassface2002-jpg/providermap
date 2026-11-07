"""Tests for dashboard/insights.py - the data and charts behind the Insights tab."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import insights  # noqa: E402

from providermap.map_data import TYPE_COLUMNS  # noqa: E402

COLORS = {
    "Physician": "#1f77b4",
    "Advanced Practice Provider": "#17becf",
    "Other clinician": "#8c564b",
    "Not assessed": "#9aa0a6",
}


def test_mix_columns_match_what_the_builder_writes():
    assert insights.MIX_COLUMNS == TYPE_COLUMNS


class TestSiteSizeBands:
    def test_groups_places_and_providers_by_size(self):
        sizes = pd.Series([1, 1, 3, 7, 25, 25, 150])
        bands = insights.site_size_bands(sizes).set_index("band")
        assert bands["sites"].to_dict() == {
            "1 (alone)": 2,
            "2-4": 1,
            "5-19": 1,
            "20-99": 2,
            "100 or more": 1,
        }
        assert bands["providers"].to_dict() == {
            "1 (alone)": 2,
            "2-4": 3,
            "5-19": 7,
            "20-99": 50,
            "100 or more": 150,
        }
        assert bands["share"].sum() == pytest.approx(1.0)

    def test_the_band_edges_belong_to_the_higher_band_start(self):
        bands = insights.site_size_bands(pd.Series([4, 5, 19, 20, 99, 100])).set_index("band")
        assert bands["sites"].to_dict() == {
            "1 (alone)": 0,
            "2-4": 1,
            "5-19": 2,
            "20-99": 2,
            "100 or more": 1,
        }

    def test_no_places_means_zero_shares_not_an_error(self):
        bands = insights.site_size_bands(pd.Series([], dtype="int64"))
        assert bands["share"].tolist() == [0.0] * 5


class TestLargestSites:
    def _locations(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "site": ["Big Hospital", "Big Hospital", "Clinic", None, "Other Hospital"],
                "city": ["Phoenix", "Phoenix", "Mesa", "Phoenix", "Tucson"],
                "state": ["AZ", "AZ", "AZ", "AZ", "AZ"],
                "providers": [30, 20, 7, 99, 40],
            }
        )

    def test_points_of_one_site_in_one_city_count_once_and_unnamed_points_are_left_out(self):
        top = insights.largest_sites(self._locations())
        assert top["site"].tolist() == ["Big Hospital", "Other Hospital", "Clinic"]
        assert top["providers"].tolist() == [50, 40, 7]
        assert top["place"].tolist() == ["Phoenix, AZ", "Tucson, AZ", "Mesa, AZ"]

    def test_limits_to_n_and_trims_long_names(self):
        frame = self._locations().assign(site=lambda d: d["site"].fillna("x") + "!" * 80)
        top = insights.largest_sites(frame, n=2)
        assert len(top) == 2 and top["site"].str.len().max() == 60

    def test_a_missing_city_does_not_leave_a_dangling_comma(self):
        frame = pd.DataFrame({"site": ["S"], "city": [None], "state": ["AZ"], "providers": [9]})
        assert insights.largest_sites(frame)["place"].tolist() == ["AZ"]


class TestProviderMix:
    def _by_state(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "state": ["AZ", "NM", "WY"],
                "providers": [1000, 800, 100],
                "n_physician": [500, 600, 50],
                "n_app": [300, 100, 30],
                "n_other": [150, 100, 20],
                "n_type_unknown": [50, 0, 0],
            }
        )

    def test_shares_sum_to_one_and_states_sort_by_advanced_practice_share(self):
        mix = insights.provider_mix(self._by_state())
        assert mix["state"].tolist() == ["AZ", "NM"]  # WY is below the minimum
        assert mix[list(insights.MIX_COLUMNS)].sum(axis=1).tolist() == pytest.approx([1.0, 1.0])
        assert mix["Advanced Practice Provider"].tolist() == pytest.approx([0.3, 0.125])

    def test_only_the_largest_states_are_kept(self):
        mix = insights.provider_mix(self._by_state(), minimum=1, top=2)
        assert sorted(mix["state"]) == ["AZ", "NM"]

    def test_no_state_big_enough_gives_an_empty_frame(self):
        assert insights.provider_mix(self._by_state(), minimum=10_000).empty


def test_specialty_by_state_sums_over_datasets():
    frame = pd.DataFrame(
        {
            "dataset": ["A", "B", "A"],
            "state": ["AZ", "AZ", "NM"],
            "specialty": ["Cardiology", "Cardiology", "Cardiology"],
            "providers": [5, 7, 3],
        }
    )
    out = insights.specialty_by_state(frame, "Cardiology").set_index("state")["providers"]
    assert out.to_dict() == {"AZ": 12, "NM": 3}
    assert insights.specialty_by_state(frame, "Nothing").empty


class TestHeadlines:
    @pytest.fixture()
    def data(self):
        by_state = pd.DataFrame(
            {"state": ["AZ", "NM"], "providers": [80, 20], "map_points": [10, 5], "n_app": [20, 5]}
        )
        by_specialty = pd.DataFrame(
            {
                "specialty": ["Cardiology", "Counselor", "Unknown"],
                "providers": [60, 30, 10],
            }
        )
        bands = insights.site_size_bands(pd.Series([1] * 10 + [50, 40]))
        sites = pd.DataFrame(
            {"site": ["Big Hospital"], "place": ["Phoenix, AZ"], "providers": [90]}
        )
        return by_state, by_specialty, bands, sites

    def test_each_fact_is_computed_from_the_data(self, data):
        facts = insights.headlines(*data)
        text = "\n".join(facts)
        assert "**100 providers** in 2 states, at 15 mapped places" in text
        assert "**90%** work at a place with 20 or more" in text  # 90 of 100 at 50- and 40-sites
        assert "**10%** are the only one listed" in text
        assert "**Cardiology** (60 providers, 60%)" in text  # 'Unknown' is never the top
        assert "**Big Hospital** (Phoenix, AZ) with 90 providers" in text
        assert "**25%** of providers" in text

    def test_no_providers_no_facts(self, data):
        by_state, by_specialty, bands, sites = data
        assert insights.headlines(by_state.iloc[0:0], by_specialty, bands, sites) == []

    def test_no_named_site_just_omits_that_fact(self, data):
        by_state, by_specialty, bands, sites = data
        facts = insights.headlines(by_state, by_specialty, bands, sites.iloc[0:0])
        assert not any("largest named site" in fact for fact in facts)

    def test_singular_state(self, data):
        by_state, by_specialty, bands, sites = data
        facts = insights.headlines(by_state.iloc[:1], by_specialty, bands, sites)
        assert "in 1 state," in facts[0]


class TestFigures:
    def test_ranked_bars_put_the_largest_on_top(self):
        frame = pd.DataFrame({"name": ["a", "b", "c"], "n": [5, 30, 12]})
        fig = insights.ranked_bars(frame, "name", "n")
        (bar,) = fig.data
        assert list(bar.y) == ["a", "c", "b"]  # plotly draws the last category at the top
        assert bar.marker.color == insights.ACCENT  # one series, one colour

    def test_state_map_scale_always_starts_at_zero_and_covers_the_largest_state(self):
        fig = insights.state_map(pd.DataFrame({"state": ["AZ"], "providers": [2949]}), "x")
        assert (fig.data[0].zmin, fig.data[0].zmax) == (0, 2949)

    def test_state_map_survives_no_data(self):
        fig = insights.state_map(pd.DataFrame({"state": [], "providers": []}), "x")
        assert fig.data[0].zmax == 1

    def test_mix_bars_has_one_gapped_segment_per_type_in_the_dashboards_colours(self):
        mix = pd.DataFrame({"state": ["AZ"], **{label: [0.25] for label in insights.MIX_COLUMNS}})
        fig = insights.mix_bars(mix, COLORS)
        assert [trace.name for trace in fig.data] == list(insights.MIX_COLUMNS)
        assert [trace.marker.color for trace in fig.data] == [
            COLORS[n] for n in insights.MIX_COLUMNS
        ]
        assert all(trace.marker.line.width == 2 for trace in fig.data)  # the surface gap
        assert fig.layout.barmode == "stack"

    def test_band_bars_print_each_share(self):
        bands = insights.site_size_bands(pd.Series([1, 1, 30]))  # 2 of 32 providers are alone
        (bar,) = insights.band_bars(bands).data
        assert list(bar.text) == ["6%", "0%", "0%", "94%", "0%"]
        assert len(bar.x) == len(insights.SIZE_BANDS)
