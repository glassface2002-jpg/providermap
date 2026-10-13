"""Tests for providermap.cli's argument parser.

Deliberately narrow: ``build_parser()`` is pure argparse wiring with no
config/adapter construction, so these tests exercise only that - nothing
here runs a command or touches a database.
"""

from __future__ import annotations

import pytest

from providermap.cli import build_parser


class TestIngestProvidersDiscoveryModeFlag:
    """``--discovery-mode`` (Phase E, Stage 5) - a per-run override of
    ``providers.cigna_fhir_discovery_mode``, mirroring the existing
    ``--provider-adapter``/``--dry-run`` flags on this same subcommand."""

    def test_defaults_to_none_when_omitted(self) -> None:
        args = build_parser().parse_args(["ingest-providers"])
        assert args.discovery_mode is None

    def test_accepts_state_scoped(self) -> None:
        args = build_parser().parse_args(["ingest-providers", "--discovery-mode", "state_scoped"])
        assert args.discovery_mode == "state_scoped"

    def test_accepts_national(self) -> None:
        args = build_parser().parse_args(["ingest-providers", "--discovery-mode", "national"])
        assert args.discovery_mode == "national"

    def test_rejects_unknown_mode(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["ingest-providers", "--discovery-mode", "bogus"])
