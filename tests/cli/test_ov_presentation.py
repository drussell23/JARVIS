"""ov facade: cockpit action opts into COCKPIT presentation; run/daemon
force SOAK; the facade never does more than set env + delegate (Mandate 3)."""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.cli import ov as ov_cli


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_OV_PRESENTATION", raising=False)
    yield
    # ov.main() mutates os.environ DURING the test (mode declaration) —
    # monkeypatch can't see that write, so restore explicitly or the
    # mode leaks into every later suite in the process (the silent_boot
    # cockpit-threshold cross-pollution, 2026-07-18).
    import os as _os
    _os.environ.pop("JARVIS_OV_PRESENTATION", None)


def _capture_delegation(monkeypatch):
    seen = {}

    def fake_battle_main(argv):
        seen["argv"] = list(argv)
        seen["mode_env"] = os.environ.get("JARVIS_OV_PRESENTATION")

    import scripts.ouroboros_battle_test as bt
    monkeypatch.setattr(bt, "main", fake_battle_main)
    return seen


def test_cockpit_sets_cockpit_mode_before_delegating(monkeypatch):
    # Thin-Client Split (2026-07-18): bare `ov` no longer boots the
    # organism in-process by default — the legacy delegation contract
    # is preserved behind the master flag / --legacy-boot.
    monkeypatch.setenv("JARVIS_OV_THIN_CLIENT", "false")
    seen = _capture_delegation(monkeypatch)
    assert ov_cli.main([]) == 0
    assert seen["mode_env"] == "cockpit"
    assert seen["argv"] == []


def test_bare_ov_routes_thin_by_default(monkeypatch, tmp_path):
    # Default bare `ov` = presentation shell: no in-process delegation;
    # with no daemon and a failing spawner it degrades with exit 1 and
    # NEVER imports the bootstrap.
    monkeypatch.setenv(
        "JARVIS_ATTACH_IPC_SOCKET", str(tmp_path / "absent.sock"),
    )
    monkeypatch.setenv("JARVIS_OV_BOOT_WAIT_S", "5")
    seen = _capture_delegation(monkeypatch)
    from backend.core.ouroboros.cli import thin_client

    def _no_spawn(*a, **k):
        raise OSError("spawn disabled in unit test")

    monkeypatch.setattr(thin_client, "spawn_daemon", lambda **k: None)
    assert ov_cli.main([]) == 1
    assert "argv" not in seen or seen.get("argv") is None  # never delegated


def test_run_forces_soak(monkeypatch):
    seen = _capture_delegation(monkeypatch)
    assert ov_cli.main(["run", "--cost-cap", "1.00"]) == 0
    assert seen["mode_env"] == "soak"
    assert seen["argv"] == ["--headless", "--cost-cap", "1.00"]


def test_daemon_forces_soak(monkeypatch):
    seen = _capture_delegation(monkeypatch)
    assert ov_cli.main(["daemon"]) == 0
    assert seen["mode_env"] == "soak"
