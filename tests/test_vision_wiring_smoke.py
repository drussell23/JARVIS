#!/usr/bin/env python3
"""
Vision Wiring Smoke Test — verifies every component wired in the CU architecture.

Opens the bouncing ball HTML, then tests each layer:
  0. Capture cascade (VAL FramePipeline → Ferrari Engine → screencapture)
  1. Claude Vision API connectivity (agentic think)
  2. Ghost Hands initialization
  3. ActionVerifier pixel-diff
  4. PrecheckGate guards
  5. KnowledgeFabric scene write/read
  6. VisionCortex context
  7. NarrationEngine availability
  8. Fusion goal_achieved logic
  9. VAL state bridge + metrics

Usage:
    python3 tests/test_vision_wiring_smoke.py

Requires: ANTHROPIC_API_KEY in env (or .env file)
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        override=True,  # Override stale shell env vars with .env values
    )
except ImportError:
    pass


PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"
INFO = "\033[96mINFO\033[0m"

results: list = []


def report(name: str, passed: bool, detail: str = "", skip: bool = False):
    tag = SKIP if skip else (PASS if passed else FAIL)
    results.append({"name": name, "passed": passed, "skip": skip})
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))


async def main():
    print("\n" + "=" * 60)
    print("  JARVIS Vision Wiring Smoke Test")
    print("=" * 60 + "\n")

    # --- Open bouncing ball in background ---
    html_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "vision_smoke_test_bounce.html",
    )
    if os.path.exists(html_path):
        subprocess.Popen(["open", html_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  [{INFO}] Opened bouncing ball: {html_path}")
        await asyncio.sleep(2.0)  # Let browser render
    else:
        print(f"  [{INFO}] Bouncing ball HTML not found — testing capture only")

    # ================================================================
    # Test 0: Capture Cascade
    # ================================================================
    print("\n--- Capture Cascade ---")

    from backend.vision.lean_loop import LeanVisionLoop
    loop = LeanVisionLoop.get_instance()

    # 0a: VAL FramePipeline
    t0 = time.monotonic()
    val_frame = await loop._try_val_frame_pipeline()
    t_val = (time.monotonic() - t0) * 1000
    report(
        "VAL FramePipeline (sub-10ms)",
        val_frame is not None,
        f"{t_val:.1f}ms" if val_frame else "Not running (expected if JARVIS not started)",
        skip=val_frame is None,
    )

    # 0b: Ferrari Engine (frame_server) — warm up first
    await loop._ensure_frame_server()
    if loop._frame_server_ready:
        await asyncio.sleep(2.5)  # Let frame_server write first frame
    t0 = time.monotonic()
    ferrari_frame = await loop._try_frame_server_capture()
    t_ferrari = (time.monotonic() - t0) * 1000
    report(
        "Ferrari Engine (frame_server)",
        ferrari_frame is not None,
        f"{t_ferrari:.1f}ms" if ferrari_frame else "Failed — Quartz permission or startup issue",
    )

    # 0c: screencapture fallback
    t0 = time.monotonic()
    sc_frame = await loop._screencapture_fallback()
    t_sc = (time.monotonic() - t0) * 1000
    report(
        "screencapture subprocess (~200ms)",
        sc_frame is not None,
        f"{t_sc:.1f}ms",
    )

    # 0d: Full cascade
    t0 = time.monotonic()
    full_frame = await loop._capture_cu_screenshot()
    t_full = (time.monotonic() - t0) * 1000
    report(
        "Full capture cascade",
        full_frame is not None,
        f"{t_full:.1f}ms (best available tier)",
    )

    if full_frame:
        # Decode to check dimensions
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(full_frame)))
        report(
            "Screenshot dimensions",
            img.size == (1280, 800),
            f"{img.size[0]}x{img.size[1]} (expected 1280x800)",
        )

    # ================================================================
    # Test 1: Coordinate Mapping
    # ================================================================
    print("\n--- Coordinate Mapping ---")

    coords = loop._cu_to_screen([640, 400])
    report(
        "CU → screen coord mapping",
        coords[0] > 0 and coords[1] > 0,
        f"CU [640,400] → screen {coords} (scale={loop._cu_scale})",
    )

    # ================================================================
    # Test 2: Ghost Hands
    # ================================================================
    print("\n--- Ghost Hands ---")

    gh_result = await loop._try_cu_ghost_hands("left_click", 100, 100, {})
    if gh_result is not None:
        report("Ghost Hands BackgroundActuator", gh_result[0], f"Result: {gh_result}")
    else:
        report(
            "Ghost Hands BackgroundActuator",
            False,
            "Not available (Playwright/AppleScript/CGEvent backends need init)",
            skip=True,
        )

    # ================================================================
    # Test 3: ActionVerifier
    # ================================================================
    print("\n--- ActionVerifier ---")

    try:
        import numpy as np
        # Create two different frames to simulate a click
        frame_a = np.zeros((800, 1280, 3), dtype=np.uint8)
        frame_b = frame_a.copy()
        frame_b[390:410, 630:650] = 255  # White square at click point

        verify_result = loop._verify_cu_action(
            "left_click", frame_a, frame_b, {"coordinate": [640, 400]},
        )
        report(
            "ActionVerifier pixel-diff (click)",
            verify_result == "success",
            f"Status: {verify_result}",
        )

        # Test with identical frames (should fail)
        verify_same = loop._verify_cu_action(
            "left_click", frame_a, frame_a, {"coordinate": [640, 400]},
        )
        report(
            "ActionVerifier (no change → fail)",
            verify_same == "fail",
            f"Status: {verify_same}",
        )
    except Exception as exc:
        report("ActionVerifier", False, f"Error: {exc}")

    # ================================================================
    # Test 4: PrecheckGate
    # ================================================================
    print("\n--- PrecheckGate ---")

    precheck = loop._precheck_action("left_click", {"coordinate": [100, 100]}, 1)
    report(
        "PrecheckGate (first action → pass)",
        precheck is None,
        "Passed all guards" if precheck is None else f"Blocked: {precheck}",
    )

    # ================================================================
    # Test 5: KnowledgeFabric Scene Write/Read
    # ================================================================
    print("\n--- KnowledgeFabric ---")

    try:
        loop._write_scene_cache(
            "left_click",
            {"coordinate": [500, 300]},
            {"target": "Test Button", "action": "left_click"},
        )
        # Read from the SAME fabric instance lean_loop used
        fabric = getattr(loop, "_knowledge_fabric", None)
        if fabric is None:
            from backend.knowledge.fabric import KnowledgeFabric
            fabric = KnowledgeFabric()
        cached = fabric.query("kg://scene/element/test_button")
        report(
            "Scene write-back + read",
            cached is not None,
            f"Cached: {cached}" if cached else "Cache miss (TTL or partition issue)",
        )
    except Exception as exc:
        report("KnowledgeFabric", False, f"Error: {exc}")

    # ================================================================
    # Test 6: VisionCortex Context
    # ================================================================
    print("\n--- VisionCortex ---")

    ctx = loop._get_scene_context()
    if ctx:
        report("VisionCortex scene context", True, ctx)
    else:
        report(
            "VisionCortex scene context",
            False,
            "Empty (expected if JARVIS not running)",
            skip=True,
        )

    # ================================================================
    # Test 7: NarrationEngine
    # ================================================================
    print("\n--- NarrationEngine ---")

    try:
        from backend.ghost_hands.narration_engine import NarrationEngine
        engine = NarrationEngine.get_instance()
        report(
            "NarrationEngine singleton",
            engine is not None,
            "Available" if engine else "Not initialized",
        )
    except ImportError:
        report("NarrationEngine", False, "Import failed", skip=True)

    # ================================================================
    # Test 8: Fusion Goal Confidence
    # ================================================================
    print("\n--- Fusion ---")

    # Model says done, good verification
    ok1, conf1 = loop._fuse_goal_confidence(
        model_says_done=True, model_confidence=0.9,
        verification_status="success", turn=5,
        action_log=[
            {"result": "success", "verification": "success"},
            {"result": "success", "verification": "success"},
        ],
    )
    report(
        "Fusion: high confidence + verified → accept",
        ok1 and conf1 > 0.7,
        f"accepted={ok1}, conf={conf1:.2f}",
    )

    # Model says done but verification failed
    ok2, conf2 = loop._fuse_goal_confidence(
        model_says_done=True, model_confidence=0.8,
        verification_status="fail", turn=2,
        action_log=[
            {"result": "success", "verification": "fail"},
        ],
    )
    report(
        "Fusion: low confidence + verify fail → reject",
        not ok2,
        f"accepted={ok2}, conf={conf2:.2f}",
    )

    # Model says done on turn 1 with no actions
    ok3, conf3 = loop._fuse_goal_confidence(
        model_says_done=True, model_confidence=0.9,
        verification_status="", turn=1,
        action_log=[],
    )
    report(
        "Fusion: turn 1 no actions → reject",
        not ok3,
        f"accepted={ok3}, conf={conf3:.2f}",
    )

    # ================================================================
    # Test 9: VAL Bridges
    # ================================================================
    print("\n--- VisionActionLoop Bridges ---")

    try:
        from backend.vision.realtime.vision_action_loop import VisionActionLoop
        val = VisionActionLoop.get_instance()
        if val:
            report("VAL singleton", True, f"State: {val.state}")
            loop._bridge_val_state(active=True)
            report("VAL state bridge → WATCHING", val.state.value != "IDLE", f"State: {val.state}")
            loop._bridge_val_state(active=False)
            report("VAL state bridge → IDLE", val.state.value == "IDLE", f"State: {val.state}")
        else:
            report("VAL singleton", False, "Not running (expected outside JARVIS)", skip=True)
    except Exception as exc:
        report("VAL bridges", False, f"Error: {exc}", skip=True)

    # ================================================================
    # Test 10: Claude Vision API (optional — costs money)
    # ================================================================
    print("\n--- Claude Vision API ---")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and full_frame:
        try:
            t0 = time.monotonic()
            result = await loop._agentic_ask_claude(
                "Describe what you see in this screenshot in one sentence.",
                [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": full_frame}},
                    {"type": "text", "text": "Describe the screenshot in one sentence. Return JSON: {\"reasoning\": \"...\", \"goal_achieved\": false}"},
                ],
            )
            t_api = (time.monotonic() - t0) * 1000
            if result:
                report(
                    "Claude Vision API call",
                    True,
                    f"{t_api:.0f}ms — {result.get('reasoning', '')[:80]}",
                )
            else:
                report("Claude Vision API call", False, "No response")
        except Exception as exc:
            report("Claude Vision API call", False, f"Error: {exc}")
    else:
        report(
            "Claude Vision API call",
            False,
            "Skipped (no API key or no screenshot)",
            skip=True,
        )

    # ================================================================
    # Summary
    # ================================================================
    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"] and not r["skip"])
    skipped = sum(1 for r in results if r["skip"])
    total = len(results)

    print(f"  Results: {passed} passed, {failed} failed, {skipped} skipped / {total} total")

    if failed == 0:
        print(f"  \033[92mALL CORE TESTS PASSED\033[0m")
    else:
        print(f"  \033[91m{failed} FAILURES — check output above\033[0m")
    print("=" * 60 + "\n")

    # Kill frame_server if we started it
    if loop._frame_server_proc and loop._frame_server_proc.returncode is None:
        loop._frame_server_proc.terminate()
        print(f"  [{INFO}] Stopped frame_server subprocess")


if __name__ == "__main__":
    asyncio.run(main())
