"""Atomic Write-Replace + Loop-Lag Watchdog spine.

Mandate 4 verbatim (2026-07-19): a filesystem write interrupted
midway through a ledger flush → the original .json Preference Ledger
remains completely intact + uncorrupted, and the state machine
recovers on the next tick.
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.comms.duplex import (
    sovereign_governor as sg,
)
from backend.core.ouroboros.governance.comms.duplex.preference_ledger import (
    PreferenceLedger,
)
from backend.core.ouroboros.governance.comms.duplex.sovereign_governor import (
    LoopLagWatchdog,
    loop_lag_degraded,
)


@pytest.fixture(autouse=True)
def _reset_lag():
    sg._LAG_DEGRADED["active"] = False
    yield
    sg._LAG_DEGRADED["active"] = False


class TestAtomicWriteReplace:
    def test_interrupted_write_leaves_original_intact(
        self, tmp_path, monkeypatch,
    ):
        """MANDATE 4 VERBATIM."""
        path = tmp_path / "preference_ledger.json"
        # Seed a KNOWN-GOOD ledger on disk.
        good = {"schema_version": "preference_ledger.1",
                "paths": [{"key": "abc", "strategy": "verified", "score": 0.9}],
                "stats": {}}
        path.write_text(json.dumps(good))
        original_bytes = path.read_bytes()

        ledger = PreferenceLedger(path=path)
        ledger._dirty = True
        ledger._paths.clear()

        # Interrupt the write MIDWAY: os.fsync raises (power loss during
        # flush). The tmp is being written; os.replace is NEVER reached.
        real_fsync = __import__("os").fsync
        def _boom_fsync(fd):
            raise OSError("simulated power loss mid-flush")
        monkeypatch.setattr("os.fsync", _boom_fsync)

        ledger._write_sync()                          # the interrupted flush

        # The ORIGINAL file is byte-for-byte intact — never touched:
        assert path.read_bytes() == original_bytes
        loaded = json.loads(path.read_text())         # still valid JSON
        assert loaded["paths"][0]["strategy"] == "verified"
        # No stray .tmp left behind:
        assert not (tmp_path / "preference_ledger.tmp").exists()

        # Recovery on the next tick: fsync restored, a clean write lands.
        monkeypatch.setattr("os.fsync", real_fsync)
        ledger._dirty = True
        ledger._write_sync()
        assert path.exists() and json.loads(path.read_text())  # recovered

    def test_successful_write_is_atomic_swap(self, tmp_path):
        path = tmp_path / "led.json"
        ledger = PreferenceLedger(path=path)
        ledger._dirty = True
        ledger._write_sync()
        assert path.exists()
        assert json.loads(path.read_text())["schema_version"] == \
            "preference_ledger.1"
        assert not path.with_suffix(".tmp").exists()  # tmp swapped away

    def test_uses_replace_not_append_pin(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] /
               "backend/core/ouroboros/governance/comms/duplex/"
               "preference_ledger.py").read_text()
        assert "os.replace(" in src                   # atomic swap
        assert "os.fsync(" in src                     # durable flush
        # Never opens the LIVE ledger in append mode:
        assert 'open(self._path, "a"' not in src


class TestLoopLagWatchdog:
    def test_spike_degrades_via_governor_discipline(self):
        published = []
        wd = LoopLagWatchdog(publish=published.append)
        assert loop_lag_degraded() is False
        wd.observe_lag_ms(120.0)                       # > 50ms threshold
        assert loop_lag_degraded() is True             # DEGRADED
        assert wd.state == "lag_degraded"
        assert published == ["lag_degraded"]
        assert wd.stats["degradations"] == 1
        # Loop settles → restored:
        wd.observe_lag_ms(2.0)
        assert loop_lag_degraded() is False
        assert wd.stats["restorations"] == 1

    def test_healthy_lag_never_degrades(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOOP_LAG_THRESHOLD_MS", "50")
        wd = LoopLagWatchdog()
        for lag in (1.0, 5.0, 12.0, 3.0):
            wd.observe_lag_ms(lag)
        assert loop_lag_degraded() is False
        assert wd.stats["degradations"] == 0

    def test_threshold_env_tunable(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOOP_LAG_THRESHOLD_MS", "200")
        wd = LoopLagWatchdog()
        wd.observe_lag_ms(120.0)                        # under new threshold
        assert loop_lag_degraded() is False

    def test_telemetry_ring_consults_watchdog_pin(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] /
               "backend/core/ouroboros/governance/residency_telemetry.py").read_text()
        assert "LoopLagWatchdog" in src
        assert "loop_lag_degraded" in src
        assert "throttled" in src                       # verbosity throttle

    def test_dry_shares_thermal_degradation_pattern_pin(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] /
               "backend/core/ouroboros/governance/comms/duplex/"
               "sovereign_governor.py").read_text()
        # Loop-lag lives in the SAME module as thermal (one degradation
        # hub, mandate 3) and mirrors the publish/state discipline.
        assert "class LoopLagWatchdog" in src
        assert "class ThermalGovernor" in src
