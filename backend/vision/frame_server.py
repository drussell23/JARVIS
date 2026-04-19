#!/usr/bin/env python3
"""
Frame Server -- persistent subprocess for continuous screen capture.

Captures frames using Quartz CGWindowListCreateImage at ~15fps and
writes the latest JPEG to an atomic file.  The main JARVIS process
reads this file for sub-10ms frame access.

Quartz is imported HERE (safe in a dedicated subprocess) and NEVER
in the main JARVIS process (where it conflicts with CoreAudio/sounddevice).

Output files (atomic writes via rename):
    /tmp/claude/latest_frame.jpg   -- latest JPEG frame
    /tmp/claude/latest_frame.json  -- {ts, width, height, dhash, frame_number}

Filename contract: the sidecar JSON MUST be ``latest_frame.json`` — this is
the name ``VisionSensor._DEFAULT_METADATA_PATH`` reads. Producer conforms
to consumer, not the other way around.

Run:  python3 backend/vision/frame_server.py [--fps 15] [--quality 70] [--max-dim 1280]

Stdout protocol:
    Line 1: {"ok":true,"status":"ready","pid":12345}
    Subsequent: {"frame_number":N,"width":W,"height":H,"fps":F} (every 100 frames)
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

_TMP_DIR = os.environ.get("VISION_FRAME_DIR", "/tmp/claude")
_FRAME_PATH = os.path.join(_TMP_DIR, "latest_frame.jpg")
_META_PATH = os.path.join(_TMP_DIR, "latest_frame.json")


def _respond(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _dhash(pixel_data: bytes, width: int, height: int, hash_size: int = 8) -> int:
    """Compute a fast perceptual hash from raw BGRA pixel data.

    Samples a grid of (hash_size+1) x hash_size luminance values directly
    from the raw pixel buffer.  No PIL or numpy required.
    """
    if not pixel_data or width <= 0 or height <= 0:
        return 0

    bpp = 4  # BGRA
    row_bytes = width * bpp
    step_x = max(1, width // (hash_size + 1))
    step_y = max(1, height // hash_size)

    # Sample luminance values (0.299*R + 0.587*G + 0.114*B)
    rows = []
    for gy in range(hash_size):
        py = min(gy * step_y, height - 1)
        row = []
        for gx in range(hash_size + 1):
            px = min(gx * step_x, width - 1)
            offset = py * row_bytes + px * bpp
            if offset + 2 < len(pixel_data):
                b = pixel_data[offset]
                g = pixel_data[offset + 1]
                r = pixel_data[offset + 2]
                lum = int(0.299 * r + 0.587 * g + 0.114 * b)
            else:
                lum = 0
            row.append(lum)
        rows.append(row)

    # Compute dhash: compare adjacent columns
    bits = []
    for row in rows:
        for i in range(len(row) - 1):
            bits.append(1 if row[i] < row[i + 1] else 0)

    # Pack into integer
    result = 0
    for bit in bits[:64]:
        result = (result << 1) | bit
    return result


def capture_loop(fps: int = 15, quality: float = 0.7, max_dim: int = 1280) -> None:
    """Main capture loop using Quartz."""
    try:
        import Quartz
        from Quartz import (
            CGWindowListCreateImage,
            CGRectInfinite,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            kCGWindowImageDefault,
            CGImageGetWidth,
            CGImageGetHeight,
            CGImageGetDataProvider,
            CGDataProviderCopyData,
            CGImageDestinationCreateWithURL,
            CGImageDestinationAddImage,
            CGImageDestinationFinalize,
            CGMainDisplayID,
            CGDisplayBounds,
        )
        from CoreFoundation import (
            CFURLCreateWithFileSystemPath,
            kCFURLPOSIXPathStyle,
        )
    except ImportError as e:
        _respond({"ok": False, "status": "error", "error": f"Quartz import failed: {e}"})
        sys.exit(1)

    os.makedirs(_TMP_DIR, exist_ok=True)

    # Capture ONLY the main display, not the union of all displays.
    # CGRectInfinite spans all monitors (including virtual ghost displays),
    # which composites them into one image and shrinks the primary content.
    main_display_rect = CGDisplayBounds(CGMainDisplayID())
    _respond({
        "ok": True, "status": "ready", "pid": os.getpid(),
        "main_display": {
            "x": main_display_rect.origin.x,
            "y": main_display_rect.origin.y,
            "w": main_display_rect.size.width,
            "h": main_display_rect.size.height,
        },
    })

    frame_number = 0
    interval = 1.0 / fps
    tmp_path = _FRAME_PATH + ".tmp"

    while True:
        t0 = time.monotonic()

        try:
            # Capture MAIN display only (not ghost/virtual displays)
            image_ref = CGWindowListCreateImage(
                main_display_rect,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
                kCGWindowImageDefault,
            )
            if image_ref is None:
                time.sleep(interval)
                continue

            width = CGImageGetWidth(image_ref)
            height = CGImageGetHeight(image_ref)

            # Get raw pixel data for dhash
            provider = CGImageGetDataProvider(image_ref)
            raw_data = CGDataProviderCopyData(provider)
            raw_bgra = bytes(raw_data)

            # Enforce max-dim clamp: Retina captures land at physical resolution
            # (e.g. 2880x1800), which bottlenecks the encode loop and wastes
            # downstream token/latency budget. Downscale above threshold.
            pil_img = None
            if max_dim > 0 and max(width, height) > max_dim:
                if not _PIL_AVAILABLE:
                    _respond({"ok": False, "status": "error", "error": "Pillow required for --max-dim resize (pip install Pillow)"})
                    sys.exit(1)
                scale = max_dim / max(width, height)
                new_w = max(1, int(width * scale))
                new_h = max(1, int(height * scale))
                pil_img = Image.frombuffer("RGBA", (width, height), raw_bgra, "raw", "BGRA", 0, 1)
                pil_img = pil_img.resize((new_w, new_h), Image.BILINEAR)
                width, height = new_w, new_h
                raw_bgra = pil_img.tobytes("raw", "BGRA", 0, 1)

            dhash_val = _dhash(raw_bgra, width, height)

            # JPEG encode: PIL for resized frames, Quartz ImageIO for native frames
            if pil_img is not None:
                pil_img.convert("RGB").save(tmp_path, "JPEG", quality=int(quality * 100))
            else:
                url = CFURLCreateWithFileSystemPath(
                    None, tmp_path, kCFURLPOSIXPathStyle, False,
                )
                dest = CGImageDestinationCreateWithURL(url, "public.jpeg", 1, None)
                if dest is None:
                    time.sleep(interval)
                    continue

                props = {Quartz.kCGImageDestinationLossyCompressionQuality: quality}
                CGImageDestinationAddImage(dest, image_ref, props)
                CGImageDestinationFinalize(dest)

            # Atomic rename
            os.rename(tmp_path, _FRAME_PATH)

            # Write metadata
            meta = {
                "ts": time.time(),
                "width": width,
                "height": height,
                "dhash": dhash_val,
                "frame_number": frame_number,
            }
            meta_tmp = _META_PATH + ".tmp"
            with open(meta_tmp, "w") as f:
                json.dump(meta, f)
            os.rename(meta_tmp, _META_PATH)

            frame_number += 1

            # Periodic status (every 100 frames)
            if frame_number % 100 == 0:
                elapsed = time.monotonic() - t0
                actual_fps = 1.0 / elapsed if elapsed > 0 else 0
                _respond({
                    "frame_number": frame_number,
                    "width": width,
                    "height": height,
                    "fps": round(actual_fps, 1),
                })

        except Exception as exc:
            # Log but don't crash — keep capturing
            sys.stderr.write(f"[FrameServer] Capture error: {exc}\n")
            sys.stderr.flush()

        # Sleep for remainder of interval
        elapsed = time.monotonic() - t0
        sleep_time = max(0, interval - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)


def main() -> None:
    parser = argparse.ArgumentParser(description="JARVIS Frame Server")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--quality", type=float, default=0.7)
    parser.add_argument("--max-dim", type=int, default=1280)
    args = parser.parse_args()
    capture_loop(fps=args.fps, quality=args.quality, max_dim=args.max_dim)


if __name__ == "__main__":
    main()
