#!/usr/bin/env python3
"""
Smoke test for the Lean Vision Loop.

Run directly:  python3 backend/vision/test_lean_loop_smoke.py

Tests each step independently so you can see exactly where it breaks:
  1. CAPTURE  -- can we take a screenshot?
  2. THINK    -- does Claude respond to a vision query?
  3. ACT      -- can we click/type on screen?
  4. FULL     -- does the entire loop work end-to-end?
"""
import asyncio
import logging
import os
import sys
import time

# Set up path for running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
)
logger = logging.getLogger("smoke_test")


async def test_capture():
    """Test Step 1: Can we capture a screenshot?"""
    logger.info("=" * 60)
    logger.info("TEST 1: CAPTURE -- taking screenshot via async subprocess")
    logger.info("=" * 60)

    from vision.lean_loop import LeanVisionLoop
    loop = LeanVisionLoop()

    start = time.monotonic()
    b64, w, h = await loop._capture_screen()
    elapsed = time.monotonic() - start

    if b64 is None:
        logger.error("FAIL: Screenshot returned None (check Screen Recording permissions)")
        return False

    kb = len(b64) * 3 / 4 / 1024  # base64 to raw bytes to KB
    logger.info("PASS: Captured %dx%d image (%.0f KB JPEG, %.2fs)", w, h, kb, elapsed)
    return True


async def test_think(screenshot_b64=None, width=1024, height=640):
    """Test Step 2: Does Claude Vision respond?"""
    logger.info("=" * 60)
    logger.info("TEST 2: THINK -- sending screenshot to Claude Vision API")
    logger.info("=" * 60)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("FAIL: ANTHROPIC_API_KEY not set in environment")
        return False

    from vision.lean_loop import LeanVisionLoop
    loop = LeanVisionLoop()

    # Capture if not provided
    if screenshot_b64 is None:
        screenshot_b64, width, height = await loop._capture_screen()
        if screenshot_b64 is None:
            logger.error("FAIL: Cannot test THINK without a screenshot")
            return False

    start = time.monotonic()
    response = await loop._ask_claude(
        goal="Describe what you see on the screen",
        screenshot_b64=screenshot_b64,
        img_w=width,
        img_h=height,
        action_log=[],
        turn=1,
    )
    elapsed = time.monotonic() - start

    reasoning = response.get("reasoning", "(none)")
    scene = response.get("scene_summary", "(none)")
    goal_achieved = response.get("goal_achieved")

    if "timed out" in reasoning.lower() or "error" in reasoning.lower():
        logger.error("FAIL: Claude returned error: %s (%.1fs)", reasoning, elapsed)
        return False

    logger.info("PASS: Claude responded in %.1fs", elapsed)
    logger.info("  goal_achieved: %s", goal_achieved)
    logger.info("  reasoning: %s", reasoning[:150])
    logger.info("  scene: %s", scene[:150])
    return True


async def test_act():
    """Test Step 3: Can we run actions (click, type)?"""
    logger.info("=" * 60)
    logger.info("TEST 3: ACT -- testing pyautogui and clipboard")
    logger.info("=" * 60)

    # Test pyautogui import and screen size
    try:
        import pyautogui
        w, h = pyautogui.size()
        logger.info("  pyautogui OK: screen size %dx%d", w, h)
    except Exception as e:
        logger.error("FAIL: pyautogui error: %s", e)
        logger.error("  Check: System Preferences > Privacy > Accessibility")
        return False

    # Test clipboard (pbcopy)
    try:
        proc = await asyncio.create_subprocess_exec(
            "pbcopy",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate(b"lean_loop_smoke_test")
        if proc.returncode == 0:
            logger.info("  pbcopy OK: clipboard write works")
        else:
            logger.error("FAIL: pbcopy returned %d", proc.returncode)
            return False
    except Exception as e:
        logger.error("FAIL: pbcopy error: %s", e)
        return False

    # Test mouse position read (non-destructive)
    try:
        pos = pyautogui.position()
        logger.info("  mouse position: (%d, %d)", pos.x, pos.y)
    except Exception as e:
        logger.error("FAIL: Cannot read mouse position: %s", e)
        logger.error("  Check: System Preferences > Privacy > Accessibility")
        return False

    logger.info("PASS: Action ready (pyautogui + clipboard)")
    logger.info("  NOTE: Not testing actual clicks (would move your mouse)")
    return True


async def test_full_loop():
    """Test Step 4: Full end-to-end loop with a simple goal."""
    logger.info("=" * 60)
    logger.info("TEST 4: FULL LOOP -- describe the screen (goal on turn 1)")
    logger.info("=" * 60)

    from vision.lean_loop import LeanVisionLoop
    loop = LeanVisionLoop()

    start = time.monotonic()
    result = await loop.run(
        "Look at the screen and describe what you see. "
        "Set goal_achieved=true after describing it."
    )
    elapsed = time.monotonic() - start

    success = result.get("success", False)
    turns = result.get("turns", 0)
    result_text = result.get("result", "(none)")

    if success:
        logger.info("PASS: Loop completed in %.1fs (%d turns)", elapsed, turns)
        logger.info("  result: %s", result_text[:150])
    else:
        logger.error("FAIL: Loop failed in %.1fs (%d turns)", elapsed, turns)
        logger.error("  result: %s", result_text[:200])

    return success


async def main():
    logger.info("")
    logger.info("LEAN VISION LOOP SMOKE TEST")
    logger.info("===========================")
    logger.info("")

    results = {}

    # Step 1: Capture
    results["capture"] = await test_capture()
    logger.info("")

    # Step 2: Think
    if results["capture"]:
        results["think"] = await test_think()
    else:
        logger.warning("Skipping THINK test (CAPTURE failed)")
        results["think"] = False
    logger.info("")

    # Step 3: Act
    results["act"] = await test_act()
    logger.info("")

    # Step 4: Full loop (only if all prereqs pass)
    if all(results.values()):
        results["full_loop"] = await test_full_loop()
    else:
        logger.warning("Skipping FULL LOOP test (prerequisites failed)")
        results["full_loop"] = False

    # Summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("RESULTS SUMMARY")
    logger.info("=" * 60)
    for test_name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        logger.info("  %s: %s", test_name.upper().ljust(12), status)

    all_pass = all(results.values())
    logger.info("")
    if all_pass:
        logger.info("ALL TESTS PASSED -- Lean Vision Loop is ready")
    else:
        failed = [k for k, v in results.items() if not v]
        logger.error("FAILED TESTS: %s", ", ".join(failed))
        logger.error("Fix these before testing live with JARVIS")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
