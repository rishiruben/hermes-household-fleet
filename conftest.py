"""Test bootstrap.

The repository root *is* the plugin package (``__init__.py`` lives here, as the Hermes plugin
loader requires), which makes the test harness slightly delicate: under pytest, importing the root
as a package resolves ``__package__`` to ``"__init__"`` and every relative import inside the plugin
fails. So the suite builds the package explicitly — a module with ``__path__`` pointing at the repo
root, registered in ``sys.modules`` — and then imports the submodules normally. That is exactly how
Hermes resolves them at runtime, minus the entry-point execution (which
``hermes plugins validate`` / ``doctor`` cover).

``collect_ignore`` keeps pytest from collecting the plugin's own ``__init__.py`` and the throwaway
virtualenv.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
PACKAGE = "household_fleet"

collect_ignore = ["__init__.py", ".venv"]

_SUBMODULES = ("config", "ledger", "gate", "ha_mirror", "tools", "prompt")


def _load_plugin_package() -> types.ModuleType:
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    package = types.ModuleType(PACKAGE)
    package.__package__ = PACKAGE
    package.__path__ = [str(ROOT)]  # where importlib looks for the submodules
    sys.modules[PACKAGE] = package
    for submodule in _SUBMODULES:
        importlib.import_module(f"{PACKAGE}.{submodule}")
    return package


@pytest.fixture(scope="session")
def hf() -> types.ModuleType:
    """The plugin package, imported the way Hermes imports it."""
    return _load_plugin_package()


@pytest.fixture()
def ledger(hf, tmp_path):
    """A ledger backed by a throwaway directory, so no test touches real state."""
    return hf.ledger.Ledger.load(tmp_path / "comms")


@pytest.fixture()
def config(hf):
    return hf.config.GateConfig(timezone="America/New_York")


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so the plugin never reads the developer's real state."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("FLEET_COMMS_HOME", str(home / "fleet" / "comms"))
    return home
