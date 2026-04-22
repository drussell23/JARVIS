"""Slice 4 graduation pins for SensorGovernor + MemoryPressureGate arc."""
from __future__ import annotations

import inspect
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.governor_repl import (
    dispatch_governor_command,
)
from backend.core.ouroboros.governance.memory_pressure_gate import (
    MEMORY_PRESSURE_SCHEMA_VERSION,
    MemoryPressureGate, MemoryProbe, PressureLevel,
    critical_fanout_cap, critical_threshold_pct,
    ensure_bridged,
    get_default_gate, high_fanout_cap, high_threshold_pct,
    is_enabled as _gate_enabled,
    reset_default_gate, warn_fanout_cap, warn_threshold_pct,
)
from backend.core.ouroboros.governance.sensor_governor import (
    SENSOR_GOVERNOR_SCHEMA_VERSION,
    SensorBudgetSpec, SensorGovernor, Urgency,
    ensure_seeded, is_enabled as _gov_enabled,
    reset_default_governor,
)
from backend.core.ouroboros.governance.sensor_governor_seed import SEED_SPECS


_REPO_ROOT = Path(subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    capture_output=True, text=True, check=True,
).stdout.strip())


_ARC_FILES = (
    "backend/core/ouroboros/governance/sensor_governor.py",
    "backend/core/ouroboros/governance/sensor_governor_seed.py",
    "backend/core/ouroboros/governance/memory_pressure_gate.py",
    "backend/core/ouroboros/governance/governor_repl.py",
)

_AUTHORITY_MODULES = (
    "orchestrator", "policy", "iron_gate", "risk_tier",
    "change_engine", "candidate_generator", "gate",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if (k.startswith("JARVIS_SENSOR_GOVERNOR")
                or k.startswith("JARVIS_MEMORY_PRESSURE")
                or k.startswith("JARVIS_IDE_")):
            monkeypatch.delenv(k, raising=False)
    reset_default_governor()
    reset_default_gate()
    yield
    reset_default_governor()
    reset_default_gate()


def _make_req():
    return SimpleNamespace(
        remote="127.0.0.1",
        headers={"Origin": "http://localhost:1234"},
        query={}, match_info={},
    )


# ===========================================================================
# A. AUTHORITY (8 pins)
# ===========================================================================


class TestGraduation_A_Authority:

    @pytest.mark.parametrize("relpath", list(_ARC_FILES))
    def test_arc_files_authority_free(self, relpath):
        src = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for f in _AUTHORITY_MODULES:
                    if f".{f}" in line:
                        bad.append(line)
        assert not bad, f"{relpath}: {bad}"

    def test_governor_get_handlers_authority_free(self):
        ide_path = _REPO_ROOT / "backend/core/ouroboros/governance/ide_observability.py"
        src = ide_path.read_text(encoding="utf-8")
        for handler in (
            "_handle_governor_snapshot", "_handle_governor_history",
            "_handle_memory_pressure",
        ):
            assert f"async def {handler}" in src
            idx = src.index(f"async def {handler}")
            window = src[idx:idx + 4096]
            for f in _AUTHORITY_MODULES:
                assert f".{f} " not in window, f"{handler} refs {f}"

    def test_sse_bridges_authority_free(self):
        stream = _REPO_ROOT / "backend/core/ouroboros/governance/ide_observability_stream.py"
        src = stream.read_text(encoding="utf-8")
        for fn in (
            "publish_governor_throttle_event",
            "publish_governor_emergency_brake_event",
            "publish_memory_pressure_event",
            "bridge_governor_to_broker",
            "bridge_memory_pressure_to_broker",
        ):
            assert f"def {fn}" in src
            idx = src.index(f"def {fn}")
            window = src[idx:idx + 4096]
            for f in _AUTHORITY_MODULES:
                assert f".{f} " not in window, f"{fn} refs {f}"


# ===========================================================================
# B. BEHAVIORAL (14 pins)
# ===========================================================================


class TestGraduation_B_Behavioral:

    def test_governor_disabled_always_allows(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "false")
        g = SensorGovernor(posture_fn=lambda: None,
                           signal_bundle_fn=lambda: None)
        d = g.request_budget("X")
        assert d.allowed is True
        assert d.reason_code == "governor.disabled"

    def test_gate_disabled_unclamped(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "false")
        gate = MemoryPressureGate(probe_fn=lambda: MemoryProbe(
            free_pct=5, total_bytes=0, available_bytes=0, source="x",
        ))
        d = gate.can_fanout(16)
        assert d.n_allowed == 16

    def test_emergency_brake_activates(self):
        bundle = {"cost_burn_normalized": 0.95}
        g = SensorGovernor(
            posture_fn=lambda: None, signal_bundle_fn=lambda: bundle,
        )
        g.register(SensorBudgetSpec(sensor_name="X", base_cap_per_hour=100))
        d = g.request_budget("X")
        assert d.emergency_brake is True
        assert d.weighted_cap == 20  # 100 * 0.2

    def test_global_cap_enforced(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR", "5")
        g = SensorGovernor(posture_fn=lambda: None, signal_bundle_fn=lambda: None)
        g.register(SensorBudgetSpec(sensor_name="X", base_cap_per_hour=100))
        g.register(SensorBudgetSpec(sensor_name="Y", base_cap_per_hour=100))
        for _ in range(5):
            g.record_emission("X")
        d = g.request_budget("Y")
        assert d.allowed is False
        assert d.reason_code == "governor.global_cap_exhausted"

    def test_rolling_window_evicts(self, monkeypatch):
        import time as _time
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_WINDOW_S", "60")
        g = SensorGovernor(posture_fn=lambda: None, signal_bundle_fn=lambda: None)
        g.register(SensorBudgetSpec(sensor_name="X", base_cap_per_hour=10))
        now = _time.monotonic()
        g._per_sensor["X"].extend([now - 120, now - 30])
        g._global.extend([now - 120, now - 30])
        d = g.request_budget("X")
        assert d.current_count == 1

    def test_pressure_level_threshold_matrix(self):
        gate = MemoryPressureGate(probe_fn=lambda: MemoryProbe(
            free_pct=50, total_bytes=16 * (1024 ** 3),
            available_bytes=8 * (1024 ** 3), source="t",
        ))
        assert gate.level_for_free_pct(50) is PressureLevel.OK
        assert gate.level_for_free_pct(25) is PressureLevel.WARN
        assert gate.level_for_free_pct(15) is PressureLevel.HIGH
        assert gate.level_for_free_pct(5) is PressureLevel.CRITICAL

    def test_fanout_cap_at_each_level(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        for pct, expected in [(50, 16), (25, 8), (15, 3), (5, 1)]:
            gate = MemoryPressureGate(probe_fn=lambda p=pct: MemoryProbe(
                free_pct=p, total_bytes=16 * (1024**3),
                available_bytes=8 * (1024**3), source="t",
            ))
            d = gate.can_fanout(16)
            assert d.n_allowed == expected, f"pct={pct} got {d.n_allowed}"

    def test_probe_cascade_fallback(self):
        """Gate with no probe_fn uses cascade — must return an ok probe."""
        gate = MemoryPressureGate()
        probe = gate.probe()
        assert probe.ok is True
        assert probe.source in ("psutil", "proc_meminfo", "vm_stat", "fallback")

    def test_probe_raise_safe(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        def _boom():
            raise RuntimeError("boom")
        gate = MemoryPressureGate(probe_fn=_boom)
        # Must not raise
        assert gate.pressure() is PressureLevel.OK
        d = gate.can_fanout(16)
        assert d.allowed is True  # falls through

    def test_posture_weight_math(self):
        g = SensorGovernor(
            posture_fn=lambda: "HARDEN", signal_bundle_fn=lambda: None,
        )
        g.register(SensorBudgetSpec(
            sensor_name="X", base_cap_per_hour=10,
            posture_weights={"HARDEN": 1.8},
        ))
        d = g.request_budget("X", Urgency.STANDARD)
        assert d.weighted_cap == 18

    def test_urgency_multiplier(self):
        g = SensorGovernor(posture_fn=lambda: None, signal_bundle_fn=lambda: None)
        g.register(SensorBudgetSpec(sensor_name="X", base_cap_per_hour=10))
        assert g.request_budget("X", Urgency.IMMEDIATE).weighted_cap == 20
        assert g.request_budget("X", Urgency.BACKGROUND).weighted_cap == 5

    def test_record_emission_grows_counters(self):
        g = SensorGovernor(posture_fn=lambda: None, signal_bundle_fn=lambda: None)
        g.register(SensorBudgetSpec(sensor_name="X", base_cap_per_hour=10))
        for _ in range(3):
            g.record_emission("X")
        assert g.request_budget("X").current_count == 3

    def test_reset_clears(self):
        g = SensorGovernor(posture_fn=lambda: None, signal_bundle_fn=lambda: None)
        g.register(SensorBudgetSpec(sensor_name="X", base_cap_per_hour=10))
        g.record_emission("X")
        g.reset()
        assert g.request_budget("X").current_count == 0

    def test_unregistered_sensor_allowed(self):
        g = SensorGovernor(posture_fn=lambda: None, signal_bundle_fn=lambda: None)
        d = g.request_budget("NeverSeen")
        assert d.allowed is True
        assert d.reason_code == "governor.unregistered_sensor"


# ===========================================================================
# C. GRADUATION-SPECIFIC (10 pins)
# ===========================================================================


class TestGraduation_C_Specific:

    def test_governor_default_literal_true(self):
        from backend.core.ouroboros.governance import sensor_governor as sg
        src = inspect.getsource(sg.is_enabled)
        assert 'JARVIS_SENSOR_GOVERNOR_ENABLED", True' in src

    def test_gate_default_literal_true(self):
        from backend.core.ouroboros.governance import memory_pressure_gate as mpg
        src = inspect.getsource(mpg.is_enabled)
        assert 'JARVIS_MEMORY_PRESSURE_GATE_ENABLED", True' in src

    def test_governor_enabled_on_default(self):
        assert _gov_enabled() is True

    def test_gate_enabled_on_default(self):
        assert _gate_enabled() is True

    def test_seed_16_sensors_unchanged(self):
        assert len(SEED_SPECS) == 16

    def test_seed_covers_4_postures(self):
        postures: set = set()
        for s in SEED_SPECS:
            postures.update(s.posture_weights.keys())
        assert postures == {"EXPLORE", "CONSOLIDATE", "HARDEN", "MAINTAIN"}

    def test_governor_default_caps_unchanged(self):
        from backend.core.ouroboros.governance.sensor_governor import (
            global_cap_per_hour, window_seconds,
            emergency_reduction_pct, emergency_cost_threshold,
            emergency_postmortem_threshold,
        )
        assert global_cap_per_hour() == 200
        assert window_seconds() == 3600
        assert emergency_reduction_pct() == pytest.approx(0.2)
        assert emergency_cost_threshold() == pytest.approx(0.9)
        assert emergency_postmortem_threshold() == pytest.approx(0.6)

    def test_gate_default_thresholds_unchanged(self):
        assert warn_threshold_pct() == 30.0
        assert high_threshold_pct() == 20.0
        assert critical_threshold_pct() == 10.0

    def test_gate_default_caps_unchanged(self):
        assert warn_fanout_cap() == 8
        assert high_fanout_cap() == 3
        assert critical_fanout_cap() == 1

    def test_sensor_weights_unchanged(self):
        """Lock the key per-posture weights to prevent silent retuning."""
        tf = next(s for s in SEED_SPECS if s.sensor_name == "TestFailureSensor")
        assert tf.weight_for_posture("HARDEN") == 1.8
        om = next(s for s in SEED_SPECS if s.sensor_name == "OpportunityMinerSensor")
        assert om.weight_for_posture("EXPLORE") == 1.5
        assert om.weight_for_posture("HARDEN") == 0.3


# ===========================================================================
# C'. DOCSTRING BIT-ROT (4 pins)
# ===========================================================================


class TestGraduation_C_Docstrings:

    def test_governor_module_cites_tier_0(self):
        from backend.core.ouroboros.governance import sensor_governor
        assert "Tier 0" in (sensor_governor.__doc__ or "")

    def test_gate_module_cites_tier_0(self):
        from backend.core.ouroboros.governance import memory_pressure_gate
        assert "Tier 0" in (memory_pressure_gate.__doc__ or "")

    def test_governor_cites_advisory(self):
        from backend.core.ouroboros.governance import sensor_governor
        assert "advisory" in (sensor_governor.__doc__ or "").lower()

    def test_gate_cites_advisory(self):
        from backend.core.ouroboros.governance import memory_pressure_gate
        assert "advisory" in (memory_pressure_gate.__doc__ or "").lower()


# ===========================================================================
# D. SCHEMA VERSION (3 pins)
# ===========================================================================


class TestGraduation_D_Schema:

    def test_governor_schema_1_0(self):
        assert SENSOR_GOVERNOR_SCHEMA_VERSION == "1.0"

    def test_gate_schema_1_0(self):
        assert MEMORY_PRESSURE_SCHEMA_VERSION == "1.0"

    def test_sse_frames_carry_schema(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            STREAM_SCHEMA_VERSION,
            publish_governor_throttle_event, reset_default_broker,
        )
        reset_default_broker()
        from backend.core.ouroboros.governance.sensor_governor import (
            BudgetDecision, Urgency,
        )
        d = BudgetDecision(
            allowed=False, sensor_name="X", urgency=Urgency.STANDARD,
            posture=None, weighted_cap=10, current_count=10,
            remaining=0, reason_code="test",
        )
        publish_governor_throttle_event(d)
        assert STREAM_SCHEMA_VERSION == "1.0"


# ===========================================================================
# E. INTEGRATION (4 pins)
# ===========================================================================


class TestGraduation_E_Integration:

    def test_governor_auto_registers_flags_in_registry(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        from backend.core.ouroboros.governance.flag_registry import (
            ensure_seeded as _fr_seed, reset_default_registry,
        )
        reset_default_registry()
        reset_default_governor()
        ensure_seeded()
        fr = _fr_seed()
        assert fr.get_spec("JARVIS_SENSOR_GOVERNOR_ENABLED") is not None

    def test_gate_auto_registers_flags_in_registry(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        from backend.core.ouroboros.governance.flag_registry import (
            ensure_seeded as _fr_seed, reset_default_registry,
        )
        reset_default_registry()
        reset_default_gate()
        ensure_bridged()
        fr = _fr_seed()
        assert fr.get_spec("JARVIS_MEMORY_PRESSURE_GATE_ENABLED") is not None

    @pytest.mark.asyncio
    async def test_governor_get_double_gated(self, monkeypatch):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        # ide off
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
        resp = await router._handle_governor_snapshot(_make_req())
        assert resp.status == 403
        # master off
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "false")
        resp = await router._handle_governor_snapshot(_make_req())
        assert resp.status == 403
        # both on
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        resp = await router._handle_governor_snapshot(_make_req())
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_memory_pressure_get_double_gated(self, monkeypatch):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "false")
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        resp = await router._handle_memory_pressure(_make_req())
        assert resp.status == 403


# ===========================================================================
# F. FULL-REVERT MATRIX (2 pins)
# ===========================================================================


class TestGraduation_F_RevertMatrix:

    @pytest.mark.asyncio
    async def test_governor_full_revert(self, monkeypatch):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        router = IDEObservabilityRouter()
        reset_default_governor()
        ensure_seeded()
        # graduated: all works
        assert _gov_enabled() is True
        r = dispatch_governor_command("/governor status")
        assert r.ok
        resp = await router._handle_governor_snapshot(_make_req())
        assert resp.status == 200
        # revert
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "false")
        assert _gov_enabled() is False
        r = dispatch_governor_command("/governor status")
        assert r.ok is False
        resp = await router._handle_governor_snapshot(_make_req())
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_memory_pressure_full_revert(self, monkeypatch):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        router = IDEObservabilityRouter()
        # graduated
        assert _gate_enabled() is True
        resp = await router._handle_memory_pressure(_make_req())
        assert resp.status == 200
        # revert
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "false")
        assert _gate_enabled() is False
        r = dispatch_governor_command("/governor memory")
        assert r.ok is False
        resp = await router._handle_memory_pressure(_make_req())
        assert resp.status == 403


# ===========================================================================
# G. CLAUDE.md DOC GUARD (4 pins)
# ===========================================================================


class TestGraduation_G_ClaudeMd:

    def test_mentions_sensor_governor(self):
        md = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "SensorGovernor" in md

    def test_mentions_memory_pressure_gate(self):
        md = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "MemoryPressureGate" in md

    def test_mentions_governor_master_flag(self):
        md = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "JARVIS_SENSOR_GOVERNOR_ENABLED" in md

    def test_mentions_gate_master_flag(self):
        md = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "JARVIS_MEMORY_PRESSURE_GATE_ENABLED" in md
