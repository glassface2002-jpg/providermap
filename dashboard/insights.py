"""Data and charts for the dashboard's Insights tab.

Pure pandas / Plotly: nothing here touches Streamlit, so each function is testable on its own.
The inputs are the small summaries the dashboard already reads (counts by state, counts by
specialty, and the map points), never provider rows. Imports nothing from the pipeline or the app.
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd
import plotly.graph_objects as go

ACCENT = "#2f6f8f"  # one colour for ranked bars: a single series needs no colour key
INK_MUTED = "#6b7280"
GAP = "#ffffff"  # the 2px surface gap between stacked segments
BIG_SITE = 20  # "works at a large site" means this many listed providers at one place
MIN_STATE_PROVIDERS = 500  # states with fewer providers are left out of state comparisons
TOP_STATES = 20
SIZE_BANDS = [
    ("1 (alone)", 1, 1),
    ("2-4", 2, 4),
    ("5-19", 5, 19),
    ("20-99", 20, 99),
    ("100 or more", 100, None),
]
# Count columns written by providermap/map_data.py (a test checks these stay in step with it).
MIX_COLUMNS = {
    "Physician": "n_physician",
    "Advanced Practice Provider": "n_app",
    "Other clinician": "n_other",
    "Not assessed": "n_type_unknown",
}


def site_size_bands(providers_per_point: pd.Series) -> pd.DataFrame:
    """How many places, and how many providers, fall in each site-size band.

    A "site" is one map point (a building, or a ZIP or city centre when the street could not
    be placed). ``share`` is the band's share of all providers."""
    rows = []
    for label, low, high in SIZE_BANDS:
        in_band = providers_per_point >= low
        if high is not None:
            in_band &= providers_per_point <= high
        rows.append(
            {
                "band": label,
                "sites": int(in_band.sum()),
                "providers": int(providers_per_point[in_band].sum()),
            }
        )
    out = pd.DataFrame(rows)
    total = out["providers"].sum()
    out["share"] = out["providers"] / total if total else 0.0
    return out


def largest_sites(locations: pd.DataFrame, n: int = 12) -> pd.DataFrame:
    """The ``n`` biggest sites that carry a published name (five or more providers at a point).

    A site with several map points in one city (separate buildings or suites) counts once."""
    named = locations.dropna(subset=["site"]).assign(city=lambda d: d["city"].fillna(""))
    grouped = named.groupby(["site", "city", "state"], as_index=False)["providers"].sum()
    top = grouped.nlargest(n, "providers")
    return pd.DataFrame(
        {
            "site": top["site"].str.slice(0, 60),
            "place": (top["city"] + ", " + top["state"]).str.strip(", "),
            "providers": top["providers"],
        }
    ).reset_index(drop=True)


def provider_mix(
    by_state: pd.DataFrame, minimum: int = MIN_STATE_PROVIDERS, top: int = TOP_STATES
) -> pd.DataFrame:
    """Each big-enough state's split of providers by type, as shares, sorted by advanced-practice
    share (highest first)."""
    states = by_state[by_state["providers"] >= minimum].nlargest(top, "providers")
    out = pd.DataFrame({"state": states["state"].to_numpy()})
    for label, column in MIX_COLUMNS.items():
        out[label] = (states[column] / states["providers"]).to_numpy()
    return out.sort_values("Advanced Practice Provider", ascending=False, ignore_index=True)


def specialty_by_state(by_specialty: pd.DataFrame, specialty: str) -> pd.DataFrame:
    """Providers with ``specialty`` in each state."""
    part = by_specialty[by_specialty["specialty"] == specialty]
    return part.groupby("state", as_index=False)["providers"].sum()


def headlines(
    by_state: pd.DataFrame,
    by_specialty: pd.DataFrame,
    bands: pd.DataFrame,
    sites: pd.DataFrame,
) -> list[str]:
    """A few plain-English facts about the current selection, each computed from the data."""
    providers = int(by_state["providers"].sum())
    if not providers:
        return []
    facts = [
        f"**{providers:,} providers** in {by_state['state'].nunique()} "
        f"state{'s' if by_state['state'].nunique() != 1 else ''}, at "
        f"{int(by_state['map_points'].sum()):,} mapped places."
    ]
    big = bands[bands["band"].isin(_bands_from(BIG_SITE))]["share"].sum()
    alone = float(bands.loc[bands["band"] == SIZE_BANDS[0][0], "share"].sum())
    facts.append(
        f"**{big:.0%}** work at a place with {BIG_SITE} or more listed providers; "
        f"**{alone:.0%}** are the only one listed at their address."
    )
    by_name = by_specialty.groupby("specialty")["providers"].sum().drop("Unknown", errors="ignore")
    if not by_name.empty:
        facts.append(
            f"The most common specialty is **{by_name.idxmax()}** "
            f"({int(by_name.max()):,} providers, {by_name.max() / providers:.0%})."
        )
    if not sites.empty:
        top = sites.iloc[0]
        facts.append(
            f"The largest named site is **{top['site']}** ({top['place']}) with "
            f"{int(top['providers']):,} providers."
        )
    app_share = by_state["n_app"].sum() / providers
    facts.append(f"Advanced practice providers are **{app_share:.0%}** of providers.")
    return facts


def _bands_from(minimum: int) -> list[str]:
    """Names of the size bands that start at or above ``minimum`` providers."""
    return [label for label, low, _ in SIZE_BANDS if low >= minimum]


def _layout(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(
        height=height,
        margin={"l": 0, "r": 10, "t": 10, "b": 0},
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font={"size": 13},
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(128,128,128,0.2)", zeroline=False)
    fig.update_yaxes(showgrid=False)
    return fig


def ranked_bars(frame: pd.DataFrame, label: str, value: str, height: int = 420) -> go.Figure:
    """Horizontal bars, largest on top, one colour."""
    ordered = frame.sort_values(value)
    fig = go.Figure(
        go.Bar(
            x=ordered[value],
            y=ordered[label],
            orientation="h",
            marker={"color": ACCENT},
            hovertemplate="%{y}<br>%{x:,}<extra></extra>",
        )
    )
    return _layout(fig, height)


def state_map(frame: pd.DataFrame, name: str) -> go.Figure:
    """US map shaded by ``providers`` for each state (one hue, light to dark; 0 stays palest)."""
    fig = go.Figure(
        go.Choropleth(
            locations=frame["state"],
            z=frame["providers"],
            locationmode="USA-states",
            zmin=0,
            zmax=max(int(frame["providers"].max()) if len(frame) else 1, 1),
            colorscale="Blues",
            colorbar={"title": "Providers", "tickformat": ",d"},
            hovertemplate=f"<b>%{{location}}</b><br>%{{z:,}} {name}<extra></extra>",
        )
    )
    fig.update_geos(scope="usa")
    fig.update_layout(margin={"l": 0, "r": 0, "t": 0, "b": 0}, height=380)
    return fig


def mix_bars(mix: pd.DataFrame, colors: Mapping[str, str]) -> go.Figure:
    """Stacked 100% horizontal bars: each state's provider-type split, with a gap between segments."""
    fig = go.Figure()
    for label in MIX_COLUMNS:
        fig.add_bar(
            name=label,
            y=mix["state"],
            x=mix[label],
            orientation="h",
            marker={"color": colors[label], "line": {"color": GAP, "width": 2}},
            hovertemplate=f"%{{y}}: {label} %{{x:.0%}}<extra></extra>",
        )
    fig.update_layout(
        barmode="stack", legend={"orientation": "h", "y": 1.08, "traceorder": "normal"}
    )
    fig.update_xaxes(tickformat=".0%", range=[0, 1])
    fig.update_yaxes(autorange="reversed")
    return _layout(fig, 520)


def band_bars(bands: pd.DataFrame) -> go.Figure:
    """Providers by site size: ordered categories, one colour, share printed on each bar."""
    fig = go.Figure(
        go.Bar(
            x=bands["band"],
            y=bands["providers"],
            marker={"color": ACCENT},
            text=[f"{share:.0%}" for share in bands["share"]],
            textposition="outside",
            customdata=bands["sites"],
            hovertemplate="%{x} providers at the place<br>%{y:,} providers at "
            "%{customdata:,} places<extra></extra>",
        )
    )
    fig.update_xaxes(title="Providers listed at the same place")
    fig.update_yaxes(title="Providers")
    return _layout(fig, 360)
