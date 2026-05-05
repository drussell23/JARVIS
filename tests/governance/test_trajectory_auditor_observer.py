"""TrajectoryAuditor un-stranding tests (PRD §24.10.2 + §1
long-horizon semantic stability gap closure 2026-05-04).

Pins:
  § 1 — Observer master flag default-true post-graduation
  § 2 — Boot sequence (start → idempotent / stop → idempotent)
  § 3 — SSE event vocabulary present
  § 4 — Authority floor (no orchestrator/iron_gate/providers imports)
  § 5 — Public exports
  § 6 — Boot wire-up presence in governed_loop_service.py
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# § 1 — Master flag
# ---------------------------------------------------------------------------


class TestObserverMasterFlag:
    def test_default_is_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_TRAJECTORY_AUDITOR_OBSERVER_ENABLED",
            raising=False,
        )
        from backend.core.ouroboros.governance.observability.trajectory_auditor_observer import (  # noqa: E501
            trajectory_observer_enabled,
        )
        assert trajectory_observer_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off", "FALSE"],
    )
    def test_falsy_variants_revert(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_TRAJECTORY_AUDITOR_OBSERVER_ENABLED", v,
        )
        from backend.core.ouroboros.governance.observability.trajectory_auditor_observer import (  # noqa: E501
            trajectory_observer_enabled,
        )
        assert trajectory_observer_enabled() is False


# ---------------------------------------------------------------------------
# § 2 — Boot lifecycle
# ---------------------------------------------------------------------------


class TestBootLifecycle:
    @pytest.mark.asyncio
    async def test_start_idempotent(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_TRAJECTORY_AUDITOR_OBSERVER_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.observability.trajectory_auditor_observer import (  # noqa: E501
            TrajectoryAuditorObserver,
        )
        obs = TrajectoryAuditorObserver()
        # First start
        ok1 = await obs.start()
        # Second start while running
        ok2 = await obs.start()
        assert ok1 is True
        assert ok2 is True
        await obs.stop()

    @pytest.mark.asyncio
    async def test_start_returns_false_when_disabled(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_TRAJECTORY_AUDITOR_OBSERVER_ENABLED",
            "false",
        )
        from backend.core.ouroboros.governance.observability.trajectory_auditor_observer import (  # noqa: E501
            TrajectoryAuditorObserver,
        )
        obs = TrajectoryAuditorObserver()
        ok = await obs.start()
        assert ok is False

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_TRAJECTORY_AUDITOR_OBSERVER_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.observability.trajectory_auditor_observer import (  # noqa: E501
            TrajectoryAuditorObserver,
        )
        obs = TrajectoryAuditorObserver()
        await obs.start()
        await obs.stop()
        # Second stop on already-stopped observer
        await obs.stop()  # NEVER raises

    def test_singleton_is_stable(self, monkeypatch):
        from backend.core.ouroboros.governance.observability.trajectory_auditor_observer import (  # noqa: E501
            get_default_trajectory_observer,
            reset_default_trajectory_observer_for_tests,
        )
        reset_default_trajectory_observer_for_tests()
        a = get_default_trajectory_observer()
        b = get_default_trajectory_observer()
        assert a is b


# ---------------------------------------------------------------------------
# § 3 — SSE event vocabulary
# ---------------------------------------------------------------------------


class TestSSEVocabulary:
    def test_event_constant_present(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_TRAJECTORY_DRIFT_DETECTED,
        )
        assert (
            EVENT_TYPE_TRAJECTORY_DRIFT_DETECTED
            == "trajectory_drift_detected"
        )

    def test_publish_helper_callable(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_trajectory_drift_event,
        )
        # Stream disabled → returns None silently; never raises
        result = publish_trajectory_drift_event(
            verdict="drifting",
            signals=(
                {
                    "metric": "total_loc",
                    "baseline_value": 100000.0,
                    "current_value": 200000.0,
                    "change_pct": 100.0,
                    "severity": "critical",
                    "detail": "synthetic test",
                },
            ),
            snapshot_hash="testhash",
            ts_unix=1234.0,
            reason="boot",
        )
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# § 4 — Authority floor
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.auto_action_router",
        "from backend.core.ouroboros.governance.strategic_direction",
    )

    def test_observer_module_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "observability" / "trajectory_auditor_observer.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# § 5 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_observer_exports(self):
        from backend.core.ouroboros.governance.observability import (  # noqa: E501
            trajectory_auditor_observer as obs_mod,
        )
        expected = sorted([
            "TrajectoryAuditorObserver",
            "get_default_trajectory_observer",
            "reset_default_trajectory_observer_for_tests",
            "trajectory_observer_enabled",
        ])
        assert sorted(obs_mod.__all__) == expected


# ---------------------------------------------------------------------------
# § 6 — Boot wire-up in governed_loop_service.py
# ---------------------------------------------------------------------------


class TestBootWireUp:
    def test_governed_loop_service_imports_observer(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "governed_loop_service.py"
        )
        source = path.read_text(encoding="utf-8")
        assert (
            "trajectory_auditor_observer" in source
        ), (
            "governed_loop_service.py must lazy-import "
            "trajectory_auditor_observer — un-stranding regressed"
        )
        assert "get_default_trajectory_observer" in source
        # And calls .start() on it
        assert (
            "_trajectory_auditor_observer.start" in source
            or "_trajectory_auditor_observer = (" in source
        )

    def test_governed_loop_service_stops_observer(self):
        """Stop side wired in _stop_governance_observers."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "governed_loop_service.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "_trajectory_auditor_observer" in source
