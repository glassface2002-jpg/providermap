"""Optional lookup of the providers at a clicked map point, from a PRIVATE store.

The public repository holds counts only. To show *who* the providers at a point are, the
dashboard reads a separate file, ``providers_private.parquet`` (written by ``providermap
build-map-data --private-out``), from a private GitHub repository, using a read-only token kept
in Streamlit's secrets. The file is never part of this repository, and the app only ever shows
the rows of the points a visitor clicks (capped at :data:`MAX_ROWS`, with Streamlit's table
download turned off in ``.streamlit/config.toml``).

This is a deterrent against bulk copying, not a lock: a determined visitor could still click
through every point and copy what they see.

Secrets (``.streamlit/secrets.toml`` locally, the app's Secrets box on Streamlit Cloud)::

    [private_data]
    repo = "your-name/providermap-private"
    path = "providers_private.parquet"
    token = "github_pat_..."      # fine-grained, read-only, that one repository
    ref = "main"                  # optional

When the section is absent the feature is simply off. Imports nothing from the pipeline.
"""

from __future__ import annotations

import io
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

SECRET_SECTION = "private_data"
MAX_ROWS = 300  # most providers shown for the clicked points; a name filter narrows beyond that
MAX_POINTS = 5  # most clicked points listed at once
TIMEOUT_SECONDS = 120
LOCATION_TOLERANCE = 1e-6  # degrees (~0.1 m): map points are rounded to 1e-5
TYPE_INFERRED = "Inferred from specialty"
REQUIRED_COLUMNS = [  # a test checks these against providermap.map_data.PRIVATE_COLUMNS
    "dataset",
    "state",
    "lat",
    "lon",
    "name",
    "credentials",
    "specialty",
    "provider_type",
    "provider_type_source",
    "npi",
    "primary_site",
    "address",
    "city",
    "zip",
]
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


class PrivateDataError(Exception):
    """The private list could not be set up or fetched. The message is safe to show: it never
    contains the token."""


@dataclass(frozen=True)
class Source:
    repo: str  # "owner/name"
    path: str  # file path inside the repository
    token: str
    ref: str = "main"


def source_from_secrets(secrets: Mapping[str, Any]) -> Source | None:
    """The configured private source, or ``None`` when the feature is not set up.

    Raises :class:`PrivateDataError` when the section exists but is incomplete or malformed."""
    section = secrets.get(SECRET_SECTION)
    if not section:
        return None
    values = {key: str(section.get(key, "")).strip() for key in ("repo", "path", "token", "ref")}
    if not (values["repo"] and values["path"] and values["token"]):
        raise PrivateDataError(f"The [{SECRET_SECTION}] secret needs repo, path and token.")
    if not _REPO.match(values["repo"]):
        raise PrivateDataError("The repo secret must look like owner/name.")
    if ".." in values["path"].split("/"):
        raise PrivateDataError("The path secret must stay inside the repository.")
    return Source(values["repo"], values["path"], values["token"], values["ref"] or "main")


def download(source: Source) -> bytes:
    """The raw file from the private repository (GitHub contents API, files up to 100 MB)."""
    url = (
        f"https://api.github.com/repos/{source.repo}/contents/"
        f"{urllib.parse.quote(source.path)}?ref={urllib.parse.quote(source.ref)}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {source.token}",
            "Accept": "application/vnd.github.raw+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "providermap-dashboard",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data: bytes = response.read()
            return data
    except urllib.error.HTTPError as err:
        hint = {
            401: "the token was rejected",
            403: "the token may lack access or GitHub is rate-limiting",
            404: "repo, path or branch not found, or the token cannot see that repository",
        }.get(err.code, "unexpected answer")
        raise PrivateDataError(f"GitHub answered {err.code}: {hint}.") from None
    except (urllib.error.URLError, TimeoutError):
        raise PrivateDataError("Could not reach GitHub.") from None


def load_table(data: bytes) -> pa.Table:
    """The private list as an Arrow table (compact in memory), checked for its columns."""
    try:
        table = pq.read_table(io.BytesIO(data))
    except (pa.ArrowException, OSError):
        raise PrivateDataError("The private file is not a readable Parquet file.") from None
    missing = [column for column in REQUIRED_COLUMNS if column not in table.column_names]
    if missing:
        raise PrivateDataError(f"The private file lacks columns: {', '.join(missing)}.")
    return table.select(REQUIRED_COLUMNS)


def _at_point(table: pa.Table, dataset: str, state: str, lat: float, lon: float) -> pa.Table:
    close = [
        pc.less(pc.abs(pc.subtract(table[column], value)), LOCATION_TOLERANCE)
        for column, value in (("lat", lat), ("lon", lon))
    ]
    mask = pc.and_(
        pc.and_(pc.equal(table["dataset"], dataset), pc.equal(table["state"], state)),
        pc.and_(*close),
    )
    return table.filter(mask)


def providers_at(
    table: pa.Table, dataset: str, points: pd.DataFrame, text: str = ""
) -> tuple[pd.DataFrame, int]:
    """The providers at the clicked ``points`` (rows with state, lat, lon), narrowed by ``text``.

    Returns at most :data:`MAX_ROWS` rows sorted by name, and how many matched in all. ``text``
    matches the name, specialty or NPI (case-insensitive)."""
    found = [
        _at_point(table, dataset, row.state, row.lat, row.lon).to_pandas()
        for row in points.itertuples()
    ]
    frame = pd.concat(found, ignore_index=True) if found else pd.DataFrame(columns=REQUIRED_COLUMNS)
    text = text.strip()
    if text:
        haystack = frame[["name", "specialty", "npi"]].fillna("").agg(" ".join, axis=1)
        frame = frame[haystack.str.contains(text, case=False, regex=False)]
    frame = frame.sort_values("name", ignore_index=True)
    return frame.head(MAX_ROWS), len(frame)


def display_frame(providers: pd.DataFrame) -> pd.DataFrame:
    """The columns shown to visitors, with an inferred provider type marked as such."""
    kind = providers["provider_type"].where(
        providers["provider_type_source"] != TYPE_INFERRED,
        providers["provider_type"] + " (inferred)",
    )
    return pd.DataFrame(
        {
            "name": providers["name"],
            "specialty": providers["specialty"],
            "provider type": kind,
            "NPI": providers["npi"],
            "practice site": providers["primary_site"].fillna(""),
        }
    )
