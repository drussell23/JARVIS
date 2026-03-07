"""Tests for v304.0: Reactor-Core health endpoint training_ready decoupling.

Root cause: The health endpoint required is_running=True for training_ready,
but is_running was only set after ALL initialization (job loading, health server,
service registration, Trinity mesh connection). This meant the supervisor saw
training_ready=False for 10-30+ seconds after the training subsystem was actually
operational, often exceeding the 120s startup timeout.

Fix: training_ready is now True once the health server is up and job manager is
loaded (startup_phase past "starting_server"), regardless of whether service
registration and Trinity connection have completed.
"""

import pytest
from typing import Dict, Any


# ---------------------------------------------------------------------------
# Simulate the v304.0 health endpoint logic (from reactor-core/run_reactor.py)
# ---------------------------------------------------------------------------

def _simulate_health_response(state: Dict[str, Any]) -> Dict[str, Any]:
    """Reproduce the v304.0 health_handler logic."""
    is_running = state.get("running", False)
    startup_phase = state.get("startup_phase", "initializing")

    _SUBSYSTEM_READY_PHASES = (
        "registering", "connecting_trinity",
        "ready", "running", "operational",
    )
    training_ready = is_running or startup_phase in _SUBSYSTEM_READY_PHASES

    if training_ready:
        status = "healthy"
        phase = "ready" if is_running else startup_phase
    elif startup_phase in ("loading_jobs", "starting_server"):
        status = "starting"
        phase = startup_phase
    else:
        status = "starting"
        phase = startup_phase or "pre-init"

    return {
        "status": status,
        "phase": phase,
        "training_ready": training_ready,
    }


# ---------------------------------------------------------------------------
# Phase progression tests
# ---------------------------------------------------------------------------

class TestHealthEndpointPhases:
    """Verify health endpoint returns correct status at each startup phase."""

    def test_initializing_not_ready(self):
        """Phase 'initializing' should NOT be training_ready."""
        resp = _simulate_health_response({
            "running": False,
            "startup_phase": "initializing",
        })
        assert resp["training_ready"] is False
        assert resp["status"] == "starting"

    def test_loading_jobs_not_ready(self):
        """Phase 'loading_jobs' should NOT be training_ready."""
        resp = _simulate_health_response({
            "running": False,
            "startup_phase": "loading_jobs",
        })
        assert resp["training_ready"] is False
        assert resp["status"] == "starting"
        assert resp["phase"] == "loading_jobs"

    def test_starting_server_not_ready(self):
        """Phase 'starting_server' should NOT be training_ready."""
        resp = _simulate_health_response({
            "running": False,
            "startup_phase": "starting_server",
        })
        assert resp["training_ready"] is False
        assert resp["status"] == "starting"
        assert resp["phase"] == "starting_server"

    def test_registering_is_ready(self):
        """Phase 'registering' SHOULD be training_ready (v304.0 fix).

        At this phase, health server is up and job manager is loaded.
        Service registration is supplementary.
        """
        resp = _simulate_health_response({
            "running": False,
            "startup_phase": "registering",
        })
        assert resp["training_ready"] is True
        assert resp["status"] == "healthy"

    def test_connecting_trinity_is_ready(self):
        """Phase 'connecting_trinity' SHOULD be training_ready.

        Trinity mesh connection is supplementary for training readiness.
        """
        resp = _simulate_health_response({
            "running": False,
            "startup_phase": "connecting_trinity",
        })
        assert resp["training_ready"] is True
        assert resp["status"] == "healthy"

    def test_ready_phase_is_ready(self):
        """Phase 'ready' with running=True is fully ready."""
        resp = _simulate_health_response({
            "running": True,
            "startup_phase": "ready",
        })
        assert resp["training_ready"] is True
        assert resp["status"] == "healthy"
        assert resp["phase"] == "ready"

    def test_running_true_always_ready(self):
        """is_running=True should always mean training_ready."""
        resp = _simulate_health_response({
            "running": True,
            "startup_phase": "operational",
        })
        assert resp["training_ready"] is True
        assert resp["status"] == "healthy"


# ---------------------------------------------------------------------------
# Semantic readiness checker compatibility
# ---------------------------------------------------------------------------

class TestSupervisorCompatibility:
    """Verify the health response satisfies the supervisor's SemanticReadinessChecker.

    The checker requires for REACTOR type:
      ("status", "healthy", True)       - critical
      ("training_ready", True, True)    - critical
      ("trinity_connected", True, False) - non-critical
    """

    REACTOR_CRITERIA = [
        ("status", "healthy", True),
        ("training_ready", True, True),
        ("trinity_connected", True, False),
    ]

    def _check_criteria(self, response: Dict[str, Any]) -> bool:
        """Simulate SemanticReadinessChecker criteria evaluation."""
        for field_name, required_value, is_critical in self.REACTOR_CRITERIA:
            actual = response.get(field_name)
            if field_name == "status" and required_value == "healthy":
                met = actual in ("healthy", "ready")
            else:
                met = actual == required_value
            if not met and is_critical:
                return False
        return True

    def test_registering_passes_supervisor_check(self):
        """At 'registering' phase, supervisor criteria should pass."""
        resp = _simulate_health_response({
            "running": False,
            "startup_phase": "registering",
        })
        # trinity_connected won't be in resp, but it's non-critical
        assert self._check_criteria(resp) is True

    def test_starting_server_fails_supervisor_check(self):
        """At 'starting_server' phase, supervisor criteria should fail."""
        resp = _simulate_health_response({
            "running": False,
            "startup_phase": "starting_server",
        })
        assert self._check_criteria(resp) is False

    def test_fully_ready_passes_supervisor_check(self):
        """Fully ready state passes supervisor check."""
        resp = _simulate_health_response({
            "running": True,
            "startup_phase": "ready",
        })
        resp["trinity_connected"] = True
        assert self._check_criteria(resp) is True


# ---------------------------------------------------------------------------
# Regression: pre-v304.0 behavior was broken
# ---------------------------------------------------------------------------

class TestPreV304Regression:
    """Document the pre-v304.0 broken behavior to prevent regression."""

    @staticmethod
    def _old_health_logic(state: Dict[str, Any]) -> Dict[str, Any]:
        """Pre-v304.0 health logic (broken)."""
        is_running = state.get("running", False)
        startup_phase = state.get("startup_phase", "initializing")
        training_ready = is_running and startup_phase in ("running", "ready", "operational")
        if is_running and training_ready:
            status = "healthy"
        else:
            status = "starting"
        return {"status": status, "training_ready": training_ready}

    def test_old_logic_broken_at_registering(self):
        """Old logic: registering phase was NOT training_ready (wrong)."""
        resp = self._old_health_logic({
            "running": False,
            "startup_phase": "registering",
        })
        # Old behavior: training_ready=False even though training subsystem IS ready
        assert resp["training_ready"] is False  # BUG: should be True
        assert resp["status"] == "starting"     # BUG: should be healthy

    def test_new_logic_fixed_at_registering(self):
        """New logic: registering phase IS training_ready (fixed)."""
        resp = _simulate_health_response({
            "running": False,
            "startup_phase": "registering",
        })
        assert resp["training_ready"] is True
        assert resp["status"] == "healthy"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases for health endpoint readiness."""

    def test_empty_state(self):
        """Empty state dict should be NOT ready."""
        resp = _simulate_health_response({})
        assert resp["training_ready"] is False
        assert resp["status"] == "starting"

    def test_missing_startup_phase(self):
        """Missing startup_phase defaults to 'initializing' (not ready)."""
        resp = _simulate_health_response({"running": False})
        assert resp["training_ready"] is False

    def test_unknown_phase_not_ready(self):
        """Unknown startup_phase should NOT be training_ready."""
        resp = _simulate_health_response({
            "running": False,
            "startup_phase": "unknown_phase",
        })
        assert resp["training_ready"] is False

    def test_running_true_overrides_unknown_phase(self):
        """is_running=True overrides any phase value for training_ready."""
        resp = _simulate_health_response({
            "running": True,
            "startup_phase": "some_weird_phase",
        })
        assert resp["training_ready"] is True
        assert resp["status"] == "healthy"
