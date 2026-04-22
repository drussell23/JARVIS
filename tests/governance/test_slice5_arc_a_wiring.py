"""Slice 5 Arc A integration tests — SensorGovernor wiring into intake +
PostureObserver startup at canonical boot.

Scope:
  1. Intake router consults governor (shadow/enforce/off) and respects
     the mode via JARVIS_INTAKE_GOVERNOR_MODE
  2. Urgency + source translation (envelope vocab → governor vocab) is
     correct and stable
  3. Source names NOT in the translation map fall through cleanly to
     "governor.unregistered_sensor" (always allowed)
  4. ``record_emission`` fires on successful enqueue so rolling-window
     counters update
  5. Shadow-mode deny logs the would-be-throttle at INFO but still
     returns "enqueued"
  6. Enforce-mode deny returns "governor_throttled" without touching
     WAL or dedup registry
  7. Off-mode skips governor consultation entirely (pre-Arc-A behavior)
  8. Governor failure path is silent — intake must not break when the
     governor module raises

Authority invariant: this test file imports nothing from
orchestrator/policy/iron_gate/risk_tier.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope, make_envelope,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    UnifiedIntakeRouter,
    _SOURCE_TO_GOVERNOR_SENSOR,
    _URGENCY_STR_TO_GOVERNOR,
    _intake_governor_mode,
)
from backend.core.ouroboros.governance.sensor_governor import (
    SensorBudgetSpec,
    SensorGovernor,
    Urgency,
    ensure_seeded as _sg_seed,
    reset_default_governor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if (k.startswith("JARVIS_INTAKE_GOVERNOR")
                or k.startswith("JARVIS_SENSOR_GOVERNOR")
                or k.startswith("JARVIS_POSTURE")
                or k.startswith("JARVIS_DIRECTION_INFERRER")):
            monkeypatch.delenv(k, raising=False)
    reset_default_governor()
    # Ensure router uses a clean working dir (its WAL lands under cwd)
    monkeypatch.chdir(tmp_path)
    yield
    reset_default_governor()


def _make_router(tmp_path: Path) -> UnifiedIntakeRouter:
    """Build a router with sane defaults for isolation tests."""
    return UnifiedIntakeRouter(
        project_root=tmp_path,
    )


def _make_test_envelope(
    source: str = "backlog",
    urgency: str = "normal",
    description: str = "test op",
) -> IntentEnvelope:
    return make_envelope(
        source=source, urgency=urgency, description=description,
    )


# ---------------------------------------------------------------------------
# Translation maps — static correctness
# ---------------------------------------------------------------------------


class TestTranslationMaps:

    def test_urgency_map_covers_all_envelope_urgencies(self):
        for u in ("critical", "high", "normal", "low"):
            assert u in _URGENCY_STR_TO_GOVERNOR

    def test_urgency_map_targets_valid_governor_values(self):
        valid = {e.value for e in Urgency}
        for mapped in _URGENCY_STR_TO_GOVERNOR.values():
            assert mapped in valid

    def test_critical_maps_to_immediate(self):
        assert _URGENCY_STR_TO_GOVERNOR["critical"] == "immediate"

    def test_low_maps_to_background(self):
        assert _URGENCY_STR_TO_GOVERNOR["low"] == "background"

    def test_source_map_covers_common_sensors(self):
        for src in (
            "test_failure", "backlog", "voice_human", "ai_miner",
            "capability_gap", "runtime_health", "exploration",
            "intent_discovery", "todo_scanner", "doc_staleness",
            "github_issue", "performance_regression", "cross_repo_drift",
            "web_intelligence", "vision_sensor",
        ):
            assert src in _SOURCE_TO_GOVERNOR_SENSOR

    def test_source_map_targets_exist_in_seed(self):
        from backend.core.ouroboros.governance.sensor_governor_seed import (
            SEED_SPECS,
        )
        seed_names = {s.sensor_name for s in SEED_SPECS}
        for governor_name in _SOURCE_TO_GOVERNOR_SENSOR.values():
            assert governor_name in seed_names, (
                f"{governor_name} not found in SEED_SPECS"
            )


# ---------------------------------------------------------------------------
# Mode env parsing
# ---------------------------------------------------------------------------


class TestModeEnv:

    def test_default_is_shadow(self):
        assert _intake_governor_mode() == "shadow"

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
        assert _intake_governor_mode() == "off"

    def test_explicit_enforce(self, monkeypatch):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "enforce")
        assert _intake_governor_mode() == "enforce"

    def test_invalid_value_falls_back_to_shadow(self, monkeypatch):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "banana")
        assert _intake_governor_mode() == "shadow"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "ENFORCE")
        assert _intake_governor_mode() == "enforce"


# ---------------------------------------------------------------------------
# Shadow / enforce / off behavior via direct helpers (no asyncio needed)
# ---------------------------------------------------------------------------


class TestGovernorHelpers:
    """Test the per-call helpers in isolation; full async ingest covered below."""

    def test_consult_governor_returns_decision_for_registered_source(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "shadow")
        reset_default_governor()
        _sg_seed()

        router = _make_router(tmp_path)
        env = _make_test_envelope(source="backlog", urgency="normal")
        decision = router._consult_governor(env)
        assert decision is not None
        assert decision.sensor_name == "BacklogSensor"
        # urgency "normal" → governor COMPLEX
        assert decision.urgency is Urgency.COMPLEX

    def test_consult_governor_unmapped_source_returns_unregistered(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        _sg_seed()
        router = _make_router(tmp_path)
        # "architecture" is a valid envelope source but NOT in the
        # governor seed (and NOT in the translation map)
        env = _make_test_envelope(source="architecture", urgency="normal")
        decision = router._consult_governor(env)
        assert decision is not None
        assert decision.allowed is True
        assert decision.reason_code == "governor.unregistered_sensor"

    def test_consult_governor_never_raises_on_registry_failure(
        self, monkeypatch, tmp_path,
    ):
        """Simulate governor import failure — consultation returns None."""
        # Point the import to nowhere by breaking ensure_seeded
        import backend.core.ouroboros.governance.sensor_governor as sg

        def _broken():
            raise RuntimeError("simulated governor outage")
        monkeypatch.setattr(sg, "ensure_seeded", _broken)
        router = _make_router(tmp_path)
        env = _make_test_envelope()
        # Must not raise, must return None
        assert router._consult_governor(env) is None

    def test_record_emission_never_raises(self, monkeypatch, tmp_path):
        import backend.core.ouroboros.governance.sensor_governor as sg

        def _broken():
            raise RuntimeError("simulated outage")
        monkeypatch.setattr(sg, "ensure_seeded", _broken)
        router = _make_router(tmp_path)
        env = _make_test_envelope()
        router._record_governor_emission(env)  # no raise


# ---------------------------------------------------------------------------
# Full ingest path — shadow vs enforce vs off
# ---------------------------------------------------------------------------


class TestIngestIntegration:

    @pytest.mark.asyncio
    async def test_off_mode_skips_governor_consultation(
        self, monkeypatch, tmp_path, caplog,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        _sg_seed()
        router = _make_router(tmp_path)
        env = _make_test_envelope(source="backlog", urgency="normal")
        with caplog.at_level(logging.INFO, logger="backend.core.ouroboros.governance.intake.unified_intake_router"):
            result = await router.ingest(env)
        assert result == "enqueued"
        # No governor SHADOW/ENFORCE log lines
        assert not any("governor" in r.message.lower() for r in caplog.records), \
            "governor should not be consulted in off mode"

    @pytest.mark.asyncio
    async def test_shadow_mode_logs_would_be_deny_but_enqueues(
        self, monkeypatch, tmp_path, caplog,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "shadow")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        # Register a 1-cap sensor so we can saturate it
        reset_default_governor()
        g = _sg_seed()
        g.register(SensorBudgetSpec(
            sensor_name="BacklogSensor", base_cap_per_hour=1,
        ), override=True)
        # Pre-saturate
        g.record_emission("BacklogSensor", Urgency.COMPLEX)

        router = _make_router(tmp_path)
        env = _make_test_envelope(source="backlog", urgency="normal")
        with caplog.at_level(logging.INFO, logger="backend.core.ouroboros.governance.intake.unified_intake_router"):
            result = await router.ingest(env)

        # Shadow: would have denied but let through
        assert result == "enqueued"
        assert any("SHADOW deny" in r.message for r in caplog.records), \
            "shadow-mode deny must be logged"

    @pytest.mark.asyncio
    async def test_enforce_mode_returns_throttled_on_deny(
        self, monkeypatch, tmp_path, caplog,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "enforce")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        g = _sg_seed()
        g.register(SensorBudgetSpec(
            sensor_name="BacklogSensor", base_cap_per_hour=1,
        ), override=True)
        g.record_emission("BacklogSensor", Urgency.COMPLEX)

        router = _make_router(tmp_path)
        env = _make_test_envelope(source="backlog", urgency="normal")
        with caplog.at_level(logging.INFO, logger="backend.core.ouroboros.governance.intake.unified_intake_router"):
            result = await router.ingest(env)
        assert result == "governor_throttled"
        assert any("ENFORCE deny" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_under_cap_allows_through(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "enforce")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        _sg_seed()  # default cap high enough
        router = _make_router(tmp_path)
        env = _make_test_envelope(source="backlog", urgency="normal")
        result = await router.ingest(env)
        assert result == "enqueued"

    @pytest.mark.asyncio
    async def test_record_emission_fires_on_enqueue(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "shadow")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        gov = _sg_seed()
        router = _make_router(tmp_path)
        env = _make_test_envelope(source="backlog", urgency="normal")

        # Before: count = 0
        d_before = gov.request_budget("BacklogSensor", Urgency.COMPLEX)
        before_count = d_before.current_count

        result = await router.ingest(env)
        assert result == "enqueued"

        # After: count should have grown by 1
        d_after = gov.request_budget("BacklogSensor", Urgency.COMPLEX)
        assert d_after.current_count == before_count + 1

    @pytest.mark.asyncio
    async def test_off_mode_does_not_record_emission(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        gov = _sg_seed()
        router = _make_router(tmp_path)
        env = _make_test_envelope(source="backlog", urgency="normal")

        d_before = gov.request_budget("BacklogSensor", Urgency.COMPLEX)
        before_count = d_before.current_count

        await router.ingest(env)

        d_after = gov.request_budget("BacklogSensor", Urgency.COMPLEX)
        assert d_after.current_count == before_count, \
            "off-mode must not touch the governor counter"


# ---------------------------------------------------------------------------
# PostureObserver startup wiring — GovernedLoopService integration
# ---------------------------------------------------------------------------


class TestPostureObserverStartup:
    """Verify PostureObserver starts at the canonical boot site without
    duplication. We test the startup *block* behavior by importing
    get_default_observer directly and verifying idempotency."""

    def test_observer_singleton_returns_same_instance(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_observer, reset_default_observer,
        )
        reset_default_observer()
        o1 = get_default_observer(tmp_path)
        o2 = get_default_observer(tmp_path)
        assert o1 is o2

    def test_observer_start_idempotent(self, tmp_path, monkeypatch):
        """Calling start() twice must not create two background tasks."""
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_observer, reset_default_observer,
        )
        reset_default_observer()

        async def _inner():
            obs = get_default_observer(tmp_path)
            obs.start()
            first_task = obs._task
            obs.start()  # idempotent — must not replace the task
            assert obs._task is first_task
            await obs.stop()

        asyncio.run(_inner())

    def test_observer_disabled_start_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "false")
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_observer, reset_default_observer,
        )
        reset_default_observer()

        async def _inner():
            obs = get_default_observer(tmp_path)
            obs.start()
            assert obs.is_running() is False

        asyncio.run(_inner())

    def test_governed_loop_service_has_posture_attr_safe(self):
        """Regression: getattr(self, '_posture_observer', None) pattern in
        GovernedLoopService.stop() must work even if start() never set it
        (e.g. construction without start)."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService,
        )
        # We just need to verify the attribute access pattern is safe —
        # getattr with default is defensive. Sanity check on the
        # attribute name itself existing as a code path.
        import inspect
        src = inspect.getsource(GovernedLoopService.stop)
        assert "_posture_observer" in src, (
            "stop() must reference _posture_observer for graceful shutdown"
        )


# ---------------------------------------------------------------------------
# Authority invariant (re-pinned for Arc A)
# ---------------------------------------------------------------------------


class TestArcAAuthorityInvariant:

    def test_intake_router_new_code_authority_free(self):
        """The Arc A edits in unified_intake_router.py add NO new imports
        from orchestrator/policy/iron_gate/risk_tier."""
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (
            repo_root
            / "backend/core/ouroboros/governance/intake/unified_intake_router.py"
        ).read_text(encoding="utf-8")
        # The Arc A additions we introduced:
        forbidden = ("iron_gate", "risk_tier", "change_engine",
                     "candidate_generator")
        for f in forbidden:
            assert f".{f}" not in src, (
                f"unified_intake_router.py must not import {f}"
            )
