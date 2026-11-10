"""Smoke test: the package is installed and importable."""

import agent_monitor


def test_package_exposes_version() -> None:
    assert agent_monitor.__version__ == "0.1.0"
