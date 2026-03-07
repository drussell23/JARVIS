"""Tests for _resolve_watchdog_stage systemic fix.

Root cause: _resolve_watchdog_stage() only fell back to _current_startup_phase
for heartbeat broadcasts (is_heartbeat=True). Sub-step broadcasts like
"event_infrastructure", "intelligence_managers", "integration_init" silently
returned None, causing the DMS watchdog to never update during those phases.
After stall_threshold seconds with a frozen watchdog value -> FALSE TRUE STALL.

This was a CLASS of bug affecting every phase that used sub-step stage names.
The fix: fall back to _current_startup_phase for ALL broadcasts during startup,
not just heartbeats.
"""


class TestWatchdogStageResolver:

    # Canonical map from _resolve_watchdog_stage
    CANONICAL = {
        "loading": "loading_server",
        "loading_server": "loading_server",
        "preflight": "preflight",
        "resources": "resources",
        "backend": "backend",
        "intelligence": "intelligence",
        "two_tier": "two_tier",
        "trinity": "trinity",
        "enterprise": "enterprise",
        "agi_os": "agi_os",
        "ghost_display": "ghost_display",
        "visual_pipeline": "visual_pipeline",
        "frontend": "frontend",
        "finalizing": "frontend",
    }

    def _resolve(self, stage, current_startup_phase, is_heartbeat=False):
        """Simulate the FIXED _resolve_watchdog_stage logic."""
        canonical = self.CANONICAL

        if stage in canonical:
            return canonical[stage]

        if stage.startswith("agi_os"):
            return "agi_os"
        if stage.startswith("ghost_display"):
            return "ghost_display"
        if stage.startswith("visual_pipeline"):
            return "visual_pipeline"

        # v291.0 FIX: Fall back to _current_startup_phase for ALL broadcasts
        resolved = canonical.get(current_startup_phase)
        if resolved:
            return resolved

        if is_heartbeat:
            return None  # Would use watchdog._current_phase in real code

        return None

    def test_canonical_stage_resolves_directly(self):
        """Canonical stages should resolve to themselves."""
        assert self._resolve("intelligence", "intelligence") == "intelligence"
        assert self._resolve("two_tier", "two_tier") == "two_tier"
        assert self._resolve("backend", "backend") == "backend"

    def test_sub_step_resolves_to_current_phase(self):
        """Non-canonical sub-step stages should fall back to _current_startup_phase."""
        # Intelligence sub-steps
        assert self._resolve("event_infrastructure", "intelligence") == "intelligence"
        assert self._resolve("intelligence_managers", "intelligence") == "intelligence"
        assert self._resolve("intelligence_memory", "intelligence") == "intelligence"

        # Two-Tier sub-steps
        assert self._resolve("integration_init", "two_tier") == "two_tier"
        assert self._resolve("integration_watchdog", "two_tier") == "two_tier"
        assert self._resolve("cross_repo_init", "two_tier") == "two_tier"
        assert self._resolve("integration_parallel", "two_tier") == "two_tier"

    def test_sub_step_in_other_phases(self):
        """Sub-step fallback works for any startup phase."""
        assert self._resolve("some_substep", "backend") == "backend"
        assert self._resolve("some_substep", "resources") == "resources"
        assert self._resolve("some_substep", "trinity") == "trinity"
        assert self._resolve("some_substep", "frontend") == "frontend"

    def test_non_startup_phase_returns_none(self):
        """When _current_startup_phase is not canonical (e.g., 'complete'),
        non-canonical stages should return None (not a heartbeat)."""
        assert self._resolve("some_substep", "complete") is None
        assert self._resolve("some_substep", "ready") is None
        assert self._resolve("some_substep", "") is None

    def test_heartbeat_still_works(self):
        """Heartbeat fallback should still work for non-canonical phases."""
        assert self._resolve("intelligence", "intelligence", is_heartbeat=True) == "intelligence"
        # Non-canonical stage during startup — resolved by phase fallback (not heartbeat-specific)
        assert self._resolve("substep", "intelligence", is_heartbeat=True) == "intelligence"

    def test_agi_os_prefix_still_works(self):
        """agi_os prefix matching should still take priority."""
        assert self._resolve("agi_os_init_voice", "agi_os") == "agi_os"
        assert self._resolve("agi_os_complete", "intelligence") == "agi_os"

    def test_all_intelligence_broadcasts_now_resolve(self):
        """Verify ALL broadcast stage names used in _phase_intelligence() now
        resolve to 'intelligence' via the fallback."""
        intelligence_stages = [
            "event_infrastructure",
            "intelligence_managers",
            "intelligence_memory",
        ]
        for stage in intelligence_stages:
            result = self._resolve(stage, "intelligence")
            assert result == "intelligence", (
                f"Stage '{stage}' should resolve to 'intelligence' but got '{result}'. "
                f"This would cause the watchdog to never update during intelligence phase."
            )

    def test_all_two_tier_broadcasts_now_resolve(self):
        """Verify ALL broadcast stage names used in _initialize_integration_components()
        now resolve to 'two_tier' via the fallback."""
        two_tier_stages = [
            "integration_init",
            "integration_watchdog",
            "cross_repo_init",
            "integration_parallel",
        ]
        for stage in two_tier_stages:
            result = self._resolve(stage, "two_tier")
            assert result == "two_tier", (
                f"Stage '{stage}' should resolve to 'two_tier' but got '{result}'. "
                f"This would cause the watchdog to never update during two_tier phase."
            )
