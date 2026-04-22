"""Slice 1 regression spine — SensorGovernor + 16-sensor seed."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.sensor_governor import (
    BudgetDecision,
    SENSOR_GOVERNOR_SCHEMA_VERSION,
    SensorBudgetSpec,
    SensorGovernor,
    Urgency,
    emergency_cost_threshold,
    emergency_postmortem_threshold,
    emergency_reduction_pct,
    ensure_seeded,
    get_default_governor,
    global_cap_per_hour,
    is_enabled,
    reset_default_governor,
    window_seconds,
)
from backend.core.ouroboros.governance.sensor_governor_seed import (
    SEED_SPECS,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("JARVIS_SENSOR_GOVERNOR"):
            monkeypatch.delenv(key, raising=False)
    reset_default_governor()
    yield
    reset_default_governor()


@pytest.fixture
def governor(monkeypatch) -> SensorGovernor:
    """Master flag on, empty governor (no seed)."""
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    return SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
    )


def _spec(
    name: str = "TestSensor",
    cap: int = 10,
    weights: dict = None,
) -> SensorBudgetSpec:
    return SensorBudgetSpec(
        sensor_name=name, base_cap_per_hour=cap,
        posture_weights=weights or {},
    )


# ---------------------------------------------------------------------------
# SensorBudgetSpec shape + immutability
# ---------------------------------------------------------------------------


class TestSpec:

    def test_frozen_immutable(self):
        s = _spec()
        with pytest.raises((AttributeError, TypeError)):
            s.base_cap_per_hour = 99

    def test_weight_for_posture_defaults_1(self):
        s = _spec(weights={"HARDEN": 1.8})
        assert s.weight_for_posture("HARDEN") == 1.8
        assert s.weight_for_posture("EXPLORE") == 1.0  # missing → 1.0
        assert s.weight_for_posture(None) == 1.0

    def test_weight_case_insensitive(self):
        s = _spec(weights={"HARDEN": 1.8})
        assert s.weight_for_posture("harden") == 1.8

    def test_urgency_multiplier_defaults(self):
        s = _spec()
        # Default table: IMMEDIATE=2.0, STANDARD=1.0, BACKGROUND=0.5
        assert s.urgency_mult(Urgency.IMMEDIATE) == 2.0
        assert s.urgency_mult(Urgency.STANDARD) == 1.0
        assert s.urgency_mult(Urgency.BACKGROUND) == 0.5
        assert s.urgency_mult(Urgency.SPECULATIVE) == 0.3

    def test_urgency_override_in_spec(self):
        s = SensorBudgetSpec(
            sensor_name="X", base_cap_per_hour=10,
            urgency_multipliers={"immediate": 5.0},
        )
        assert s.urgency_mult(Urgency.IMMEDIATE) == 5.0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:

    def test_register_then_lookup(self, governor):
        governor.register(_spec("X"))
        assert governor.get_spec("X") is not None

    def test_register_rejects_wrong_type(self, governor):
        with pytest.raises(TypeError):
            governor.register("not-a-spec")

    def test_override_default(self, governor):
        governor.register(_spec("X", cap=10))
        governor.register(_spec("X", cap=99))
        assert governor.get_spec("X").base_cap_per_hour == 99

    def test_override_false_raises(self, governor):
        governor.register(_spec("X"))
        with pytest.raises(ValueError):
            governor.register(_spec("X"), override=False)

    def test_bulk_register(self, governor):
        governor.bulk_register([_spec(f"S{i}") for i in range(5)])
        assert len(governor.list_specs()) == 5


# ---------------------------------------------------------------------------
# Budget decisions
# ---------------------------------------------------------------------------


class TestBudgetDecisions:

    def test_disabled_master_always_allows(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "false")
        g = SensorGovernor(posture_fn=lambda: None,
                           signal_bundle_fn=lambda: None)
        d = g.request_budget("any", Urgency.STANDARD)
        assert d.allowed is True
        assert d.reason_code == "governor.disabled"

    def test_unregistered_sensor_allowed_with_code(self, governor):
        d = governor.request_budget("NeverRegistered", Urgency.STANDARD)
        assert d.allowed is True
        assert d.reason_code == "governor.unregistered_sensor"

    def test_under_cap_allowed(self, governor):
        governor.register(_spec("X", cap=5))
        d = governor.request_budget("X", Urgency.STANDARD)
        assert d.allowed is True
        assert d.reason_code == "governor.ok"
        assert d.weighted_cap == 5
        assert d.remaining == 5

    def test_at_cap_denied(self, governor):
        governor.register(_spec("X", cap=3))
        for _ in range(3):
            governor.record_emission("X")
        d = governor.request_budget("X", Urgency.STANDARD)
        assert d.allowed is False
        assert d.reason_code == "governor.sensor_cap_exhausted"
        assert d.remaining == 0

    def test_global_cap_exhausted(self, governor, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR", "5")
        governor.register(_spec("X", cap=100))
        governor.register(_spec("Y", cap=100))
        for _ in range(5):
            governor.record_emission("X")
        d = governor.request_budget("Y", Urgency.STANDARD)
        assert d.allowed is False
        assert d.reason_code == "governor.global_cap_exhausted"

    def test_decision_records_posture(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        g = SensorGovernor(
            posture_fn=lambda: "HARDEN",
            signal_bundle_fn=lambda: None,
        )
        g.register(_spec("X"))
        d = g.request_budget("X")
        assert d.posture == "HARDEN"


# ---------------------------------------------------------------------------
# Posture weighting
# ---------------------------------------------------------------------------


class TestPostureWeighting:

    def test_weighted_cap_respects_posture(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        g = SensorGovernor(posture_fn=lambda: "HARDEN", signal_bundle_fn=lambda: None)
        g.register(_spec("X", cap=10, weights={"HARDEN": 1.8}))
        d = g.request_budget("X", Urgency.STANDARD)
        # 10 * 1.8 * 1.0 = 18
        assert d.weighted_cap == 18

    def test_urgency_multiplier_applied(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        g = SensorGovernor(posture_fn=lambda: None, signal_bundle_fn=lambda: None)
        g.register(_spec("X", cap=10))
        d_imm = g.request_budget("X", Urgency.IMMEDIATE)
        d_bg = g.request_budget("X", Urgency.BACKGROUND)
        # IMMEDIATE = 2.0x, BACKGROUND = 0.5x
        assert d_imm.weighted_cap == 20
        assert d_bg.weighted_cap == 5

    def test_missing_posture_no_effect(self, governor):
        governor.register(_spec("X", cap=10, weights={"HARDEN": 1.8}))
        # posture_fn returns None → weight=1.0
        d = governor.request_budget("X")
        assert d.weighted_cap == 10


# ---------------------------------------------------------------------------
# Emergency brake
# ---------------------------------------------------------------------------


class TestEmergencyBrake:

    def test_brake_inactive_by_default(self, governor):
        governor.register(_spec("X", cap=100))
        d = governor.request_budget("X")
        assert d.emergency_brake is False
        assert d.weighted_cap == 100

    def test_brake_on_high_cost_burn(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        bundle = {"cost_burn_normalized": 0.95, "postmortem_failure_rate": 0.0}
        g = SensorGovernor(
            posture_fn=lambda: None, signal_bundle_fn=lambda: bundle,
        )
        g.register(_spec("X", cap=100))
        d = g.request_budget("X")
        assert d.emergency_brake is True
        # 100 * 0.2 = 20
        assert d.weighted_cap == 20

    def test_brake_on_high_postmortem(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        bundle = {"cost_burn_normalized": 0.0, "postmortem_failure_rate": 0.8}
        g = SensorGovernor(
            posture_fn=lambda: None, signal_bundle_fn=lambda: bundle,
        )
        g.register(_spec("X", cap=100))
        d = g.request_budget("X")
        assert d.emergency_brake is True

    def test_brake_below_thresholds(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        bundle = {"cost_burn_normalized": 0.5, "postmortem_failure_rate": 0.3}
        g = SensorGovernor(
            posture_fn=lambda: None, signal_bundle_fn=lambda: bundle,
        )
        g.register(_spec("X", cap=100))
        d = g.request_budget("X")
        assert d.emergency_brake is False

    def test_brake_malformed_bundle_silent(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        g = SensorGovernor(
            posture_fn=lambda: None,
            signal_bundle_fn=lambda: {"cost_burn_normalized": "bad"},
        )
        g.register(_spec("X", cap=100))
        d = g.request_budget("X")
        # Malformed bundle → brake treated as inactive
        assert d.emergency_brake is False

    def test_brake_reduces_global_cap_too(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR", "100")
        bundle = {"cost_burn_normalized": 0.95}
        g = SensorGovernor(
            posture_fn=lambda: None, signal_bundle_fn=lambda: bundle,
        )
        g.register(_spec("X"))
        d = g.request_budget("X")
        # global 100 * 0.2 = 20
        assert d.global_cap == 20


# ---------------------------------------------------------------------------
# Rolling window eviction
# ---------------------------------------------------------------------------


class TestRollingWindow:

    def test_window_evicts_old_emissions(self, governor, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_WINDOW_S", "60")
        governor.register(_spec("X", cap=5))
        # Manually push old timestamps
        now = time.monotonic()
        governor._per_sensor["X"].extend([now - 120, now - 90, now - 30])  # 2 old, 1 fresh
        governor._global.extend([now - 120, now - 90, now - 30])
        d = governor.request_budget("X")
        # After eviction only 1 counts
        assert d.current_count == 1

    def test_record_then_request_sees_count(self, governor):
        governor.register(_spec("X", cap=5))
        governor.record_emission("X")
        governor.record_emission("X")
        d = governor.request_budget("X")
        assert d.current_count == 2
        assert d.remaining == 3


# ---------------------------------------------------------------------------
# Snapshot + history
# ---------------------------------------------------------------------------


class TestSnapshotHistory:

    def test_snapshot_disabled_master(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "false")
        g = SensorGovernor()
        snap = g.snapshot()
        assert snap["enabled"] is False

    def test_snapshot_enabled_shape(self, governor):
        governor.register(_spec("X", cap=10))
        governor.register(_spec("Y", cap=5))
        snap = governor.snapshot()
        assert snap["enabled"] is True
        assert snap["schema_version"] == "1.0"
        assert len(snap["sensors"]) == 2
        assert snap["global"]["cap"] > 0
        assert snap["window_s"] == window_seconds()

    def test_snapshot_contains_emergency_thresholds(self, governor):
        snap = governor.snapshot()
        assert "emergency_thresholds" in snap
        assert snap["emergency_thresholds"]["cost_burn"] == 0.9

    def test_recent_decisions(self, governor):
        governor.register(_spec("X"))
        for _ in range(5):
            governor.request_budget("X")
        recent = governor.recent_decisions(limit=3)
        assert len(recent) == 3
        assert all(isinstance(d, BudgetDecision) for d in recent)


# ---------------------------------------------------------------------------
# reset() + record_emission semantics
# ---------------------------------------------------------------------------


class TestResetAndRecord:

    def test_reset_clears_counters(self, governor):
        governor.register(_spec("X"))
        for _ in range(3):
            governor.record_emission("X")
        governor.reset()
        d = governor.request_budget("X")
        assert d.current_count == 0

    def test_record_when_disabled_noop(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "false")
        g = SensorGovernor()
        g.register(_spec("X"))
        g.record_emission("X")
        # No-op when disabled; snapshot reports enabled:false
        assert g.snapshot()["enabled"] is False


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:

    def test_concurrent_request_and_record(self, governor):
        governor.register(_spec("X", cap=500))

        def worker():
            for _ in range(50):
                governor.request_budget("X")
                governor.record_emission("X")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        # 4 × 50 = 200 emissions across threads
        d = governor.request_budget("X")
        assert d.current_count == 200


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


class TestEnvHelpers:

    def test_is_enabled_default_false(self):
        assert is_enabled() is False

    def test_global_cap_default_200(self):
        assert global_cap_per_hour() == 200

    def test_window_default_3600(self):
        assert window_seconds() == 3600

    def test_emergency_reduction_pct_default_0_2(self):
        assert emergency_reduction_pct() == pytest.approx(0.2)

    def test_emergency_cost_threshold_default_0_9(self):
        assert emergency_cost_threshold() == pytest.approx(0.9)

    def test_emergency_postmortem_threshold_default_0_6(self):
        assert emergency_postmortem_threshold() == pytest.approx(0.6)

    def test_schema_version_1_0(self):
        assert SENSOR_GOVERNOR_SCHEMA_VERSION == "1.0"


# ---------------------------------------------------------------------------
# Seed content pins
# ---------------------------------------------------------------------------


class TestSeedContent:

    def test_seed_has_16_sensors(self):
        assert len(SEED_SPECS) == 16

    def test_all_sensor_names_unique(self):
        names = [s.sensor_name for s in SEED_SPECS]
        assert len(names) == len(set(names))

    def test_all_4_postures_represented(self):
        postures: set = set()
        for s in SEED_SPECS:
            postures.update(s.posture_weights.keys())
        assert postures == {"EXPLORE", "CONSOLIDATE", "HARDEN", "MAINTAIN"}

    def test_test_failure_heavier_in_harden(self):
        s = next(s for s in SEED_SPECS if s.sensor_name == "TestFailureSensor")
        assert s.weight_for_posture("HARDEN") > s.weight_for_posture("EXPLORE")

    def test_opportunity_miner_heavier_in_explore(self):
        s = next(s for s in SEED_SPECS if s.sensor_name == "OpportunityMinerSensor")
        assert s.weight_for_posture("EXPLORE") > s.weight_for_posture("HARDEN")

    def test_doc_staleness_heavier_in_consolidate(self):
        s = next(s for s in SEED_SPECS if s.sensor_name == "DocStalenessSensor")
        assert s.weight_for_posture("CONSOLIDATE") > s.weight_for_posture("HARDEN")

    def test_every_spec_has_description(self):
        for s in SEED_SPECS:
            assert s.description and len(s.description) > 5


# ---------------------------------------------------------------------------
# Singleton + seed integration
# ---------------------------------------------------------------------------


class TestSingletonSeed:

    def test_default_governor_singleton(self):
        g1 = get_default_governor()
        g2 = get_default_governor()
        assert g1 is g2

    def test_ensure_seeded_installs_16(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        g = ensure_seeded()
        assert len(g.list_specs()) == 16

    def test_ensure_seeded_idempotent(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        ensure_seeded()
        before = len(get_default_governor().list_specs())
        ensure_seeded()
        after = len(get_default_governor().list_specs())
        assert before == after

    def test_ensure_seeded_registers_flag_registry_specs(self, monkeypatch):
        """Wave 1 #2 consumer — governor flags appear in FlagRegistry."""
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        from backend.core.ouroboros.governance.flag_registry import (
            ensure_seeded as _fr_seed, reset_default_registry,
        )
        reset_default_registry()
        reset_default_governor()
        ensure_seeded()
        fr = _fr_seed()
        assert fr.get_spec("JARVIS_SENSOR_GOVERNOR_ENABLED") is not None
        assert fr.get_spec("JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR") is not None


# ---------------------------------------------------------------------------
# Authority invariant (grep)
# ---------------------------------------------------------------------------


_AUTHORITY_MODULES = (
    "orchestrator", "policy", "iron_gate", "risk_tier",
    "change_engine", "candidate_generator", "gate",
)


class TestAuthorityInvariant:

    @pytest.mark.parametrize("relpath", [
        "backend/core/ouroboros/governance/sensor_governor.py",
        "backend/core/ouroboros/governance/sensor_governor_seed.py",
    ])
    def test_arc_file_authority_free(self, relpath):
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (repo_root / relpath).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for forbidden in _AUTHORITY_MODULES:
                    if f".{forbidden}" in line:
                        bad.append(line)
        assert not bad, f"{relpath} authority violations: {bad}"
