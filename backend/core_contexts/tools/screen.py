"""
Atomic screen capture and motion detection tools.

These tools provide the Executor context with visual perception of the
host macOS display.  Every function is async, stateless, and delegates
to the existing FramePipeline / ScreenCaptureBridge / CoreGraphics
infrastructure.  No pyautogui.  No blocking I/O on the main thread.

The 397B Architect selects these tools by reading docstrings.  Keep
them precise: what it does, what it returns, when to use it.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

_TMP_DIR = os.environ.get("VISION_TMP_DIR", "/tmp/claude")
_CAPTURE_TIMEOUT_S = float(os.environ.get("TOOL_CAPTURE_TIMEOUT_S", "5.0"))
_MAX_IMAGE_DIM = int(os.environ.get("TOOL_MAX_IMAGE_DIM", "1024"))
_JPEG_QUALITY = int(os.environ.get("TOOL_JPEG_QUALITY", "70"))
_SETTLEMENT_POLL_MS = int(os.environ.get("TOOL_SETTLEMENT_POLL_MS", "50"))
_SETTLEMENT_MAX_MS = int(os.environ.get("TOOL_SETTLEMENT_MAX_MS", "2000"))
_DHASH_SIZE = int(os.environ.get("TOOL_DHASH_SIZE", "8"))


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScreenFrame:
    """A captured screen frame with metadata.

    Attributes:
        data: RGB pixel array, shape (H, W, 3).
        width: Frame width in pixels.
        height: Frame height in pixels.
        timestamp: POSIX timestamp at capture.
        scale_factor: Retina scale (2.0 on HiDPI, 1.0 otherwise).
        dhash: 64-bit perceptual hash for motion comparison.
    """
    data: np.ndarray
    width: int
    height: int
    timestamp: float
    scale_factor: float
    dhash: int


@dataclass(frozen=True)
class CompressedFrame:
    """A JPEG-compressed frame ready for vision model consumption.

    Attributes:
        b64: Base64-encoded JPEG bytes.
        width: Image width after downscaling.
        height: Image height after downscaling.
        coord_scale: Multiply image coords by this to get logical screen coords.
        size_kb: Compressed size in kilobytes.
    """
    b64: str
    width: int
    height: int
    coord_scale: float
    size_kb: float


# ---------------------------------------------------------------------------
# Tool: capture_screen
# ---------------------------------------------------------------------------

async def capture_screen() -> Optional[ScreenFrame]:
    """Capture the current screen state as an RGB numpy array.

    Tries the following backends in order:
      1. FramePipeline.latest_frame (sub-10ms if Ferrari Engine is running)
      2. VisionCortex frame pipeline
      3. screencapture subprocess (async, ~2s, always available)

    Returns:
        ScreenFrame with RGB pixel data, dimensions, and dhash.
        None if all capture methods fail (check Screen Recording permissions).

    Use when:
        The Executor needs to see the current state of the screen before
        deciding what to click, type, or verify.
    """
    # --- Primary: FramePipeline (sub-10ms) ---
    frame = _try_frame_pipeline()
    if frame is not None:
        logger.info("[tool:screen] Frame from FramePipeline (sub-10ms)")
        return frame

    # --- Fallback: screencapture subprocess ---
    return await _capture_subprocess()


async def capture_and_compress(
    logical_screen_size: Optional[Tuple[int, int]] = None,
) -> Optional[CompressedFrame]:
    """Capture the screen and compress to JPEG for vision model consumption.

    Downscales the Retina screenshot to logical screen coordinates so that
    pixel coordinates returned by the vision model map directly to the
    actuator's coordinate space.  No manual coordinate conversion needed.

    Args:
        logical_screen_size: (width, height) of the logical display.
            Auto-detected via pyautogui if not provided.

    Returns:
        CompressedFrame with base64 JPEG, dimensions, and coord_scale.
        None if capture fails.

    Use when:
        The Executor needs a screenshot to send to the vision model
        (J-Prime or Claude) for action planning.
    """
    frame = await capture_screen()
    if frame is None:
        return None

    return _compress_frame(frame, logical_screen_size)


# ---------------------------------------------------------------------------
# Tool: compute_dhash
# ---------------------------------------------------------------------------

def compute_dhash(frame_data: np.ndarray, hash_size: int = _DHASH_SIZE) -> int:
    """Compute a 64-bit perceptual difference hash (dhash) for a frame.

    The dhash encodes spatial brightness gradients into a compact integer.
    Two frames with a small Hamming distance between their dhashes look
    visually similar; a large distance means significant visual change.

    Args:
        frame_data: RGB numpy array, shape (H, W, 3) or grayscale (H, W).
        hash_size: Grid size for the hash (default 8 produces 64-bit hash).

    Returns:
        64-bit integer encoding the perceptual hash.

    Use when:
        The Observer needs to detect whether the screen has changed
        between two captures (e.g., after an action, before the next turn).
    """
    from PIL import Image

    if len(frame_data.shape) == 3:
        gray = np.mean(frame_data, axis=2).astype(np.uint8)
    else:
        gray = frame_data.astype(np.uint8)

    img = Image.fromarray(gray).resize(
        (hash_size + 1, hash_size), Image.LANCZOS,
    )
    pixels = np.array(img)
    diff = pixels[:, 1:] > pixels[:, :-1]
    return int.from_bytes(np.packbits(diff.flatten()[:64]).tobytes(), "big")


def hamming_distance(hash_a: int, hash_b: int) -> int:
    """Count differing bits between two 64-bit dhash values.

    Args:
        hash_a: First dhash.
        hash_b: Second dhash.

    Returns:
        Number of differing bits (0 = identical, 64 = maximally different).

    Use when:
        Comparing two dhashes to decide if the screen changed enough
        to warrant a new vision query.
    """
    return bin(hash_a ^ hash_b).count("1")


# ---------------------------------------------------------------------------
# Tool: await_pixel_settlement
# ---------------------------------------------------------------------------

async def await_pixel_settlement(
    reference_dhash: Optional[int] = None,
    threshold_bits: int = 4,
) -> Optional[ScreenFrame]:
    """Wait until the screen pixels stop changing, then return the settled frame.

    After an action (click, type, scroll), the UI animates briefly.  This
    tool polls the screen at short intervals and returns the first frame
    whose dhash is stable (differs from the previous frame by fewer than
    threshold_bits).  Replaces blind asyncio.sleep() waits.

    Args:
        reference_dhash: If provided, also requires the settled frame to
            differ from this hash (ensures the action had visible effect).
        threshold_bits: Maximum Hamming distance between consecutive frames
            to consider the screen "settled" (default 4 out of 64 bits).

    Returns:
        The settled ScreenFrame, or None if settlement times out.

    Use when:
        The Executor just performed an action and needs to wait for the
        UI to finish updating before taking the next screenshot.
    """
    poll_s = _SETTLEMENT_POLL_MS / 1000.0
    max_s = _SETTLEMENT_MAX_MS / 1000.0
    start = time.monotonic()
    prev_hash: Optional[int] = None

    while (time.monotonic() - start) < max_s:
        frame = await capture_screen()
        if frame is None:
            await asyncio.sleep(poll_s)
            continue

        current_hash = frame.dhash

        if prev_hash is not None:
            dist = hamming_distance(current_hash, prev_hash)
            if dist <= threshold_bits:
                # Screen is settled.  If caller wants to confirm the action
                # caused visible change, check against reference.
                if reference_dhash is not None:
                    ref_dist = hamming_distance(current_hash, reference_dhash)
                    if ref_dist <= threshold_bits:
                        # Screen settled but looks the same as before action.
                        # Keep polling -- the action effect hasn't appeared yet.
                        prev_hash = current_hash
                        await asyncio.sleep(poll_s)
                        continue

                logger.info(
                    "[tool:screen] Pixels settled after %.0fms (dist=%d)",
                    (time.monotonic() - start) * 1000, dist,
                )
                return frame

        prev_hash = current_hash
        await asyncio.sleep(poll_s)

    logger.warning(
        "[tool:screen] Pixel settlement timed out after %dms", _SETTLEMENT_MAX_MS,
    )
    # Return the last captured frame even if not fully settled
    return await capture_screen()


# ---------------------------------------------------------------------------
# Tool: detect_motion
# ---------------------------------------------------------------------------

def detect_motion(
    frame_a: ScreenFrame,
    frame_b: ScreenFrame,
    threshold_bits: int = 4,
) -> bool:
    """Detect whether significant visual change occurred between two frames.

    Args:
        frame_a: The earlier frame.
        frame_b: The later frame.
        threshold_bits: Minimum Hamming distance to count as motion.

    Returns:
        True if the frames differ significantly (motion detected).

    Use when:
        The Observer needs to know if something changed on screen
        (e.g., a notification appeared, a page finished loading).
    """
    return hamming_distance(frame_a.dhash, frame_b.dhash) > threshold_bits


# ---------------------------------------------------------------------------
# Internal helpers (not exposed as tools)
# ---------------------------------------------------------------------------

def _try_frame_pipeline() -> Optional[ScreenFrame]:
    """Try to read from existing FramePipeline singleton."""
    for import_path in ("backend.vision.realtime.vision_action_loop",
                        "vision.realtime.vision_action_loop"):
        try:
            import importlib
            mod = importlib.import_module(import_path)
            cls = getattr(mod, "VisionActionLoop", None)
            if cls and hasattr(cls, "get_instance"):
                instance = cls.get_instance()
                if instance and hasattr(instance, "frame_pipeline"):
                    pipeline = instance.frame_pipeline
                    if pipeline and pipeline.latest_frame is not None:
                        fd = pipeline.latest_frame
                        return ScreenFrame(
                            data=fd.data,
                            width=fd.width,
                            height=fd.height,
                            timestamp=fd.timestamp,
                            scale_factor=getattr(fd, "scale_factor", 2.0),
                            dhash=compute_dhash(fd.data),
                        )
        except (ImportError, Exception):
            continue
    return None


async def _capture_subprocess() -> Optional[ScreenFrame]:
    """Capture via async screencapture subprocess."""
    os.makedirs(_TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(_TMP_DIR, f"tool_{uuid.uuid4().hex[:8]}.png")

    try:
        proc = await asyncio.create_subprocess_exec(
            "screencapture", "-x", "-C", tmp_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        returncode = await asyncio.wait_for(
            proc.wait(), timeout=_CAPTURE_TIMEOUT_S,
        )
        if returncode != 0:
            logger.error("[tool:screen] screencapture exit code %d", returncode)
            return None

        from PIL import Image
        img = Image.open(tmp_path)
        if img.mode == "RGBA":
            img = img.convert("RGB")

        data = np.array(img)
        w, h = img.size

        return ScreenFrame(
            data=data,
            width=w,
            height=h,
            timestamp=time.time(),
            scale_factor=2.0,
            dhash=compute_dhash(data),
        )

    except asyncio.TimeoutError:
        logger.error("[tool:screen] screencapture timed out")
        return None
    except Exception as exc:
        logger.error("[tool:screen] capture error: %s", exc)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _compress_frame(
    frame: ScreenFrame,
    logical_size: Optional[Tuple[int, int]] = None,
) -> CompressedFrame:
    """Downscale and JPEG-compress a frame for vision model consumption."""
    from PIL import Image

    img = Image.fromarray(frame.data)

    # Downscale Retina to logical so coords map directly to actuator space
    if logical_size is None:
        try:
            import pyautogui
            logical_size = pyautogui.size()
        except Exception:
            logical_size = (frame.width, frame.height)

    lw, lh = logical_size
    if lw > 0 and lh > 0 and (frame.width, frame.height) != (lw, lh):
        img = img.resize((lw, lh), Image.LANCZOS)

    # Further downscale if over max dimension
    coord_scale = 1.0
    cur_w, cur_h = img.size
    if max(cur_w, cur_h) > _MAX_IMAGE_DIM:
        ratio = _MAX_IMAGE_DIM / max(cur_w, cur_h)
        img = img.resize((int(cur_w * ratio), int(cur_h * ratio)), Image.LANCZOS)
        coord_scale = 1.0 / ratio

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    raw = buf.getvalue()
    b64 = base64.b64encode(raw).decode("ascii")

    final_w, final_h = img.size
    return CompressedFrame(
        b64=b64,
        width=final_w,
        height=final_h,
        coord_scale=coord_scale,
        size_kb=len(raw) / 1024,
    )
