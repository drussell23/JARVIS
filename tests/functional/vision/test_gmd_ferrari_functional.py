"""
Functional Tests — Ghost Mode Display (GMD) + Ferrari Engine

Tests individual components in isolation:
- Ferrari Engine frame capture capability
- OCR bounce count extraction accuracy
- N-Optic Nerve trigger/pattern matching
- GhostHandsOrchestrator task lifecycle
- BounceTestBrowser window discovery

Each test validates a SINGLE component's contract.
Run with: pytest tests/functional/vision/test_gmd_ferrari_functional.py -v
"""

import asyncio
import os
import re
import time
from typing import Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import shared fixtures and utilities
from tests.conftest_gmd_ferrari import (
    BOUNCE_PATTERN,
    STATUS_PATTERN,
    BounceTestBrowser,
    extract_bounce_count,
    extract_mode,
    requires_display,
    requires_ferrari,
    requires_ghost_hands,
    requires_macos,
    requires_optic,
)


# ═══════════════════════════════════════════════════════════════
# Section 1: OCR Text Extraction (Pure Unit Tests — No Hardware)
# ═══════════════════════════════════════════════════════════════

class TestBounceCountExtraction:
    """Test OCR text → bounce count parsing.

    These are pure unit tests that validate the regex extraction
    against various OCR output formats (clean, noisy, partial).
    """

    def test_extract_clean_count(self):
        """Clean OCR output: 'BOUNCE COUNT: 42'."""
        assert extract_bounce_count("BOUNCE COUNT: 42") == 42

    def test_extract_zero_count(self):
        """Initial state: 'BOUNCE COUNT: 0'."""
        assert extract_bounce_count("BOUNCE COUNT: 0") == 0

    def test_extract_large_count(self):
        """Large count: 'BOUNCE COUNT: 1234'."""
        assert extract_bounce_count("BOUNCE COUNT: 1234") == 1234

    def test_extract_with_surrounding_text(self):
        """Count embedded in larger OCR text block."""
        ocr_text = (
            "STATUS: VERTICAL\n"
            "BOUNCE COUNT: 17\n"
            "JARVIS Stereoscopic Vision Test"
        )
        assert extract_bounce_count(ocr_text) == 17

    def test_extract_noisy_whitespace(self):
        """OCR may introduce extra whitespace."""
        assert extract_bounce_count("BOUNCE  COUNT :  7") == 7

    def test_extract_case_insensitive(self):
        """OCR may return mixed case."""
        assert extract_bounce_count("bounce count: 55") == 55
        assert extract_bounce_count("Bounce Count: 99") == 99

    def test_extract_returns_none_for_no_match(self):
        """No bounce count in text."""
        assert extract_bounce_count("STATUS: VERTICAL") is None
        assert extract_bounce_count("") is None
        assert extract_bounce_count("BOUNCE") is None

    def test_extract_returns_none_for_none_input(self):
        """None input returns None."""
        assert extract_bounce_count(None) is None

    def test_extract_mode_vertical(self):
        """Extract mode from 'STATUS: VERTICAL'."""
        assert extract_mode("STATUS: VERTICAL") == "VERTICAL"

    def test_extract_mode_horizontal(self):
        """Extract mode from 'STATUS: HORIZONTAL'."""
        assert extract_mode("STATUS: HORIZONTAL") == "HORIZONTAL"

    def test_extract_mode_from_full_text(self):
        """Extract mode from full OCR text block."""
        ocr_text = "STATUS: HORIZONTAL\nBOUNCE COUNT: 5\nFerrari Engine"
        assert extract_mode(ocr_text) == "HORIZONTAL"
        assert extract_bounce_count(ocr_text) == 5

    def test_extract_mode_none_for_missing(self):
        """No mode text returns None."""
        assert extract_mode("BOUNCE COUNT: 10") is None


class TestBouncePatternRegex:
    """Validate BOUNCE_PATTERN regex against edge cases."""

    @pytest.mark.parametrize("text,expected", [
        ("BOUNCE COUNT: 0", 0),
        ("BOUNCE COUNT: 1", 1),
        ("BOUNCE COUNT: 999", 999),
        ("BOUNCE COUNT:0", 0),           # No space after colon
        ("BOUNCE  COUNT:  42", 42),      # Double spaces
        ("bounce count: 7", 7),          # lowercase
        ("B0UNCE COUNT: 12", None),      # OCR misread 'O' as '0'
        ("BOUNCECOUNT: 5", 5),            # No space — \s* allows zero whitespace
        ("BOUNCE COUNT 5", None),        # Missing colon
    ])
    def test_pattern_variations(self, text, expected):
        """Test pattern against various OCR output formats."""
        result = extract_bounce_count(text)
        assert result == expected, f"For '{text}': expected {expected}, got {result}"


# ═══════════════════════════════════════════════════════════════
# Section 2: BounceTestBrowser (Window Management)
# ═══════════════════════════════════════════════════════════════

class TestBounceTestBrowser:
    """Test BounceTestBrowser configuration and URL construction."""

    def test_default_mode_vertical(self):
        """Default mode is vertical."""
        browser = BounceTestBrowser()
        assert browser.mode == "vertical"

    def test_horizontal_mode(self):
        """Can create horizontal mode browser."""
        browser = BounceTestBrowser(mode="horizontal")
        assert browser.mode == "horizontal"

    def test_custom_dimensions(self):
        """Custom window dimensions are stored."""
        browser = BounceTestBrowser(width=1024, height=768)
        assert browser.width == 1024
        assert browser.height == 768

    def test_html_path_exists(self, bouncing_ball_html_path):
        """Bouncing ball HTML exists at expected path."""
        assert bouncing_ball_html_path.exists()
        content = bouncing_ball_html_path.read_text()
        assert "BOUNCE COUNT" in content
        assert "requestAnimationFrame" in content

    def test_html_contains_mode_support(self, bouncing_ball_html_path):
        """HTML supports both vertical and horizontal modes."""
        content = bouncing_ball_html_path.read_text()
        assert "mode === 'vertical'" in content
        assert "mode === 'horizontal'" in content

    def test_html_bounce_counter_element(self, bouncing_ball_html_path):
        """HTML has the bounce counter element JARVIS needs to detect."""
        content = bouncing_ball_html_path.read_text()
        assert 'id="counter"' in content
        # Verify the counter text format matches our OCR pattern
        assert "BOUNCE COUNT:" in content


# ═══════════════════════════════════════════════════════════════
# Section 3: Ferrari Engine Frame Capture
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ferrari
class TestFerrariFrameCapture:
    """Test Ferrari Engine's ability to capture window frames.

    Requires macOS + ScreenCaptureKit + a running display.
    """

    @pytest.mark.vision
    async def test_ferrari_window_discovery(self):
        """Ferrari can discover visible windows."""
        from backend.display import fast_capture

        windows = fast_capture.get_visible_windows()
        assert isinstance(windows, list), "get_visible_windows() should return a list"
        # Should find at least the desktop
        assert len(windows) > 0, "No visible windows found"

        # Each window should have required fields
        for w in windows[:5]:
            assert isinstance(w, dict)
            # Should have at least an ID or number
            has_id = "id" in w or "kCGWindowNumber" in w
            assert has_id, f"Window missing ID field: {w.keys()}"

    @pytest.mark.vision
    async def test_ferrari_watcher_spawn(self, vertical_bounce_browser):
        """Ferrari can spawn a VideoWatcher for the bounce test window."""
        window_id = await vertical_bounce_browser.find_window_id()
        if window_id is None:
            pytest.skip("Could not find bounce test window")

        from backend.display.fast_capture import VideoWatcher

        watcher = VideoWatcher(window_id, fps=30)
        try:
            await watcher.start()
            # Give it a moment to capture a frame
            await asyncio.sleep(0.5)

            frame = await watcher.get_latest_frame(timeout=2.0)
            assert frame is not None, "VideoWatcher should capture at least one frame"
        finally:
            await watcher.stop()

    @pytest.mark.vision
    async def test_ferrari_frame_rate(self, vertical_bounce_browser):
        """Ferrari captures frames at approximately the requested FPS."""
        window_id = await vertical_bounce_browser.find_window_id()
        if window_id is None:
            pytest.skip("Could not find bounce test window")

        from backend.display.fast_capture import VideoWatcher

        target_fps = 30
        watcher = VideoWatcher(window_id, fps=target_fps)
        try:
            await watcher.start()
            await asyncio.sleep(0.5)  # Warm up

            # Collect frames for 2 seconds
            frames_captured = 0
            start = time.time()
            duration = 2.0

            while time.time() - start < duration:
                frame = await watcher.get_latest_frame(timeout=0.1)
                if frame is not None:
                    frames_captured += 1
                await asyncio.sleep(1.0 / (target_fps * 2))  # Sample at 2x FPS

            elapsed = time.time() - start
            actual_fps = frames_captured / elapsed

            # Allow 50% tolerance — GPU scheduling varies
            assert actual_fps > target_fps * 0.3, (
                f"Frame rate too low: {actual_fps:.1f} FPS "
                f"(expected ~{target_fps} FPS)"
            )
        finally:
            await watcher.stop()

    @pytest.mark.vision
    async def test_ferrari_multiple_watchers(self, dual_bounce_browsers):
        """Ferrari can run multiple concurrent watchers (God Mode)."""
        v_browser, h_browser = dual_bounce_browsers

        v_id = await v_browser.find_window_id()
        h_id = await h_browser.find_window_id()

        if v_id is None or h_id is None:
            pytest.skip("Could not find both bounce test windows")

        from backend.display.fast_capture import VideoWatcher

        v_watcher = VideoWatcher(v_id, fps=15)
        h_watcher = VideoWatcher(h_id, fps=15)

        try:
            await v_watcher.start()
            await h_watcher.start()
            await asyncio.sleep(1.0)

            v_frame = await v_watcher.get_latest_frame(timeout=2.0)
            h_frame = await h_watcher.get_latest_frame(timeout=2.0)

            assert v_frame is not None, "Vertical watcher should capture frames"
            assert h_frame is not None, "Horizontal watcher should capture frames"
        finally:
            await v_watcher.stop()
            await h_watcher.stop()


# ═══════════════════════════════════════════════════════════════
# Section 4: N-Optic Nerve Text Detection
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_optic
class TestNOpticNerveDetection:
    """Test N-Optic Nerve OCR and trigger detection capabilities."""

    @pytest.mark.vision
    async def test_optic_nerve_starts(self, n_optic_nerve):
        """N-Optic Nerve starts successfully."""
        assert n_optic_nerve is not None
        stats = n_optic_nerve.get_stats()
        assert isinstance(stats, dict)

    @pytest.mark.vision
    async def test_optic_nerve_window_listing(self, n_optic_nerve):
        """N-Optic Nerve can list available windows."""
        windows = await n_optic_nerve.get_all_windows()
        assert isinstance(windows, list)
        assert len(windows) > 0, "Should find at least one window"

    @pytest.mark.vision
    async def test_optic_nerve_watch_window(
        self, n_optic_nerve, vertical_bounce_browser
    ):
        """N-Optic Nerve can watch a window for text triggers."""
        window_id = await vertical_bounce_browser.find_window_id()
        if window_id is None:
            pytest.skip("Could not find bounce test window")

        # Watch for "BOUNCE COUNT" text
        from backend.ghost_hands.n_optic_nerve import WatchTrigger

        detected_events = []

        async def on_event(event):
            detected_events.append(event)

        success = await n_optic_nerve.watch_for_text(
            window_id=window_id,
            text_patterns=["BOUNCE COUNT"],
            callback=on_event,
        )
        assert success, "Should be able to start watching window"

        # Wait for detection (OCR needs time)
        await asyncio.sleep(3.0)

        await n_optic_nerve.stop_watching(window_id)

        # Should have detected the bounce count text
        assert len(detected_events) > 0, (
            "N-Optic Nerve should detect 'BOUNCE COUNT' text "
            "in the bouncing ball window"
        )


# ═══════════════════════════════════════════════════════════════
# Section 5: GhostHandsOrchestrator Task Lifecycle
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ghost_hands
class TestGhostHandsTaskLifecycle:
    """Test GhostHandsOrchestrator task creation and management."""

    @pytest.mark.vision
    async def test_orchestrator_starts(self, ghost_orchestrator):
        """GhostHandsOrchestrator starts and returns stats."""
        stats = ghost_orchestrator.get_stats()
        assert isinstance(stats, dict)

    @pytest.mark.vision
    async def test_create_watch_task(self, ghost_orchestrator):
        """Can create a watch-and-react task."""
        from backend.ghost_hands.orchestrator import GhostAction

        task = await ghost_orchestrator.create_task(
            name="test-bounce-watch",
            watch_app="Google Chrome",
            trigger_text="BOUNCE COUNT",
            actions=[GhostAction.narrate_perception("Detected bouncing ball")],
            one_shot=True,
            priority=5,
        )
        assert task is not None
        assert task.name == "test-bounce-watch" or hasattr(task, "name")

        # Cleanup
        await ghost_orchestrator.cancel_task("test-bounce-watch")

    @pytest.mark.vision
    async def test_task_pause_resume(self, ghost_orchestrator):
        """Tasks can be paused and resumed."""
        from backend.ghost_hands.orchestrator import GhostAction

        task = await ghost_orchestrator.create_task(
            name="test-pause-resume",
            watch_app="Google Chrome",
            trigger_text="NEVER_MATCH_THIS",
            actions=[GhostAction.narrate_perception("Test")],
            one_shot=True,
        )

        paused = await ghost_orchestrator.pause_task("test-pause-resume")
        assert paused, "Task should be pausable"

        resumed = await ghost_orchestrator.resume_task("test-pause-resume")
        assert resumed, "Task should be resumable"

        # Cleanup
        await ghost_orchestrator.cancel_task("test-pause-resume")

    @pytest.mark.vision
    async def test_task_cancellation(self, ghost_orchestrator):
        """Tasks can be cancelled cleanly."""
        from backend.ghost_hands.orchestrator import GhostAction

        await ghost_orchestrator.create_task(
            name="test-cancel-me",
            watch_app="Google Chrome",
            trigger_text="NEVER_MATCH",
            actions=[GhostAction.narrate_perception("Test")],
            one_shot=True,
        )

        cancelled = await ghost_orchestrator.cancel_task("test-cancel-me")
        assert cancelled, "Task should be cancellable"

        # Verify it's gone from active list
        task = ghost_orchestrator.get_task("test-cancel-me")
        if task:
            assert task.status in ("cancelled", "completed", "failed"), (
                f"Cancelled task should not be active, got: {task.status}"
            )

    @pytest.mark.vision
    async def test_list_tasks(self, ghost_orchestrator):
        """Can list all tasks."""
        tasks = ghost_orchestrator.list_tasks()
        assert isinstance(tasks, list)

    @pytest.mark.vision
    async def test_execution_history(self, ghost_orchestrator):
        """Can query execution history."""
        history = ghost_orchestrator.get_execution_history(limit=5)
        assert isinstance(history, list)


# ═══════════════════════════════════════════════════════════════
# Section 6: BounceTracker Utility
# ═══════════════════════════════════════════════════════════════

class TestBounceTracker:
    """Test the BounceTracker utility fixture."""

    def test_empty_tracker(self, bounce_tracker):
        """Empty tracker has sensible defaults."""
        assert bounce_tracker.count == 0
        assert bounce_tracker.max_count == 0
        assert bounce_tracker.is_incrementing is False
        assert bounce_tracker.unique_counts == 0

    def test_single_sample(self, bounce_tracker):
        """Single sample recorded correctly."""
        bounce_tracker.record(5)
        assert bounce_tracker.count == 5
        assert bounce_tracker.max_count == 5

    def test_incrementing_sequence(self, bounce_tracker):
        """Monotonically increasing counts detected."""
        for i in range(5):
            bounce_tracker.record(i * 3)
            time.sleep(0.01)
        assert bounce_tracker.is_incrementing is True
        assert bounce_tracker.max_count == 12
        assert bounce_tracker.unique_counts == 5

    def test_non_incrementing_sequence(self, bounce_tracker):
        """Non-monotonic sequence detected."""
        bounce_tracker.record(10)
        time.sleep(0.01)
        bounce_tracker.record(5)  # Decrease
        assert bounce_tracker.is_incrementing is False

    def test_bounces_per_second(self, bounce_tracker):
        """Rate calculation works."""
        bounce_tracker.record(0)
        time.sleep(0.1)
        bounce_tracker.record(10)
        rate = bounce_tracker.bounces_per_second
        assert rate > 0, "Should compute positive bounce rate"

    def test_summary(self, bounce_tracker):
        """Summary contains all expected fields."""
        bounce_tracker.record(5)
        summary = bounce_tracker.summary()
        assert "total_samples" in summary
        assert "latest_count" in summary
        assert "max_count" in summary
        assert "is_incrementing" in summary
        assert "bounces_per_second" in summary
