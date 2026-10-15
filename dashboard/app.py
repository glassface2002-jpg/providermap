"""ProviderMap dashboard: where providers practice, on an interactive map.

Reads the snapshots written by ``providermap build-map-data`` (providers, plus
``build_info.json``) and ``providermap build-hospital-data`` (optional trauma-
centre and NICU layers). Deliberately imports nothing from the pipeline, so
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
HOSPITAL_FILE = "hospitals.parquet"
MAP_STYLE = "carto-positron"  # density view; the location map uses pydeck's matching "light"
LAYER_ID = "locations"
DEFAULT_COLOR = COLORS[NOT_ASSESSED]
DOT_ALPHA = 200

# Hospital layers (drawn as rings beneath the provider dots). Palettes run darkest = highest level.
NOT_STATED = "Not stated"
HOSPITAL_LAYERS = {
    "Trauma centers": {
        "kind": "trauma",
        "title": "Trauma level",
        "palette": {
            "I": "#7f1d1d",
            "II": "#b91c1c",
            "III": "#ef4444",
            "IV": "#f87171",
            "V": "#fca5a5",
            NOT_STATED: "#9aa0a6",
        },
    },
    "NICUs": {
        "kind": "nicu",
        "title": "NICU level",
        "palette": {"IV": "#4c1d95", "III": "#7c3aed", "II": "#c4b5fd"},
    },
}
HOSPITAL_RADIUS_M = 800
HOSPITAL_MIN_PX = 9
HOSPITAL_MAX_PX = 34

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


@st.cache_data
def _read_hospitals(path: str, modified_ns: int) -> pd.DataFrame:
    return pd.read_parquet(path)  # `modified_ns` is a cache key, as in _read_snapshot


def load_hospitals() -> pd.DataFrame | None:
    """The hospital snapshot, or ``None`` when this deployment has no hospital layers."""
    path = DATA_DIR / HOSPITAL_FILE
    return _read_hospitals(str(path), path.stat().st_mtime_ns) if path.exists() else None


def sidebar_filters(
    df: pd.DataFrame, hospitals: pd.DataFrame | None
) -> tuple[pd.DataFrame, list[str]]:
    """Filtered providers, plus the chosen states (the hospital layers follow the State filter).

    The State list also offers states that only have hospitals, so a state can be chosen before
    any provider data for it exists."""
    st.sidebar.header("Filters")
    hospital_states = set(hospitals["state"].dropna()) if hospitals is not None else set()

    def pick(
        label: str, column: str, order: list[str] | None = None, extra: set[str] = frozenset()
    ) -> list[str]:
        counts = df[column].value_counts()
        options = order if order else counts.index.tolist() + sorted(extra - set(counts.index))
        options = [o for o in options if o in counts.index or o in extra]
        return st.sidebar.multiselect(label, options, help="Leave empty to include all.")

    chosen = {
        "dataset": pick("Dataset", "dataset"),
        "state": pick("State", "state", extra=hospital_states),
        "provider_type": pick("Provider type", "provider_type"),
        "specialty": pick("Specialty", "specialty"),
        "site_confidence": pick("Site confidence", "site_confidence", CONFIDENCE_ORDER),
        "accepting_new_patients": pick("Accepting new patients", "accepting_new_patients"),
    }
    mask = pd.Series(True, index=df.index)
    for column, values in chosen.items():
        if values:
            mask &= df[column].isin(values)
    return df[mask], chosen["state"]


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
    out["detail"] = [
        f"{n:,} provider{'s' if n != 1 else ''}" + (f" - {spec}" if spec else "")
        for n, spec in zip(out["providers"], out["specialties"], strict=True)
    ]
    out["radius_m"] = RADIUS_M_PER_SQRT_PROVIDER * out["providers"] ** 0.5
    # Largest first, so small dots draw on top of big ones and stay clickable.
    return out.sort_values("providers", ascending=False, ignore_index=True)


def color_for(label: str, palette: dict[str, str] = COLORS) -> str:
    return palette.get(label, DEFAULT_COLOR)


def hex_to_rgba(hex_color: str, alpha: int = DOT_ALPHA) -> list[int]:
    """``'#1f77b4'`` -> ``[31, 119, 180, 200]`` (deck.gl colours are RGBA lists)."""
    digits = hex_color.lstrip("#")
    return [int(digits[i : i + 2], 16) for i in (0, 2, 4)] + [alpha]


def legend_markdown(labels: list[str], palette: dict[str, str] = COLORS) -> str:
    """Coloured-dot legend (deck.gl has none). Known labels first, in palette order."""
    known = [label for label in palette if label in labels]
    unknown = sorted(set(labels) - set(palette))
    return "&nbsp;&nbsp;&nbsp;".join(
        f"<span style='color:{color_for(label, palette)}'>&#9679;</span> {html.escape(label)}"
        for label in known + unknown
    )


def address_line(row: pd.Series) -> str:
    """'320 E South St, Orlando, FL 32801' from whichever parts the source gave."""
    place = " ".join(part for part in (row["state"], row["zip"]) if isinstance(part, str))
    parts = [row["address"], row["city"], place]
    return ", ".join(part for part in parts if isinstance(part, str) and part)


def hospital_points(
    hospitals: pd.DataFrame, kind: str, states: list[str], verified_only: bool = False
) -> pd.DataFrame:
    """Rows for one hospital layer, ready for deck.gl: placed hospitals of that kind (in the
    chosen states, if any) with ring colours and the text shown on hover. ``verified_only``
    keeps just the ACS-verified trauma centres (it has no effect on NICUs)."""
    palette = next(c["palette"] for c in HOSPITAL_LAYERS.values() if c["kind"] == kind)
    rows = hospitals[(hospitals["kind"] == kind) & hospitals["lat"].notna()]
    if states:
        rows = rows[rows["state"].isin(states)]
    if verified_only and kind == "trauma":
        rows = rows[rows["acs_verified"] == "Yes"]

    def detail(row: pd.Series) -> str:
        if kind == "trauma":
            level = "level not stated" if row["level"] == NOT_STATED else f"level {row['level']}"
            verified = " - ACS verified" if row["acs_verified"] == "Yes" else ""
            return f"Trauma center, {level}{verified}"
        beds = f" - {int(row['nicu_beds'])} beds" if pd.notna(row["nicu_beds"]) else ""
        return f"NICU level {row['level']}{beds}"

    return pd.DataFrame(
        {
            "lat": rows["lat"],
            "lon": rows["lon"],
            "level": rows["level"],
            "site": rows["name"],
            "address": rows.apply(address_line, axis=1),
            "detail": rows.apply(detail, axis=1),
            "color": rows["level"].map(lambda v: hex_to_rgba(color_for(v, palette), 255)),
            "fill": rows["level"].map(lambda v: hex_to_rgba(color_for(v, palette), 45)),
        }
    ).reset_index(drop=True)


def hospital_layer(points: pd.DataFrame, kind: str) -> pdk.Layer:
    """Hollow, tinted rings with a thick outline, sized in metres like the provider dots."""
    return pdk.Layer(
        "ScatterplotLayer",
        id=f"{kind}-centers",
        data=points,
        get_position=["lon", "lat"],
        get_radius=HOSPITAL_RADIUS_M,
        radius_min_pixels=HOSPITAL_MIN_PX,
        radius_max_pixels=HOSPITAL_MAX_PX,
        get_fill_color="fill",
        get_line_color="color",
        stroked=True,
        line_width_min_pixels=3,
        pickable=True,
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


def provider_layer(locations: pd.DataFrame) -> pdk.Layer:
    """Provider dots: radius in metres (grows with zoom), clamped to a pixel range."""
    return pdk.Layer(
        "ScatterplotLayer",
        id=LAYER_ID,
        data=locations[
            ["lat", "lon", "location_id", "radius_m", "color", "site", "address", "detail"]
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


def map_tab(df: pd.DataFrame, hospitals: pd.DataFrame | None, states: list[str]) -> None:
    mapped = df.dropna(subset=["lat", "lon"])
    columns = st.columns(3)
    view = columns[0].radio("View", ["Locations", "Density"], horizontal=True)

    if view == "Density":
        if mapped.empty:
            st.info("No mappable providers match the current filters.")
            return
        fig = px.density_map(
            mapped,
            lat="lat",
            lon="lon",
            radius=12,
            center={"lat": mapped["lat"].mean(), "lon": mapped["lon"].mean()},
            zoom=zoom_for(mapped),
            map_style=MAP_STYLE,
        )
        fig.update_layout(
            margin={"l": 0, "r": 0, "t": 0, "b": 0}, height=600, coloraxis_showscale=False
        )
        st.plotly_chart(fig, width="stretch")
        st.caption("Density of providers; brighter means more providers nearby.")
        return

    color_by = columns[1].radio("Color by", ["Provider type", "Site confidence"], horizontal=True)
    color_col = "provider_type" if color_by == "Provider type" else "site_confidence"
    shown = []
    if hospitals is not None:
        shown = columns[2].multiselect(
            "Hospitals",
            list(HOSPITAL_LAYERS),
            help="Rings drawn beneath the provider dots. They follow the State filter.",
        )
    verified_only = False
    if "Trauma centers" in shown:
        verified_only = columns[2].checkbox(
            "ACS-verified trauma centers only",
            value=True,
            help="Levels without ACS verification are the source's own and look inconsistent "
            "between states, so they are hidden by default.",
        )

    layers: list[pdk.Layer] = []  # bottom to top: hospital rings first, provider dots last
    legends: list[str] = []
    extent: list[pd.DataFrame] = []
    for name in shown:
        spec = HOSPITAL_LAYERS[name]
        points = hospital_points(hospitals, spec["kind"], states, verified_only)
        if points.empty:
            continue
        layers.append(hospital_layer(points, spec["kind"]))
        extent.append(points[["lat", "lon"]])
        legends.append(
            f"**{spec['title']}** (rings) "
            + legend_markdown(points["level"].unique().tolist(), spec["palette"])
        )

    locations = None
    if not mapped.empty:
        locations = aggregate_locations(mapped)
        locations["color"] = locations[color_col].map(lambda label: hex_to_rgba(color_for(label)))
        layers.append(provider_layer(locations))
        extent.append(mapped[["lat", "lon"]])
        legends.insert(0, legend_markdown(locations[color_col].unique().tolist()))

    if not layers:
        hint = " Turn on a hospital layer to see hospitals." if hospitals is not None else ""
        st.info("No mappable providers match the current filters." + hint)
        return

    for legend in legends:
        st.markdown(legend, unsafe_allow_html=True)
    frame = pd.concat(extent)
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(
            latitude=frame["lat"].mean(), longitude=frame["lon"].mean(), zoom=zoom_for(frame)
        ),
        map_style="light",
        tooltip={"text": "{site}\n{address}\n{detail}"},
    )
    event = st.pydeck_chart(
        deck, height=600, on_select="rerun", selection_mode="multi-object", key="provider_map"
    )

    selected = selected_location_ids(event) if locations is not None else []
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


def hospital_quality(hospitals: pd.DataFrame) -> None:
    st.subheader("Hospital layers")
    rows = []
    for name, spec in HOSPITAL_LAYERS.items():
        part = hospitals[hospitals["kind"] == spec["kind"]]
        counts = part["geocode_precision"].value_counts().to_dict()
        rows.append(
            {"layer": name, "hospitals": len(part), "states": part["state"].nunique()}
            | {f"{precision} match": n for precision, n in counts.items()}
        )
    st.dataframe(pd.DataFrame(rows).fillna(0), hide_index=True, width="stretch")
    trauma = hospitals[hospitals["kind"] == "trauma"]
    st.markdown(
        "Trauma centers and NICUs come from two third-party directories (named in the headers of "
        "the source files: verifiedtraumacenters.com and neonatologysolutions.com). A hospital in "
        "both appears once in each layer, and a source is not checked against the other.\n\n"
        f"- Only {int((trauma['acs_verified'] == 'Yes').sum()):,} of {len(trauma):,} trauma centers "
        "are marked ACS-verified in the source. Levels without verification are the source's own "
        "and look inconsistent between states, so the map shows verified centers only unless you "
        "untick the box.\n"
        f"- '{NOT_STATED}' means the source gave no trauma level (often children's hospitals)."
    )
    everywhere = set(hospitals["state"])
    for name, spec in HOSPITAL_LAYERS.items():
        absent = sorted(everywhere - set(hospitals.loc[hospitals["kind"] == spec["kind"], "state"]))
        if absent:
            st.caption(f"{name}: the source has no hospitals for {', '.join(absent)}.")


def quality_tab(info: dict, hospitals: pd.DataFrame | None) -> None:
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
        "- **city**: no street or ZIP match, so the point is the centre of the city. "
        "Roughly right for the city, not for the building.\n"
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
    if hospitals is not None:
        hospital_quality(hospitals)
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
    hospitals = load_hospitals()
    filtered, states = sidebar_filters(df, hospitals)
    kpis(filtered)
    map_t, insights_t, find_t, quality_t = st.tabs(
        ["Map", "Insights", "Find a provider", "Data quality"]
    )
    with map_t:
        map_tab(filtered, hospitals, states)
    with insights_t:
        insights_tab(filtered)
    with find_t:
        find_tab(df)
    with quality_t:
        quality_tab(info, hospitals)


if __name__ == "__main__":
    main()
