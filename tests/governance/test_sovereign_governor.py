"""Sovereign Governor spine — thermal degradation + threshold auto-tune.

Mandate 4 verbatim (2026-07-19): a mocked
NSProcessInfoThermalStateSerious notification → event caught, Rolling
Biometric Evolution bypassed in the FSM, and the jarvis thin-client
state sync reflects the degraded hardware posture.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from backend.core.ouroboros.governance.comms.duplex import sovereign_governor as sg
from backend.core.ouroboros.governance.comms.duplex.sovereign_governor import (
    ThermalGovernor,
    ThresholdAutoTuner,
)


@pytest.fixture(autouse=True)
def _reset():
    sg._DEGRADED["active"] = False
    yield
    sg._DEGRADED["active"] = False


class _FakeSentry:
    chunk_stride = 1


class TestThermalGovernor:
    def test_serious_notification_degrades_and_syncs_topology(self):
        """MANDATE 4 VERBATIM."""
        from backend.core.ouroboros.cli.jarvis_thin import TopologyMap
        thermal_raw = {"v": 0}
        published = []
        sentry = _FakeSentry()
        topo = TopologyMap()

        def _publish(state):
            published.append(state)
            topo.on_thermal(state)          # the thin-client state sync

        gov = ThermalGovernor(
            sentry=sentry, publish_thermal=_publish,
            thermal_source=lambda: thermal_raw["v"],
        )
        # The mocked NSProcessInfoThermalStateSerious notification:
        thermal_raw["v"] = 2
        gov.on_thermal_change()
        assert sg.evolution_permitted() is False        # evolution bypassed
        assert sentry.chunk_stride >= 2                 # DSP load shed
        assert published == ["serious"]                 # event bus caught it
        assert "[THERMAL DEGRADATION ACTIVE]" in topo.render()  # jarvis sync
        # Nominal restores everything automatically:
        thermal_raw["v"] = 0
        gov.on_thermal_change()
        assert sg.evolution_permitted() is True
        assert sentry.chunk_stride == 1
        assert "[THERMAL DEGRADATION ACTIVE]" not in topo.render()
        assert gov.stats == {"degradations": 1, "restorations": 1}

    def test_critical_also_degrades_fair_does_not(self):
        raw = {"v": 1}
        gov = ThermalGovernor(thermal_source=lambda: raw["v"])
        gov.on_thermal_change()
        assert sg.evolution_permitted() is True         # fair = full power
        raw["v"] = 3
        gov.on_thermal_change()
        assert sg.evolution_permitted() is False        # critical sheds

    def test_evolution_hook_consults_governor_pin(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/comms/duplex/sentry_bootstrap.py"
        ).read_text()
        assert "evolution_permitted()" in src
        assert "tuned_threshold(0.70)" in src

    def test_sentry_stride_sheds_passive_evaluation(self):
        from backend.core.ouroboros.governance.comms.duplex.passive_sentry import (  # noqa: E501
            PassiveSentry,
        )

        class _CountingGate:
            fed = 0
            def feed(self, _c):
                _CountingGate.fed += 1
                return None
            def close_window(self):
                pass

        s = PassiveSentry(gate=_CountingGate())
        s.chunk_stride = 4
        for _ in range(40):
            s.feed_chunk(np.zeros(480, dtype=np.float32))
        assert _CountingGate.fed == 10                  # every 4th only


class TestThresholdAutoTuner:
    def test_rejection_cluster_lowers_boundary(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_TIER1_VBIA_THRESHOLD", raising=False)
        t = ThresholdAutoTuner(state_path=tmp_path / "tune.json")
        assert t.calibrating() is True
        for _ in range(5):
            t.record(0.66, verified=False)             # just under 0.70
        assert t.threshold < 0.70                       # EMA-lowered
        assert t.stats["lowered"] == 1
        saved = json.loads((tmp_path / "tune.json").read_text())
        assert saved["threshold"] == round(t.threshold, 4)

    def test_unvalidated_storm_raises_boundary(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_TIER1_VBIA_THRESHOLD", raising=False)
        t = ThresholdAutoTuner(state_path=tmp_path / "tune.json")
        for _ in range(25):
            t.record(0.2, verified=False)              # noise, never near
        assert t.threshold > 0.70
        assert t.stats["raised"] == 1

    def test_hard_clamps_hold(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_TIER1_VBIA_THRESHOLD", raising=False)
        t = ThresholdAutoTuner(state_path=tmp_path / "tune.json")
        for _ in range(40):
            for _ in range(5):
                t.record(0.10, verified=False)
        assert t.threshold <= 0.90
        t2 = ThresholdAutoTuner(state_path=tmp_path / "t2.json")
        t2.threshold = 0.56
        for _ in range(10):
            for _ in range(5):
                t2.record(0.50, verified=False)
        assert t2.threshold >= 0.55

    def test_env_override_always_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_BIOMETRIC_TUNE_FILE", str(tmp_path / "tune.json"),
        )
        (tmp_path / "tune.json").write_text(
            json.dumps({"threshold": 0.60}),
        )
        monkeypatch.setenv("JARVIS_TIER1_VBIA_THRESHOLD", "0.82")
        assert sg.tuned_threshold() == 0.82             # operator sovereign
        monkeypatch.delenv("JARVIS_TIER1_VBIA_THRESHOLD")
        assert sg.tuned_threshold() == 0.60             # tuner otherwise

    def test_calibration_window_expires(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_TIER1_VBIA_THRESHOLD", raising=False)
        p = tmp_path / "tune.json"
        p.write_text(json.dumps({
            "threshold": 0.70, "ignited_at": 1000.0,   # long ago
        }))
        t = ThresholdAutoTuner(state_path=p)
        assert t.calibrating() is False
        for _ in range(5):
            t.record(0.66, verified=False)
        assert t.threshold == 0.70                      # telemetry-only now
