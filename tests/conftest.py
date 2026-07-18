"""Shared fixtures for the ProviderMap test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow `import providermap` / `import adapters` without an editable install,
# matching how run.py locates them.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.providers.adventhealth.adapter import AdventHealthAdapter  # noqa: E402
from adapters.providers.adventhealth.fixtures import DATASET  # noqa: E402
from providermap.config import Config, load_config  # noqa: E402
from providermap.database import Database  # noqa: E402


@pytest.fixture()
def config() -> Config:
    """The real config.example.yaml, loaded fresh for each test."""
    return load_config(Path(__file__).resolve().parent.parent / "config.example.yaml")


@pytest.fixture()
def adapter(config: Config) -> AdventHealthAdapter:
    return AdventHealthAdapter(config)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """An empty database in a pytest tmp_path - never touches the real one."""
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture()
def fixture_dataset() -> dict:
    return DATASET
