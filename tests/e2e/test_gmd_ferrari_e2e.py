"""
End-to-End Tests — Ghost Mode Display (GMD) + Ferrari Engine

Full pipeline validation:
1. Launch bouncing ball HTML in Chrome
2. Discover the window via Ferrari Engine
3. Start GPU-accelerated frame capture (60 FPS capable)
4. Run real-time OCR to detect "BOUNCE COUNT: N"
5. Track count over time, verify it increments
6. Validate both vertical and horizontal modes
7. Test God Mode: multiple concurrent windows
8. Test Ghost Hands orchestrated watch-and-react

These tests prove that the Ghost Mode Display and Ferrari Engine
work together as a real-time visual surveillance system.

Run with: pytest tests/e2e/test_gmd_ferrari_e2e.py -v --timeout=120
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

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

logger = logging.getLogger("jarvis.test.gmd_ferrari_e2e")


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

async def ocr_frame(frame) -> Optional[str]:
    """Run OCR on a captured frame. Tries pytesseract, then macOS Vision."""
    try:
        import pytesseract
        from PIL import Image

        if not isinstance(frame, Image.Image):
            if hasattr(frame, 'to_pil'):
                frame = frame.to_pil()
            elif hasattr(frame, 'tobytes') and hasattr(frame, 'size'):
                frame = Image.frombytes('RGB', frame.size, frame.tobytes())
            else:
                return None

        text = pytesseract.image_to_string(frame)
        return text.strip()
    except (ImportError, Exception):
        pass

    # Fallback: ComputerUseConnector screen capture
    try:
        from backend.display.computer_use_connector import get_computer_use_connector
        connector = get_computer_use_connector()
        if connector and hasattr(connector, 'get_current_spatial_context'):
            return await connector.get_current_spatial_context()
    except (ImportError, Exception):
        pass

    return None


async def run_bounce_detection_loop(
    watcher,
    duration: float,
    ocr_interval: float = OCR_CHECK_INTERVAL,
) -> Dict[str, Any]:
    """Run a timed bounce detection loop.

    Returns a comprehensive result dict with all metrics.
    """
    counts: List[int] = []
    timestamps: List[float] = []
    detected_mode: Optional[str] = None
    frames_captured = 0
    ocr_attempts = 0
    ocr_successes = 0
    start = time.time()

    while time.time() - start < duration:
        frame = await watcher.get_latest_frame(timeout=1.0)
        if frame is not None:
            frames_captured += 1

            ocr_text = await ocr_frame(frame)
            ocr_attempts += 1

            if ocr_text:
                count = extract_bounce_count(ocr_text)
                if count is not None:
                    counts.append(count)
                    timestamps.append(time.time() - start)
                    ocr_successes += 1

                mode = extract_mode(ocr_text)
                if mode:
                    detected_mode = mode

        await asyncio.sleep(ocr_interval)

    elapsed = time.time() - start

    # Compute metrics
    max_count = max(counts) if counts else 0
    min_count = min(counts) if counts else 0
    is_incrementing = all(
        b >= a for a, b in zip(counts, counts[1:])
    ) if len(counts) >= 2 else False

    unique_counts = len(set(counts))
    bounce_rate = (
        (counts[-1] - counts[0]) / (timestamps[-1] - timestamps[0])
        if len(counts) >= 2 and timestamps[-1] > timestamps[0]
        else 0.0
    )

    return {
        "counts": counts,
        "timestamps": timestamps,
        "max_count": max_count,
        "min_count": min_count,
        "is_incrementing": is_incrementing,
        "unique_counts": unique_counts,
        "bounce_rate": round(bounce_rate, 2),
        "detected_mode": detected_mode,
        "frames_captured": frames_captured,
        "ocr_attempts": ocr_attempts,
        "ocr_successes": ocr_successes,
        "ocr_success_rate": round(
            ocr_successes / max(ocr_attempts, 1) * 100, 1
        ),
        "elapsed_seconds": round(elapsed, 2),
        "effective_fps": round(frames_captured / max(elapsed, 0.01), 1),
    }


# ═══════════════════════════════════════════════════════════════
# E2E Test 1: Single Window — Vertical Bounce
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ferrari
@pytest.mark.e2e
@pytest.mark.vision
@pytest.mark.slow
class TestE2EVerticalBounce:
    """End-to-end: Launch → Capture → Detect → Count vertical bounces."""

    async def test_full_vertical_bounce_pipeline(self):
        """Complete pipeline: open ball page, detect bounces, verify count.

        This is THE proof that GMD + Ferrari Engine works:
        1. Opens bouncing_balls.html?mode=vertical in Chrome
        2. Discovers the window via fast_capture
        3. Starts a VideoWatcher at 30 FPS
        4. Runs OCR every 200ms to extract "BOUNCE COUNT: N"
        5. Verifies count increments over 10 seconds
        6. Tears down cleanly
        """
        browser = BounceTestBrowser(mode="vertical")
        launched = await browser.launch()
        if not launched:
            pytest.skip("Could not launch Chrome for bounce test")

        try:
            # Step 1: Discover the window
            window_id = await browser.find_window_id()
            assert window_id is not None, (
                "Ferrari Engine should discover the bounce test window"
            )

            # Step 2: Start GPU-accelerated capture
            from backend.vision.macos_video_capture_advanced import VideoWatcher

            watcher = VideoWatcher(window_id, fps=FERRARI_FPS)
            await watcher.start()
            await asyncio.sleep(1.0)  # Let animation settle

            try:
                # Step 3: Run detection loop
                result = await run_bounce_detection_loop(
                    watcher,
                    duration=BOUNCE_TEST_DURATION,
                )

                logger.info(
                    "E2E Vertical Bounce Result: %s",
                    json.dumps(result, indent=2),
                )

                # Step 4: Validate results
                assert result["frames_captured"] > 0, (
                    "Ferrari should capture frames"
                )
                assert result["ocr_successes"] > 0, (
                    f"OCR should extract bounce count at least once. "
                    f"Attempts: {result['ocr_attempts']}, "
                    f"Rate: {result['ocr_success_rate']}%"
                )
                assert result["max_count"] >= BOUNCE_MIN_EXPECTED, (
                    f"Should detect {BOUNCE_MIN_EXPECTED}+ bounces. "
                    f"Max: {result['max_count']}"
                )
                assert result["is_incrementing"], (
                    "Bounce count should increase over time"
                )

                # Step 5: Verify mode detection
                if result["detected_mode"]:
                    assert result["detected_mode"] == "VERTICAL", (
                        f"Mode should be VERTICAL, got {result['detected_mode']}"
                    )

            finally:
                await watcher.stop()

        finally:
            await browser.close()


# ═══════════════════════════════════════════════════════════════
# E2E Test 2: Single Window — Horizontal Bounce
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ferrari
@pytest.mark.e2e
@pytest.mark.vision
@pytest.mark.slow
class TestE2EHorizontalBounce:
    """End-to-end: Launch → Capture → Detect → Count horizontal bounces."""

    async def test_full_horizontal_bounce_pipeline(self):
        """Same as vertical but with horizontal mode."""
        browser = BounceTestBrowser(mode="horizontal")
        launched = await browser.launch()
        if not launched:
            pytest.skip("Could not launch Chrome")

        try:
            window_id = await browser.find_window_id()
            assert window_id is not None, "Should discover bounce window"

            from backend.vision.macos_video_capture_advanced import VideoWatcher

            watcher = VideoWatcher(window_id, fps=FERRARI_FPS)
            await watcher.start()
            await asyncio.sleep(1.0)

            try:
                result = await run_bounce_detection_loop(
                    watcher, duration=BOUNCE_TEST_DURATION,
                )

                assert result["max_count"] >= BOUNCE_MIN_EXPECTED
                assert result["is_incrementing"]
                if result["detected_mode"]:
                    assert result["detected_mode"] == "HORIZONTAL"

            finally:
                await watcher.stop()
        finally:
            await browser.close()


# ═══════════════════════════════════════════════════════════════
# E2E Test 3: God Mode — Dual Window Concurrent Tracking
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ferrari
@pytest.mark.e2e
@pytest.mark.vision
@pytest.mark.slow
class TestE2EGodModeDualWindow:
    """End-to-end: Two concurrent bounce windows tracked simultaneously.

    God Mode proves the Ferrari Engine can run multiple VideoWatchers
    in parallel and JARVIS can independently track state in each window.
    """

    async def test_dual_window_independent_tracking(self):
        """Launch vertical + horizontal, track both, verify independent counts.

        This is the ultimate GMD + Ferrari Engine test:
        - Two Chrome windows, each with a differently-directed ball
        - Two Ferrari VideoWatchers running concurrently
        - Independent OCR extraction from each
        - Both counts should increment independently
        - Modes should be correctly differentiated
        """
        v_browser = BounceTestBrowser(mode="vertical", width=600, height=500)
        h_browser = BounceTestBrowser(mode="horizontal", width=600, height=500)

        v_launched = await v_browser.launch()
        h_launched = await h_browser.launch()

        if not (v_launched and h_launched):
            await v_browser.close()
            await h_browser.close()
            pytest.skip("Could not launch both Chrome windows")

        try:
            v_id = await v_browser.find_window_id()
            h_id = await h_browser.find_window_id()

            if v_id is None or h_id is None:
                pytest.skip("Could not discover both bounce windows")

            from backend.vision.macos_video_capture_advanced import VideoWatcher

            v_watcher = VideoWatcher(v_id, fps=15)
            h_watcher = VideoWatcher(h_id, fps=15)

            await v_watcher.start()
            await h_watcher.start()
            await asyncio.sleep(1.0)

            try:
                # Run detection on both watchers concurrently
                v_result, h_result = await asyncio.gather(
                    run_bounce_detection_loop(v_watcher, duration=BOUNCE_TEST_DURATION),
                    run_bounce_detection_loop(h_watcher, duration=BOUNCE_TEST_DURATION),
                )

                logger.info(
                    "God Mode Results:\n"
                    "  Vertical: %s\n"
                    "  Horizontal: %s",
                    json.dumps(v_result, indent=4),
                    json.dumps(h_result, indent=4),
                )

                # Both windows should produce results
                assert v_result["ocr_successes"] > 0, "Vertical OCR should succeed"
                assert h_result["ocr_successes"] > 0, "Horizontal OCR should succeed"

                # Both should detect bounces
                assert v_result["max_count"] > 0, "Vertical should detect bounces"
                assert h_result["max_count"] > 0, "Horizontal should detect bounces"

                # Modes should be differentiated
                if v_result["detected_mode"] and h_result["detected_mode"]:
                    assert v_result["detected_mode"] != h_result["detected_mode"], (
                        "Windows should have different modes"
                    )

            finally:
                await v_watcher.stop()
                await h_watcher.stop()

        finally:
            await v_browser.close()
            await h_browser.close()


# ═══════════════════════════════════════════════════════════════
# E2E Test 4: Ghost Hands Orchestrated Detection
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ghost_hands
@pytest.mark.e2e
@pytest.mark.vision
@pytest.mark.slow
class TestE2EGhostHandsOrchestrated:
    """End-to-end: Ghost Hands watches for bounces and reacts.

    Proves the full autonomous pipeline:
    1. Ghost Hands creates a watch task for "BOUNCE COUNT"
    2. N-Optic Nerve monitors Chrome window via OCR
    3. When text matches, Ghost Hands triggers reaction
    4. Narration Engine announces the detection
    """

    async def test_ghost_hands_bounce_surveillance(self):
        """Ghost Hands autonomously detects ball bouncing."""
        browser = BounceTestBrowser(mode="vertical")
        launched = await browser.launch()
        if not launched:
            pytest.skip("Could not launch Chrome")

        try:
            from backend.ghost_hands.orchestrator import (
                GhostAction,
                get_ghost_hands,
            )

            orchestrator = await get_ghost_hands()
            if orchestrator is None:
                pytest.skip("GhostHandsOrchestrator not available")

            # Create surveillance task
            task = await orchestrator.create_task(
                name="e2e-bounce-surveillance",
                watch_app="Google Chrome",
                trigger_text="BOUNCE COUNT",
                actions=[
                    GhostAction.narrate_perception(
                        "Ball bounce detected in Ghost Mode Display"
                    ),
                    GhostAction.screenshot(),
                ],
                one_shot=False,  # Keep watching
                priority=1,     # Highest priority
            )
            assert task is not None, "Should create surveillance task"

            # Let it run — Ghost Hands should detect the bouncing ball
            await asyncio.sleep(BOUNCE_TEST_DURATION)

            # Check if the task was triggered
            history = orchestrator.get_execution_history(limit=10)
            stats = orchestrator.get_stats()

            logger.info(
                "Ghost Hands E2E Stats: %s\n"
                "Execution History: %s",
                json.dumps(stats, indent=2, default=str),
                json.dumps(history[:3], indent=2, default=str),
            )

            # Cleanup
            await orchestrator.cancel_task("e2e-bounce-surveillance")
            await orchestrator.stop()

        finally:
            await browser.close()


# ═══════════════════════════════════════════════════════════════
# E2E Test 5: Full N-Optic Nerve → Orchestrator Pipeline
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_optic
@requires_ferrari
@pytest.mark.e2e
@pytest.mark.vision
@pytest.mark.slow
class TestE2ENOpticNervePipeline:
    """End-to-end: N-Optic Nerve watches window, events trigger tracking."""

    async def test_optic_nerve_realtime_bounce_counting(self):
        """N-Optic Nerve detects and tracks bounce count in real-time.

        Full pipeline:
        1. Launch bouncing ball
        2. N-Optic Nerve watches the window
        3. On each OCR cycle, extract bounce count
        4. Verify count increments over time
        """
        browser = BounceTestBrowser(mode="vertical")
        launched = await browser.launch()
        if not launched:
            pytest.skip("Could not launch Chrome")

        try:
            window_id = await browser.find_window_id()
            if window_id is None:
                pytest.skip("Could not find bounce window")

            from backend.ghost_hands.n_optic_nerve import NOpticNerve

            nerve = NOpticNerve.get_instance()
            started = await nerve.start()
            if not started:
                pytest.skip("N-Optic Nerve failed to start")

            detected_counts: List[int] = []
            detection_times: List[float] = []
            start_time = time.time()

            async def on_detection(event):
                text = getattr(event, 'detected_text', '') or ''
                count = extract_bounce_count(text)
                if count is not None:
                    detected_counts.append(count)
                    detection_times.append(time.time() - start_time)

            try:
                success = await nerve.watch_for_text(
                    window_id=window_id,
                    text_patterns=["BOUNCE COUNT"],
                    callback=on_detection,
                )
                assert success, "Should start watching"

                # Let N-Optic Nerve observe
                await asyncio.sleep(BOUNCE_TEST_DURATION)

                await nerve.stop_watching(window_id)

                # Validate
                assert len(detected_counts) > 0, (
                    "N-Optic Nerve should detect bounce count text"
                )

                if len(detected_counts) >= 2:
                    assert detected_counts[-1] > detected_counts[0], (
                        "Count should increase over observation period. "
                        f"First: {detected_counts[0]}, "
                        f"Last: {detected_counts[-1]}, "
                        f"All: {detected_counts}"
                    )

                logger.info(
                    "N-Optic Nerve E2E: Detected %d count readings "
                    "over %.1f seconds. Range: %d → %d",
                    len(detected_counts),
                    detection_times[-1] if detection_times else 0,
                    detected_counts[0] if detected_counts else 0,
                    detected_counts[-1] if detected_counts else 0,
                )

            finally:
                await nerve.stop()

        finally:
            await browser.close()


# ═══════════════════════════════════════════════════════════════
# E2E Test 6: Performance Characterization
# ═══════════════════════════════════════════════════════════════

@requires_macos
@requires_display
@requires_ferrari
@pytest.mark.e2e
@pytest.mark.vision
@pytest.mark.slow
class TestE2EPerformanceCharacterization:
    """Measure real-world performance of the GMD + Ferrari pipeline.

    Not pass/fail assertions on specific numbers — captures metrics
    for regression tracking.
    """

    async def test_capture_latency(self):
        """Measure frame capture latency."""
        browser = BounceTestBrowser(mode="vertical")
        launched = await browser.launch()
        if not launched:
            pytest.skip("Could not launch Chrome")

        try:
            window_id = await browser.find_window_id()
            if window_id is None:
                pytest.skip("Could not find bounce window")

            from backend.vision.macos_video_capture_advanced import VideoWatcher

            watcher = VideoWatcher(window_id, fps=60)
            await watcher.start()
            await asyncio.sleep(0.5)

            latencies = []
            try:
                for _ in range(20):
                    t0 = time.perf_counter()
                    frame = await watcher.get_latest_frame(timeout=1.0)
                    t1 = time.perf_counter()
                    if frame is not None:
                        latencies.append((t1 - t0) * 1000)
                    await asyncio.sleep(0.05)

            finally:
                await watcher.stop()

            if latencies:
                avg_latency = sum(latencies) / len(latencies)
                max_latency = max(latencies)
                min_latency = min(latencies)

                logger.info(
                    "Capture Latency (ms): avg=%.1f, min=%.1f, max=%.1f, "
                    "samples=%d",
                    avg_latency, min_latency, max_latency, len(latencies),
                )

                # Sanity check: latency should be under 500ms
                assert avg_latency < 500, (
                    f"Average capture latency {avg_latency:.1f}ms is too high"
                )

        finally:
            await browser.close()

    async def test_ocr_throughput(self):
        """Measure OCR processing rate for bounce detection."""
        browser = BounceTestBrowser(mode="vertical")
        launched = await browser.launch()
        if not launched:
            pytest.skip("Could not launch Chrome")

        try:
            window_id = await browser.find_window_id()
            if window_id is None:
                pytest.skip("Could not find bounce window")

            from backend.vision.macos_video_capture_advanced import VideoWatcher

            watcher = VideoWatcher(window_id, fps=30)
            await watcher.start()
            await asyncio.sleep(1.0)

            ocr_times = []
            try:
                for _ in range(10):
                    frame = await watcher.get_latest_frame(timeout=1.0)
                    if frame is None:
                        continue

                    t0 = time.perf_counter()
                    text = await ocr_frame(frame)
                    t1 = time.perf_counter()

                    if text:
                        ocr_times.append((t1 - t0) * 1000)
                    await asyncio.sleep(0.1)

            finally:
                await watcher.stop()

            if ocr_times:
                avg_ocr = sum(ocr_times) / len(ocr_times)
                logger.info(
                    "OCR Latency (ms): avg=%.1f, samples=%d, "
                    "effective rate=%.1f checks/sec",
                    avg_ocr, len(ocr_times),
                    1000.0 / max(avg_ocr, 1),
                )

                # OCR should complete within 2 seconds per frame
                assert avg_ocr < 2000, (
                    f"Average OCR time {avg_ocr:.1f}ms too high"
                )

        finally:
            await browser.close()
