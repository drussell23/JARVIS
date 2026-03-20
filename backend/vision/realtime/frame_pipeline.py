"""
Frame pipeline for the Real-Time Vision Action Loop.

Wraps an SCK capture stream with:
- Bounded asyncio queue (drops oldest on overflow)
- dhash-based motion detection (configurable threshold + debounce)
- Animated UI throttle support via motion_detect flag
- Fully async, zero hardcoding — all tuning via env vars or constructor args
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven defaults (no hardcoding)
# ---------------------------------------------------------------------------
_DEFAULT_MOTION_THRESHOLD = float(os.environ.get("VISION_MOTION_THRESHOLD", "0.05"))
_DEFAULT_DEBOUNCE_MS = int(os.environ.get("VISION_MOTION_DEBOUNCE_MS", "0"))
_DEFAULT_MAX_QUEUE_SIZE = int(os.environ.get("VISION_FRAME_QUEUE_SIZE", "10"))
_DEFAULT_HASH_SIZE = int(os.environ.get("VISION_DHASH_SIZE", "8"))


# ---------------------------------------------------------------------------
# FrameData
# ---------------------------------------------------------------------------

@dataclass
class FrameData:
    """A single captured video frame with metadata."""

    data: np.ndarray          # RGB pixel array, shape (H, W, 3)
    width: int
    height: int
    timestamp: float          # time.time() at capture
    frame_number: int
    scale_factor: float = 1.0


# ---------------------------------------------------------------------------
# dhash helper
# ---------------------------------------------------------------------------

def _dhash(frame: np.ndarray, hash_size: int = _DEFAULT_HASH_SIZE) -> int:
    """
    Compute a perceptual difference hash (dhash) for a frame.

    Resizes to (hash_size+1, hash_size) grayscale and encodes left/right
    pixel brightness relationships into a 64-bit integer.
    """
    from PIL import Image  # lazy import — not needed when SCK unavailable in CI

    if len(frame.shape) == 3:
        gray = np.mean(frame, axis=2).astype(np.uint8)
    else:
        gray = frame.astype(np.uint8)

    img = Image.fromarray(gray).resize(
        (hash_size + 1, hash_size), Image.LANCZOS
    )
    pixels = np.array(img)
    diff = pixels[:, 1:] > pixels[:, :-1]
    return int.from_bytes(np.packbits(diff.flatten()[:64]).tobytes(), "big")


def _hamming_distance(a: int, b: int) -> int:
    """Count differing bits between two 64-bit integers."""
    return bin(a ^ b).count("1")


def _mean_luminance(frame: np.ndarray) -> float:
    """Return mean luminance in [0, 1] for a frame."""
    if len(frame.shape) == 3:
        return float(np.mean(frame)) / 255.0
    return float(np.mean(frame)) / 255.0


# ---------------------------------------------------------------------------
# MotionDetector
# ---------------------------------------------------------------------------

class MotionDetector:
    """
    Per-stream motion detector using dhash.

    Parameters
    ----------
    threshold : float
        Fraction of hash bits that must differ to consider a frame "changed"
        (Hamming distance / 64).  Range 0.0–1.0; default from env
        VISION_MOTION_THRESHOLD (0.05).
    debounce_ms : int
        Minimum milliseconds between reported motion events.  Frames arriving
        within this window after a change are suppressed.  Default from env
        VISION_MOTION_DEBOUNCE_MS (100).
    hash_size : int
        Side length of the dhash grid (produces hash_size² bits).  Default 8.
    """

    def __init__(
        self,
        threshold: float = _DEFAULT_MOTION_THRESHOLD,
        debounce_ms: int = _DEFAULT_DEBOUNCE_MS,
        hash_size: int = _DEFAULT_HASH_SIZE,
    ) -> None:
        self._threshold = threshold
        self._debounce_s = debounce_ms / 1000.0
        self._hash_size = hash_size
        self._prev_hash: Optional[int] = None
        self._prev_luminance: Optional[float] = None
        self._last_change_ts: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect_change(self, frame: np.ndarray) -> bool:
        """
        Return True if the frame represents meaningful motion relative to the
        previous call.

        - First call always returns True (no baseline yet).
        - Subsequent calls return True only when:
          1. The dhash Hamming distance exceeds the threshold, AND
          2. The debounce window has elapsed since the last reported change.
        """
        now = time.monotonic()
        current_hash = _dhash(frame, self._hash_size)
        current_luminance = _mean_luminance(frame)

        # First frame — establish baseline and report as changed
        if self._prev_hash is None:
            self._prev_hash = current_hash
            self._prev_luminance = current_luminance
            self._last_change_ts = now
            return True

        # Debounce: suppress if we just reported a change
        if (now - self._last_change_ts) < self._debounce_s:
            # Still update the baseline so we track the latest frame
            self._prev_hash = current_hash
            self._prev_luminance = current_luminance
            return False

        # --- Primary signal: dhash Hamming distance ---
        distance = _hamming_distance(current_hash, self._prev_hash)
        hash_fraction = distance / (self._hash_size * self._hash_size)

        # --- Secondary signal: mean-luminance delta ---
        # dhash encodes *structure* but is blind to uniform brightness shifts
        # (e.g. all-black vs all-white both hash to 0).  The luminance delta
        # catches that class of change regardless of threshold.
        lum_delta = abs(current_luminance - (self._prev_luminance or 0.0))

        # A frame is "changed" if *either* signal exceeds the threshold
        changed = (hash_fraction > self._threshold) or (lum_delta > self._threshold)

        if changed:
            self._last_change_ts = now

        self._prev_hash = current_hash
        self._prev_luminance = current_luminance
        return changed

    def reset(self) -> None:
        """Clear baseline — next frame will always be reported as changed."""
        self._prev_hash = None
        self._prev_luminance = None
        self._last_change_ts = 0.0


# ---------------------------------------------------------------------------
# FramePipeline
# ---------------------------------------------------------------------------

class FramePipeline:
    """
    Async frame pipeline that bridges an SCK capture stream to downstream
    vision consumers via a bounded asyncio.Queue.

    Features
    --------
    - Bounded queue — drops the oldest frame when the queue is full so that
      consumers always see the most recent content.
    - dhash motion detection — static frames are filtered before enqueue.
    - use_sck=False mode — SCK capture is skipped entirely; frames are
      injected directly via _enqueue_frame() for test environments.

    Parameters
    ----------
    use_sck : bool
        When True, start() launches a capture task via AsyncCaptureStream.
        When False, no capture task is started (test / mock mode).
    max_queue_size : int
        Capacity of the bounded frame queue.  Default from env
        VISION_FRAME_QUEUE_SIZE (10).
    motion_detect : bool
        When True, frames identical to the previous one (per dhash) are
        filtered out and never enqueued.  Default True.
    window_id : int
        SCK window ID to capture (only used when use_sck=True).
    motion_threshold : float
        Forwarded to MotionDetector.  Default from env.
    motion_debounce_ms : int
        Forwarded to MotionDetector.  Default from env.
    """

    def __init__(
        self,
        use_sck: bool = True,
        max_queue_size: int = _DEFAULT_MAX_QUEUE_SIZE,
        motion_detect: bool = True,
        window_id: int = 0,
        motion_threshold: float = _DEFAULT_MOTION_THRESHOLD,
        motion_debounce_ms: int = _DEFAULT_DEBOUNCE_MS,
    ) -> None:
        self._use_sck = use_sck
        self._max_queue_size = max_queue_size
        self._motion_detect = motion_detect
        self._window_id = window_id

        # Bounded queue — asyncio.Queue does NOT auto-drop; we handle overflow
        # manually in _enqueue_frame so we can drop the *oldest* item.
        self._frame_queue: asyncio.Queue[FrameData] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._motion_detector = MotionDetector(
            threshold=motion_threshold,
            debounce_ms=motion_debounce_ms,
        )

        self._capture_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._frame_counter: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the pipeline.  Idempotent — safe to call multiple times."""
        if self._running:
            return

        self._running = True
        self._motion_detector.reset()

        if self._use_sck:
            self._capture_task = asyncio.get_event_loop().create_task(
                self._sck_capture_loop(),
                name="frame_pipeline.sck_capture",
            )
            logger.info(
                "FramePipeline started — SCK capture task launched (window %d)",
                self._window_id,
            )
        else:
            logger.info("FramePipeline started — mock mode (no SCK capture)")

    async def stop(self) -> None:
        """Stop the pipeline and cancel the capture task if running."""
        if not self._running:
            return

        self._running = False

        if self._capture_task is not None and not self._capture_task.done():
            self._capture_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._capture_task), timeout=2.0
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._capture_task = None

        logger.info("FramePipeline stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Frame access
    # ------------------------------------------------------------------

    async def get_frame(self, timeout_s: float = 1.0) -> Optional[FrameData]:
        """
        Retrieve the next frame from the queue.

        Returns None on timeout rather than raising.
        """
        try:
            return await asyncio.wait_for(self._frame_queue.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _should_process(self, frame: FrameData) -> bool:
        """
        Gate: return True if the frame should be enqueued.

        When motion_detect is disabled, every frame passes.
        """
        if not self._motion_detect:
            return True
        return self._motion_detector.detect_change(frame.data)

    def _enqueue_frame(self, frame: FrameData) -> None:
        """
        Enqueue a frame into the bounded queue.

        If the queue is already full, the *oldest* frame is dropped to make
        room for the incoming one — ensuring consumers always see the most
        recent content.
        """
        if self._frame_queue.full():
            try:
                dropped = self._frame_queue.get_nowait()
                logger.debug(
                    "Queue full — dropped oldest frame #%d", dropped.frame_number
                )
            except asyncio.QueueEmpty:
                pass  # race between full() and get_nowait() — no action needed

        try:
            self._frame_queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Rare race: another coroutine filled the slot between the drop and
            # this put.  Log and discard rather than blocking.
            logger.debug(
                "Queue still full after drop — discarding frame #%d",
                frame.frame_number,
            )

    # ------------------------------------------------------------------
    # SCK capture loop
    # ------------------------------------------------------------------

    async def _sck_capture_loop(self) -> None:
        """
        Background task: pull frames from AsyncCaptureStream and feed the
        pipeline.  Gracefully handles ImportError so the module can be
        imported on machines without the SCK extension installed.
        """
        try:
            from backend.native_extensions.macos_sck_stream import (
                AsyncCaptureStream,
                StreamingConfig,
            )
        except ImportError as exc:
            logger.error(
                "SCK extension not available — capture loop exiting: %s", exc
            )
            self._running = False
            return

        config = StreamingConfig(
            target_fps=int(os.environ.get("VISION_CAPTURE_FPS", "30")),
            max_buffer_size=self._max_queue_size,
            drop_frames_on_overflow=True,
        )

        stream = AsyncCaptureStream(self._window_id, config)

        try:
            started = await stream.start()
            if not started:
                logger.error("AsyncCaptureStream failed to start")
                self._running = False
                return

            logger.info(
                "SCK capture loop running — window %d", self._window_id
            )

            while self._running:
                raw = await stream.get_frame(timeout_ms=50)
                if raw is None:
                    await asyncio.sleep(0)
                    continue

                self._frame_counter += 1
                frame = FrameData(
                    data=raw.get("data", np.empty((0,), dtype=np.uint8)),
                    width=raw.get("width", 0),
                    height=raw.get("height", 0),
                    timestamp=raw.get("timestamp", time.time()),
                    frame_number=self._frame_counter,
                    scale_factor=raw.get("scale_factor", 1.0),
                )

                if self._should_process(frame):
                    self._enqueue_frame(frame)

        except asyncio.CancelledError:
            logger.debug("SCK capture loop cancelled")
            raise
        except Exception as exc:
            logger.exception("SCK capture loop error: %s", exc)
        finally:
            try:
                await stream.stop()
            except Exception:
                pass
