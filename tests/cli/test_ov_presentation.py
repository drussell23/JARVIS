"""ov facade: cockpit action opts into COCKPIT presentation; run/daemon
force SOAK; the facade never does more than set env + delegate (Mandate 3)."""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.cli import ov as ov_cli


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_OV_PRESENTATION", raising=False)


def _capture_delegation(monkeypatch):
    seen = {}

    def fake_battle_main(argv):
        seen["argv"] = list(argv)
        seen["mode_env"] = os.environ.get("JARVIS_OV_PRESENTATION")

    import scripts.ouroboros_battle_test as bt
    monkeypatch.setattr(bt, "main", fake_battle_main)
    return seen


def test_cockpit_sets_cockpit_mode_before_delegating(monkeypatch):
    seen = _capture_delegation(monkeypatch)
    assert ov_cli.main([]) == 0
    assert seen["mode_env"] == "cockpit"
    assert seen["argv"] == []


def test_run_forces_soak(monkeypatch):
    seen = _capture_delegation(monkeypatch)
    assert ov_cli.main(["run", "--cost-cap", "1.00"]) == 0
    assert seen["mode_env"] == "soak"
    assert seen["argv"] == ["--headless", "--cost-cap", "1.00"]


def test_daemon_forces_soak(monkeypatch):
    seen = _capture_delegation(monkeypatch)
    assert ov_cli.main(["daemon"]) == 0
    assert seen["mode_env"] == "soak"
