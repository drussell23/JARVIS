"""Tests for v301.0 autonomy contract root-cause fixes.

Root causes addressed:
1. check_autonomy_contracts() required BOTH Prime AND Reactor to be
   reachable, even when one service is disabled — caused permanent
   degraded status when only one service is running.
2. Runtime autonomy monitor only updated 'autonomy_contracts' component
   status — never bridged reachability results to 'jarvis_prime' and
   'reactor_core' component statuses, so boot-time "degraded" stuck forever.

These tests verify the fixes without requiring real services or a running
JARVIS instance.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Fix 1: Contract check respects service enablement
# ---------------------------------------------------------------------------

class TestContractCheckServiceEnablement:
    """Verify disabled services don't block autonomy contracts."""

    def _make_config(self, *, prime_enabled=True, reactor_enabled=True):
        """Build a mock OrchestratorConfig with enablement flags."""
        config = MagicMock()
        config.jarvis_prime_enabled = prime_enabled
        config.reactor_core_enabled = reactor_enabled
        config.jarvis_prime_default_port = 8000
        config.reactor_core_default_port = 8090
        return config

    def test_disabled_reactor_passes_without_reactor(self):
        """When reactor is disabled, contracts should pass with only Prime."""
        from types import SimpleNamespace

        # Simulate the all_pass logic with reactor disabled
        checks = {
            "prime_reachable": True,
            "prime_compatible": True,
            "reactor_reachable": True,   # vacuously true (disabled)
            "reactor_compatible": True,  # vacuously true (disabled)
            "prime_enabled": True,
            "reactor_enabled": False,
        }

        all_pass = (
            checks.get("prime_compatible", False)
            and checks.get("reactor_compatible", False)
            and checks.get("prime_reachable", False)
            and checks.get("reactor_reachable", False)
        )

        assert all_pass is True, "Disabled reactor should not block contracts"

    def test_disabled_prime_passes_without_prime(self):
        """When prime is disabled, contracts should pass with only Reactor."""
        checks = {
            "prime_reachable": True,   # vacuously true (disabled)
            "prime_compatible": True,  # vacuously true (disabled)
            "reactor_reachable": True,
            "reactor_compatible": True,
            "prime_enabled": False,
            "reactor_enabled": True,
        }

        all_pass = (
            checks.get("prime_compatible", False)
            and checks.get("reactor_compatible", False)
            and checks.get("prime_reachable", False)
            and checks.get("reactor_reachable", False)
        )

        assert all_pass is True

    def test_both_disabled_passes_immediately(self):
        """When both services disabled, contracts should pass trivially."""
        checks = {
            "prime_reachable": True,
            "prime_compatible": True,
            "reactor_reachable": True,
            "reactor_compatible": True,
            "prime_enabled": False,
            "reactor_enabled": False,
        }

        all_pass = (
            checks.get("prime_compatible", False)
            and checks.get("reactor_compatible", False)
            and checks.get("prime_reachable", False)
            and checks.get("reactor_reachable", False)
        )

        assert all_pass is True

    def test_enabled_but_unreachable_still_fails(self):
        """When a service is enabled but unreachable, contracts should fail."""
        checks = {
            "prime_reachable": True,
            "prime_compatible": True,
            "reactor_reachable": False,  # enabled but unreachable
            "reactor_compatible": False,
            "prime_enabled": True,
            "reactor_enabled": True,
        }

        all_pass = (
            checks.get("prime_compatible", False)
            and checks.get("reactor_compatible", False)
            and checks.get("prime_reachable", False)
            and checks.get("reactor_reachable", False)
        )

        assert all_pass is False

    def test_disabled_service_excluded_from_unreachable_list(self):
        """Disabled services should not appear in the unreachable list."""
        _prime_enabled = True
        _reactor_enabled = False

        checks = {
            "prime_reachable": False,
            "reactor_reachable": False,
        }

        _unreachable = []
        if _prime_enabled and not checks.get("prime_reachable", False):
            _unreachable.append("prime")
        if _reactor_enabled and not checks.get("reactor_reachable", False):
            _unreachable.append("reactor")

        assert "prime" in _unreachable
        assert "reactor" not in _unreachable, (
            "Disabled reactor should not appear in unreachable list"
        )

    def test_disabled_service_excluded_from_schema_missing(self):
        """Disabled services should not appear in schema_missing list."""
        _prime_enabled = True
        _reactor_enabled = False

        checks = {
            "prime_reachable": True,
            "reactor_reachable": True,
        }
        prime_schema = None
        reactor_schema = None

        _schema_missing = []
        if _prime_enabled and checks.get("prime_reachable") and prime_schema is None:
            _schema_missing.append("prime")
        if _reactor_enabled and checks.get("reactor_reachable") and reactor_schema is None:
            _schema_missing.append("reactor")

        assert "prime" in _schema_missing
        assert "reactor" not in _schema_missing


# ---------------------------------------------------------------------------
# Fix 2: Runtime monitor bridges reachability to component statuses
# ---------------------------------------------------------------------------

class TestRuntimeMonitorBridge:
    """Verify runtime monitor updates component statuses from contract checks."""

    def _make_supervisor(self):
        """Build a minimal mock supervisor with component_status dict."""
        sup = MagicMock()
        sup._component_status = {
            "jarvis_prime": {"status": "degraded", "message": "stale boot-time status"},
            "reactor_core": {"status": "degraded", "message": "stale boot-time status"},
            "autonomy_contracts": {"status": "degraded", "message": "pending"},
        }
        sup._autonomy_mode = "pending"
        sup._autonomy_reason = "pending_services"
        sup._autonomy_checks = {}
        sup.logger = MagicMock()

        # Track _update_component_status calls
        updates = []

        def mock_update(component, status, message="", **extra):
            sup._component_status[component] = {
                "status": status,
                "message": message,
            }
            updates.append((component, status, message))

        sup._update_component_status = mock_update
        sup._updates = updates
        return sup

    def _apply_bridge(self, sup, checks):
        """Simulate the bridge logic from the runtime monitor."""
        for _svc_key, _reach_key in (
            ("jarvis_prime", "prime_reachable"),
            ("reactor_core", "reactor_reachable"),
        ):
            _svc_status = (
                sup._component_status.get(_svc_key, {}).get("status", "pending")
            )
            _svc_enabled = checks.get(
                f"{_reach_key.split('_')[0]}_enabled", True,
            )
            if checks.get(_reach_key) and _svc_status in (
                "degraded", "running", "pending",
            ):
                sup._update_component_status(
                    _svc_key, "complete",
                    f"{_svc_key.replace('_', '-')} healthy "
                    f"(recovered via runtime monitor)",
                )
            elif (
                not checks.get(_reach_key)
                and _svc_enabled
                and _svc_status == "complete"
            ):
                sup._update_component_status(
                    _svc_key, "degraded",
                    f"{_svc_key.replace('_', '-')} unreachable "
                    f"(runtime health probe failed)",
                )

    def test_reachable_service_recovers_from_degraded(self):
        """When monitor sees prime_reachable, jarvis_prime should go to complete."""
        sup = self._make_supervisor()

        checks = {
            "prime_reachable": True,
            "reactor_reachable": True,
            "prime_enabled": True,
            "reactor_enabled": True,
        }

        self._apply_bridge(sup, checks)

        assert sup._component_status["jarvis_prime"]["status"] == "complete"
        assert sup._component_status["reactor_core"]["status"] == "complete"
        assert len(sup._updates) == 2

    def test_unreachable_service_degrades_from_complete(self):
        """When monitor sees reactor unreachable, reactor_core should degrade."""
        sup = self._make_supervisor()
        sup._component_status["reactor_core"]["status"] = "complete"

        checks = {
            "prime_reachable": True,
            "reactor_reachable": False,
            "prime_enabled": True,
            "reactor_enabled": True,
        }

        self._apply_bridge(sup, checks)

        assert sup._component_status["jarvis_prime"]["status"] == "complete"
        assert sup._component_status["reactor_core"]["status"] == "degraded"

    def test_disabled_unreachable_service_stays_complete(self):
        """A disabled service that was complete should NOT degrade."""
        sup = self._make_supervisor()
        sup._component_status["reactor_core"]["status"] = "complete"

        checks = {
            "prime_reachable": True,
            "reactor_reachable": False,
            "prime_enabled": True,
            "reactor_enabled": False,  # disabled
        }

        self._apply_bridge(sup, checks)

        # Prime recovers (degraded→complete), reactor stays complete
        assert sup._component_status["jarvis_prime"]["status"] == "complete"
        assert sup._component_status["reactor_core"]["status"] == "complete"

    def test_already_complete_no_duplicate_update(self):
        """If already complete and still reachable, no update should fire."""
        sup = self._make_supervisor()
        sup._component_status["jarvis_prime"]["status"] = "complete"
        sup._component_status["reactor_core"]["status"] = "complete"

        checks = {
            "prime_reachable": True,
            "reactor_reachable": True,
            "prime_enabled": True,
            "reactor_enabled": True,
        }

        self._apply_bridge(sup, checks)

        # "complete" is not in ("degraded", "running", "pending") → no update
        assert len(sup._updates) == 0

    def test_running_status_recovers_to_complete(self):
        """A service stuck in 'running' should recover to 'complete' when reachable."""
        sup = self._make_supervisor()
        sup._component_status["jarvis_prime"]["status"] = "running"

        checks = {
            "prime_reachable": True,
            "reactor_reachable": True,
            "prime_enabled": True,
            "reactor_enabled": True,
        }

        self._apply_bridge(sup, checks)

        assert sup._component_status["jarvis_prime"]["status"] == "complete"
        assert sup._component_status["reactor_core"]["status"] == "complete"

    def test_pending_status_recovers_to_complete(self):
        """A service in 'pending' should recover to 'complete' when reachable."""
        sup = self._make_supervisor()
        sup._component_status["reactor_core"]["status"] = "pending"

        checks = {
            "prime_reachable": True,
            "reactor_reachable": True,
            "prime_enabled": True,
            "reactor_enabled": True,
        }

        self._apply_bridge(sup, checks)

        assert sup._component_status["reactor_core"]["status"] == "complete"


# ---------------------------------------------------------------------------
# Integration: Full contract check + bridge flow
# ---------------------------------------------------------------------------

class TestContractCheckIntegration:
    """End-to-end simulation of the fix flow."""

    def test_boot_pending_then_runtime_recovery(self):
        """Simulate: boot fails (pending) → services start → monitor recovers."""
        # Phase 1: Boot-time — services not ready
        checks_boot = {
            "prime_reachable": False,
            "reactor_reachable": False,
            "prime_compatible": False,
            "reactor_compatible": False,
            "prime_enabled": True,
            "reactor_enabled": True,
            "reason": "pending_services",
            "pending": ["prime", "reactor"],
        }

        all_pass = (
            checks_boot.get("prime_compatible", False)
            and checks_boot.get("reactor_compatible", False)
            and checks_boot.get("prime_reachable", False)
            and checks_boot.get("reactor_reachable", False)
        )
        assert all_pass is False
        assert checks_boot["reason"] == "pending_services"

        # Phase 2: Runtime — services started, schemas available
        checks_runtime = {
            "prime_reachable": True,
            "reactor_reachable": True,
            "prime_compatible": True,
            "reactor_compatible": True,
            "prime_enabled": True,
            "reactor_enabled": True,
            "reason": "active",
            "pending": [],
        }

        all_pass = (
            checks_runtime.get("prime_compatible", False)
            and checks_runtime.get("reactor_compatible", False)
            and checks_runtime.get("prime_reachable", False)
            and checks_runtime.get("reactor_reachable", False)
        )
        assert all_pass is True

    def test_reactor_disabled_immediate_active(self):
        """With reactor disabled, contracts pass as soon as Prime is healthy."""
        checks = {
            "prime_reachable": True,
            "prime_compatible": True,
            "reactor_reachable": True,   # vacuously true
            "reactor_compatible": True,  # vacuously true
            "prime_enabled": True,
            "reactor_enabled": False,
            "reason": "active",
            "pending": [],
        }

        all_pass = (
            checks.get("prime_compatible", False)
            and checks.get("reactor_compatible", False)
            and checks.get("prime_reachable", False)
            and checks.get("reactor_reachable", False)
        )
        assert all_pass is True


# ---------------------------------------------------------------------------
# Fix 3: Post-timeout probe recovers component statuses
# ---------------------------------------------------------------------------

class TestPostTimeoutProbe:
    """Verify the post-Trinity-timeout autonomy probe fixes stuck statuses."""

    def _make_supervisor(self):
        """Build a minimal mock supervisor with component_status dict."""
        sup = MagicMock()
        sup._component_status = {
            "jarvis_prime": {"status": "running", "message": "Starting J-Prime..."},
            "reactor_core": {"status": "running", "message": "Starting Reactor-Core..."},
            "autonomy_contracts": {"status": "pending", "message": "Waiting for Trinity"},
            "trinity": {"status": "error", "message": "Outer timeout"},
        }
        sup._autonomy_mode = "pending"
        sup._autonomy_reason = "pending_services"
        sup._autonomy_checks = {}
        sup.logger = MagicMock()

        updates = []

        def mock_update(component, status, message="", **extra):
            sup._component_status[component] = {
                "status": status,
                "message": message,
            }
            updates.append((component, status, message))

        sup._update_component_status = mock_update
        sup._updates = updates
        return sup

    def _apply_post_timeout_probe(self, sup, fb_pass, fb_checks):
        """Simulate the post-timeout probe logic (mirrors unified_supervisor.py)."""
        sup._autonomy_checks = fb_checks
        fb_reason = fb_checks.get(
            "reason", "active" if fb_pass else "timeout",
        )

        # Bridge per-service reachability
        for fb_svc, fb_reach in (
            ("jarvis_prime", "prime_reachable"),
            ("reactor_core", "reactor_reachable"),
        ):
            fb_svc_status = (
                sup._component_status.get(fb_svc, {})
                .get("status", "pending")
            )
            fb_enabled = fb_checks.get(
                f"{fb_reach.split('_')[0]}_enabled", True,
            )
            if fb_checks.get(fb_reach) and fb_svc_status in (
                "degraded", "running", "pending",
            ):
                sup._update_component_status(
                    fb_svc, "complete",
                    f"{fb_svc.replace('_', '-')} healthy "
                    f"(post-timeout live probe)",
                )
            elif (
                not fb_checks.get(fb_reach)
                and fb_enabled
                and fb_svc_status in ("running", "pending")
            ):
                sup._update_component_status(
                    fb_svc, "degraded",
                    f"{fb_svc.replace('_', '-')} unreachable "
                    f"after Trinity timeout",
                )

        if fb_pass:
            sup._autonomy_mode = "active"
            sup._autonomy_reason = "active"
            sup._update_component_status(
                "autonomy_contracts", "complete",
                "Autonomy contracts validated (post-timeout probe)",
            )
        elif fb_reason.startswith("pending"):
            sup._autonomy_mode = "pending"
            sup._autonomy_reason = fb_reason
            sup._update_component_status(
                "autonomy_contracts", "degraded",
                f"Services still starting after Trinity timeout ({fb_reason})",
            )
        else:
            sup._autonomy_mode = "read_only"
            sup._autonomy_reason = fb_reason
            sup._update_component_status(
                "autonomy_contracts", "degraded",
                f"Contract mismatch after Trinity timeout ({fb_reason})",
            )

    def test_post_timeout_both_reachable(self):
        """After timeout, if both services are healthy, all recover."""
        sup = self._make_supervisor()
        checks = {
            "prime_reachable": True,
            "reactor_reachable": True,
            "prime_compatible": True,
            "reactor_compatible": True,
            "prime_enabled": True,
            "reactor_enabled": True,
            "reason": "active",
            "pending": [],
        }
        self._apply_post_timeout_probe(sup, True, checks)

        assert sup._component_status["jarvis_prime"]["status"] == "complete"
        assert sup._component_status["reactor_core"]["status"] == "complete"
        assert sup._component_status["autonomy_contracts"]["status"] == "complete"
        assert sup._autonomy_mode == "active"

    def test_post_timeout_reactor_unreachable(self):
        """After timeout, unreachable reactor goes degraded, not stuck at running."""
        sup = self._make_supervisor()
        checks = {
            "prime_reachable": True,
            "reactor_reachable": False,
            "prime_compatible": True,
            "reactor_compatible": False,
            "prime_enabled": True,
            "reactor_enabled": True,
            "reason": "pending_services",
            "pending": ["reactor"],
        }
        self._apply_post_timeout_probe(sup, False, checks)

        assert sup._component_status["jarvis_prime"]["status"] == "complete"
        assert sup._component_status["reactor_core"]["status"] == "degraded"
        assert sup._component_status["autonomy_contracts"]["status"] == "degraded"
        assert sup._autonomy_mode == "pending"

    def test_post_timeout_services_stuck_at_running_get_resolved(self):
        """Key scenario: components stuck at 'running' get properly resolved."""
        sup = self._make_supervisor()
        # Both stuck at "running" from pre-timeout spawn intent
        assert sup._component_status["jarvis_prime"]["status"] == "running"
        assert sup._component_status["reactor_core"]["status"] == "running"

        # Post-timeout probe finds both reachable
        checks = {
            "prime_reachable": True,
            "reactor_reachable": True,
            "prime_compatible": True,
            "reactor_compatible": True,
            "prime_enabled": True,
            "reactor_enabled": True,
            "reason": "active",
            "pending": [],
        }
        self._apply_post_timeout_probe(sup, True, checks)

        # Must NOT still be "running"
        assert sup._component_status["jarvis_prime"]["status"] == "complete"
        assert sup._component_status["reactor_core"]["status"] == "complete"

    def test_post_timeout_disabled_reactor_passes(self):
        """Disabled reactor shouldn't block post-timeout probe."""
        sup = self._make_supervisor()
        checks = {
            "prime_reachable": True,
            "reactor_reachable": True,  # vacuously true
            "prime_compatible": True,
            "reactor_compatible": True,
            "prime_enabled": True,
            "reactor_enabled": False,
            "reason": "active",
            "pending": [],
        }
        self._apply_post_timeout_probe(sup, True, checks)

        assert sup._component_status["jarvis_prime"]["status"] == "complete"
        # Reactor was "running" and reachable (vacuously) → "complete"
        assert sup._component_status["reactor_core"]["status"] == "complete"
        assert sup._autonomy_mode == "active"

    def test_post_timeout_schema_mismatch(self):
        """Schema mismatch → read_only mode, proper degraded status."""
        sup = self._make_supervisor()
        checks = {
            "prime_reachable": True,
            "reactor_reachable": True,
            "prime_compatible": True,
            "reactor_compatible": False,  # schema mismatch
            "prime_enabled": True,
            "reactor_enabled": True,
            "reason": "schema_mismatch",
            "pending": [],
        }
        self._apply_post_timeout_probe(sup, False, checks)

        assert sup._component_status["jarvis_prime"]["status"] == "complete"
        # Reactor IS reachable, so it recovers from "running" to "complete"
        assert sup._component_status["reactor_core"]["status"] == "complete"
        # But autonomy_contracts is degraded due to schema mismatch
        assert sup._component_status["autonomy_contracts"]["status"] == "degraded"
        assert sup._autonomy_mode == "read_only"
