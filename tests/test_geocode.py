"""Tests for providermap.geocode - offline, using httpx.MockTransport."""

from __future__ import annotations

import csv
import re
from pathlib import Path

import httpx
import pytest

from providermap import geocode
from providermap.geocode import (
    Address,
    GeoResult,
    address_key,
    geocode_addresses,
    parse_batch_response,
    strip_unit,
    zip_centroid,
)

# Real response shape from the Census /geographies/addressbatch endpoint:
# id, input, match, type, matched address, "lon,lat", tiger id, side, state, county, tract, block
_MATCH_ROW = (
    '"{id}","in","Match","Exact","133 BENMORE DR, WINTER PARK, FL, 32792",'
    '"-81.327161660836,28.595824582048","94400868","R","12","095","016301","2014"'
)


def _batch_handler(requests: list[httpx.Request], unmatched_street: str = "1 Nowhere Fake Rd"):
    """Mock Census: every street matches except ``unmatched_street``."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # The multipart body embeds our CSV rows verbatim: "<id>,<street>,<city>,<state>,<zip>".
        lines = []
        for line in request.read().decode("utf-8").splitlines():
            if re.match(r"^\d+,", line):
                row = next(csv.reader([line]))
                if row[1] == unmatched_street:
                    lines.append(f'"{row[0]}","in","No_Match"')
                else:
                    lines.append(_MATCH_ROW.format(id=row[0]))
        return httpx.Response(200, text="\n".join(lines))

    return handler


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestStripUnit:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("133 Benmore Drive Suite 200", "133 Benmore Drive"),
            ("4421 Sun N Lake Boulevard  Suite B", "4421 Sun N Lake Boulevard"),
            ("500 Main St, Ste. 3", "500 Main St"),
            ("12 Oak Ave Apt 4B", "12 Oak Ave"),
            ("99 Elm St #12", "99 Elm St"),
            ("1 Kettle Point Ave", "1 Kettle Point Ave"),
            ("7 Roomy Lane", "7 Roomy Lane"),
            ("Suite 5", None),
            (None, None),
            ("", None),
        ],
    )
    def test_strip_unit(self, raw, expected):
        assert strip_unit(raw) == expected


class TestAddressKey:
    def test_suite_variants_share_a_key(self):
        a = Address("133 Benmore Drive Suite 200", "Winter Park", "FL", "32792")
        b = Address("133 Benmore Dr", "Winter Park", "FL", "32792-1234")
        assert address_key(a) == address_key(b)

    def test_missing_zip_or_street_has_no_key(self):
        assert address_key(Address("1 Main St", "X", "RI", None)) is None
        assert address_key(Address(None, "X", "RI", "02904")) is None


class TestParseBatchResponse:
    def test_match_nomatch_tie_and_malformed(self):
        text = "\n".join(
            [
                _MATCH_ROW.format(id=0),
                '"1","in","No_Match"',
                '"2","in","Tie"',
                '"3","in","Match","Exact","x","not-a-coordinate","1","R","12","095","1","2"',
                "garbage",
            ]
        )
        ids = {"0": "k0", "1": "k1", "2": "k2", "3": "k3"}
        out = parse_batch_response(text, ids)
        assert out["k0"] == GeoResult(28.595824582048, -81.327161660836, "12095", "address")
        for key in ("k1", "k2", "k3"):
            assert out[key].precision == "none"
            assert out[key].lat is None

    def test_unknown_ids_are_ignored(self):
        out = parse_batch_response(_MATCH_ROW.format(id=99), {"0": "k0"})
        assert out == {"k0": GeoResult(None, None, None, "none")}


class TestZipCentroid:
    def test_known_zip(self):
        lat, lon = zip_centroid("02904-5762")  # type: ignore[misc]
        assert 41.7 < lat < 42.0
        assert -71.6 < lon < -71.3

    @pytest.mark.parametrize("bad", [None, "", "0290", "abcde", "00000"])
    def test_unusable_zip(self, bad):
        assert zip_centroid(bad) is None


class TestGeocodeAddresses:
    def test_dedupes_caches_and_falls_back_to_zip(self, tmp_path: Path):
        requests: list[httpx.Request] = []
        cache = tmp_path / "geocode.csv"
        addrs = [
            Address("133 Benmore Drive Suite 200", "Winter Park", "FL", "32792"),
            Address("133 Benmore Dr", "Winter Park", "FL", "32792"),  # same building
            Address("1 Nowhere Fake Rd", "Providence", "RI", "02904"),  # no Census match
            Address("5 No Zip St", "Providence", "RI", None),  # no key -> absent
        ]

        with _client(_batch_handler(requests)) as client:
            out = geocode_addresses(addrs, cache, client)

        assert len(requests) == 1
        assert len(out) == 2
        matched = out[address_key(addrs[0])]  # type: ignore[index]
        assert matched.precision == "address"
        assert matched.county_fips == "12095"
        fallback = out[address_key(addrs[2])]  # type: ignore[index]
        assert fallback.precision == "zip"
        assert fallback.county_fips is None

        # A rerun sends nothing - including the known non-match - and gives the same answer.
        with _client(_batch_handler(requests)) as client:
            again = geocode_addresses(addrs, cache, client)
        assert len(requests) == 1
        assert again == out

    def test_no_zip_centroid_means_unmapped(self, tmp_path: Path):
        requests: list[httpx.Request] = []
        addr = Address("1 Nowhere Fake Rd", "Nowhere", "RI", "00000")
        with _client(_batch_handler(requests)) as client:
            out = geocode_addresses([addr], tmp_path / "c.csv", client)
        assert out[address_key(addr)] == GeoResult(None, None, None, "none")  # type: ignore[index]

    def test_chunks_requests_and_keeps_earlier_chunks_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(geocode, "CHUNK_SIZE", 2)
        cache = tmp_path / "geocode.csv"
        addrs = [Address(f"{n} Main St", "Providence", "RI", "02904") for n in range(1, 6)]
        requests: list[httpx.Request] = []
        good = _batch_handler(requests)

        def flaky(request: httpx.Request) -> httpx.Response:
            if len(requests) == 1:  # second chunk fails
                requests.append(request)
                return httpx.Response(500)
            return good(request)

        with _client(flaky) as client, pytest.raises(httpx.HTTPStatusError):
            geocode_addresses(addrs, cache, client)
        assert len(requests) == 2  # chunk 1 ok, chunk 2 failed

        # Resume: only the 3 addresses not yet cached are sent (2 + 1 = two more requests).
        resumed: list[httpx.Request] = []
        with _client(_batch_handler(resumed)) as client:
            out = geocode_addresses(addrs, cache, client)
        assert len(resumed) == 2
        assert len(out) == 5
        assert all(r.precision == "address" for r in out.values())
