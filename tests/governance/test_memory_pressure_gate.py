"""Slice 2 regression spine — MemoryPressureGate."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from backend.core.ouroboros.governance.memory_pressure_gate import (
    FanoutDecision,
    MEMORY_PRESSURE_SCHEMA_VERSION,
    MemoryPressureGate,
    MemoryProbe,
    PressureLevel,
    critical_fanout_cap,
    critical_threshold_pct,
    ensure_bridged,
    get_default_gate,
    high_fanout_cap,
    high_threshold_pct,
    is_enabled,
    reset_default_gate,
    warn_fanout_cap,
    warn_threshold_pct,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("JARVIS_MEMORY_PRESSURE"):
            monkeypatch.delenv(key, raising=False)
    reset_default_gate()
    yield
    reset_default_gate()


def _fake_probe(free_pct: float, source: str = "test") -> MemoryProbe:
    return MemoryProbe(
        free_pct=free_pct, total_bytes=16 * (1024 ** 3),
        available_bytes=int(free_pct * 16 * (1024 ** 3) / 100.0),
        source=source,
    )


# ---------------------------------------------------------------------------
# Enums + dataclass shape
# ---------------------------------------------------------------------------


class TestVocabulary:

    def test_pressure_level_4_values(self):
        assert set(PressureLevel) == {
            PressureLevel.OK, PressureLevel.WARN,
            PressureLevel.HIGH, PressureLevel.CRITICAL,
        }

    def test_memory_probe_frozen(self):
        p = _fake_probe(50.0)
        with pytest.raises((AttributeError, TypeError)):
            p.free_pct = 99

    def test_fanout_decision_frozen(self):
        d = FanoutDecision(
            allowed=True, n_requested=1, n_allowed=1, level=PressureLevel.OK,
            free_pct=50, reason_code="ok", source="test",
        )
        with pytest.raises((AttributeError, TypeError)):
            d.allowed = False

    def test_fanout_decision_to_dict_shape(self):
        d = FanoutDecision(
            allowed=True, n_requested=4, n_allowed=3,
            level=PressureLevel.HIGH, free_pct=15.0,
            reason_code="capped", source="psutil",
        )
        payload = d.to_dict()
        assert payload["schema_version"] == "1.0"
        assert payload["level"] == "high"
        assert payload["n_allowed"] == 3


# ---------------------------------------------------------------------------
# Level thresholds
# ---------------------------------------------------------------------------


class TestLevelThresholds:

    def test_ok_at_high_free(self):
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(50.0))
        assert gate.level_for_free_pct(50.0) is PressureLevel.OK

    def test_warn_between_30_and_20(self):
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(25.0))
        assert gate.level_for_free_pct(25.0) is PressureLevel.WARN

    def test_high_between_20_and_10(self):
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(15.0))
        assert gate.level_for_free_pct(15.0) is PressureLevel.HIGH

    def test_critical_below_10(self):
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(5.0))
        assert gate.level_for_free_pct(5.0) is PressureLevel.CRITICAL

    def test_exact_threshold_boundaries(self):
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(30.0))
        # 30% exact → OK (free_pct < warn_threshold_pct is the trigger)
        assert gate.level_for_free_pct(30.0) is PressureLevel.OK
        assert gate.level_for_free_pct(20.0) is PressureLevel.WARN
        assert gate.level_for_free_pct(10.0) is PressureLevel.HIGH

    def test_custom_thresholds(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_WARN_PCT", "50.0")
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_HIGH_PCT", "30.0")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(40.0))
        # Under custom thresholds, 40% is < 50% → WARN
        assert gate.level_for_free_pct(40.0) is PressureLevel.WARN


# ---------------------------------------------------------------------------
# pressure() + disabled flag
# ---------------------------------------------------------------------------


class TestPressure:

    def test_disabled_always_ok(self):
        # Master default false
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(5.0))
        assert gate.pressure() is PressureLevel.OK

    def test_enabled_uses_probe(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(5.0))
        assert gate.pressure() is PressureLevel.CRITICAL

    def test_probe_raises_yields_ok(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        def _boom():
            raise RuntimeError("boom")
        gate = MemoryPressureGate(probe_fn=_boom)
        # Resilient — swallows exception, returns OK
        assert gate.pressure() is PressureLevel.OK

    def test_probe_unreliable_yields_ok(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        bad = MemoryProbe(
            free_pct=0, total_bytes=0, available_bytes=0,
            source="x", ok=False, error="parse error",
        )
        gate = MemoryPressureGate(probe_fn=lambda: bad)
        assert gate.pressure() is PressureLevel.OK


# ---------------------------------------------------------------------------
# can_fanout matrix
# ---------------------------------------------------------------------------


class TestCanFanout:

    def test_disabled_unclamped(self):
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(5.0))
        d = gate.can_fanout(16)
        assert d.allowed is True
        assert d.n_allowed == 16
        assert d.reason_code == "memory_pressure_gate.disabled"

    def test_ok_level_unlimited(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(80.0))
        d = gate.can_fanout(16)
        assert d.level is PressureLevel.OK
        assert d.n_allowed == 16

    def test_warn_clamps_to_8(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(25.0))
        d = gate.can_fanout(16)
        assert d.level is PressureLevel.WARN
        assert d.n_allowed == 8

    def test_high_clamps_to_3(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(15.0))
        d = gate.can_fanout(16)
        assert d.level is PressureLevel.HIGH
        assert d.n_allowed == 3

    def test_critical_clamps_to_1(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(5.0))
        d = gate.can_fanout(16)
        assert d.level is PressureLevel.CRITICAL
        assert d.n_allowed == 1

    def test_n_allowed_never_exceeds_n_requested(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(25.0))  # WARN cap=8
        # Request < cap → returns n_requested, not cap
        d = gate.can_fanout(3)
        assert d.n_allowed == 3
        assert d.level is PressureLevel.WARN

    def test_custom_caps(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_HIGH_FANOUT_CAP", "5")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(15.0))
        d = gate.can_fanout(16)
        assert d.n_allowed == 5

    def test_probe_failure_falls_through(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        bad = MemoryProbe(
            free_pct=0, total_bytes=0, available_bytes=0,
            source="x", ok=False, error="e",
        )
        gate = MemoryPressureGate(probe_fn=lambda: bad)
        d = gate.can_fanout(16)
        assert d.allowed is True
        assert d.n_allowed == 16
        assert d.reason_code == "memory_pressure_gate.probe_unreliable"

    def test_probe_raises_returns_allow(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        def _boom():
            raise RuntimeError("bad")
        gate = MemoryPressureGate(probe_fn=_boom)
        d = gate.can_fanout(16)
        assert d.allowed is True

    def test_n_zero_degenerate(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(5.0))
        d = gate.can_fanout(0)
        assert d.n_requested == 0
        assert d.n_allowed == 0

    def test_reason_code_encodes_cap_and_level(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(15.0))
        d = gate.can_fanout(16)
        assert "capped_to_3_at_high" in d.reason_code


# ---------------------------------------------------------------------------
# Probe cascade behavior (mocked paths)
# ---------------------------------------------------------------------------


class TestProbeCascade:

    def test_default_gate_uses_cascade(self):
        gate = MemoryPressureGate()  # no probe_fn → cascade
        probe = gate.probe()
        # Must succeed with one of the sources (real system or fallback)
        assert probe.source in (
            "psutil", "proc_meminfo", "vm_stat", "fallback",
        )

    def test_fallback_always_ok(self, monkeypatch):
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            _probe_fallback,
        )
        p = _probe_fallback()
        assert p.source == "fallback"
        assert p.ok is True
        assert p.free_pct == 100.0

    def test_proc_meminfo_parser_happy(self, tmp_path, monkeypatch):
        """Simulate /proc/meminfo by monkey-patching os.path.exists + open."""
        from backend.core.ouroboros.governance import memory_pressure_gate as mpg
        fake_content = (
            "MemTotal:       16000000 kB\n"
            "MemFree:          500000 kB\n"
            "MemAvailable:    8000000 kB\n"
        )

        with patch.object(mpg.os.path, "exists", return_value=True), \
             patch("builtins.open", MagicMock()) as mock_open:
            mock_open.return_value.__enter__ = MagicMock(
                return_value=MagicMock(read=lambda: fake_content),
            )
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            probe = mpg._probe_proc_meminfo()
        assert probe is not None
        assert probe.source == "proc_meminfo"
        assert probe.ok is True
        # 8M / 16M = 50%
        assert probe.free_pct == pytest.approx(50.0, abs=0.1)

    def test_proc_meminfo_absent_returns_none(self, monkeypatch):
        from backend.core.ouroboros.governance import memory_pressure_gate as mpg
        with patch.object(mpg.os.path, "exists", return_value=False):
            assert mpg._probe_proc_meminfo() is None

    def test_vm_stat_non_darwin_returns_none(self, monkeypatch):
        from backend.core.ouroboros.governance import memory_pressure_gate as mpg
        with patch.object(mpg.sys, "platform", "linux"):
            assert mpg._probe_vm_stat() is None

    def test_vm_stat_subprocess_failure_records_error(self, monkeypatch):
        from backend.core.ouroboros.governance import memory_pressure_gate as mpg
        with patch.object(mpg.sys, "platform", "darwin"), \
             patch.object(mpg.subprocess, "run",
                          side_effect=FileNotFoundError("missing")):
            probe = mpg._probe_vm_stat()
            assert probe is not None
            assert probe.ok is False


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------


class TestSnapshot:

    def test_snapshot_shape(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(25.0))
        snap = gate.snapshot()
        assert snap["schema_version"] == "1.0"
        assert snap["enabled"] is True
        assert "probe" in snap
        assert "level" in snap
        assert "thresholds" in snap
        assert "fanout_caps" in snap

    def test_snapshot_reports_current_level(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(5.0))
        snap = gate.snapshot()
        assert snap["level"] == "critical"

    def test_snapshot_probe_raise_captured(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        def _boom():
            raise RuntimeError("boom")
        gate = MemoryPressureGate(probe_fn=_boom)
        snap = gate.snapshot()
        assert snap["ok"] is False
        assert "error" in snap

    def test_snapshot_disabled(self):
        gate = MemoryPressureGate(probe_fn=lambda: _fake_probe(50.0))
        snap = gate.snapshot()
        assert snap["enabled"] is False


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


class TestEnvHelpers:

    def test_is_enabled_default_false(self):
        assert is_enabled() is False

    def test_threshold_defaults(self):
        assert warn_threshold_pct() == 30.0
        assert high_threshold_pct() == 20.0
        assert critical_threshold_pct() == 10.0

    def test_fanout_cap_defaults(self):
        assert warn_fanout_cap() == 8
        assert high_fanout_cap() == 3
        assert critical_fanout_cap() == 1

    def test_schema_version_literal(self):
        assert MEMORY_PRESSURE_SCHEMA_VERSION == "1.0"


# ---------------------------------------------------------------------------
# Singleton + FlagRegistry bridge
# ---------------------------------------------------------------------------


class TestSingletonBridge:

    def test_default_gate_singleton(self):
        g1 = get_default_gate()
        g2 = get_default_gate()
        assert g1 is g2

    def test_reset(self):
        g1 = get_default_gate()
        reset_default_gate()
        g2 = get_default_gate()
        assert g1 is not g2

    def test_ensure_bridged_registers_flags_in_registry(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        from backend.core.ouroboros.governance.flag_registry import (
            ensure_seeded as _fr_seed, reset_default_registry,
        )
        reset_default_registry()
        reset_default_gate()
        ensure_bridged()
        fr = _fr_seed()
        assert fr.get_spec("JARVIS_MEMORY_PRESSURE_GATE_ENABLED") is not None
        assert fr.get_spec("JARVIS_MEMORY_PRESSURE_HIGH_FANOUT_CAP") is not None

    def test_ensure_bridged_idempotent(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        reset_default_gate()
        ensure_bridged()
        ensure_bridged()
        # No raise, no duplicate specs
        g = get_default_gate()
        assert g is not None


# ---------------------------------------------------------------------------
# Authority invariant
# ---------------------------------------------------------------------------


_AUTHORITY_MODULES = (
    "orchestrator", "policy", "iron_gate", "risk_tier",
    "change_engine", "candidate_generator", "gate",
)


class TestAuthorityInvariant:

    def test_arc_file_authority_free(self):
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (
            repo_root
            / "backend/core/ouroboros/governance/memory_pressure_gate.py"
        ).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for forbidden in _AUTHORITY_MODULES:
                    if f".{forbidden}" in line:
                        bad.append(line)
        assert not bad, f"memory_pressure_gate.py authority violations: {bad}"
