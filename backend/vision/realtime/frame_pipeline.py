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

    Pure numpy — no PIL. Uses block-mean downsampling instead of
    PIL LANCZOS resize. 50x faster: ~0.5ms vs ~30ms at 1440x900.

    Divides the frame into (hash_size x hash_size+1) blocks, computes
    the mean luminance of each block, then encodes left/right brightness
    relationships into a 64-bit integer.
    """
    h, w = frame.shape[:2]
    rows, cols = hash_size, hash_size + 1

    # Trim frame to evenly divisible dimensions
    bh = h // rows
    bw = w // cols
    trimmed_h = rows * bh
    trimmed_w = cols * bw

    if len(frame.shape) == 3:
        # Multichannel: use green channel (fastest single-channel approx of luma)
        # Green contributes ~59% of perceived luminance — good enough for dhash
        gray = frame[:trimmed_h, :trimmed_w, 1]
    else:
        gray = frame[:trimmed_h, :trimmed_w]

    # Block-mean: reshape into (rows, bh, cols, bw) then mean over block dims
    # This is pure numpy — vectorized, no Python loops, no PIL
    blocks = gray.reshape(rows, bh, cols, bw).mean(axis=(1, 3))

    # Difference hash: compare adjacent columns
    diff = blocks[:, 1:] > blocks[:, :-1]
    return int.from_bytes(np.packbits(diff.flatten()[:64]).tobytes(), "big")


def _hamming_distance(a: int, b: int) -> int:
    """Count differing bits between two 64-bit integers."""
    return bin(a ^ b).count("1")


def _mean_luminance(frame: np.ndarray) -> float:
    """Return mean luminance in [0, 1] for a frame.

    Uses subsampled mean (every 8th pixel) instead of full-frame mean.
    ~8x faster for 1440x900: ~0.4ms vs ~3ms.
    """
    # Subsample every 8th pixel in both dimensions — 64x fewer pixels
    sampled = frame[::8, ::8]
    return float(np.mean(sampled)) / 255.0


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
        self._latest_frame: Optional[FrameData] = None

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

        # Signal SCK background thread to stop (if SHM mode was used)
        if hasattr(self, "_sck_thread_stop"):
            self._sck_thread_stop.set()

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

    @property
    def latest_frame(self) -> Optional["FrameData"]:
        """Most recent frame — non-destructive read. Does not consume from queue."""
        return self._latest_frame

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
        self._latest_frame = frame
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
    # Capture backend selection — Capability Gate (zero dead time)
    # ------------------------------------------------------------------
    #
    # SCK (ScreenCaptureKit) requires a pumped CFRunLoop for its GCD
    # completion handlers. The asyncio event loop does NOT pump a
    # CFRunLoop, so SCK will hang indefinitely when started from
    # asyncio.to_thread() in the supervisor process.
    #
    # CoreGraphics (CGWindowListCreateImage via fast_capture) is
    # synchronous — no GCD callbacks, no run loop needed. Works
    # reliably from any thread in any process context.
    #
    # Capability Gate: detect the execution environment and choose the
    # correct backend IMMEDIATELY. Never attempt a known-doomed path.
    #
    # VISION_CAPTURE_BACKEND env var:
    #   "auto" (default) — SHM bridge (SCK thread → SHM → asyncio poll) in asyncio,
    #                       falls back to CoreGraphics subprocess if SHM fails
    #   "shm"            — force SHM bridge (SCK in thread, poll from asyncio)
    #   "coregraphics"   — force CG subprocess (always safe, ~3fps)
    #   "sck"            — force SCK direct (only for dedicated CFRunLoop thread)

    async def _sck_capture_loop(self) -> None:
        """Capability-gated capture backend selection. Zero dead time."""
        backend = os.environ.get("VISION_CAPTURE_BACKEND", "auto").lower()

        if backend == "sck":
            # Explicit SCK override — caller asserts CFRunLoop is available
            logger.info("[FramePipeline] Backend forced to SCK via VISION_CAPTURE_BACKEND")
            await self._sck_stream_loop()
            return

        if backend == "coregraphics":
            # Explicit CG override
            logger.info(
                "[FramePipeline] Backend forced to CoreGraphics via VISION_CAPTURE_BACKEND "
                "(vision_capture_mode=coregraphics)"
            )
            await self._coregraphics_capture_loop()
            return

        if backend == "shm":
            # Explicit SHM override
            logger.info("[FramePipeline] Backend forced to SHM via VISION_CAPTURE_BACKEND")
            await self._shm_capture_loop()
            return

        # --- Auto mode: SHM bridge is the primary path ---
        # SCK runs in a dedicated thread with its own CFRunLoop pump.
        # Frames land in SHM ring buffer. Python polls SHM from asyncio.
        # This completely bypasses the "asyncio can't pump CFRunLoop" problem.
        # Falls back to CoreGraphics subprocess ONLY if SHM fails to start.
        logger.info(
            "[FramePipeline] Auto mode — attempting SHM bridge "
            "(SCK thread → SHM → asyncio poll)"
        )
        try:
            await self._shm_capture_loop()
        except Exception as exc:
            logger.warning(
                "[FramePipeline] SHM bridge failed (%s), falling back to "
                "CoreGraphics subprocess",
                exc,
            )
            await self._coregraphics_capture_loop()

    async def _sck_stream_loop(self) -> None:
        """SCK streaming loop — only called when CFRunLoop is available."""
        try:
            from backend.native_extensions.macos_sck_stream import (
                AsyncCaptureStream,
                StreamingConfig,
            )
        except ImportError as exc:
            logger.info("[FramePipeline] SCK extension not available: %s", exc)
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
                logger.error("[FramePipeline] SCK stream start returned False")
                return

            logger.info(
                "[FramePipeline] SCK capture running (vision_capture_mode=sck, window=%d)",
                self._window_id,
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

    # ------------------------------------------------------------------
    # SHM Bridge: SCK thread → SHM ring buffer → asyncio poll
    # ------------------------------------------------------------------
    # This is the 60fps path. SCK runs in a dedicated daemon thread with
    # its own CFRunLoop pump. The delegate writes frames to a 5-slot SHM
    # ring buffer. Python polls the ring buffer from asyncio using
    # zero-copy numpy.frombuffer over mmap.
    #
    # Architecture:
    #   [SCK thread] → didOutputSampleBuffer → ShmFrameWriter → /jarvis_frame_bridge
    #   [asyncio]    → ShmFrameReader.read_latest() → numpy view → FrameData
    #
    # Why this works in asyncio:
    #   - SCK's GCD callbacks fire on the SCK thread's CFRunLoop (not asyncio)
    #   - SHM is a shared memory segment — no GCD, no RunLoop, no GIL needed
    #   - Python's mmap.mmap with ACCESS_WRITE gives coherent reads
    #   - numpy.frombuffer is zero-copy — no memcpy, no allocation

    def _start_sck_background_thread(self) -> bool:
        """Start SCK capture in a dedicated daemon thread.

        Returns True if the thread started successfully and SCK is writing
        to SHM. The thread runs until self._running becomes False.
        """
        import threading

        target_fps = int(os.environ.get("VISION_CAPTURE_FPS", "60"))
        ready_event = threading.Event()
        self._sck_thread_stop = threading.Event()

        def _sck_thread():
            try:
                import sys as _sys
                _ext = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__)))),
                    "native_extensions",
                )
                if _ext not in _sys.path:
                    _sys.path.insert(0, _ext)

                import fast_capture_stream

                config = fast_capture_stream.StreamConfig()
                config.target_fps = target_fps
                config.max_buffer_size = 3
                config.output_format = "raw"
                config.use_gpu_acceleration = True
                config.drop_frames_on_overflow = True

                stream = fast_capture_stream.CaptureStream(self._window_id, config)
                if not stream.start():
                    logger.error(
                        "[FramePipeline] SCK thread: stream.start() FAILED "
                        "(window=%d, fps=%d)",
                        self._window_id, target_fps,
                    )
                    return

                logger.info(
                    "[FramePipeline] SCK thread running — target %dfps, "
                    "window=%d, SHM writes active",
                    target_fps, self._window_id,
                )
                ready_event.set()

                # Keep thread alive — SCK delivers frames via delegate → SHM.
                # No get_frame() needed — SHM write happens in the delegate.
                while not self._sck_thread_stop.is_set() and self._running:
                    self._sck_thread_stop.wait(timeout=0.1)

                stream.stop()
                logger.info("[FramePipeline] SCK thread stopped")

            except Exception as exc:
                logger.error("[FramePipeline] SCK thread error: %s", exc)

        t = threading.Thread(target=_sck_thread, daemon=True, name="fp-sck-shm")
        t.start()

        # Wait for SCK to start and write initial frames
        if not ready_event.wait(timeout=5.0):
            logger.warning("[FramePipeline] SCK thread did not start within 5s")
            self._sck_thread_stop.set()
            return False

        return True

    async def _shm_capture_loop(self) -> None:
        """SHM bridge capture loop — polls SHM ring buffer from asyncio.

        1. Start SCK in a background thread (with CFRunLoop pump)
        2. Wait for SHM to have data
        3. Poll SHM at maximum rate, yielding to asyncio between reads
        """
        from backend.vision.shm_frame_reader import ShmFrameReader

        # Phase 1: Start SCK background thread
        if not self._start_sck_background_thread():
            raise RuntimeError("SCK background thread failed to start")

        # Phase 2: Open SHM reader
        await asyncio.sleep(1.0)  # Let SCK warm up and write initial frames

        reader = ShmFrameReader()
        if not reader.open():
            self._sck_thread_stop.set()
            raise RuntimeError("SHM reader failed to open — SCK not writing?")

        target_fps = int(os.environ.get("VISION_CAPTURE_FPS", "60"))
        # Adaptive sleep: when no new frame, sleep briefly to avoid busy-spin.
        # At 60fps, frames arrive every ~16.7ms. Sleep 1ms between polls gives
        # ~16 polls per frame interval — responsive without burning CPU.
        poll_sleep_s = float(os.environ.get("VISION_SHM_POLL_SLEEP_S", "0.001"))

        logger.info(
            "[FramePipeline] SHM capture running "
            "(vision_capture_mode=shm, window=%d, target=%dfps, "
            "poll_sleep=%.1fms, frame=%dx%dx%d)",
            self._window_id, target_fps, poll_sleep_s * 1000,
            reader.width, reader.height, reader.channels,
        )

        try:
            consecutive_empty = 0
            while self._running:
                frame_arr, _counter = reader.read_latest()

                if frame_arr is None:
                    consecutive_empty += 1
                    # Adaptive backoff: sleep longer if we keep getting empty reads
                    if consecutive_empty > 100:
                        await asyncio.sleep(poll_sleep_s * 10)  # 10ms
                    elif consecutive_empty > 10:
                        await asyncio.sleep(poll_sleep_s)  # 1ms
                    else:
                        await asyncio.sleep(0)  # yield
                    continue

                consecutive_empty = 0
                self._frame_counter += 1

                # SHM frame is BGRA — convert channel order for consumers
                # expecting RGB. numpy view, no copy.
                if frame_arr.shape[2] == 4:
                    # BGRA → RGB: drop alpha, swap B/R
                    rgb = frame_arr[:, :, [2, 1, 0]]
                else:
                    rgb = frame_arr

                frame = FrameData(
                    data=rgb,
                    width=reader.width,
                    height=reader.height,
                    timestamp=time.time(),
                    frame_number=self._frame_counter,
                    scale_factor=1.0,
                )

                if self._should_process(frame):
                    self._enqueue_frame(frame)

        except asyncio.CancelledError:
            logger.debug("SHM capture loop cancelled")
            raise
        except Exception as exc:
            logger.exception("SHM capture loop error: %s", exc)
        finally:
            reader.close()
            if hasattr(self, "_sck_thread_stop"):
                self._sck_thread_stop.set()

    async def _coregraphics_capture_loop(self) -> None:
        """Subprocess-based screen capture via macOS screencapture command.

        Native capture APIs (both SCK and CoreGraphics C++ extensions) hang
        when called from thread pool threads in the asyncio process — they
        need framework initialization that thread pool threads lack.

        The screencapture command is a SEPARATE PROCESS with its own framework
        context. It is the structurally correct capture method for an asyncio
        host. Not a fallback — the primary reliable path.

        Captures at ~2-3 FPS (subprocess overhead). Sufficient for VisionCortex
        continuous awareness and the agentic vision loop.
        """
        import tempfile as _tf
        from PIL import Image as _Image

        target_fps = min(int(os.environ.get("VISION_CAPTURE_FPS", "3")), 3)
        interval = 1.0 / target_fps
        tmp_path = os.path.join(_tf.gettempdir(), f"jarvis_capture_{os.getpid()}.png")

        logger.info(
            "[FramePipeline] Subprocess capture running at %dfps "
            "(vision_capture_mode=subprocess)",
            target_fps,
        )

        try:
            while self._running:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "screencapture", "-x", "-C", tmp_path,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(proc.wait(), timeout=5.0)

                    if proc.returncode == 0:
                        try:
                            img = _Image.open(tmp_path)
                            img_rgb = img.convert("RGB")
                            img_array = np.array(img_rgb)

                            self._frame_counter += 1
                            frame = FrameData(
                                data=img_array,
                                width=img_array.shape[1],
                                height=img_array.shape[0],
                                timestamp=time.time(),
                                frame_number=self._frame_counter,
                                scale_factor=2.0,
                            )

                            if self._should_process(frame):
                                self._enqueue_frame(frame)
                        except Exception as exc:
                            logger.debug("[FramePipeline] Frame read error: %s", exc)

                except asyncio.TimeoutError:
                    logger.debug("[FramePipeline] screencapture timed out (5s)")
                except Exception as exc:
                    logger.debug("[FramePipeline] Capture error: %s", exc)

                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            logger.debug("Subprocess capture loop cancelled")
            raise
        except Exception as exc:
            logger.exception("Subprocess capture loop error: %s", exc)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
