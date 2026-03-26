#!/usr/bin/env python3
"""
JARVIS Vision-Language-Action (VLA) Pipeline

Dual-model parallel perception:
  - Doubleword 235B VL: fast structural read (text, numbers, elements)
  - Claude Vision: deep semantic understanding (scene, spatial, context)
  - Apple Vision OCR: local deterministic text extraction (fallback)

Both cloud models fire in parallel on the same frame. Results are fused
into a rich perception that JARVIS narrates with voice.

Usage:
    python3 tests/test_vision_realtime_sharp.py [--duration 60]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import os
import re
import subprocess
import sys
import time
from typing import Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_root, ".env"), override=True)
    load_dotenv(os.path.join(_root, "backend", ".env"), override=True)
except ImportError:
    pass

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Voice -- serial queue, ONE speaker at a time, never overlapping
# ---------------------------------------------------------------------------

_speech_queue: asyncio.Queue = None  # type: ignore[assignment]
_speech_task: Optional[asyncio.Task] = None


async def _speech_worker() -> None:
    """Drain the speech queue serially. One utterance at a time."""
    while True:
        text, voice = await _speech_queue.get()
        try:
            proc = await asyncio.create_subprocess_exec(
                "say", "-v", voice, "-r", "185", text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception:
            pass
        _speech_queue.task_done()


def _ensure_speech_worker() -> None:
    global _speech_queue, _speech_task
    if _speech_queue is None:
        _speech_queue = asyncio.Queue()
    if _speech_task is None or _speech_task.done():
        _speech_task = asyncio.ensure_future(_speech_worker())


async def jarvis_say(text: str, voice: str = "Daniel") -> None:
    """Queue speech and wait for it to finish. Never overlaps."""
    _ensure_speech_worker()
    await _speech_queue.put((text, voice))
    await _speech_queue.join()


def jarvis_say_background(text: str, voice: str = "Daniel") -> None:
    """Queue speech without waiting. Still serial — no overlap."""
    _ensure_speech_worker()
    _speech_queue.put_nowait((text, voice))


# ---------------------------------------------------------------------------
# Targeted window capture — capture Chrome even when terminal has focus
# ---------------------------------------------------------------------------

def _find_chrome_ball_window() -> Optional[int]:
    """Find the Chrome window ID showing the bouncing ball."""
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
        )
        for w in windows:
            owner = w.get("kCGWindowOwnerName", "")
            title = w.get("kCGWindowName", "")
            if "Chrome" in owner and "Bouncing Ball" in str(title):
                return w.get("kCGWindowNumber", 0)
    except Exception:
        pass
    return None


def _capture_window_raw_numpy(wid: int) -> Optional[np.ndarray]:
    """Raw Memory Bypass: Quartz CGImage → numpy array. Zero b64. Zero PNG.

    Returns RGB numpy array at CU resolution (1280x800), or None.
    This is the fast path for the Deterministic Retina — ~15ms total.
    """
    try:
        import Quartz
        from PIL import Image as _Img

        image_ref = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow,
            wid,
            Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
        if image_ref is None:
            return None

        w = Quartz.CGImageGetWidth(image_ref)
        h = Quartz.CGImageGetHeight(image_ref)
        provider = Quartz.CGImageGetDataProvider(image_ref)
        data = Quartz.CGDataProviderCopyData(provider)

        # BGRA raw pixels → numpy (zero copy where possible)
        arr = np.frombuffer(bytes(data), dtype=np.uint8).reshape((h, w, 4))
        # BGRA → RGB (drop alpha, swap channels)
        rgb = arr[:, :, [2, 1, 0]]

        # Resize to CU resolution via PIL (LANCZOS for quality)
        img = _Img.fromarray(rgb).resize((1280, 800), _Img.Resampling.LANCZOS)
        return np.array(img)
    except Exception:
        return None


async def _capture_window_raw_async(wid: int) -> Optional[np.ndarray]:
    """Async wrapper: runs the blocking Quartz capture in a thread pool.

    Principle 3 (Asynchronous Tendrils): the ~15ms CGWindowListCreateImage
    call runs in a ThreadPoolExecutor so it never blocks the event loop.
    """
    return await asyncio.get_event_loop().run_in_executor(
        None, _capture_window_raw_numpy, wid,
    )


def _numpy_to_b64(frame: np.ndarray) -> str:
    """Slow path: encode numpy → b64 PNG. Only for cloud model API calls."""
    from PIL import Image as _Img
    img = _Img.fromarray(frame)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# Ball Tracker — deterministic numpy, runs on every raw frame (~2ms)
# ---------------------------------------------------------------------------

class BallTracker:
    """Tracks a green ball across frames and detects bounces by velocity reversal.

    This is the Deterministic Retina — pure numpy, zero API calls, ~2ms/frame.
    It WATCHES the ball and COUNTS bounces independently, then cross-validates
    against the HUD text that OCR reads periodically.

    Bounce detection: when ball_x was increasing (moving right) and suddenly
    decreases (hit right wall and reversed), that's a horizontal bounce.
    Same logic for ball_y and vertical bounces.
    """

    def __init__(self) -> None:
        self.ball_x: int = 0
        self.ball_y: int = 0
        self.prev_x: int = 0
        self.prev_y: int = 0
        self.vel_x: float = 0.0  # positive = moving right
        self.vel_y: float = 0.0  # positive = moving down
        self.h_bounces: int = 0
        self.v_bounces: int = 0
        self.total_bounces: int = 0
        self.quadrant: str = "unknown"
        self.trail_direction: str = "unknown"
        self.frames_processed: int = 0
        self._last_bounce_frame: int = 0  # debounce: ignore bounces within 3 frames
        self._initialized: bool = False

    def process_frame(self, frame: np.ndarray) -> dict:
        """Process a raw RGB numpy frame. Returns perception dict.

        ~2ms on a 1280x800 frame. Zero allocations beyond the mask.
        """
        h, w = frame.shape[:2]
        green = frame[:, :, 1]  # green channel

        # Find bright green pixels (ball + trail)
        mask = green > 180
        ys, xs = np.where(mask)

        if len(xs) < 10:
            # No green pixels — ball not visible
            return self._state_dict("no_ball")

        # Ball centroid = mean of all bright green pixels
        cx = int(np.mean(xs))
        cy = int(np.mean(ys))

        # Bright core (the ball itself, not the trail) for better centroid
        core_mask = green > 230
        core_ys, core_xs = np.where(core_mask)
        if len(core_xs) > 5:
            cx = int(np.mean(core_xs))
            cy = int(np.mean(core_ys))

        self.prev_x, self.prev_y = self.ball_x, self.ball_y
        self.ball_x, self.ball_y = cx, cy
        self.frames_processed += 1

        # Need at least 2 frames for velocity
        if not self._initialized:
            self._initialized = True
            self._update_quadrant(w, h)
            self._update_trail(xs, ys, cx, cy)
            return self._state_dict("initializing")

        # Velocity = position delta
        new_vel_x = float(self.ball_x - self.prev_x)
        new_vel_y = float(self.ball_y - self.prev_y)

        # Bounce detection: velocity sign flip with debounce
        frames_since_bounce = self.frames_processed - self._last_bounce_frame
        if frames_since_bounce > 3:  # debounce: 3 frames minimum between bounces
            # Horizontal bounce: vel_x sign flipped AND ball near left/right edge
            if (self.vel_x * new_vel_x < 0) and abs(new_vel_x) > 2:
                near_edge = cx < 50 or cx > (w - 50)
                if near_edge:
                    self.h_bounces += 1
                    self.total_bounces += 1
                    self._last_bounce_frame = self.frames_processed

            # Vertical bounce: vel_y sign flipped AND ball near top/bottom edge
            if (self.vel_y * new_vel_y < 0) and abs(new_vel_y) > 2:
                near_edge = cy < 50 or cy > (h - 50)
                if near_edge:
                    self.v_bounces += 1
                    self.total_bounces += 1
                    self._last_bounce_frame = self.frames_processed

        self.vel_x = new_vel_x
        self.vel_y = new_vel_y

        self._update_quadrant(w, h)
        self._update_trail(xs, ys, cx, cy)

        return self._state_dict("tracking")

    def _update_quadrant(self, w: int, h: int) -> None:
        mid_x, mid_y = w // 2, h // 2
        if self.ball_x < mid_x:
            self.quadrant = "top-left" if self.ball_y < mid_y else "bottom-left"
        else:
            self.quadrant = "top-right" if self.ball_y < mid_y else "bottom-right"

    def _update_trail(
        self, xs: np.ndarray, ys: np.ndarray, cx: int, cy: int,
    ) -> None:
        """Trail direction = where the green mass is relative to the ball core."""
        mass_x = float(np.mean(xs))
        mass_y = float(np.mean(ys))
        dx = mass_x - cx
        dy = mass_y - cy

        parts = []
        if abs(dy) > 5:
            parts.append("up" if dy < 0 else "down")
        if abs(dx) > 5:
            parts.append("left" if dx < 0 else "right")
        self.trail_direction = "-".join(parts) if parts else "stationary"

    def _state_dict(self, status: str) -> dict:
        return {
            "status": status,
            "ball_x": self.ball_x,
            "ball_y": self.ball_y,
            "vel_x": round(self.vel_x, 1),
            "vel_y": round(self.vel_y, 1),
            "h_bounces": self.h_bounces,
            "v_bounces": self.v_bounces,
            "total_bounces": self.total_bounces,
            "quadrant": self.quadrant,
            "trail_direction": self.trail_direction,
            "frames": self.frames_processed,
        }


async def _capture_chrome_window(wid: int) -> Optional[str]:
    """Legacy b64 capture — used by OCR fallback and cloud models."""
    frame = await _capture_window_raw_async(wid)
    if frame is None:
        return None
    return _numpy_to_b64(frame)


# ---------------------------------------------------------------------------
# OCR -- read exactly what's on screen
# ---------------------------------------------------------------------------

async def ocr_read_screen(b64_png: str) -> Dict[str, str]:
    """Read text from screen using Apple Vision Framework.

    Apple Vision is native macOS, ~50ms, 1.00 confidence on clean text,
    handles glow/shadow that Tesseract struggles with.
    Falls back to Tesseract if Apple Vision unavailable.
    """
    import tempfile

    # Write frame to temp file for Apple Vision
    tmp = os.path.join(tempfile.gettempdir(), "jarvis_ocr_frame.png")
    try:
        raw_bytes = base64.b64decode(b64_png)
        with open(tmp, "wb") as f:
            f.write(raw_bytes)

        # Try Apple Vision first
        try:
            from backend.vision.apple_ocr import apple_ocr_read_async
            lines = await apple_ocr_read_async(tmp, min_confidence=0.8)
            if lines:
                return _parse_ocr_lines([l["text"] for l in lines])
        except Exception:
            pass

        # Fallback: Tesseract
        try:
            import pytesseract
            img = Image.open(tmp)
            w, h = img.size
            hud = img.crop((0, 0, int(w * 0.4), int(h * 0.22)))
            hud = hud.resize((hud.width * 3, hud.height * 3), Image.Resampling.NEAREST)
            text = pytesseract.image_to_string(hud, config="--psm 6")
            return _parse_ocr_lines(text.strip().split("\n"))
        except Exception:
            pass

    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    return {}


def _parse_ocr_lines(lines: list) -> Dict[str, str]:
    """Parse bounce counter values from OCR text lines.

    Apple Vision may split 'Horizontal' and 'Bounces: 33' into separate
    lines. We join all lines into one blob then extract with regex.
    """
    blob = " ".join(str(l).strip() for l in lines)
    result = {}

    m = re.search(r"[Hh]orizontal\s*[Bb]ounces?:?\s*(\d+)", blob)
    if m:
        result["horizontal"] = m.group(1)

    m = re.search(r"[Vv]ertical\s*[Bb]ounces?:?\s*(\d+)", blob)
    if m:
        result["vertical"] = m.group(1)

    m = re.search(r"[Tt]otal\s*[Bb]ounces?:?\s*(\d+)", blob)
    if m:
        result["total"] = m.group(1)

    m = re.search(r"[Ss]peed:?\s*(\d+)", blob)
    if m:
        result["speed"] = m.group(1)

    return result


# ---------------------------------------------------------------------------
# Main loop — VLA Pipeline (Vision + Language + Action)
# ---------------------------------------------------------------------------

async def main(duration_s: int = 60):
    print("\n" + "=" * 70)
    print("  JARVIS VLA Pipeline — Dual-Model Parallel Perception")
    print("  OCR (local) + 235B (structural) + Claude (semantic)")
    print("=" * 70)

    # Open bouncing ball and bring it to front
    html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vision_smoke_test_bounce.html")
    if os.path.exists(html):
        subprocess.Popen(["open", html], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(2.0)

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "Google Chrome" to activate',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await asyncio.sleep(1.0)
    except Exception:
        pass

    await jarvis_say(
        "JARVIS Vision Language Action pipeline online. "
        "Dual model perception activated."
    )

    # Re-focus Chrome
    try:
        refocus = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "Google Chrome" to activate',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await refocus.wait()
        await asyncio.sleep(0.5)
    except Exception:
        pass

    # Start capture
    from backend.vision.lean_loop import LeanVisionLoop
    loop = LeanVisionLoop.get_instance()
    # Force fresh frame_server for main-display-only capture
    loop._frame_server_proc = None
    loop._frame_server_ready = False
    await loop._ensure_frame_server()
    if loop._frame_server_ready:
        await asyncio.sleep(2.0)

    # Find Chrome bouncing ball window ID for targeted capture
    _chrome_wid = _find_chrome_ball_window()
    if _chrome_wid:
        print(f"  Capture: ONLINE (Chrome window wid={_chrome_wid})")
    else:
        print("  Capture: ONLINE (full main display — Chrome window not found)")

    # Initialize cloud clients
    claude_client = None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        try:
            import anthropic
            claude_client = anthropic.AsyncAnthropic(api_key=api_key)
            print("  Claude Vision: ONLINE")
        except ImportError:
            print("  Claude Vision: OFFLINE (no anthropic)")

    dw_key = os.environ.get("DOUBLEWORD_API_KEY", "")
    print(f"  Doubleword 235B: {'ONLINE' if dw_key else 'OFFLINE (no key)'}")

    # --- Ouroboros: VisionReflexCompiler ---
    from backend.vision.vision_reflex import VisionReflexCompiler
    reflex_compiler = VisionReflexCompiler.get_instance()
    TASK_KEY = "vla_perception"

    _tel_dir = os.environ.get(
        "VISION_TELEMETRY_DIR", "/tmp/claude/vision_telemetry",
    )
    _latest = os.path.join(_tel_dir, "vision_last_perception.png")

    # --- Ball Tracker: primary perception (deterministic, ~2ms/frame) ---
    tracker = BallTracker()
    print(f"  Ball Tracker: ONLINE (deterministic numpy, ~2ms/frame)")

    print(f"\n  Running {duration_s}s...\n  " + "-" * 60)

    t_start = time.monotonic()
    n_cycles = 0
    n_vla_cycles = 0
    n_agreements = 0
    n_disagreements = 0
    last_ocr_vals: Dict[str, str] = {}
    last_ocr_time = 0.0
    # Background tasks for cloud models (non-blocking)
    claude_task: Optional[asyncio.Task] = None
    dw_task: Optional[asyncio.Task] = None
    # Ouroboros 397B synthesis runs in background (non-blocking)
    ouroboros_task: Optional[asyncio.Task] = None
    ouroboros_t0 = 0.0
    # Tracker output
    last_tracker_print = 0.0
    last_spoken_quad = ""
    last_spoken_total = 0
    # Pending results from the SAME VLA cycle — held until both return
    pending_claude: Optional[str] = None
    pending_dw: Optional[str] = None
    pending_ocr_snapshot: Dict[str, str] = {}
    last_vla_time = 0.0

    while (time.monotonic() - t_start) < duration_s:
        # ---- CAPTURE: Dual-Output Router ----
        # Raw numpy for Deterministic Retina (reflex), b64 only when needed.
        raw_frame: Optional[np.ndarray] = None
        b64: Optional[str] = None

        if _chrome_wid:
            raw_frame = await _capture_window_raw_async(_chrome_wid)
        if raw_frame is None:
            # Fallback: frame_server → b64
            b64 = await loop._capture_cu_screenshot()
            if b64 is None:
                await asyncio.sleep(0.5)
                continue
        # b64 is lazily encoded from raw_frame ONLY when OCR or cloud needs it

        n_cycles += 1

        # ---- CHECK OUROBOROS BACKGROUND TASK ----
        if ouroboros_task and ouroboros_task.done():
            try:
                ok = ouroboros_task.result()
                compile_s = time.monotonic() - ouroboros_t0
                if ok:
                    tier = reflex_compiler.get_active_tier(TASK_KEY)
                    print()
                    print("  " + "=" * 60)
                    print(f"   REFLEX ASSIMILATED — Tier {tier} active ({compile_s:.0f}s)")
                    print("   Switching to CONTINUOUS MODE — reflex on every frame")
                    print("  " + "=" * 60)
                    # Reset reflex tracking for fps measurement
                    n_reflex_frames = 0
                    reflex_start_time = time.monotonic()
                    jarvis_say_background(
                        f"Ouroboros complete. Tier {tier} reflex assimilated "
                        f"after {int(compile_s)} seconds of synthesis. "
                        f"Local perception now active."
                    )
                else:
                    print(f"  [Ouroboros] Background synthesis failed ({compile_s:.0f}s) — VLA continues")
            except Exception as exc:
                print(f"  [Ouroboros] Background task error: {type(exc).__name__}: {exc}")
            ouroboros_task = None

        # ---- PRIMARY: Ball Tracker on raw numpy frame (~2ms) ----
        if raw_frame is not None:
            t_track = time.monotonic()
            tracker_state = tracker.process_frame(raw_frame)
            track_ms = (time.monotonic() - t_track) * 1000

            status = tracker_state["status"]
            bx = tracker_state["ball_x"]
            by = tracker_state["ball_y"]
            quad = tracker_state["quadrant"]
            trail = tracker_state["trail_direction"]
            h_b = tracker_state["h_bounces"]
            v_b = tracker_state["v_bounces"]
            t_b = tracker_state["total_bounces"]
            fps = tracker_state["frames"] / max(time.monotonic() - t_start, 0.1)

            # Print every ~0.5s (not every frame)
            if (time.monotonic() - last_tracker_print) > 0.5 and status == "tracking":
                print(
                    f"  [TRACKER] ({track_ms:.1f}ms) "
                    f"ball=({bx},{by}) quad={quad} trail={trail} "
                    f"H:{h_b} V:{v_b} T:{t_b} | {fps:.1f}fps"
                )
                last_tracker_print = time.monotonic()

            # Narrate bounces as they happen
            if t_b > last_spoken_total:
                delta = t_b - last_spoken_total
                last_spoken_total = t_b
                jarvis_say_background(
                    f"Bounce detected. {t_b} total. "
                    f"{h_b} horizontal, {v_b} vertical."
                )

            # Narrate quadrant changes
            if quad != last_spoken_quad and quad != "unknown":
                jarvis_say_background(f"Ball in {quad}.")
                last_spoken_quad = quad

        # ---- VALIDATION: OCR reads HUD text every ~5s ----
        elapsed = time.monotonic() - t_start
        if (elapsed - last_ocr_time) >= 5.0:
            last_ocr_time = elapsed
            if b64 is None and raw_frame is not None:
                b64 = _numpy_to_b64(raw_frame)
            if b64:
                t_ocr = time.monotonic()
                ocr_vals = await ocr_read_screen(b64)
                ocr_ms = (time.monotonic() - t_ocr) * 1000

                if ocr_vals:
                    ocr_h = ocr_vals.get("horizontal", "?")
                    ocr_v = ocr_vals.get("vertical", "?")
                    ocr_t = ocr_vals.get("total", "?")
                    # Cross-validate tracker vs HUD
                    tracker_t = tracker.total_bounces
                    try:
                        hud_t = int(ocr_t)
                        drift = abs(tracker_t - hud_t)
                        match = "MATCH" if drift <= 2 else f"DRIFT={drift}"
                    except (ValueError, TypeError):
                        match = "OCR_FAIL"
                    print(
                        f"  [VALIDATE] ({ocr_ms:.0f}ms) "
                        f"HUD: H:{ocr_h} V:{ocr_v} T:{ocr_t} | "
                        f"Tracker: H:{tracker.h_bounces} V:{tracker.v_bounces} T:{tracker_t} | "
                        f"{match}"
                    )
                    last_ocr_vals = ocr_vals.copy()

        # ---- LAYER 2+3: Cloud VLA (parallel, every ~8s) ----
        elapsed = time.monotonic() - t_start
        should_vla = (elapsed - last_vla_time) >= 8.0 and n_cycles >= 2

        # Collect finished cloud results into pending slots
        if claude_task and claude_task.done():
            try:
                pending_claude = claude_task.result()
            except Exception:
                pending_claude = None
            claude_task = None

        if dw_task and dw_task.done():
            try:
                pending_dw = dw_task.result()
            except Exception:
                pending_dw = None
            dw_task = None

        # ---- CROSS-VALIDATION: when BOTH models have returned ----
        both_done = (
            claude_task is None and dw_task is None
            and (pending_claude is not None or pending_dw is not None)
        )
        if both_done:
            n_vla_cycles += 1
            _cross_validate(
                pending_claude, pending_dw, pending_ocr_snapshot,
                n_vla_cycles,
            )
            # Count consensus
            if pending_claude and pending_dw:
                # Extract quadrant mentions from both
                cl = pending_claude.lower()
                dw = (pending_dw or "").lower()
                quadrants = ["upper-left", "upper-right", "lower-left",
                             "lower-right", "top-left", "top-right",
                             "bottom-left", "bottom-right", "center"]
                cl_quad = [q for q in quadrants if q in cl]
                dw_quad = [q for q in quadrants if q in dw]
                if cl_quad and dw_quad and set(cl_quad) & set(dw_quad):
                    n_agreements += 1
                elif cl_quad and dw_quad:
                    n_disagreements += 1

            # --- OUROBOROS FEEDBACK: feed consensus to the learning loop ---
            pos_consensus = "agree" if (
                cl_quad and dw_quad and set(cl_quad) & set(dw_quad)
            ) else "disagree" if (cl_quad and dw_quad) else "partial"

            directions = ["upward", "downward", "leftward", "rightward",
                          "up-left", "up-right", "down-left", "down-right",
                          "diagonally"]
            cl_dirs = [d for d in directions if d in (pending_claude or "").lower()]
            dw_dirs = [d for d in directions if d in (pending_dw or "").lower()]
            motion_consensus = (
                "agree" if (cl_dirs and dw_dirs and set(cl_dirs) & set(dw_dirs))
                else "disagree" if (cl_dirs and dw_dirs)
                else "partial"
            )

            reflex_compiler.feed_cross_validation(
                claude_result=pending_claude,
                dw_result=pending_dw,
                ocr_vals=pending_ocr_snapshot,
                position_consensus=pos_consensus,
                motion_consensus=motion_consensus,
            )

            # Track VLA calls for Ouroboros graduation
            event = reflex_compiler.record_call(TASK_KEY, 0)
            if event == "graduate" and pending_ocr_snapshot and ouroboros_task is None:
                print()
                print("  " + "=" * 60)
                print("   OUROBOROS: Cognitive inefficiency detected")
                print("   Launching 397B synthesis in BACKGROUND")
                print("   VLA loop continues while Ouroboros thinks...")
                print("  " + "=" * 60)
                jarvis_say_background(
                    "Ouroboros triggered. Launching 397B code synthesis "
                    "in the background. VLA loop continues."
                )
                # Fire compilation as background task — doesn't block VLA
                ouroboros_task = asyncio.create_task(
                    reflex_compiler.compile_reflexes(
                        TASK_KEY, b64, pending_ocr_snapshot,
                        on_status=lambda msg: print(f"  [Ouroboros:BG] {msg}"),
                    )
                )
                ouroboros_t0 = time.monotonic()

            # Narrate the fused perception (Claude is more articulate)
            if pending_claude:
                jarvis_say_background(pending_claude[:200])
            elif pending_dw:
                short = pending_dw.replace("\n", " ").strip()[:150]
                jarvis_say_background(short)

            pending_claude = None
            pending_dw = None

        # Fire new parallel perception if enough time passed
        if should_vla and claude_task is None and dw_task is None:
            last_vla_time = elapsed
            pending_ocr_snapshot = last_ocr_vals.copy()
            # Lazy b64 encode — only when cloud models need it
            if b64 is None and raw_frame is not None:
                b64 = _numpy_to_b64(raw_frame)
            if b64:
                print(f"\n  [VLA #{n_vla_cycles + 1}] Firing dual-model perception (T+{elapsed:.0f}s)...")
                if claude_client:
                    claude_task = asyncio.create_task(
                        _claude_vision(claude_client, b64)
                    )
                if dw_key:
                    dw_task = asyncio.create_task(
                        _doubleword_vision(b64)
                    )

        # Continuous tracking: ~15fps. Throttle slightly to share CPU.
        await asyncio.sleep(0.06)

    # Cleanup
    for task in [claude_task, dw_task, ouroboros_task]:
        if task and not task.done():
            task.cancel()

    total = time.monotonic() - t_start
    avg_fps = tracker.frames_processed / max(total, 0.1)
    print(f"\n  " + "-" * 60)
    print(f"  Frames: {tracker.frames_processed} ({avg_fps:.1f}fps) | Duration: {total:.1f}s")
    print(
        f"  Tracker bounces: H:{tracker.h_bounces} V:{tracker.v_bounces} "
        f"T:{tracker.total_bounces}"
    )
    if last_ocr_vals:
        print(
            f"  HUD (last OCR): H:{last_ocr_vals.get('horizontal','?')} "
            f"V:{last_ocr_vals.get('vertical','?')} "
            f"T:{last_ocr_vals.get('total','?')}"
        )
    print(f"  VLA perceptions: {n_vla_cycles}")
    if n_agreements or n_disagreements:
        pct = n_agreements / max(n_agreements + n_disagreements, 1) * 100
        print(
            f"  Cross-validation: {n_agreements} agreements, "
            f"{n_disagreements} disagreements ({pct:.0f}% consensus)"
        )

    summary = (
        f"VLA pipeline complete. Tracked {tracker.frames_processed} frames "
        f"at {avg_fps:.0f} F P S. Detected {tracker.total_bounces} bounces. "
        f"{n_vla_cycles} cloud analyses in {int(total)} seconds."
    )
    await jarvis_say(summary)

    print("=" * 70 + "\n")
    if loop._frame_server_proc and loop._frame_server_proc.returncode is None:
        loop._frame_server_proc.terminate()


# ---------------------------------------------------------------------------
# VLA Perception Engines (run in parallel on the same frame)
# ---------------------------------------------------------------------------

async def _claude_vision(client, b64: str) -> Optional[str]:
    """Claude Vision: deep semantic scene understanding."""
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=os.environ.get("JARVIS_CLAUDE_VISION_MODEL", "claude-sonnet-4-20250514"),
                max_tokens=80,
                system=(
                    "You are JARVIS reporting to Derek. "
                    "Describe what you see in 1-2 sentences: the scene, "
                    "where the ball is, its direction, and any notable details. "
                    "Be specific about position (quadrant, edge proximity) and motion."
                ),
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": "Describe this screen."},
                ]}],
            ),
            timeout=10,
        )
        for block in resp.content:
            if hasattr(block, "text"):
                return block.text.strip()
    except Exception:
        pass
    return None


async def _doubleword_vision(b64: str) -> Optional[str]:
    """Doubleword 235B VL: fast structural read — text, numbers, layout."""
    dw_key = os.environ.get("DOUBLEWORD_API_KEY", "")
    dw_base = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
    dw_model = os.environ.get(
        "DOUBLEWORD_VISION_MODEL", "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8",
    )
    if not dw_key:
        return None
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{dw_base}/chat/completions",
                json={
                    "model": dw_model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{b64}",
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "Read ALL text on screen precisely. "
                                    "Then describe: where is the green ball, "
                                    "what quadrant, what direction is the trail, "
                                    "and is it near any edge? Be concise."
                                ),
                            },
                        ],
                    }],
                    "max_tokens": 200,
                    "temperature": 0.0,
                },
                headers={
                    "Authorization": f"Bearer {dw_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data["choices"][0]["message"].get("content", "")
    except Exception:
        return None


def _cross_validate(
    claude_result: Optional[str],
    dw_result: Optional[str],
    ocr_snapshot: Dict[str, str],
    cycle_n: int,
) -> None:
    """Compare perceptions from all three layers on the same frame.

    Logs agreement/disagreement on:
      - Numbers: does 235B's text read match OCR?
      - Position: do both models agree on the ball's quadrant?
      - Direction: do both models agree on trail direction?
    """
    print(f"  " + "~" * 60)
    print(f"  CROSS-VALIDATION (VLA cycle #{cycle_n})")

    # --- Print raw perceptions ---
    if dw_result:
        short = dw_result.replace("\n", " ").strip()[:180]
        print(f"    235B:   {short}")
    else:
        print(f"    235B:   (no response)")

    if claude_result:
        short = claude_result.replace("\n", " ").strip()[:180]
        print(f"    Claude: {short}")
    else:
        print(f"    Claude: (no response)")

    if ocr_snapshot:
        print(f"    OCR:    H:{ocr_snapshot.get('horizontal','?')} "
              f"V:{ocr_snapshot.get('vertical','?')} "
              f"T:{ocr_snapshot.get('total','?')}")

    # --- Number cross-check: does 235B agree with OCR? ---
    if dw_result and ocr_snapshot.get("total"):
        ocr_total = ocr_snapshot["total"]
        # Look for the total number in the 235B output
        dw_text = dw_result.replace("\n", " ")
        import re as _re
        dw_totals = _re.findall(r"[Tt]otal\s*[Bb]ounces?:?\s*(\d+)", dw_text)
        if dw_totals:
            dw_total = dw_totals[0]
            try:
                drift = abs(int(dw_total) - int(ocr_total))
                if drift <= 3:
                    print(f"    Numbers: AGREE (OCR={ocr_total}, 235B={dw_total}, drift={drift})")
                else:
                    print(f"    Numbers: DRIFT (OCR={ocr_total}, 235B={dw_total}, drift={drift} — temporal lag)")
            except ValueError:
                pass

    # --- Quadrant cross-check ---
    quadrant_map = {
        "upper-left": "UL", "top-left": "UL",
        "upper-right": "UR", "top-right": "UR",
        "lower-left": "LL", "bottom-left": "LL",
        "lower-right": "LR", "bottom-right": "LR",
        "center": "C",
    }

    def _extract_quadrant(text: str) -> Optional[str]:
        if not text:
            return None
        lower = text.lower()
        for phrase, code in quadrant_map.items():
            if phrase in lower:
                return code
        return None

    cl_q = _extract_quadrant(claude_result)
    dw_q = _extract_quadrant(dw_result)
    if cl_q and dw_q:
        if cl_q == dw_q:
            print(f"    Position: CONSENSUS — both say {cl_q}")
        else:
            print(f"    Position: DISAGREE — Claude={cl_q}, 235B={dw_q} (ball moved between calls)")
    elif cl_q:
        print(f"    Position: Claude only — {cl_q}")
    elif dw_q:
        print(f"    Position: 235B only — {dw_q}")

    # --- Direction cross-check ---
    directions = ["upward", "downward", "leftward", "rightward",
                  "up-left", "up-right", "down-left", "down-right",
                  "diagonally"]

    def _extract_direction(text: str) -> list:
        if not text:
            return []
        lower = text.lower()
        return [d for d in directions if d in lower]

    cl_dirs = _extract_direction(claude_result)
    dw_dirs = _extract_direction(dw_result)
    if cl_dirs and dw_dirs:
        overlap = set(cl_dirs) & set(dw_dirs)
        if overlap:
            print(f"    Motion: CONSENSUS — shared: {', '.join(overlap)}")
        else:
            print(f"    Motion: DIFFER — Claude={cl_dirs}, 235B={dw_dirs}")

    print(f"  " + "~" * 60)


async def _fused_perception(
    claude_client, b64: str, ocr_vals: Dict[str, str],
) -> str:
    """Fire 235B + Claude in parallel, fuse results into one narration."""
    # Launch both in parallel
    tasks = []
    if claude_client:
        tasks.append(asyncio.create_task(_claude_vision(claude_client, b64)))
    else:
        tasks.append(asyncio.create_task(asyncio.sleep(0)))  # placeholder

    tasks.append(asyncio.create_task(_doubleword_vision(b64)))

    # Wait for both (with timeout so we don't block forever)
    done, pending = await asyncio.wait(tasks, timeout=12)
    for p in pending:
        p.cancel()

    claude_result = None
    dw_result = None
    for t in done:
        try:
            r = t.result()
            if r is None:
                continue
            # Claude results tend to be longer/more narrative
            # 235B results tend to start with the text data
            if claude_client and t == tasks[0]:
                claude_result = r
            else:
                dw_result = r
        except Exception:
            pass

    # Fuse: OCR numbers + 235B detail + Claude spatial reasoning
    parts = []

    # Structured data from OCR
    h = ocr_vals.get("horizontal", "?")
    v = ocr_vals.get("vertical", "?")
    t = ocr_vals.get("total", "?")
    if h != "?" and v != "?":
        parts.append(f"{t} total bounces. {h} horizontal, {v} vertical.")

    # 235B structural detail (if it adds something beyond OCR)
    if dw_result:
        parts.append(f"235B sees: {dw_result[:150]}")

    # Claude semantic understanding
    if claude_result:
        parts.append(f"Claude sees: {claude_result[:150]}")

    return " ".join(parts) if parts else ""


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=45)
    asyncio.run(main(duration_s=p.parse_args().duration))
