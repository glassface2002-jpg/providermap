"""ProviderMap dashboard: where providers practice, shown as counts on a map.

Reads the summaries written by ``providermap build-map-data`` (map points, counts
by state and specialty, and ``build_info.json``) and ``providermap
build-hospital-data`` (optional trauma-centre and NICU layers). It holds counts
only: no provider names, NPIs or profile links are ever loaded. Deliberately
imports nothing from the pipeline, so it deploys with just streamlit, plotly,
pydeck, pandas and pyarrow.

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
import plotly.graph_objects as go
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
    "Other clinician": "#8c564b",
    NOT_ASSESSED: "#9aa0a6",
}
# Count columns written by providermap/map_data.py (a test checks these stay in step with it).
TYPE_COLUMNS = {
    "Physician": "n_physician",
    "Advanced Practice Provider": "n_app",
    "Other clinician": "n_other",
    NOT_ASSESSED: "n_type_unknown",
}
CONFIDENCE_COLUMNS = {
    "high": "n_high",
    "medium": "n_medium",
    "low": "n_low",
    NOT_ASSESSED: "n_confidence_unknown",
}
PRECISION_COLUMNS = {
    "address": "n_address",
    "zip": "n_zip",
    "city": "n_city",
    "none": "n_none",
}
LOCATION_COLUMNS = [
    "lat",
    "lon",
    "precision",
    "providers",
    *TYPE_COLUMNS.values(),
    *CONFIDENCE_COLUMNS.values(),
    "city",
    "specialties",
    "site",
    "address",
]
LOCATIONS_FILE = "locations.parquet"
BY_STATE_FILE = "by_state.csv"
BY_SPECIALTY_FILE = "by_specialty.csv"
INFO_FILE = "build_info.json"
SNAPSHOT_FILES = (LOCATIONS_FILE, BY_STATE_FILE, BY_SPECIALTY_FILE, INFO_FILE)
HOSPITAL_FILE = "hospitals.parquet"
MAP_STYLE = "carto-positron"  # density view; the location map uses pydeck's matching "light"
LAYER_ID = "locations"
DEFAULT_COLOR = COLORS[NOT_ASSESSED]
DOT_ALPHA = 200

# The 50 states and DC. The overview greys out the ones a dataset has not finished.
ALL_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
).split()
PENDING_COLOR = "#d1d5db"
SOURCE_NOTE = (
    "Counts are the providers listed in each dataset's own source directory (for the Cigna "
    "datasets: providers in Cigna's network), not every provider in a state."
)

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
# A browser draws this many points comfortably; beyond it the map keeps the busiest ones.
MAX_MAP_POINTS = 25_000


@st.cache_data
def _read_snapshot(
    directory: str, modified_ns: int
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    # `modified_ns` is only a cache key: a new snapshot in the same place must not be
    # hidden by an older cached copy in a server that is still running.
    folder = Path(directory)
    by_state = pd.read_csv(folder / BY_STATE_FILE)
    by_specialty = pd.read_csv(folder / BY_SPECIALTY_FILE)
    info = json.loads((folder / INFO_FILE).read_text(encoding="utf-8"))
    return by_state, by_specialty, info


def _snapshot_stamp() -> int:
    return max((DATA_DIR / name).stat().st_mtime_ns for name in SNAPSHOT_FILES)


def load_snapshot() -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """The small summaries: counts by state, counts by specialty, and the build info."""
    return _read_snapshot(str(DATA_DIR), _snapshot_stamp())


@st.cache_data
def _read_locations(
    directory: str,
    modified_ns: int,
    dataset: str,
    states: tuple[str, ...],
    columns: tuple[str, ...],
) -> pd.DataFrame:
    filters: list[tuple[str, str, Any]] = [("dataset", "==", dataset)]
    if states:
        filters.append(("state", "in", list(states)))
    return pd.read_parquet(Path(directory) / LOCATIONS_FILE, columns=list(columns), filters=filters)


def load_locations(dataset: str, states: list[str], columns: list[str]) -> pd.DataFrame:
    """Map points of one dataset (in the chosen states, if any), reading only ``columns``.

    The file holds every state, so it is read through a filter instead of whole."""
    return _read_locations(
        str(DATA_DIR), _snapshot_stamp(), dataset, tuple(sorted(states)), tuple(columns)
    )


@st.cache_data
def _read_hospitals(path: str, modified_ns: int) -> pd.DataFrame:
    return pd.read_parquet(path)  # `modified_ns` is a cache key, as in _read_snapshot


def load_hospitals() -> pd.DataFrame | None:
    """The hospital snapshot, or ``None`` when this deployment has no hospital layers."""
    path = DATA_DIR / HOSPITAL_FILE
    return _read_hospitals(str(path), path.stat().st_mtime_ns) if path.exists() else None


def dataset_order(info: dict[str, Any]) -> list[str]:
    """Dataset names, the one with the most providers first (that one is the default)."""
    datasets = info["datasets"]
    return sorted(datasets, key=lambda name: -datasets[name]["providers"])


def sidebar_filters(
    info: dict[str, Any], by_state: pd.DataFrame, hospitals: pd.DataFrame | None
) -> tuple[str, list[str]]:
    """The chosen dataset and states (the hospital layers follow the State filter).

    The State list also offers states that only have hospitals, so a state can be chosen before
    any provider data for it exists. A dataset too big to draw at once starts on its biggest
    state instead of on nothing."""
    st.sidebar.header("Filters")
    dataset = st.sidebar.selectbox("Dataset", dataset_order(info))
    states = by_state[by_state["dataset"] == dataset].set_index("state")
    hospital_states = set(hospitals["state"].dropna()) if hospitals is not None else set()
    options = sorted(set(states.index) | hospital_states)
    default: list[str] = []
    if states["map_points"].sum() > MAX_MAP_POINTS:
        default = [states["providers"].idxmax()]
    chosen = st.sidebar.multiselect(
        "State",
        options,
        default=default,
        key=f"states-{dataset}",
        help="Leave empty to include all states (the map needs at least one for big datasets).",
    )
    return dataset, chosen


def select_rows(frame: pd.DataFrame, dataset: str, states: list[str]) -> pd.DataFrame:
    """Rows of a summary table for one dataset, narrowed to ``states`` when any are chosen."""
    rows = frame[frame["dataset"] == dataset]
    return rows[rows["state"].isin(states)] if states else rows


def high_confidence_share(counts: pd.DataFrame) -> float | None:
    """Share of providers with an assessed site confidence that is 'high' (``None`` if none)."""
    assessed = counts["providers"].sum() - counts["n_confidence_unknown"].sum()
    return float(counts["n_high"].sum() / assessed) if assessed else None


def kpis(by_state: pd.DataFrame, by_specialty: pd.DataFrame) -> None:
    share = high_confidence_share(by_state)
    cols = st.columns(4)
    cols[0].metric("Providers", f"{by_state['providers'].sum():,}")
    cols[1].metric("Mapped locations", f"{by_state['map_points'].sum():,}")
    cols[2].metric(
        "High-confidence sites",
        "n/a" if share is None else f"{share:.0%}",
        help="Share of providers whose site confidence was assessed and is 'high'.",
    )
    cols[3].metric("Specialties", f"{by_specialty['specialty'].nunique():,}")


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
            "title": rows["name"],
            "place": rows.apply(address_line, axis=1),
            "detail": rows.apply(detail, axis=1),
            "mix": "",  # the tooltip is shared with the provider dots, which have a type split
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


def dominant(frame: pd.DataFrame, columns: dict[str, str]) -> pd.Series:
    """Per row, the label whose count column is largest (ties go to the first label)."""
    labels = {column: label for label, column in columns.items()}
    return frame[list(labels)].idxmax(axis=1).map(labels)


def mix_text(row: pd.Series) -> str:
    """'Physician 3 - Advanced practice 2' from a point's type counts (zeros left out)."""
    names = {
        "n_physician": "Physician",
        "n_app": "Advanced practice",
        "n_other": "Other clinician",
        "n_type_unknown": "Type not assessed",
    }
    return " - ".join(f"{name} {row[col]:,}" for col, name in names.items() if row[col])


def place_title(row: pd.Series) -> str:
    """What to call a map point: its site if the snapshot names one, else what it stands for."""
    if isinstance(row["site"], str) and row["site"]:
        return row["site"]
    return {
        "zip": "Approximate location (centre of the ZIP code)",
        "city": "Approximate location (centre of the city)",
    }.get(row["precision"], "Small practice site")


def place_address(row: pd.Series) -> str:
    """'320 E South St, Orlando' - the street only where the snapshot publishes it."""
    parts = [row["address"], row["city"]]
    return ", ".join(part for part in parts if isinstance(part, str) and part)


def prepare_points(locations: pd.DataFrame, color_by: str) -> tuple[pd.DataFrame, int]:
    """Map points ready for deck.gl, busiest first, plus how many there were before the cap.

    ``color_by`` is "Provider type" or "Site confidence"; a point is coloured by its most common
    value, and the hover text gives the full split."""
    total = len(locations)
    points = locations.sort_values("providers", ascending=False, ignore_index=True)
    points = points.head(MAX_MAP_POINTS).copy()
    columns = TYPE_COLUMNS if color_by == "Provider type" else CONFIDENCE_COLUMNS
    points["color"] = dominant(points, columns).map(lambda label: hex_to_rgba(color_for(label)))
    points["location_id"] = points.index
    points["radius_m"] = RADIUS_M_PER_SQRT_PROVIDER * points["providers"] ** 0.5
    points["title"] = points.apply(place_title, axis=1)
    points["place"] = points.apply(place_address, axis=1)
    points["mix"] = points.apply(mix_text, axis=1)
    points["detail"] = [
        f"{n:,} provider{'s' if n != 1 else ''}" + (f" - {spec}" if spec else "")
        for n, spec in zip(points["providers"], points["specialties"].fillna(""), strict=True)
    ]
    return points, total


def selected_location_ids(event: Any) -> list[int]:
    """Location ids of the dots clicked on the map (empty when nothing is selected)."""
    if not event:
        return []
    return [obj["location_id"] for obj in event.selection.objects.get(LAYER_ID, [])]


def provider_layer(points: pd.DataFrame) -> pdk.Layer:
    """Provider dots: radius in metres (grows with zoom), clamped to a pixel range."""
    return pdk.Layer(
        "ScatterplotLayer",
        id=LAYER_ID,
        data=points[
            ["lat", "lon", "location_id", "radius_m", "color", "title", "place", "detail", "mix"]
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


def selection_summary(points: pd.DataFrame, selected: list[int]) -> None:
    """Counts for the clicked dots: totals by type and confidence, then one line per dot."""
    chosen = points[points["location_id"].isin(selected)]
    st.subheader(
        f"{chosen['providers'].sum():,} providers at {len(chosen)} selected location"
        f"{'s' if len(chosen) != 1 else ''}"
    )
    left, right = st.columns(2)
    for column, label, columns in (
        (left, "Provider type", TYPE_COLUMNS),
        (right, "Site confidence", CONFIDENCE_COLUMNS),
    ):
        counts = {name: int(chosen[col].sum()) for name, col in columns.items()}
        column.dataframe(
            pd.DataFrame({label: list(counts), "providers": list(counts.values())}),
            hide_index=True,
            width="stretch",
        )
    st.dataframe(
        chosen[["title", "place", "providers", "specialties"]].rename(
            columns={"title": "location", "place": "address"}
        ),
        hide_index=True,
        width="stretch",
    )


def map_tab(
    dataset: str,
    states: list[str],
    by_state: pd.DataFrame,
    hospitals: pd.DataFrame | None,
) -> None:
    columns = st.columns(3)
    view = columns[0].radio("View", ["Locations", "Density"], horizontal=True)
    color_by = "Provider type"
    if view == "Locations":
        color_by = columns[1].radio(
            "Color by", ["Provider type", "Site confidence"], horizontal=True
        )
    shown = []
    if view == "Locations" and hospitals is not None:
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

    wanted = select_rows(by_state, dataset, states)
    too_many = not states and wanted["map_points"].sum() > MAX_MAP_POINTS
    points = None
    total = 0
    if too_many:
        st.info(
            f"{dataset} has {wanted['map_points'].sum():,} map points, too many to draw at once. "
            "Choose one or more states in the sidebar."
        )
    elif wanted["map_points"].sum():
        locations = load_locations(dataset, states, list(LOCATION_COLUMNS))
        points, total = prepare_points(locations, color_by)

    if view == "Density":
        if points is None:
            if not too_many:
                st.info("No mappable providers match the current filters.")
            return
        fig = px.density_map(
            points,
            lat="lat",
            lon="lon",
            z="providers",
            radius=12,
            center={"lat": points["lat"].mean(), "lon": points["lon"].mean()},
            zoom=zoom_for(points),
            map_style=MAP_STYLE,
        )
        fig.update_layout(
            margin={"l": 0, "r": 0, "t": 0, "b": 0}, height=600, coloraxis_showscale=False
        )
        st.plotly_chart(fig, width="stretch")
        st.caption("Density of providers; brighter means more providers nearby.")
        return

    layers: list[pdk.Layer] = []  # bottom to top: hospital rings first, provider dots last
    legends: list[str] = []
    extent: list[pd.DataFrame] = []
    for name in shown:
        spec = HOSPITAL_LAYERS[name]
        rings = hospital_points(hospitals, spec["kind"], states, verified_only)
        if rings.empty:
            continue
        layers.append(hospital_layer(rings, spec["kind"]))
        extent.append(rings[["lat", "lon"]])
        legends.append(
            f"**{spec['title']}** (rings) "
            + legend_markdown(rings["level"].unique().tolist(), spec["palette"])
        )

    if points is not None:
        layers.append(provider_layer(points))
        extent.append(points[["lat", "lon"]])
        columns = TYPE_COLUMNS if color_by == "Provider type" else CONFIDENCE_COLUMNS
        present = set(dominant(points, columns))
        legends.insert(0, legend_markdown([label for label in columns if label in present]))

    if not layers:
        if not too_many:
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
        tooltip={"text": "{title}\n{place}\n{detail}\n{mix}"},
    )
    event = st.pydeck_chart(
        deck, height=600, on_select="rerun", selection_mode="multi-object", key="provider_map"
    )

    if points is not None and total > len(points):
        st.warning(
            f"Showing the {len(points):,} busiest of {total:,} map points. "
            "Choose fewer states to see them all."
        )
    selected = selected_location_ids(event) if points is not None else []
    if not selected:
        st.caption(
            "Dot size = number of providers at that location, and dots grow as you zoom in. "
            "Dot colour = the most common value at that location. Click a dot for its counts."
        )
        return
    selection_summary(points, selected)


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


def overview_figure(counts: pd.DataFrame, states_done: list[str]) -> go.Figure:
    """US map of providers per state; states the dataset has no data for are grey.

    Colour is on a log scale (states differ by orders of magnitude); hover shows the real count."""
    done = counts[counts["state"].isin(ALL_STATES)]
    pending = [state for state in ALL_STATES if state not in set(states_done)]
    ticks = [(2, "100"), (3, "1K"), (4, "10K"), (5, "100K"), (6, "1M")]
    logs = done["providers"].map(lambda n: math.log10(max(n, 1)))
    fig = go.Figure(
        [
            go.Choropleth(
                locations=done["state"],
                z=logs,
                zmin=2,  # a fixed range keeps the colours comparable as states are added
                zmax=max(5, math.ceil(logs.max() if len(logs) else 0)),
                locationmode="USA-states",
                customdata=done["providers"],
                colorscale="Blues",
                colorbar={
                    "title": "Providers",
                    "tickvals": [v for v, _ in ticks],
                    "ticktext": [label for _, label in ticks],
                },
                hovertemplate="<b>%{location}</b><br>%{customdata:,} providers<extra></extra>",
            ),
            go.Choropleth(
                locations=pending,
                z=[0] * len(pending),
                locationmode="USA-states",
                colorscale=[[0, PENDING_COLOR], [1, PENDING_COLOR]],
                showscale=False,
                hovertemplate="<b>%{location}</b><br>No data yet<extra></extra>",
            ),
        ]
    )
    fig.update_geos(scope="usa")
    fig.update_layout(margin={"l": 0, "r": 0, "t": 0, "b": 0}, height=460)
    return fig


def overview_tab(dataset: str, by_state: pd.DataFrame, info: dict[str, Any]) -> None:
    done = info["datasets"][dataset]["states_done"]
    counts = by_state[by_state["dataset"] == dataset]
    cols = st.columns(3)
    cols[0].metric("Providers", f"{counts['providers'].sum():,}")
    cols[1].metric("States covered", f"{len(set(done) & set(ALL_STATES))} of {len(ALL_STATES)}")
    cols[2].metric("As of", info["built_at"][:10])
    st.plotly_chart(overview_figure(counts, done), width="stretch")
    st.caption(
        "Grey = no data for this dataset: a state its run has not finished yet, or one its source "
        f"does not cover. {SOURCE_NOTE} The State filter does not apply to this tab."
    )
    assessed = counts["providers"] - counts["n_confidence_unknown"]
    share = counts["n_high"] / assessed.where(assessed > 0)
    table = counts.assign(
        **{"high-confidence share": share.map(lambda v: "n/a" if pd.isna(v) else f"{v:.0%}")}
    )
    table = table.rename(
        columns={
            "n_physician": "physicians",
            "n_app": "advanced practice",
            "n_other": "other clinicians",
            "map_points": "map locations",
        }
    ).sort_values("providers", ascending=False)
    numbers = ["providers", "physicians", "advanced practice", "other clinicians", "map locations"]
    st.dataframe(
        table[["state", *numbers, "high-confidence share"]],
        hide_index=True,
        width="stretch",
        column_config={name: st.column_config.NumberColumn(format="localized") for name in numbers},
    )


def insights_tab(
    dataset: str, states: list[str], by_state: pd.DataFrame, by_specialty: pd.DataFrame
) -> None:
    counts = select_rows(by_state, dataset, states)
    if counts.empty:
        st.info("No providers match the current filters.")
        return
    specialties = (
        select_rows(by_specialty, dataset, states)
        .groupby("specialty")["providers"]
        .sum()
        .nlargest(15)
        .reset_index()
    )
    fig = px.bar(
        specialties, x="providers", y="specialty", orientation="h", title="Top specialties"
    )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=460)
    st.plotly_chart(fig, width="stretch")

    left, right = st.columns(2)
    for column, title, columns in (
        (left, "Provider type", TYPE_COLUMNS),
        (right, "Site confidence", CONFIDENCE_COLUMNS),
    ):
        split = pd.DataFrame(
            {"label": list(columns), "providers": [int(counts[c].sum()) for c in columns.values()]}
        )
        fig = px.bar(
            split,
            x="label",
            y="providers",
            color="label",
            color_discrete_map=COLORS,
            title=title,
        )
        fig.update_layout(showlegend=False, xaxis_title=None)
        column.plotly_chart(fig, width="stretch")

    cities = load_locations(dataset, states, ["state", "city", "providers"]).dropna(subset=["city"])
    cities["place"] = cities["city"] + ", " + cities["state"]
    top_places = cities.groupby("place")["providers"].sum().nlargest(15).reset_index()
    fig = px.bar(
        top_places,
        x="providers",
        y="place",
        orientation="h",
        title="Top cities by provider count (mapped providers)",
    )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=460)
    st.plotly_chart(fig, width="stretch")


def quality_tab(
    info: dict[str, Any], by_state: pd.DataFrame, hospitals: pd.DataFrame | None
) -> None:
    st.write(
        f"Snapshot built {info['built_at'][:16].replace('T', ' ')} UTC from "
        f"{info['total_providers']:,} provider records."
    )
    st.info(
        "This site publishes counts only: how many providers are at each place, by type and "
        "confidence. It holds no provider names, NPIs, phone numbers or profile links."
    )
    rows = []
    for name, d in info["datasets"].items():
        part = by_state[by_state["dataset"] == name]
        rows.append(
            {
                "dataset": name,
                "providers": d["providers"],
                "states covered": len(d["states_done"]),
                **{f"{k} match": int(part[col].sum()) for k, col in PRECISION_COLUMNS.items()},
                "type inferred": int(part["n_type_inferred"].sum()),
            }
        )
    st.subheader("How precisely each dataset was placed on the map")
    counts = pd.DataFrame(rows)
    st.dataframe(
        counts,
        hide_index=True,
        width="stretch",
        column_config={
            name: st.column_config.NumberColumn(format="localized")
            for name in counts.columns
            if name != "dataset"
        },
    )
    st.markdown(
        "- **address**: street-level match from the US Census geocoder.\n"
        "- **zip**: the Census could not match the street, so the point is the centre of the ZIP code. "
        "Treat as approximate; providers in one ZIP stack on one point.\n"
        "- **city**: no street or ZIP match, so the point is the centre of the city. "
        "Roughly right for the city, not for the building.\n"
        "- **none**: no usable address. Counted in totals and charts, absent from the map.\n"
    )
    st.subheader("Names on the map")
    st.markdown(
        "A hover shows a site name and street address only where five or more providers share "
        "the point (a clinic or hospital rather than one person's office). Smaller points and "
        "approximate ones show counts and the most common specialties only."
    )
    st.subheader("Site confidence")
    st.markdown(
        "- **high**: single location and an identity confirmed against the CMS NPPES registry.\n"
        "- **medium**: two or three locations, or identity unconfirmed.\n"
        "- **low**: four or more locations; a single 'primary site' is not meaningful.\n"
        "- **Not assessed**: that dataset's run did not compute site confidence.\n"
    )
    st.subheader("Provider type")
    st.markdown(
        "Where a dataset recorded no provider type (Cigna's directory carries none), it is guessed "
        "from the specialty name and counted in the 'type inferred' column above. A specialty "
        "naming a nurse practitioner, physician assistant, nurse anesthetist, nurse midwife or "
        "clinical nurse specialist is an Advanced Practice Provider; a specialty containing "
        "'Physician' is a Physician; any other specialty (counselor, social worker, optometrist, "
        "therapist, ...) is an Other clinician. A specialty name does not prove a credential, so "
        "treat these as estimates. **Not assessed** means no type and no specialty to infer one from."
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
        "Where providers actually practice: public provider directory and NPI registry data, "
        "shown as counts on a map."
    )
    by_state, by_specialty, info = load_snapshot()
    hospitals = load_hospitals()
    dataset, states = sidebar_filters(info, by_state, hospitals)
    overview_t, map_t, insights_t, quality_t = st.tabs(
        ["National overview", "Map", "Insights", "Data quality"]
    )
    with overview_t:
        overview_tab(dataset, by_state, info)
    with map_t:
        kpis(select_rows(by_state, dataset, states), select_rows(by_specialty, dataset, states))
        map_tab(dataset, states, by_state, hospitals)
    with insights_t:
        insights_tab(dataset, states, by_state, by_specialty)
    with quality_t:
        quality_tab(info, by_state, hospitals)


if __name__ == "__main__":
    main()
