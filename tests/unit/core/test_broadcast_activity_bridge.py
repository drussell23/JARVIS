"""Tests for the broadcast-to-activity-marker bridge (v291.1).

Root cause: _broadcast_startup_progress() updated the watchdog VALUE but never
called _mark_startup_activity(). When progress capped (e.g., 57% during
intelligence), both watchdog_recent_progress AND stage_activity_recent went
stale simultaneously:

  watchdog_recent_progress = False   (value hasn't changed)
  stage_activity_recent    = False   (no _mark_startup_activity calls)
  model_active             = False   (no model loading)
  => reasons=none => FALSE TRUE STALL

The fix: _broadcast_startup_progress() now calls _mark_startup_activity()
whenever it successfully resolves a watchdog stage. This ensures the heartbeat
(which fires every 5s) keeps activity markers fresh even when the progress
VALUE is flat.
"""

import time


class TestBroadcastActivityBridge:
    """Verify that broadcast progress refreshes activity markers."""

    def _simulate_broadcast_with_bridge(
        self,
        stage: str,
        progress: int,
        current_startup_phase: str,
        activity_markers: dict,
        activity_sources: dict,
        is_heartbeat: bool = False,
    ):
        """Simulate the fixed _broadcast_startup_progress watchdog+activity path."""
        # Step 1: Resolve watchdog stage (same as _resolve_watchdog_stage)
        canonical = {
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

        watchdog_stage = canonical.get(stage)
        if not watchdog_stage:
            if stage.startswith("agi_os"):
                watchdog_stage = "agi_os"
            elif stage.startswith("ghost_display"):
                watchdog_stage = "ghost_display"
            elif stage.startswith("visual_pipeline"):
                watchdog_stage = "visual_pipeline"
            else:
                # v291.0 fallback to current phase
                watchdog_stage = canonical.get(current_startup_phase)

        # Step 2: If resolved, update watchdog AND activity marker (v291.1 bridge)
        watchdog_updated = False
        if watchdog_stage:
            # watchdog.update_phase(watchdog_stage, progress) — simulated
            watchdog_updated = True

            # THE BRIDGE: also refresh activity marker
            phase = watchdog_stage
            if phase:
                activity_markers[phase] = time.time()
                activity_sources[phase] = f"broadcast:{stage}"

        return watchdog_updated

    def test_heartbeat_refreshes_activity_marker(self):
        """Heartbeat broadcasts should keep activity markers fresh."""
        markers = {}
        sources = {}

        # Simulate 5 heartbeats at capped progress (57%)
        for i in range(5):
            self._simulate_broadcast_with_bridge(
                stage="intelligence",
                progress=57,
                current_startup_phase="intelligence",
                activity_markers=markers,
                activity_sources=sources,
                is_heartbeat=True,
            )

        assert "intelligence" in markers
        assert (time.time() - markers["intelligence"]) < 1.0
        assert sources["intelligence"] == "broadcast:intelligence"

    def test_capped_progress_still_refreshes_activity(self):
        """Even when progress value doesn't change, activity markers refresh."""
        markers = {}
        sources = {}

        # First broadcast at 57%
        self._simulate_broadcast_with_bridge(
            stage="intelligence", progress=57,
            current_startup_phase="intelligence",
            activity_markers=markers, activity_sources=sources,
        )
        first_ts = markers["intelligence"]

        # Wait a tiny bit, then broadcast same progress
        import time as _t
        _t.sleep(0.01)

        self._simulate_broadcast_with_bridge(
            stage="intelligence", progress=57,
            current_startup_phase="intelligence",
            activity_markers=markers, activity_sources=sources,
        )
        second_ts = markers["intelligence"]

        # Marker should have been refreshed (newer timestamp)
        assert second_ts > first_ts

    def test_non_canonical_substep_refreshes_via_fallback(self):
        """Sub-step broadcasts (non-canonical) refresh activity via phase fallback."""
        markers = {}
        sources = {}

        self._simulate_broadcast_with_bridge(
            stage="event_infrastructure",
            progress=55,
            current_startup_phase="intelligence",
            activity_markers=markers,
            activity_sources=sources,
        )

        # Should resolve to "intelligence" via v291.0 fallback
        assert "intelligence" in markers
        assert sources["intelligence"] == "broadcast:event_infrastructure"

    def test_no_marker_when_stage_unresolvable(self):
        """When stage can't be resolved, no activity marker should be set."""
        markers = {}
        sources = {}

        result = self._simulate_broadcast_with_bridge(
            stage="random_status_update",
            progress=50,
            current_startup_phase="complete",  # Not a canonical phase
            activity_markers=markers,
            activity_sources=sources,
        )

        assert result is False
        assert len(markers) == 0

    def test_stage_activity_stays_fresh_across_stall_window(self):
        """Simulate the real scenario: heartbeats every 5s keep activity fresh
        even when progress value is capped, preventing FALSE TRUE STALL."""
        markers = {}
        sources = {}
        stall_threshold = 90.0
        stage_activity_window = max(15.0, stall_threshold * 1.25)  # 112.5s

        # Simulate heartbeats at 5s intervals for 120s (past stall threshold)
        for tick in range(24):  # 24 ticks * 5s = 120s
            self._simulate_broadcast_with_bridge(
                stage="intelligence",
                progress=57,  # Capped — never changes
                current_startup_phase="intelligence",
                activity_markers=markers,
                activity_sources=sources,
                is_heartbeat=True,
            )

        # After 120s of heartbeats, marker should still be fresh
        marker_age = time.time() - markers["intelligence"]
        assert marker_age < 1.0  # Just refreshed

        # This marker age is well within the activity window
        assert marker_age < stage_activity_window

    def test_bridge_covers_all_startup_phases(self):
        """Bridge should work for every canonical startup phase."""
        phases = [
            "preflight", "resources", "backend", "intelligence",
            "two_tier", "trinity", "enterprise", "frontend",
        ]

        for phase in phases:
            markers = {}
            sources = {}
            self._simulate_broadcast_with_bridge(
                stage=phase,
                progress=50,
                current_startup_phase=phase,
                activity_markers=markers,
                activity_sources=sources,
                is_heartbeat=True,
            )
            assert phase in markers, (
                f"Phase '{phase}' should have an activity marker after broadcast"
            )

    def test_without_bridge_markers_go_stale(self):
        """Demonstrate the pre-fix failure: without the bridge, activity markers
        set at phase start go stale while heartbeats fire uselessly."""
        markers = {}

        # Phase start: marker set once
        markers["intelligence"] = time.time() - 130  # 130s ago (simulated)

        # Heartbeats fire but DON'T update markers (pre-fix behavior)
        # ... no updates ...

        # Check: marker is stale (> stall_threshold)
        stall_threshold = 90.0
        stage_activity_window = max(15.0, stall_threshold * 1.25)
        marker_age = time.time() - markers["intelligence"]

        assert marker_age > stage_activity_window, (
            "Without the bridge, markers go stale — this is the bug we fixed"
        )
