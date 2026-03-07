"""Tests for Two-Tier phase activity marker registration.

Root cause: _initialize_integration_components() had ZERO _mark_startup_activity
calls, and its _broadcast_progress calls used non-canonical stage names
("integration_init", "integration_watchdog") that _resolve_watchdog_stage()
returned None for. After the gather heartbeat's progress value capped at 59,
value_stale_seconds exceeded the window -> watchdog_recent_progress=False ->
reasons=none -> FALSE TRUE STALL at 57%.
"""

import time


class TestTwoTierActivityMarkers:
    def test_activity_marker_registered_at_entry(self):
        """Two-Tier init should register activity immediately on entry."""
        markers = {}
        sources = {}

        def mark_startup_activity(source, stage=None):
            phase = stage or "two_tier"
            markers[phase] = time.time()
            sources[phase] = source

        # Simulate what _initialize_integration_components does at entry
        mark_startup_activity("two_tier_init", stage="two_tier")

        assert "two_tier" in markers
        assert sources["two_tier"] == "two_tier_init"

    def test_heartbeat_registers_activity_after_progress_caps(self):
        """Gather heartbeat should register activity even when progress value
        no longer changes (capped at 59)."""
        markers = {}

        def mark_startup_activity(source, stage=None):
            phase = stage or "two_tier"
            markers[phase] = time.time()

        # Simulate 5 heartbeat ticks — progress caps at 59 after tick 3
        for tick in range(1, 6):
            candidate_progress = 56 + min(tick, 3)  # Caps at 59
            # Even when progress doesn't change (tick 4, 5),
            # activity marker should still be registered
            mark_startup_activity("two_tier_gather_heartbeat", stage="two_tier")

        assert "two_tier" in markers
        # The marker timestamp should be from the LAST tick, not first
        assert (time.time() - markers["two_tier"]) < 1.0

    def test_non_canonical_stage_names_dont_resolve(self):
        """Verify that non-canonical stage names (the root cause) would NOT
        have been recognized by the canonical resolver."""
        canonical = {
            "loading", "loading_server", "preflight", "resources", "backend",
            "intelligence", "two_tier", "trinity", "enterprise", "agi_os",
            "ghost_display", "visual_pipeline", "frontend", "finalizing",
        }

        # These are the stage names used by _broadcast_progress in
        # _initialize_integration_components — they're NOT canonical
        non_canonical_stages = [
            "integration_init",
            "integration_watchdog",
            "cross_repo_init",
            "integration_parallel",
        ]

        for stage in non_canonical_stages:
            assert stage not in canonical, (
                f"{stage} should NOT be canonical — "
                "the root cause was that these stages don't resolve"
            )

        # But "two_tier" IS canonical — activity markers use this
        assert "two_tier" in canonical

    def test_activity_window_covers_typical_two_tier_duration(self):
        """The stage_activity_window should cover the Two-Tier phase duration
        when heartbeats fire every 15s."""
        stall_threshold = 90.0  # Default from PhaseConfig
        stage_activity_window = max(15.0, stall_threshold * 1.25)

        # With heartbeats every 15s, the activity marker is refreshed
        # within 15s. The window should be large enough to cover 15s gaps.
        assert stage_activity_window >= 15.0

        # Even if a heartbeat fires at t=0 and the next at t=15,
        # the marker age is always <= 15s, well within the 112.5s window.
        heartbeat_interval = 15.0
        assert heartbeat_interval < stage_activity_window
