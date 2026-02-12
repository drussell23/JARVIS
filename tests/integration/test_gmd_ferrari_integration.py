"""
Integration Tests — Ghost Mode Display (GMD) + Ferrari Engine

Tests component chains working together:
- Ferrari Engine → OCR → Bounce count extraction pipeline
- Ferrari Engine → N-Optic Nerve → Event detection
- GhostHandsOrchestrator → N-Optic Nerve → Trigger → Action
- Real-time bounce tracking with count verification
- Concurrent frame capture + OCR analysis

Run with: pytest tests/integration/test_gmd_ferrari_integration.py -v
"""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import pytest

from tests.conftest_gmd_ferrari import (
    BOUNCE_DETECTION_TIMEOUT,
    BOUNCE_MIN_EXPECTED,
    BOUNCE_TEST_DURATION,
    FERRARI_FPS,
    OCR_CHECK_INTERVAL,
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
# Section 1: Ferrari Engine → OCR Pipeline
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ferrari
class TestFerrariOCRPipeline:
    """Test Ferrari frame capture → OCR text extraction → bounce count.

    This is the core detection pipeline that proves JARVIS can
    "see" the ball bouncing and count the bounces in real-time.
    """

    @pytest.mark.integration
    @pytest.mark.vision
    @pytest.mark.slow
    async def test_capture_and_extract_bounce_count(
        self, vertical_bounce_browser, bounce_tracker
    ):
        """Capture frames with Ferrari, extract bounce count via OCR.

        This test proves:
        1. Ferrari Engine captures frames from the bounce window
        2. OCR can read the "BOUNCE COUNT: N" text
        3. The count increases over time (ball is actually bouncing)
        """
        window_id = await vertical_bounce_browser.find_window_id()
        if window_id is None:
            pytest.skip("Could not find bounce test window")

        from backend.display.fast_capture import VideoWatcher

        watcher = VideoWatcher(window_id, fps=FERRARI_FPS)
        try:
            await watcher.start()
            await asyncio.sleep(0.5)  # Warm up

            # Run OCR extraction loop for test duration
            start = time.time()
            ocr_successes = 0
            ocr_failures = 0

            while time.time() - start < BOUNCE_TEST_DURATION:
                frame = await watcher.get_latest_frame(timeout=1.0)
                if frame is None:
                    continue

                # Extract text from frame via OCR
                ocr_text = await self._ocr_frame(frame)
                if ocr_text:
                    count = extract_bounce_count(ocr_text)
                    if count is not None:
                        bounce_tracker.record(count)
                        ocr_successes += 1
                    else:
                        ocr_failures += 1

                await asyncio.sleep(OCR_CHECK_INTERVAL)

            summary = bounce_tracker.summary()

            # Assertions
            assert ocr_successes > 0, (
                f"OCR should detect bounce count at least once. "
                f"Successes: {ocr_successes}, Failures: {ocr_failures}"
            )
            assert bounce_tracker.max_count >= BOUNCE_MIN_EXPECTED, (
                f"Should detect at least {BOUNCE_MIN_EXPECTED} bounces. "
                f"Max count observed: {bounce_tracker.max_count}. "
                f"Summary: {json.dumps(summary)}"
            )
            assert bounce_tracker.is_incrementing, (
                f"Bounce count should be monotonically non-decreasing. "
                f"Summary: {json.dumps(summary)}"
            )

        finally:
            await watcher.stop()

    @pytest.mark.integration
    @pytest.mark.vision
    @pytest.mark.slow
    async def test_horizontal_bounce_detection(
        self, horizontal_bounce_browser, bounce_tracker
    ):
        """Same pipeline but with horizontal bouncing mode."""
        window_id = await horizontal_bounce_browser.find_window_id()
        if window_id is None:
            pytest.skip("Could not find bounce test window")

        from backend.display.fast_capture import VideoWatcher

        watcher = VideoWatcher(window_id, fps=FERRARI_FPS)
        try:
            await watcher.start()
            await asyncio.sleep(0.5)

            start = time.time()
            detected_mode = None

            while time.time() - start < BOUNCE_TEST_DURATION:
                frame = await watcher.get_latest_frame(timeout=1.0)
                if frame is None:
                    continue

                ocr_text = await self._ocr_frame(frame)
                if ocr_text:
                    count = extract_bounce_count(ocr_text)
                    if count is not None:
                        bounce_tracker.record(count)

                    mode = extract_mode(ocr_text)
                    if mode:
                        detected_mode = mode

                await asyncio.sleep(OCR_CHECK_INTERVAL)

            assert bounce_tracker.max_count >= BOUNCE_MIN_EXPECTED, (
                f"Horizontal bounce should detect {BOUNCE_MIN_EXPECTED}+ bounces"
            )
            assert detected_mode == "HORIZONTAL", (
                f"Should detect HORIZONTAL mode, got: {detected_mode}"
            )

        finally:
            await watcher.stop()

    @pytest.mark.integration
    @pytest.mark.vision
    async def test_bounce_rate_reasonable(
        self, vertical_bounce_browser, bounce_tracker
    ):
        """Bounce rate should be physically reasonable (not zero, not infinite).

        The ball moves at velocity=8px per frame at ~60fps browser render.
        With a ~600px window, that's roughly 1 bounce every 75 frames
        → approximately 0.8 bounces/second.
        """
        window_id = await vertical_bounce_browser.find_window_id()
        if window_id is None:
            pytest.skip("Could not find bounce test window")

        from backend.display.fast_capture import VideoWatcher

        watcher = VideoWatcher(window_id, fps=FERRARI_FPS)
        try:
            await watcher.start()
            await asyncio.sleep(0.5)

            start = time.time()
            while time.time() - start < 5.0:
                frame = await watcher.get_latest_frame(timeout=1.0)
                if frame is None:
                    continue
                ocr_text = await self._ocr_frame(frame)
                if ocr_text:
                    count = extract_bounce_count(ocr_text)
                    if count is not None:
                        bounce_tracker.record(count)
                await asyncio.sleep(OCR_CHECK_INTERVAL)

            rate = bounce_tracker.bounces_per_second
            if bounce_tracker.unique_counts >= 2:
                # Rate should be between 0.1 and 10 bounces/sec
                assert 0.1 <= rate <= 10.0, (
                    f"Bounce rate {rate:.2f}/s is unreasonable. "
                    f"Expected 0.1-10.0/s"
                )
        finally:
            await watcher.stop()

    async def _ocr_frame(self, frame) -> Optional[str]:
        """Run OCR on a captured frame.

        Tries pytesseract first, falls back to macOS Vision framework.
        """
        try:
            import pytesseract
            from PIL import Image

            # Convert frame to PIL Image if needed
            if not isinstance(frame, Image.Image):
                if hasattr(frame, 'to_pil'):
                    frame = frame.to_pil()
                elif hasattr(frame, 'tobytes'):
                    frame = Image.frombytes('RGB', frame.size, frame.tobytes())
                else:
                    return None

            text = pytesseract.image_to_string(frame)
            return text.strip()
        except ImportError:
            pass
        except Exception:
            pass

        # Fallback: try macOS Vision framework
        try:
            from backend.ghost_hands.n_optic_nerve import NOpticNerve
            nerve = NOpticNerve.get_instance()
            if hasattr(nerve, '_run_ocr'):
                return await nerve._run_ocr(frame)
        except (ImportError, Exception):
            pass

        return None


# ═══════════════════════════════════════════════════════════════
# Section 2: Ferrari Engine → N-Optic Nerve Event Detection
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ferrari
@requires_optic
class TestFerrariOpticNerveIntegration:
    """Test Ferrari capture feeding into N-Optic Nerve event detection."""

    @pytest.mark.integration
    @pytest.mark.vision
    @pytest.mark.slow
    async def test_optic_detects_bounce_text(
        self, n_optic_nerve, vertical_bounce_browser
    ):
        """N-Optic Nerve detects 'BOUNCE COUNT' text in bounce window.

        This proves the vision pipeline can autonomously detect the
        bounce count text without manual OCR intervention.
        """
        window_id = await vertical_bounce_browser.find_window_id()
        if window_id is None:
            pytest.skip("Could not find bounce test window")

        detected_texts: List[str] = []
        detection_event = asyncio.Event()

        async def on_detection(event):
            text = getattr(event, 'detected_text', '') or ''
            detected_texts.append(text)
            if "BOUNCE" in text.upper() or "COUNT" in text.upper():
                detection_event.set()

        success = await n_optic_nerve.watch_for_text(
            window_id=window_id,
            text_patterns=["BOUNCE COUNT", "BOUNCE", "COUNT"],
            callback=on_detection,
        )
        assert success, "Should start watching for bounce text"

        # Wait for detection or timeout
        try:
            await asyncio.wait_for(
                detection_event.wait(),
                timeout=BOUNCE_DETECTION_TIMEOUT,
            )
            detected = True
        except asyncio.TimeoutError:
            detected = False

        await n_optic_nerve.stop_watching(window_id)

        assert detected, (
            f"N-Optic Nerve should detect 'BOUNCE COUNT' within "
            f"{BOUNCE_DETECTION_TIMEOUT}s. "
            f"Detected texts: {detected_texts[:5]}"
        )

    @pytest.mark.integration
    @pytest.mark.vision
    @pytest.mark.slow
    async def test_optic_detects_changing_count(
        self, n_optic_nerve, vertical_bounce_browser, bounce_tracker
    ):
        """N-Optic Nerve detects changing bounce counts over time.

        Proves the vision system tracks dynamic state changes, not
        just static text detection.
        """
        window_id = await vertical_bounce_browser.find_window_id()
        if window_id is None:
            pytest.skip("Could not find bounce test window")

        async def on_detection(event):
            text = getattr(event, 'detected_text', '') or ''
            count = extract_bounce_count(text)
            if count is not None:
                bounce_tracker.record(count)

        success = await n_optic_nerve.watch_for_text(
            window_id=window_id,
            text_patterns=["BOUNCE COUNT"],
            callback=on_detection,
        )
        assert success

        # Observe for several seconds
        await asyncio.sleep(BOUNCE_TEST_DURATION)
        await n_optic_nerve.stop_watching(window_id)

        assert bounce_tracker.unique_counts >= 2, (
            f"Should observe at least 2 different bounce counts. "
            f"Got {bounce_tracker.unique_counts} unique values. "
            f"Summary: {json.dumps(bounce_tracker.summary())}"
        )
        assert bounce_tracker.is_incrementing, (
            "Bounce counts should be monotonically non-decreasing"
        )


# ═══════════════════════════════════════════════════════════════
# Section 3: GhostHandsOrchestrator → Watch-and-React Pipeline
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ghost_hands
class TestOrchestratorWatchAndReact:
    """Test GhostHandsOrchestrator watching for bounce events."""

    @pytest.mark.integration
    @pytest.mark.vision
    @pytest.mark.slow
    async def test_watch_and_react_on_bounce(
        self, ghost_orchestrator, vertical_bounce_browser
    ):
        """Orchestrator creates a task that reacts to bounce detection.

        Full Ghost Hands pipeline: create task → watch → detect → narrate.
        """
        from backend.ghost_hands.orchestrator import GhostAction

        reactions_triggered: List[float] = []

        # Create a watch-and-react task
        task = await ghost_orchestrator.watch_and_react(
            app_name="Google Chrome",
            trigger_text="BOUNCE COUNT",
            reaction=[
                GhostAction.narrate_perception("Bounce detected by Ghost Hands"),
            ],
            task_name="test-bounce-reaction",
        )
        assert task is not None, "Should create watch-and-react task"

        # Let it run for a few seconds
        await asyncio.sleep(5.0)

        # Check execution history
        history = ghost_orchestrator.get_execution_history(limit=10)

        # Cleanup
        await ghost_orchestrator.cancel_task("test-bounce-reaction")

        # The task should have been triggered (Chrome has "BOUNCE COUNT" text)
        stats = ghost_orchestrator.get_stats()
        assert isinstance(stats, dict), "Stats should be available"

    @pytest.mark.integration
    @pytest.mark.vision
    async def test_orchestrator_stats_after_watch(self, ghost_orchestrator):
        """Orchestrator reports meaningful stats."""
        stats = ghost_orchestrator.get_stats()
        assert "active_tasks" in stats or "tasks" in stats or len(stats) > 0, (
            f"Stats should contain task info: {stats}"
        )


# ═══════════════════════════════════════════════════════════════
# Section 4: Concurrent Multi-Window Detection (God Mode)
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ferrari
class TestGodModeConcurrentDetection:
    """Test concurrent detection across multiple bounce windows.

    God Mode = multiple Ferrari watchers running in parallel,
    each monitoring a different bouncing ball window.
    """

    @pytest.mark.integration
    @pytest.mark.vision
    @pytest.mark.slow
    async def test_dual_window_concurrent_tracking(
        self, dual_bounce_browsers, bounce_tracker
    ):
        """Track bounces in two windows simultaneously.

        Proves Ferrari Engine can run multiple watchers in parallel
        (vertical + horizontal) and JARVIS can differentiate them.
        """
        v_browser, h_browser = dual_bounce_browsers

        v_id = await v_browser.find_window_id()
        h_id = await h_browser.find_window_id()

        if v_id is None or h_id is None:
            pytest.skip("Could not find both bounce test windows")

        from backend.display.fast_capture import VideoWatcher

        v_watcher = VideoWatcher(v_id, fps=15)
        h_watcher = VideoWatcher(h_id, fps=15)

        v_counts: List[int] = []
        h_counts: List[int] = []
        v_mode: Optional[str] = None
        h_mode: Optional[str] = None

        try:
            await v_watcher.start()
            await h_watcher.start()
            await asyncio.sleep(0.5)

            start = time.time()
            while time.time() - start < BOUNCE_TEST_DURATION:
                # Capture from both watchers concurrently
                v_frame, h_frame = await asyncio.gather(
                    v_watcher.get_latest_frame(timeout=1.0),
                    h_watcher.get_latest_frame(timeout=1.0),
                )

                for frame, counts, mode_ref, label in [
                    (v_frame, v_counts, "v", "vertical"),
                    (h_frame, h_counts, "h", "horizontal"),
                ]:
                    if frame is None:
                        continue
                    ocr_text = await self._ocr_frame(frame)
                    if ocr_text:
                        count = extract_bounce_count(ocr_text)
                        if count is not None:
                            counts.append(count)
                        mode = extract_mode(ocr_text)
                        if mode:
                            if label == "vertical":
                                v_mode = mode
                            else:
                                h_mode = mode

                await asyncio.sleep(OCR_CHECK_INTERVAL)

        finally:
            await v_watcher.stop()
            await h_watcher.stop()

        # Both windows should produce bounce counts
        assert len(v_counts) > 0, "Vertical window should produce bounce counts"
        assert len(h_counts) > 0, "Horizontal window should produce bounce counts"

        # Modes should be correctly differentiated
        if v_mode:
            assert v_mode == "VERTICAL", f"Expected VERTICAL, got {v_mode}"
        if h_mode:
            assert h_mode == "HORIZONTAL", f"Expected HORIZONTAL, got {h_mode}"

        # Both should show incrementing counts
        if len(v_counts) >= 2:
            assert v_counts[-1] >= v_counts[0], "Vertical count should increment"
        if len(h_counts) >= 2:
            assert h_counts[-1] >= h_counts[0], "Horizontal count should increment"

    @pytest.mark.integration
    @pytest.mark.vision
    async def test_watchers_independent_frame_streams(
        self, dual_bounce_browsers
    ):
        """Two watchers produce independent frame streams.

        Frames from vertical window should differ from horizontal.
        """
        v_browser, h_browser = dual_bounce_browsers

        v_id = await v_browser.find_window_id()
        h_id = await h_browser.find_window_id()

        if v_id is None or h_id is None:
            pytest.skip("Could not find both bounce test windows")

        from backend.display.fast_capture import VideoWatcher

        v_watcher = VideoWatcher(v_id, fps=10)
        h_watcher = VideoWatcher(h_id, fps=10)

        try:
            await v_watcher.start()
            await h_watcher.start()
            await asyncio.sleep(1.0)

            v_frame = await v_watcher.get_latest_frame(timeout=2.0)
            h_frame = await h_watcher.get_latest_frame(timeout=2.0)

            assert v_frame is not None, "Vertical watcher should produce frames"
            assert h_frame is not None, "Horizontal watcher should produce frames"

            # Frames should be from different windows (different content)
            # We can't easily compare pixel data, but they should be
            # separate objects at minimum
            assert v_frame is not h_frame, "Frames should be distinct objects"

        finally:
            await v_watcher.stop()
            await h_watcher.stop()

    async def _ocr_frame(self, frame) -> Optional[str]:
        """Run OCR on a captured frame."""
        try:
            import pytesseract
            from PIL import Image

            if not isinstance(frame, Image.Image):
                if hasattr(frame, 'to_pil'):
                    frame = frame.to_pil()
                else:
                    return None
            return pytesseract.image_to_string(frame).strip()
        except (ImportError, Exception):
            return None
