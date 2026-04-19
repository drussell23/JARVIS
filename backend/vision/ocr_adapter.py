"""macOS Vision framework OCR adapter for VisionSensor.

Offloads text recognition to Apple's native Vision API, which runs
hardware-accelerated on the Apple Neural Engine (M-series) or Metal
(Intel). Zero third-party runtime dependencies — uses PyObjC which is
already a transitive requirement via Quartz bindings in
``backend/vision/frame_server.py``.

**Manifesto §3 (Disciplined Concurrency)**: OCR compute is offloaded
to the OS out of the Python event loop's critical path. ``VNRecognizeTextRequest``
at the ``Fast`` recognition level typically returns in 50-150ms warm
for a 1280x800 frame on Apple Silicon (first call pays a one-time
~500ms framework-initialization cost).

**Authority invariant**: this module is a pure function surface. It
takes a JPEG file path, returns a string. No side effects, no state
mutation, no INFO-level logging. Callers (``VisionSensor._ingest_frame``
via ``ocr_fn``) own the emission pipeline and firewall sanitization.

**Fail-closed contract**: returns ``""`` on any failure path (missing
file, unreadable image, Vision framework absent, Vision request error,
any exception). The sensor treats empty OCR output the same as "no
Tier 1 match", so a silent OCR failure is observably equivalent to
"screen had nothing worth emitting" rather than a sensor crash.

Install requirement: ``pyobjc-framework-Vision`` (pulled by
``pyobjc-framework-CoreML`` as transitive). If missing, the adapter
silently returns empty strings and the Vision sensor degrades
gracefully to its no-OCR path.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.OCRAdapter")

# Lazy-loaded framework handles. Cached after first successful load so
# every subsequent call is a dict lookup, not a module-system walk.
_VISION_AVAILABLE: Optional[bool] = None
_vision_modules: Dict[str, Any] = {}


def _load_vision_framework() -> bool:
    """Import and cache the PyObjC Vision / Quartz / Foundation bindings.

    Returns ``True`` on success, ``False`` on any ImportError (framework
    not installed, running on Linux, etc.). First-call side effect
    populates ``_vision_modules``; subsequent calls short-circuit.
    """
    global _VISION_AVAILABLE
    if _VISION_AVAILABLE is not None:
        return _VISION_AVAILABLE
    try:
        from Foundation import NSURL
        from Quartz import (
            CGImageSourceCreateWithURL,
            CGImageSourceCreateImageAtIndex,
        )
        from Vision import (
            VNImageRequestHandler,
            VNRecognizeTextRequest,
            VNRequestTextRecognitionLevelFast,
            VNRequestTextRecognitionLevelAccurate,
        )
        _vision_modules.update({
            "NSURL": NSURL,
            "CGImageSourceCreateWithURL": CGImageSourceCreateWithURL,
            "CGImageSourceCreateImageAtIndex": CGImageSourceCreateImageAtIndex,
            "VNImageRequestHandler": VNImageRequestHandler,
            "VNRecognizeTextRequest": VNRecognizeTextRequest,
            "VNRequestTextRecognitionLevelFast": VNRequestTextRecognitionLevelFast,
            "VNRequestTextRecognitionLevelAccurate": VNRequestTextRecognitionLevelAccurate,
        })
        _VISION_AVAILABLE = True
    except ImportError as exc:
        logger.debug(
            "[OCRAdapter] macOS Vision framework unavailable "
            "(install pyobjc-framework-Vision): %s",
            exc,
        )
        _VISION_AVAILABLE = False
    return _VISION_AVAILABLE


def recognize_text(frame_path: str) -> str:
    """Run macOS Vision OCR on ``frame_path``. Return recognized text.

    Empty string on any failure path. Never raises — the VisionSensor
    treats ``ocr_fn`` as a pure ``Callable[[str], str]`` and wraps its
    own exception handler, but we fail closed at this layer too so
    Vision-framework oddities (entitlement prompts, resource exhaustion,
    malformed JPEGs) don't propagate up as scan-loop exceptions.

    Text output is line-joined from all ``VNRecognizedTextObservation``
    instances in top-to-bottom source order, taking the top candidate
    for each observation. Language correction is disabled — matches the
    Tier 1 regex pattern set (``traceback`` / ``panic`` / ``segfault`` /
    ``modal-error`` / ``linter-red``) which needs raw verbatim text,
    not autocorrected English.
    """
    if not os.path.exists(frame_path):
        return ""
    if not _load_vision_framework():
        return ""

    mods = _vision_modules
    try:
        url = mods["NSURL"].fileURLWithPath_(frame_path)
        source = mods["CGImageSourceCreateWithURL"](url, None)
        if source is None:
            return ""
        image = mods["CGImageSourceCreateImageAtIndex"](source, 0, None)
        if image is None:
            return ""

        request = mods["VNRecognizeTextRequest"].alloc().init()
        # Recognition level: Accurate by default (~700ms/frame warm) vs Fast
        # (~100ms/frame). Accurate is required for sparse-text screens
        # (e.g., a traceback in the top-left of an otherwise-black terminal
        # window) — empirically Fast mode silently drops such regions. The
        # VisionSensor's adaptive 1-8s scan cadence accommodates the extra
        # latency with ample headroom (§3 Disciplined Concurrency).
        # Env tunable for offline/batch use cases: JARVIS_VISION_OCR_LEVEL=fast
        _level_name = os.environ.get("JARVIS_VISION_OCR_LEVEL", "accurate").strip().lower()
        _level_key = (
            "VNRequestTextRecognitionLevelFast"
            if _level_name == "fast"
            else "VNRequestTextRecognitionLevelAccurate"
        )
        request.setRecognitionLevel_(mods[_level_key])
        request.setUsesLanguageCorrection_(False)

        handler = mods["VNImageRequestHandler"].alloc().initWithCGImage_options_(
            image, {},
        )
        # performRequests_error_ may return (bool, NSError) tuple in
        # some PyObjC versions and bool-only in others. Wrap so both
        # shapes are tolerated.
        try:
            handler.performRequests_error_([request], None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[OCRAdapter] performRequests raised: %s", exc)
            return ""

        results = request.results() or []
        lines = []
        for observation in results:
            candidates = observation.topCandidates_(1)
            if candidates and len(candidates) > 0:
                text = candidates[0].string()
                if text:
                    lines.append(str(text))
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[OCRAdapter] recognize_text failed on %s: %s", frame_path, exc)
        return ""
