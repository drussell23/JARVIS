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


# ---------------------------------------------------------------------------
# SCK Native Stream — 30fps ScreenCaptureKit (the REAL eyes-open path)
# ---------------------------------------------------------------------------

_sck_stream = None  # AsyncCaptureStream singleton


async def _start_sck_stream(wid: int) -> bool:
    """Start SCK in a dedicated thread with its own CFRunLoop.

    SCK's GCD completion handlers need a pumped CFRunLoop. The asyncio
    event loop does NOT pump one. Solution: run SCK in a background
    thread that pumps CFRunLoop, and share frames via a thread-safe queue.
    """
    global _sck_stream, _sck_frame_queue
    import threading
    import queue

    sck_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backend", "native_extensions",
    )
    if sck_path not in sys.path:
        sys.path.insert(0, sck_path)

    _sck_frame_queue = queue.Queue(maxsize=5)
    _sck_ready = threading.Event()

    def _sck_thread():
        """Dedicated thread: SCK full-screen capture → frame queue.

        Full screen (window_id=0) delivers 23fps proven. Window-specific
        capture has a delegate callback issue — full screen bypasses it.
        The tracker ignores non-ball content via green channel threshold.
        """
        try:
            import fast_capture_stream
            config = fast_capture_stream.StreamConfig()
            config.target_fps = 60
            config.max_buffer_size = 3
            config.output_format = "raw"
            config.use_gpu_acceleration = True
            config.drop_frames_on_overflow = True

            # Full screen capture (window_id=0) — proven 23fps
            stream = fast_capture_stream.CaptureStream(0, config)
            if not stream.start():
                print("  SCK thread: stream.start() returned False")
                return

            _sck_ready.set()

            while _sck_ready.is_set():
                frame = stream.get_frame(16)
                if frame is None:
                    continue
                if frame.get("image") is None:
                    continue

                if _sck_frame_queue.full():
                    try:
                        _sck_frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    _sck_frame_queue.put_nowait(frame)
                except queue.Full:
                    pass

            stream.stop()
        except Exception as exc:
            print(f"  SCK thread error: {exc}")

    try:
        t = threading.Thread(target=_sck_thread, daemon=True, name="sck-capture")
        t.start()

        # Wait for SCK to be ready (up to 3s)
        if _sck_ready.wait(timeout=3.0):
            _sck_stream = _sck_ready  # Use as signal object
            await asyncio.sleep(0.5)  # Let a few frames buffer
            return True
        return False
    except Exception as exc:
        print(f"  SCK stream failed: {exc}")
        return False


_sck_logical_size: Optional[tuple] = None
_sck_frame_queue = None  # thread-safe queue.Queue


async def _get_sck_frame() -> Optional[np.ndarray]:
    """Get the LATEST frame from the SCK thread's queue. Non-blocking.

    Strategy: drain all queued frames, keep only the newest.
    If queue is empty, return None immediately (don't block).
    The tracker runs at whatever rate frames arrive — no waiting.
    """
    global _sck_logical_size
    if _sck_frame_queue is None:
        return None
    import queue as _q

    # Non-blocking drain: grab all available, keep newest
    latest = None
    try:
        while True:
            latest = _sck_frame_queue.get_nowait()
    except _q.Empty:
        pass

    if latest is None:
        return None

    img = latest.get("image")
    if img is None:
        return None

    # KEEP BGRA — no channel swap! The tracker only needs the green channel
    # which is index 1 in BOTH BGRA and RGB. Zero copy, zero conversion.
    if img.ndim != 3 or img.shape[2] < 3:
        return None

    return img


async def _stop_sck_stream() -> None:
    global _sck_stream, _sck_frame_queue
    if _sck_stream is not None:
        # Signal the SCK thread to stop
        _sck_stream.clear()  # threading.Event.clear() stops the loop
        _sck_stream = None
    _sck_frame_queue = None


def _capture_window_raw_numpy(wid: int) -> Optional[np.ndarray]:
    """Raw Memory Bypass: Quartz CGImage → numpy array. Zero b64. Zero PNG.

    Returns RGB numpy array at NATIVE resolution. Zero resize.
    The tracker works at any resolution — np.where doesn't care about size.
    """
    try:
        import Quartz

        image_ref = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow
            | Quartz.kCGWindowListOptionOnScreenAboveWindow,
            wid,
            Quartz.kCGWindowImageDefault
            | Quartz.kCGWindowImageBoundsIgnoreFraming
            | Quartz.kCGWindowImageNominalResolution,
        )
        if image_ref is None:
            return None

        w = Quartz.CGImageGetWidth(image_ref)
        h = Quartz.CGImageGetHeight(image_ref)
        provider = Quartz.CGImageGetDataProvider(image_ref)
        data = Quartz.CGDataProviderCopyData(provider)

        # BGRA raw pixels → numpy → RGB (zero resize)
        arr = np.frombuffer(bytes(data), dtype=np.uint8).reshape((h, w, 4))
        return arr[:, :, [2, 1, 0]]
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
    """Slow path: encode numpy → b64 PNG. Only for cloud model API calls.

    Handles both BGRA (from SCK) and RGB frames. Resizes to 1280x800
    for cloud APIs. This runs every ~8s, not every frame.
    """
    from PIL import Image as _Img
    # Convert BGRA → RGB if needed
    if frame.ndim == 3 and frame.shape[2] == 4:
        rgb = frame[:, :, [2, 1, 0]]  # BGRA → RGB (copy only here, on slow path)
    else:
        rgb = frame
    img = _Img.fromarray(rgb)
    if img.width != 1280 or img.height != 800:
        img = img.resize((1280, 800), _Img.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# Ball Tracker — deterministic numpy, runs on every raw frame (~2ms)
# ---------------------------------------------------------------------------

class BallTracker:
    """Spatial awareness + prediction. No bounce counting (HUD is ground truth).

    Boundary Mandate: the HUD already has the correct bounce count. Trying to
    re-derive it from pixel physics is complexity theater. The tracker's job:

    1. WHERE is the ball? (centroid from green pixels, ~2ms)
    2. WHICH quadrant? (deterministic from position)
    3. WHERE is it heading? (velocity from position history)
    4. WHEN will it hit a wall? (linear extrapolation)

    Bounce counts come from OCR reading the HUD — the scoreboard, not physics.
    """

    EDGE_MARGIN_PCT = 0.04   # 4% of frame dimension = edge zone
    HISTORY_SIZE = 6

    def __init__(self) -> None:
        self.ball_x: int = 0
        self.ball_y: int = 0
        self.vel_x: float = 0.0
        self.vel_y: float = 0.0
        self.quadrant: str = "unknown"
        self.heading: str = "unknown"
        self.next_wall: str = "unknown"
        self.frames_to_wall: int = -1
        self.frames_processed: int = 0
        # HUD values (set externally by OCR)
        self.hud_h: str = "?"
        self.hud_v: str = "?"
        self.hud_t: str = "?"
        self.hud_speed: str = "?"
        self._pos_history: list = []
        self._initialized: bool = False

    def process_frame(self, frame: np.ndarray) -> dict:
        """Find ball, compute velocity, predict next wall.

        Optimization: subsample every 2nd pixel for the threshold scan.
        The ball is ~20px wide — subsampling by 2 still catches it but
        runs in 1/4 the time. Centroid is scaled back to full coords.
        """
        h, w = frame.shape[:2]
        # Subsample: every 2nd pixel in both dimensions (4x faster)
        green_sub = frame[::2, ::2, 1]

        core_mask = green_sub > 225
        core_ys, core_xs = np.where(core_mask)

        if len(core_xs) < 3:
            soft = green_sub > 180
            ys, xs = np.where(soft)
            if len(xs) < 5:
                return self._state("no_ball")
            # Scale back to full resolution
            cx, cy = int(np.mean(xs)) * 2, int(np.mean(ys)) * 2
        else:
            cx, cy = int(np.mean(core_xs)) * 2, int(np.mean(core_ys)) * 2

        self.ball_x, self.ball_y = cx, cy
        self.frames_processed += 1

        self._pos_history.append((cx, cy, time.monotonic()))
        if len(self._pos_history) > self.HISTORY_SIZE:
            self._pos_history.pop(0)

        if not self._initialized:
            self._initialized = True
            self._update_quadrant(w, h)
            return self._state("initializing")

        # Smoothed velocity
        if len(self._pos_history) >= 3:
            n = len(self._pos_history)
            self.vel_x = (self._pos_history[-1][0] - self._pos_history[0][0]) / max(n - 1, 1)
            self.vel_y = (self._pos_history[-1][1] - self._pos_history[0][1]) / max(n - 1, 1)

        # Heading direction (human-readable)
        parts = []
        if self.vel_y < -3:
            parts.append("up")
        elif self.vel_y > 3:
            parts.append("down")
        if self.vel_x < -3:
            parts.append("left")
        elif self.vel_x > 3:
            parts.append("right")
        self.heading = "-".join(parts) if parts else "drifting"

        # Predict next wall
        self._predict_wall(w, h)
        self._update_quadrant(w, h)
        return self._state("tracking")

    def update_hud(self, ocr_vals: dict) -> None:
        """Set HUD values from OCR. The scoreboard is the ground truth."""
        self.hud_h = ocr_vals.get("horizontal", self.hud_h)
        self.hud_v = ocr_vals.get("vertical", self.hud_v)
        self.hud_t = ocr_vals.get("total", self.hud_t)
        self.hud_speed = ocr_vals.get("speed", self.hud_speed)

    def _predict_wall(self, w: int, h: int) -> None:
        candidates = []
        m = int(max(w, h) * self.EDGE_MARGIN_PCT)
        if self.vel_x > 0.5:
            f = (w - m - self.ball_x) / self.vel_x
            if f > 0:
                candidates.append(("right", int(f)))
        if self.vel_x < -0.5:
            f = (m - self.ball_x) / self.vel_x
            if f > 0:
                candidates.append(("left", int(f)))
        if self.vel_y > 0.5:
            f = (h - m - self.ball_y) / self.vel_y
            if f > 0:
                candidates.append(("bottom", int(f)))
        if self.vel_y < -0.5:
            f = (m - self.ball_y) / self.vel_y
            if f > 0:
                candidates.append(("top", int(f)))
        if candidates:
            nearest = min(candidates, key=lambda c: c[1])
            self.next_wall, self.frames_to_wall = nearest
        else:
            self.next_wall, self.frames_to_wall = "unknown", -1

    def _update_quadrant(self, w: int, h: int) -> None:
        mx, my = w // 2, h // 2
        if self.ball_x < mx:
            self.quadrant = "top-left" if self.ball_y < my else "bottom-left"
        else:
            self.quadrant = "top-right" if self.ball_y < my else "bottom-right"

    def _state(self, status: str) -> dict:
        return {
            "status": status,
            "ball_x": self.ball_x,
            "ball_y": self.ball_y,
            "vel_x": round(self.vel_x, 1),
            "vel_y": round(self.vel_y, 1),
            "quadrant": self.quadrant,
            "heading": self.heading,
            "next_wall": self.next_wall,
            "frames_to_wall": self.frames_to_wall,
            "hud_h": self.hud_h,
            "hud_v": self.hud_v,
            "hud_t": self.hud_t,
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

    # Find Chrome window first
    _chrome_wid = _find_chrome_ball_window()

    # Capture cascade: SHM bridge (20fps) → Quartz per-frame (9fps) → frame_server
    _shm_reader = None
    _sck_active = False
    from backend.vision.lean_loop import LeanVisionLoop
    loop = LeanVisionLoop.get_instance()

    # Try SHM bridge first — start SCK daemon, read via zero-copy mmap
    try:
        _sck_active = await _start_sck_stream(0)  # Start SCK full-screen
        if _sck_active:
            await asyncio.sleep(2)  # Let SCK warm up and write to shm
            from backend.vision.shm_frame_reader import ShmFrameReader
            _shm_reader = ShmFrameReader()
            if _shm_reader.open():
                print(f"  Capture: SHM BRIDGE (zero-copy mmap) — 20fps target")
            else:
                _shm_reader = None
                print(f"  Capture: SHM failed to open — falling back")
    except Exception as exc:
        print(f"  SHM bridge error: {exc}")

    if not _shm_reader and _chrome_wid:
        print(f"  Capture: Quartz targeted (wid={_chrome_wid}) — 9fps")
    elif not _shm_reader:
        loop._frame_server_proc = None
        loop._frame_server_ready = False
        await loop._ensure_frame_server()
        if loop._frame_server_ready:
            await asyncio.sleep(2.0)
        print("  Capture: frame_server — fallback")

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
    # Get Chrome window bounds for cropping full-screen captures
    _crop_region = None  # (x, y, w, h) or None for no crop
    if _chrome_wid:
        try:
            import Quartz as _Q
            _wins = _Q.CGWindowListCopyWindowInfo(
                _Q.kCGWindowListOptionOnScreenOnly, _Q.kCGNullWindowID,
            )
            for _w in _wins:
                if _w.get("kCGWindowNumber", 0) == _chrome_wid:
                    _b = _w.get("kCGWindowBounds", {})
                    _crop_region = (
                        int(_b.get("X", 0)), int(_b.get("Y", 0)),
                        int(_b.get("Width", 0)), int(_b.get("Height", 0)),
                    )
                    break
        except Exception:
            pass
    if _crop_region:
        print(f"  Ball Tracker: ONLINE + crop to Chrome region {_crop_region}")
    else:
        print(f"  Ball Tracker: ONLINE (full screen, no crop)")

    print(f"\n  Running {duration_s}s...\n  " + "-" * 60)

    t_start = time.monotonic()
    n_cycles = 0
    n_vla_cycles = 0
    n_agreements = 0
    n_disagreements = 0
    last_ocr_vals: Dict[str, str] = {}
    last_ocr_time = 0.0
    ocr_bg_task: Optional[asyncio.Task] = None
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
    last_spoken_total_time = 0.0
    # Pending results from the SAME VLA cycle — held until both return
    pending_claude: Optional[str] = None
    pending_dw: Optional[str] = None
    pending_ocr_snapshot: Dict[str, str] = {}
    last_vla_time = 0.0

    while (time.monotonic() - t_start) < duration_s:
        # ---- CAPTURE: SHM bridge (20fps) → Quartz (9fps) → frame_server ----
        raw_frame: Optional[np.ndarray] = None
        b64: Optional[str] = None

        if _shm_reader is not None:
            # Zero-copy: numpy view over shared memory, no GIL
            raw_frame, _ = _shm_reader.read_frame()
        if raw_frame is None and _chrome_wid:
            raw_frame = await _capture_window_raw_async(_chrome_wid)
        if raw_frame is None:
            b64 = await loop._capture_cu_screenshot()
            if b64 is None:
                await asyncio.sleep(0.008)  # ~120fps yield
                continue
        # b64 lazily encoded only when OCR or cloud needs it

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

        # ---- PRIMARY: Ball Tracker (spatial) + OCR (scoreboard) ----
        if raw_frame is not None:
            # Crop to Chrome window region (removes dock, menu bar, other green)
            if _crop_region:
                cx, cy, cw, ch = _crop_region
                h_f, w_f = raw_frame.shape[:2]
                # Clamp to frame bounds
                y1 = max(0, min(cy, h_f))
                y2 = max(0, min(cy + ch, h_f))
                x1 = max(0, min(cx, w_f))
                x2 = max(0, min(cx + cw, w_f))
                if y2 > y1 and x2 > x1:
                    raw_frame = raw_frame[y1:y2, x1:x2]

            t_track = time.monotonic()
            tracker_state = tracker.process_frame(raw_frame)
            track_ms = (time.monotonic() - t_track) * 1000

            status = tracker_state["status"]
            bx = tracker_state["ball_x"]
            by = tracker_state["ball_y"]
            quad = tracker_state["quadrant"]
            heading = tracker_state["heading"]
            next_wall = tracker_state.get("next_wall", "unknown")
            frames_to = tracker_state.get("frames_to_wall", -1)
            fps = tracker_state["frames"] / max(time.monotonic() - t_start, 0.1)

            # Print tracker state every ~0.5s
            if (time.monotonic() - last_tracker_print) > 0.5 and status == "tracking":
                predict = ""
                if next_wall != "unknown" and frames_to > 0:
                    predict = f" → {next_wall} wall"
                print(
                    f"  [TRACKER] ({track_ms:.1f}ms) "
                    f"ball=({bx},{by}) {quad} heading {heading}{predict} "
                    f"| HUD: H:{tracker.hud_h} V:{tracker.hud_v} T:{tracker.hud_t} "
                    f"| {fps:.1f}fps"
                )
                last_tracker_print = time.monotonic()

            # === SYMBIOTIC NARRATION: HUD truth + spatial prediction ===

            # 1. Quadrant changed — announce with heading
            if quad != last_spoken_quad and quad != "unknown" and heading != "drifting":
                jarvis_say_background(
                    f"Ball in {quad}, heading {heading}."
                )
                last_spoken_quad = quad

            # 2. Approaching a wall — predictive
            elif (
                next_wall != "unknown"
                and 3 < frames_to < 10
                and next_wall != getattr(tracker, '_last_announced_wall', '')
            ):
                jarvis_say_background(f"Approaching {next_wall} wall.")
                tracker._last_announced_wall = next_wall

            # 3. Periodic summary every ~4s — fuse HUD + spatial
            elif (time.monotonic() - last_spoken_total_time) > 4.0 and tracker.hud_t != "?":
                jarvis_say_background(
                    f"{tracker.hud_t} bounces. {tracker.hud_h} horizontal, "
                    f"{tracker.hud_v} vertical. Ball in {quad}, heading {heading}."
                )
                last_spoken_total_time = time.monotonic()

        # ---- VALIDATION: OCR reads HUD text every ~8s (BACKGROUND, non-blocking) ----
        elapsed = time.monotonic() - t_start
        if ocr_bg_task and ocr_bg_task.done():
            try:
                ocr_result = ocr_bg_task.result()
                if ocr_result and isinstance(ocr_result, dict) and ocr_result:
                    # Feed HUD ground truth to the tracker
                    tracker.update_hud(ocr_result)
                    ocr_h = ocr_result.get("horizontal", "?")
                    ocr_v = ocr_result.get("vertical", "?")
                    ocr_t = ocr_result.get("total", "?")
                    print(
                        f"  [HUD] H:{ocr_h} V:{ocr_v} T:{ocr_t} | "
                        f"ball=({tracker.ball_x},{tracker.ball_y}) "
                        f"{tracker.quadrant} heading {tracker.heading}"
                    )
                    last_ocr_vals = ocr_result.copy()
            except Exception:
                pass
            ocr_bg_task = None

        if (elapsed - last_ocr_time) >= 8.0 and ocr_bg_task is None:
            last_ocr_time = elapsed
            if b64 is None and raw_frame is not None:
                b64 = _numpy_to_b64(raw_frame)
            if b64:
                _b64_for_ocr = b64
                ocr_bg_task = asyncio.create_task(ocr_read_screen(_b64_for_ocr))

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
            cl_quad = []
            dw_quad = []
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

        # Yield to asyncio for background tasks (OCR, cloud, speech).
        # With SCK: frames arrive from thread queue — just yield, no sleep.
        # Without SCK: Quartz capture takes ~47ms so a short sleep is fine.
        await asyncio.sleep(0)

    # Cleanup
    for task in [claude_task, dw_task, ouroboros_task, ocr_bg_task]:
        if task and not task.done():
            task.cancel()

    total = time.monotonic() - t_start
    avg_fps = tracker.frames_processed / max(total, 0.1)
    print(f"\n  " + "-" * 60)
    print(f"  Frames: {tracker.frames_processed} ({avg_fps:.1f}fps) | Duration: {total:.1f}s")
    print(
        f"  HUD (ground truth): H:{tracker.hud_h} V:{tracker.hud_v} T:{tracker.hud_t}"
    )
    print(
        f"  Last position: ({tracker.ball_x},{tracker.ball_y}) "
        f"{tracker.quadrant} heading {tracker.heading}"
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
        f"at {avg_fps:.0f} F P S. {tracker.hud_t} bounces on the scoreboard. "
        f"{n_vla_cycles} cloud analyses in {int(total)} seconds."
    )
    await jarvis_say(summary)

    print("=" * 70 + "\n")
    if _shm_reader:
        _shm_reader.close()
    await _stop_sck_stream()
    try:
        if not _sck_active and loop._frame_server_proc and loop._frame_server_proc.returncode is None:
            loop._frame_server_proc.terminate()
    except Exception:
        pass


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
