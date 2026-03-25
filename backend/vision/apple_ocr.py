#!/usr/bin/env python3
"""
Apple Vision Framework OCR bridge for Python.

Uses macOS native VNRecognizeTextRequest via a Swift subprocess.
~50-100ms per image, 1.00 confidence on clean text, handles
glow/shadow/stylized text that Tesseract struggles with.

Usage:
    from backend.vision.apple_ocr import apple_ocr_read_async
    lines = await apple_ocr_read_async("/path/to/image.png")
    # Returns: [{"text": "Horizontal Bounces: 33", "confidence": 1.0}, ...]
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_SWIFT_OCR = r'''
import Vision
import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count > 1 else { print("[]"); exit(0) }
guard let img = NSImage(contentsOfFile: args[1]),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil)
else { print("[]"); exit(0) }

let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])

var out: [[String: Any]] = []
for obs in req.results ?? [] {
    if let c = obs.topCandidates(1).first {
        out.append(["text": c.string, "confidence": c.confidence])
    }
}
if let d = try? JSONSerialization.data(withJSONObject: out),
   let s = String(data: d, encoding: .utf8) { print(s) }
else { print("[]") }
'''

_SCRIPT_PATH: Optional[str] = None


def _ensure_script() -> str:
    global _SCRIPT_PATH
    if _SCRIPT_PATH and os.path.exists(_SCRIPT_PATH):
        return _SCRIPT_PATH
    fd, path = tempfile.mkstemp(suffix=".swift", prefix="jarvis_ocr_")
    with os.fdopen(fd, "w") as f:
        f.write(_SWIFT_OCR)
    _SCRIPT_PATH = path
    return path


async def apple_ocr_read_async(
    image_path: str,
    min_confidence: float = 0.5,
    timeout_s: float = 5.0,
) -> List[Dict]:
    """Run Apple Vision OCR on an image. Returns [{text, confidence}, ...].

    Ouroboros fast-path: if VisionReflexCompiler has a pre-compiled
    Swift binary, uses that (~50ms) instead of interpreting (~2000ms).
    """
    binary = _get_compiled_binary()
    cmd = [binary, image_path] if binary else ["swift", _ensure_script(), image_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        raw = stdout.decode("utf-8", errors="replace").strip()
        if not raw or raw == "[]":
            return []
        return [
            r for r in json.loads(raw)
            if r.get("confidence", 0) >= min_confidence
        ]
    except (asyncio.TimeoutError, Exception) as exc:
        logger.debug("[AppleOCR] %s", exc)
        return []


def _get_compiled_binary() -> Optional[str]:
    """Check if VisionReflexCompiler has a pre-compiled binary available."""
    try:
        from backend.vision.vision_reflex import VisionReflexCompiler
        compiler = VisionReflexCompiler.get_instance()
        binary = compiler._compiled_binary
        if binary and os.path.exists(binary):
            return binary
    except Exception:
        pass
    return None
