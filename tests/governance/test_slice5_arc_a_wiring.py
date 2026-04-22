"""Slice 5 Arc A integration tests — SensorGovernor wiring into intake +
PostureObserver startup at canonical boot.

Authority invariant: no imports from
orchestrator/policy/iron_gate/risk_tier.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope, make_envelope,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
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


_REPO_ROOT_CACHED = Path(subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    capture_output=True, text=True, check=True,
).stdout.strip())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if (k.startswith("JARVIS_INTAKE_GOVERNOR")
                or k.startswith("JARVIS_SENSOR_GOVERNOR")
                or k.startswith("JARVIS_POSTURE")
                or k.startswith("JARVIS_DIRECTION_INFERRER")):
            monkeypatch.delenv(k, raising=False)
    reset_default_governor()
    from backend.core.ouroboros.governance.posture_observer import (
        reset_default_observer, reset_default_store,
    )
    reset_default_observer()
    reset_default_store()
    # Don't chdir — breaks `git rev-parse` used by the authority pin.
    # Router WAL writes to config.project_root (tmp_path), not cwd.
    yield
    reset_default_governor()
    reset_default_observer()
    reset_default_store()


def _make_router(tmp_path: Path) -> UnifiedIntakeRouter:
    """Router wired to a real IntakeRouterConfig under tmp_path.

    ``gls=None`` is safe: the ingest path doesn't touch ``self._gls``.
    """
    config = IntakeRouterConfig(project_root=tmp_path)
    return UnifiedIntakeRouter(gls=None, config=config)


def _make_test_envelope(
    source: str = "backlog",
    urgency: str = "normal",
    description: str = "test op",
) -> IntentEnvelope:
    return make_envelope(
        source=source,
        description=description,
        target_files=("dummy/file.py",),
        repo="jarvis",
        confidence=0.8,
        urgency=urgency,
        evidence={},
        requires_human_ack=False,
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
# Governor helpers in isolation (no ingest needed)
# ---------------------------------------------------------------------------


class TestGovernorHelpers:

    def test_consult_governor_returns_decision_for_registered_source(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        _sg_seed()
        router = _make_router(tmp_path)
        env = _make_test_envelope(source="backlog", urgency="normal")
        decision = router._consult_governor(env)
        assert decision is not None
        assert decision.sensor_name == "BacklogSensor"
        assert decision.urgency is Urgency.COMPLEX  # normal → complex

    def test_consult_governor_unmapped_source_returns_unregistered(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        _sg_seed()
        router = _make_router(tmp_path)
        env = _make_test_envelope(source="architecture", urgency="normal")
        decision = router._consult_governor(env)
        assert decision is not None
        assert decision.allowed is True
        assert decision.reason_code == "governor.unregistered_sensor"

    def test_consult_governor_never_raises(
        self, monkeypatch, tmp_path,
    ):
        import backend.core.ouroboros.governance.sensor_governor as sg

        def _broken():
            raise RuntimeError("simulated outage")
        monkeypatch.setattr(sg, "ensure_seeded", _broken)
        router = _make_router(tmp_path)
        env = _make_test_envelope()
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
# Full ingest path — shadow / enforce / off
# ---------------------------------------------------------------------------


_LOGGER = "backend.core.ouroboros.governance.intake.unified_intake_router"


class TestIngestIntegration:

    @pytest.mark.asyncio
    async def test_off_mode_no_governor_consultation(
        self, monkeypatch, tmp_path, caplog,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        _sg_seed()
        router = _make_router(tmp_path)
        env = _make_test_envelope()
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            result = await router.ingest(env)
        assert result == "enqueued"
        assert not any("governor" in r.message.lower() for r in caplog.records), \
            "governor should not be consulted in off mode"

    @pytest.mark.asyncio
    async def test_shadow_mode_logs_deny_but_enqueues(
        self, monkeypatch, tmp_path, caplog,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "shadow")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        g = _sg_seed()
        g.register(SensorBudgetSpec(
            sensor_name="BacklogSensor", base_cap_per_hour=1,
        ), override=True)
        g.record_emission("BacklogSensor", Urgency.COMPLEX)

        router = _make_router(tmp_path)
        env = _make_test_envelope(source="backlog", urgency="normal")
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            result = await router.ingest(env)

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
        with caplog.at_level(logging.INFO, logger=_LOGGER):
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
        _sg_seed()
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

        before = gov.request_budget("BacklogSensor", Urgency.COMPLEX).current_count
        result = await router.ingest(env)
        assert result == "enqueued"
        after = gov.request_budget("BacklogSensor", Urgency.COMPLEX).current_count
        assert after == before + 1

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

        before = gov.request_budget("BacklogSensor", Urgency.COMPLEX).current_count
        await router.ingest(env)
        after = gov.request_budget("BacklogSensor", Urgency.COMPLEX).current_count
        assert after == before


# ---------------------------------------------------------------------------
# PostureObserver startup wiring
# ---------------------------------------------------------------------------


class TestPostureObserverStartup:

    @pytest.mark.asyncio
    async def test_observer_singleton_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_observer,
        )
        o1 = get_default_observer(tmp_path)
        o2 = get_default_observer(tmp_path)
        assert o1 is o2

    @pytest.mark.asyncio
    async def test_observer_start_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_observer,
        )
        obs = get_default_observer(tmp_path)
        obs.start()
        first_task = obs._task
        obs.start()  # idempotent
        assert obs._task is first_task
        await obs.stop()

    @pytest.mark.asyncio
    async def test_observer_disabled_start_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "false")
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_observer,
        )
        obs = get_default_observer(tmp_path)
        obs.start()
        assert obs.is_running() is False

    def test_governed_loop_stop_references_posture_observer(self):
        """Regression: stop() uses getattr(self, '_posture_observer', None)
        safely even if start() never set it. Verified via source inspection."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService,
        )
        import inspect
        src = inspect.getsource(GovernedLoopService.stop)
        assert "_posture_observer" in src, (
            "stop() must reference _posture_observer for graceful shutdown"
        )


# ---------------------------------------------------------------------------
# Authority invariant (Arc A)
# ---------------------------------------------------------------------------


class TestArcAAuthorityInvariant:

    def test_intake_router_arc_a_additions_authority_free(self):
        src = (
            _REPO_ROOT_CACHED
            / "backend/core/ouroboros/governance/intake/unified_intake_router.py"
        ).read_text(encoding="utf-8")
        # Arc A additions must not pull in execution-authority modules
        forbidden = ("iron_gate", "risk_tier", "change_engine",
                     "candidate_generator")
        for f in forbidden:
            assert f".{f}" not in src, (
                f"unified_intake_router.py references authority module {f}"
            )
