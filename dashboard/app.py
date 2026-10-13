"""ProviderMap dashboard: where providers practice, on an interactive map.

Reads the snapshot written by ``providermap build-map-data`` (a Parquet file
plus ``build_info.json``). Deliberately imports nothing from the pipeline, so
it deploys with just streamlit, plotly, pydeck, pandas and pyarrow.

Run locally:  streamlit run dashboard/app.py
"""

from __future__ import annotations

import html
import json
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st

DATA_DIR = Path(os.environ.get("PROVIDERMAP_DATA_DIR", Path(__file__).parent / "data"))

NOT_ASSESSED = "Not assessed"
CONFIDENCE_ORDER = ["high", "medium", "low", NOT_ASSESSED]
COLORS = {
    "high": "#1b9e77",
    "medium": "#e6ab02",
    "low": "#d95f02",
    "Physician": "#1f77b4",
    "Advanced Practice Provider": "#17becf",
    NOT_ASSESSED: "#9aa0a6",
}
TABLE_COLUMNS = [
    "name",
    "credentials",
    "specialty",
    "provider_type",
    "primary_site",
    "address",
    "city",
    "state",
    "npi",
    "site_confidence",
]
SNAPSHOT_FILES = ("providers.parquet", "build_info.json")
MAP_STYLE = "carto-positron"  # density view; the location map uses pydeck's matching "light"
LAYER_ID = "locations"
DEFAULT_COLOR = COLORS[NOT_ASSESSED]
DOT_ALPHA = 200

# Dot size. The radius is in real-world metres (so dot area is proportional to the number of
# providers and dots grow as you zoom in), clamped to a pixel range so they stay visible zoomed
# out and stop growing at a cap. Tune these three if the map feels too small or too large.
RADIUS_M_PER_SQRT_PROVIDER = 100
MIN_RADIUS_PX = 4
MAX_RADIUS_PX = 24


@st.cache_data
def _read_snapshot(directory: str, modified_ns: int) -> tuple[pd.DataFrame, dict]:
    # `modified_ns` is only a cache key: a new snapshot in the same place must not be
    # hidden by an older cached copy in a server that is still running.
    folder = Path(directory)
    df = pd.read_parquet(folder / "providers.parquet")
    info = json.loads((folder / "build_info.json").read_text(encoding="utf-8"))
    return df, info


def load_snapshot() -> tuple[pd.DataFrame, dict]:
    newest = max((DATA_DIR / name).stat().st_mtime_ns for name in SNAPSHOT_FILES)
    return _read_snapshot(str(DATA_DIR), newest)


def sidebar_filters(df: pd.DataFrame) -> pd.DataFrame:
    st.sidebar.header("Filters")

    def pick(label: str, column: str, order: list[str] | None = None) -> list[str]:
        counts = df[column].value_counts()
        options = order if order else counts.index.tolist()
        options = [o for o in options if o in counts.index]
        return st.sidebar.multiselect(label, options, help="Leave empty to include all.")

    chosen = {
        "dataset": pick("Dataset", "dataset"),
        "state": pick("State", "state"),
        "provider_type": pick("Provider type", "provider_type"),
        "specialty": pick("Specialty", "specialty"),
        "site_confidence": pick("Site confidence", "site_confidence", CONFIDENCE_ORDER),
        "accepting_new_patients": pick("Accepting new patients", "accepting_new_patients"),
    }
    mask = pd.Series(True, index=df.index)
    for column, values in chosen.items():
        if values:
            mask &= df[column].isin(values)
    return df[mask]


def kpis(df: pd.DataFrame) -> None:
    mapped = df.dropna(subset=["lat", "lon"])
    assessed = df[df["site_confidence"] != NOT_ASSESSED]
    high_share = (assessed["site_confidence"] == "high").mean() if len(assessed) else None
    cols = st.columns(4)
    cols[0].metric("Providers", f"{len(df):,}")
    cols[1].metric("Mapped locations", f"{mapped[['lat', 'lon']].drop_duplicates().shape[0]:,}")
    cols[2].metric(
        "High-confidence sites",
        "n/a" if high_share is None else f"{high_share:.0%}",
        help="Share of providers whose site confidence was assessed and is 'high'.",
    )
    cols[3].metric("Specialties", f"{df['specialty'].nunique():,}")


def aggregate_locations(mapped: pd.DataFrame) -> pd.DataFrame:
    """One row per map point: how many providers practice there, and what kind."""

    def top(series: pd.Series, n: int = 1) -> str:
        return ", ".join(series.dropna().value_counts().index[:n])

    grouped = mapped.groupby(["lat", "lon"], sort=False)
    out = grouped.agg(
        providers=("npi", "size"),
        site=("primary_site", lambda s: top(s) or "Unnamed site"),
        address=("address", lambda s: top(s)),
        specialties=("specialty", lambda s: top(s, 3)),
        provider_type=("provider_type", lambda s: top(s)),
        site_confidence=("site_confidence", lambda s: top(s)),
    ).reset_index()
    out["location_id"] = out.index
    out["radius_m"] = RADIUS_M_PER_SQRT_PROVIDER * out["providers"] ** 0.5
    # Largest first, so small dots draw on top of big ones and stay clickable.
    return out.sort_values("providers", ascending=False, ignore_index=True)


def color_for(label: str) -> str:
    return COLORS.get(label, DEFAULT_COLOR)


def hex_to_rgba(hex_color: str, alpha: int = DOT_ALPHA) -> list[int]:
    """``'#1f77b4'`` -> ``[31, 119, 180, 200]`` (deck.gl colours are RGBA lists)."""
    digits = hex_color.lstrip("#")
    return [int(digits[i : i + 2], 16) for i in (0, 2, 4)] + [alpha]


def legend_markdown(labels: list[str]) -> str:
    """Coloured-dot legend (deck.gl has none). Known labels first, in COLORS order."""
    known = [label for label in COLORS if label in labels]
    unknown = sorted(set(labels) - set(COLORS))
    return "&nbsp;&nbsp;&nbsp;".join(
        f"<span style='color:{color_for(label)}'>&#9679;</span> {html.escape(label)}"
        for label in known + unknown
    )


def zoom_for(frame: pd.DataFrame) -> float:
    """Zoom level that fits the points on a ~1000px-wide map (web-mercator tile math)."""
    lon_span = frame["lon"].max() - frame["lon"].min()
    lat_span = (frame["lat"].max() - frame["lat"].min()) * 1.6
    span = max(lon_span, lat_span, 0.02)
    return float(min(max(math.log2(700 / (span * 1.2)), 3), 12))


def selected_location_ids(event: Any) -> list[int]:
    """Location ids of the dots clicked on the map (empty when nothing is selected)."""
    if not event:
        return []
    return [obj["location_id"] for obj in event.selection.objects.get(LAYER_ID, [])]


def map_tab(df: pd.DataFrame) -> None:
    mapped = df.dropna(subset=["lat", "lon"])
    if mapped.empty:
        st.info("No mappable providers match the current filters.")
        return

    left, right = st.columns([1, 1])
    view = left.radio("View", ["Locations", "Density"], horizontal=True)
    center = {"lat": mapped["lat"].mean(), "lon": mapped["lon"].mean()}
    zoom = zoom_for(mapped)

    if view == "Density":
        fig = px.density_map(
            mapped, lat="lat", lon="lon", radius=12, center=center, zoom=zoom, map_style=MAP_STYLE
        )
        fig.update_layout(
            margin={"l": 0, "r": 0, "t": 0, "b": 0}, height=600, coloraxis_showscale=False
        )
        st.plotly_chart(fig, width="stretch")
        st.caption("Density of providers; brighter means more providers nearby.")
        return

    color_by = right.radio("Color by", ["Provider type", "Site confidence"], horizontal=True)
    color_col = "provider_type" if color_by == "Provider type" else "site_confidence"
    locations = aggregate_locations(mapped)
    locations["color"] = locations[color_col].map(lambda label: hex_to_rgba(color_for(label)))
    st.markdown(legend_markdown(locations[color_col].unique().tolist()), unsafe_allow_html=True)
    layer = pdk.Layer(
        "ScatterplotLayer",
        id=LAYER_ID,
        data=locations[
            [
                "lat",
                "lon",
                "location_id",
                "radius_m",
                "color",
                "site",
                "address",
                "providers",
                "specialties",
            ]
        ],
        get_position=["lon", "lat"],
        get_radius="radius_m",
        radius_min_pixels=MIN_RADIUS_PX,
        radius_max_pixels=MAX_RADIUS_PX,
        get_fill_color="color",
        stroked=True,
        get_line_color=[255, 255, 255, 230],
        line_width_min_pixels=1,
        pickable=True,
        auto_highlight=True,
    )
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(
            latitude=center["lat"], longitude=center["lon"], zoom=zoom
        ),
        map_style="light",
        tooltip={"text": "{site}\n{address}\nProviders: {providers}\n{specialties}"},
    )
    event = st.pydeck_chart(
        deck, height=600, on_select="rerun", selection_mode="multi-object", key="provider_map"
    )

    selected = selected_location_ids(event)
    if not selected:
        st.caption(
            "Dot size = number of providers at that location, and dots grow as you zoom in. "
            "Click a dot to list its providers."
        )
        return
    chosen = locations[locations["location_id"].isin(selected)]
    rows = mapped.merge(chosen[["lat", "lon"]], on=["lat", "lon"])
    st.subheader(f"{len(rows):,} providers at {len(chosen)} selected location(s)")
    st.dataframe(rows[TABLE_COLUMNS], hide_index=True, width="stretch")


def insights_tab(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No providers match the current filters.")
        return
    top = df["specialty"].value_counts().head(15).index
    by_dataset = (
        df[df["specialty"].isin(top)]
        .groupby(["specialty", "dataset"])
        .size()
        .reset_index(name="providers")
    )
    fig = px.bar(
        by_dataset,
        x="providers",
        y="specialty",
        color="dataset",
        orientation="h",
        title="Top specialties",
    )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=460)
    st.plotly_chart(fig, width="stretch")

    left, right = st.columns(2)
    confidence = df.groupby(["dataset", "site_confidence"]).size().reset_index(name="providers")
    fig = px.bar(
        confidence,
        x="dataset",
        y="providers",
        color="site_confidence",
        color_discrete_map=COLORS,
        category_orders={"site_confidence": CONFIDENCE_ORDER},
        title="Site confidence by dataset",
    )
    left.plotly_chart(fig, width="stretch")

    types = df.groupby(["dataset", "provider_type"]).size().reset_index(name="providers")
    fig = px.bar(
        types,
        x="dataset",
        y="providers",
        color="provider_type",
        color_discrete_map=COLORS,
        barmode="relative",
        title="Provider type by dataset",
    )
    right.plotly_chart(fig, width="stretch")

    cities = df.groupby(["city", "state", "dataset"]).size().reset_index(name="providers")
    cities["place"] = cities["city"].str.title() + ", " + cities["state"]
    top_places = cities.groupby("place")["providers"].sum().nlargest(15).index
    fig = px.bar(
        cities[cities["place"].isin(top_places)],
        x="providers",
        y="place",
        color="dataset",
        orientation="h",
        title="Top cities by provider count",
    )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=460)
    st.plotly_chart(fig, width="stretch")


def find_tab(df: pd.DataFrame) -> None:
    st.caption("Searches every provider in the snapshot; the sidebar filters do not apply here.")
    query = st.text_input("Provider name", placeholder="e.g. Menezes")
    if not query.strip():
        return
    matches = df[df["name"].str.contains(query.strip(), case=False, regex=False, na=False)].head(50)
    if matches.empty:
        st.info("No provider by that name.")
        return
    labels = {
        i: f"{r['name']} - {r['city']}, {r['state']} (NPI {r['npi']})"
        for i, r in matches.iterrows()
    }
    choice = st.selectbox("Matches", list(labels), format_func=labels.get)
    row = matches.loc[choice]
    st.subheader(row["name"])
    st.markdown(
        f"**Specialty:** {row['specialty'] or 'n/a'}  \n"
        f"**Primary site of care:** {row['primary_site'] or 'n/a'}  \n"
        f"**Address:** {row['address']}, {row['city']}, {row['state']} {row['zip']}  \n"
        f"**NPI:** {row['npi']}  \n"
        f"**Site confidence:** {row['site_confidence']}  \n"
        f"**Dataset:** {row['dataset']}"
    )
    if row["profile_url"]:
        st.link_button("Public directory profile", row["profile_url"])


def quality_tab(info: dict) -> None:
    st.write(
        f"Snapshot built {info['built_at'][:16].replace('T', ' ')} UTC from {info['total_providers']:,} provider records."
    )
    rows = [
        {
            "dataset": name,
            "providers": d["providers"],
            **{f"{k} match": v for k, v in d["geocode_precision"].items()},
        }
        for name, d in info["datasets"].items()
    ]
    st.subheader("How precisely each dataset was placed on the map")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.markdown(
        "- **address**: street-level match from the US Census geocoder.\n"
        "- **zip**: the Census could not match the street, so the point is the centre of the ZIP code. "
        "Treat as approximate; providers in one ZIP stack on one point.\n"
        "- **none**: no usable address. Counted in totals and charts, absent from the map.\n"
    )
    st.subheader("Site confidence")
    st.markdown(
        "- **high**: single location and an identity confirmed against the CMS NPPES registry.\n"
        "- **medium**: two or three locations, or identity unconfirmed.\n"
        "- **low**: four or more locations; a single 'primary site' is not meaningful.\n"
        "- **Not assessed**: that dataset's run did not compute site confidence or provider type.\n"
    )
    st.subheader("Specialty names")
    st.markdown(
        "Specialties are shown exactly as each source reports them, so the same specialty can "
        "appear under different names across datasets (for example 'Internal Medicine' and "
        "'Internal Medicine Physician'). They are not merged."
    )
    st.caption(
        "Public NPI registry and provider-directory information only. No patient data or PHI."
    )


def main() -> None:
    st.set_page_config(page_title="ProviderMap", layout="wide")
    st.title("ProviderMap")
    st.caption(
        "Where providers actually practice: public provider directory and NPI registry data on a map."
    )
    df, info = load_snapshot()
    filtered = sidebar_filters(df)
    kpis(filtered)
    map_t, insights_t, find_t, quality_t = st.tabs(
        ["Map", "Insights", "Find a provider", "Data quality"]
    )
    with map_t:
        map_tab(filtered)
    with insights_t:
        insights_tab(filtered)
    with find_t:
        find_tab(df)
    with quality_t:
        quality_tab(info)


if __name__ == "__main__":
    main()
