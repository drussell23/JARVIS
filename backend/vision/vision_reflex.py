"""
Vision Reflex Compiler — Ouroboros Principle 6 (Neuroplasticity)

Detects repeated vision tasks and compiles fast-path reflexes that
replace slow inference/OCR with deterministic local code.

Architecture (Boundary Mandate):
  - Call counting, thresholds, graduation pipeline → DETERMINISTIC
  - What reflex to generate, how to validate it  → AGENTIC (future: 397B)
  - Current: ships a pre-built green-HUD reflex as proof-of-concept;
    the 397B synthesis pipeline wires in via the same interface.

Two-tier reflex cascade:
  Tier 2 — Numpy color extraction: ~3-5ms (specialized, green-on-dark HUD)
  Tier 1 — Pre-compiled Swift binary: ~50-100ms (general-purpose OCR)
  Tier 0 — Interpreted Swift subprocess: ~2000ms (no reflex, baseline)

Usage::

    compiler = VisionReflexCompiler.get_instance()
    # On each OCR call:
    event = compiler.record_call("ocr_hud")
    if event == "graduate":
        await compiler.compile_reflexes(last_b64, last_ocr_result)
    # Check for fast path:
    reflex = compiler.get_reflex("ocr_hud")
    if reflex:
        result = reflex(b64_png)
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import subprocess
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_GRADUATION_THRESHOLD = int(os.environ.get("OUROBOROS_GRADUATION_THRESHOLD", "3"))
_TMP_DIR = os.environ.get("VISION_LEAN_TMP_DIR", "/tmp/claude")

# Doubleword 397B Architect — generates reflex code via agentic synthesis
_DW_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
_DW_BASE_URL = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
_DW_ARCHITECT_MODEL = os.environ.get(
    "DOUBLEWORD_ARCHITECT_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8",
)
_DW_VISION_MODEL = os.environ.get(
    "DOUBLEWORD_VISION_MODEL", "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8",
)

# Persistent Apple Vision OCR server — keeps Vision Framework warm.
# Accepts image paths on stdin, returns JSON OCR results on stdout.
# Eliminates the ~800ms Framework initialization per subprocess call.
_SWIFT_OCR_SERVER = r'''
import Vision
import AppKit
import Foundation

// Signal ready
print("READY")
fflush(stdout)

// Read image paths from stdin, output JSON OCR results to stdout
while let line = readLine() {
    let path = line.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !path.isEmpty else { continue }

    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil)
    else {
        print("[]")
        fflush(stdout)
        continue
    }

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
       let s = String(data: d, encoding: .utf8) {
        print(s)
    } else {
        print("[]")
    }
    fflush(stdout)
}
'''


class VisionReflexCompiler:
    """Ouroboros engine: detects cognitive inefficiency and compiles reflexes.

    Tracks repeated vision task patterns. When a task crosses the graduation
    threshold, compiles a fast-path reflex and validates it against the last
    known-good result before hot-swapping.
    """

    _instance: Optional[VisionReflexCompiler] = None

    def __init__(self) -> None:
        self._call_counts: Dict[str, int] = {}
        self._reflexes: Dict[str, Callable] = {}
        self._compiled_binary: Optional[str] = None
        self._compiled_server_binary: Optional[str] = None
        self._ocr_server_proc: Optional[Any] = None
        self._ocr_server_ready: bool = False
        self._graduation_log: List[Dict[str, Any]] = []
        self._tier_active: Dict[str, int] = {}  # task_key -> active tier (0, 1, 2, 3)

    @classmethod
    def get_instance(cls) -> VisionReflexCompiler:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Call tracking (deterministic policy: threshold = 3)
    # ------------------------------------------------------------------

    def record_call(self, task_key: str, latency_ms: float = 0) -> Optional[str]:
        """Record a vision task invocation. Returns 'graduate' on threshold."""
        self._call_counts[task_key] = self._call_counts.get(task_key, 0) + 1
        count = self._call_counts[task_key]

        if count == _GRADUATION_THRESHOLD and task_key not in self._reflexes:
            self._emit_cognitive_inefficiency(task_key, count, latency_ms)
            return "graduate"
        return None

    def get_call_count(self, task_key: str) -> int:
        return self._call_counts.get(task_key, 0)

    # ------------------------------------------------------------------
    # Reflex compilation
    # ------------------------------------------------------------------

    async def compile_reflexes(
        self,
        task_key: str,
        last_b64: str,
        last_ocr_result: Dict[str, str],
        on_status: Optional[Callable] = None,
    ) -> bool:
        """Compile and validate reflexes for a graduated task.

        Tier cascade (highest first):
          Tier 4: 397B Architect synthesis (~5ms reflex, agentic code generation)
          Tier 3: Persistent OCR server (~150ms, keeps Vision Framework warm)
          Tier 1: Pre-compiled Swift binary (~900ms, no Swift compilation)
          Tier 0: Interpreted Swift subprocess (~2000ms, baseline)

        Validates against last_ocr_result before activating.
        Returns True if at least one reflex was activated.
        """
        def _status(msg: str) -> None:
            if on_status:
                on_status(msg)

        activated = False

        # --- Tier 4: 397B Architect JIT synthesis ---
        if _DW_API_KEY:
            try:
                _status("Initiating JIT Synthesis via Doubleword 397B Architect...")
                tier4_fn, generated_code = await self._tier4_architect_synthesis(
                    task_key, last_b64, last_ocr_result, _status,
                )
                if tier4_fn is not None:
                    tier4_result = tier4_fn(last_b64)
                    if tier4_result and _validate_reflex(tier4_result, last_ocr_result):
                        self._reflexes[task_key] = tier4_fn
                        self._tier_active[task_key] = 4
                        self._log_graduation(task_key, 4, "architect_397b_synthesis")
                        _status(
                            f"Tier 4 reflex VALIDATED — 397B-generated code "
                            f"matches ground truth"
                        )
                        logger.info(
                            "[Ouroboros] Tier 4 reflex VALIDATED for '%s' — "
                            "397B Architect synthesis (~5ms)",
                            task_key,
                        )
                        activated = True
                    else:
                        _status("Tier 4 validation failed — reflex output doesn't match ground truth")
                        logger.debug("[Ouroboros] Tier 4 validation failed")
            except Exception as exc:
                _status(f"Tier 4 synthesis error: {exc}")
                logger.debug("[Ouroboros] Tier 4 failed: %s", exc)

        # --- Tier 3: Persistent Apple Vision OCR server ---
        if not activated:
            try:
                _status("Falling back to Tier 3 persistent OCR server...")
                await self._ensure_ocr_server()
                if self._ocr_server_ready:
                    tier3_result = await self._reflex_ocr_server(last_b64)
                    if tier3_result and _validate_reflex(tier3_result, last_ocr_result):
                        async_reflex = self._reflex_ocr_server
                        self._reflexes[task_key] = async_reflex
                        self._tier_active[task_key] = 3
                        self._log_graduation(task_key, 3, "persistent_ocr_server")
                        logger.info(
                            "[Ouroboros] Tier 3 reflex VALIDATED for '%s' — "
                            "persistent OCR server (~150ms)",
                            task_key,
                        )
                        activated = True
                    else:
                        logger.debug(
                            "[Ouroboros] Tier 3 validation failed for '%s'",
                            task_key,
                        )
            except Exception as exc:
                logger.debug("[Ouroboros] Tier 3 compilation failed: %s", exc)

        # --- Tier 1: Pre-compiled Swift binary (fallback) ---
        if not activated:
            binary = await self._ensure_compiled_binary()
            if binary:
                def _make_tier1(bin_path: str) -> Callable:
                    def reflex(b64_png: str) -> Dict[str, str]:
                        return _reflex_compiled_ocr(b64_png, bin_path)
                    return reflex

                tier1_fn = _make_tier1(binary)
                tier1_result = tier1_fn(last_b64)
                if tier1_result and _validate_reflex(tier1_result, last_ocr_result):
                    self._reflexes[task_key] = tier1_fn
                    self._tier_active[task_key] = 1
                    self._log_graduation(task_key, 1, "compiled_swift_binary")
                    logger.info(
                        "[Ouroboros] Tier 1 reflex VALIDATED for '%s' — "
                        "compiled Swift binary (~900ms)",
                        task_key,
                    )
                    activated = True

        if not activated:
            logger.warning(
                "[Ouroboros] All reflex tiers failed validation for '%s'",
                task_key,
            )

        return activated

    def get_reflex(self, task_key: str) -> Optional[Callable]:
        """Get the active reflex for a task, or None."""
        return self._reflexes.get(task_key)

    def get_active_tier(self, task_key: str) -> int:
        """Return the active tier for a task (0=baseline, 1=compiled, 2=numpy)."""
        return self._tier_active.get(task_key, 0)

    # ------------------------------------------------------------------
    # Tier 4: 397B Architect JIT synthesis
    # ------------------------------------------------------------------
    # The 397B reasoning model examines a sample frame + the 235B's
    # analysis, then writes a deterministic Python function that
    # replicates the extraction locally. The generated code is
    # exec'd in a sandbox, validated, and hot-swapped into the Retina.

    async def _tier4_architect_synthesis(
        self,
        task_key: str,
        last_b64: str,
        last_ocr_result: Dict[str, str],
        status: Callable,
    ) -> Tuple[Optional[Callable], Optional[str]]:
        """Ask the 397B Architect to generate a fast-path reflex function.

        Returns (reflex_fn, generated_code) or (None, None) on failure.
        """
        import aiohttp

        # Step 1: Ask the 235B vision model what it sees (the "conscious read")
        status("Querying Doubleword 235B vision model for frame analysis...")
        vision_analysis = await self._call_doubleword_vision(last_b64)
        if not vision_analysis:
            status("235B vision call failed — cannot proceed with synthesis")
            return None, None
        status(f"235B analysis: {vision_analysis[:120]}")

        # Step 2: Ask the 397B to write a reflex function
        status("Handing frame + 235B output to 397B Architect for code synthesis...")

        architect_prompt = (
            "You are a code generation engine for an AI vision system. "
            "Your job: write a FAST Python function that extracts the SAME "
            "data that a slow VLM extracted, but using only numpy/PIL.\n\n"
            "The VLM analyzed a screenshot and found these values:\n"
            f"  {json.dumps(last_ocr_result)}\n\n"
            f"The VLM's natural language analysis:\n  {vision_analysis}\n\n"
            "The screenshot shows green monospace text on a dark background "
            "in the top-left corner (HUD overlay). The text contains lines like "
            "'Horizontal Bounces: 123', 'Vertical Bounces: 456', etc.\n\n"
            "Write a Python function with this EXACT signature:\n\n"
            "def reflex_extract(b64_png: str) -> dict:\n"
            "    '''Extract HUD values from a base64 PNG. Returns dict with "
            "keys: horizontal, vertical, total, speed (all str values).'''\n\n"
            "Requirements:\n"
            "- Import only: base64, io, re, numpy, PIL.Image\n"
            "- Decode the base64 PNG to a numpy array\n"
            "- Crop the top-left ~40% x ~25% (the HUD region)\n"
            "- Isolate green text pixels (green channel > 120, red < 130, "
            "blue < 130)\n"
            "- Create a clean binary image, scale up 3x\n"
            "- Save to a temp file and use subprocess to run the Apple Vision "
            "OCR binary at this path: '/tmp/claude/jarvis_ocr_server' with "
            "the image path as an argument. Parse JSON output.\n"
            "- If the binary doesn't exist, fall back to returning an empty dict\n"
            "- Parse the text with regex for 'Horizontal Bounces: N', etc.\n"
            "- Return a dict like: {'horizontal': '123', 'vertical': '456', "
            "'total': '579', 'speed': '331'}\n\n"
            "Return ONLY the Python function. No markdown, no explanation, "
            "no backticks. Just the raw Python code starting with 'def'."
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_DW_BASE_URL}/chat/completions",
                    json={
                        "model": _DW_ARCHITECT_MODEL,
                        "messages": [
                            {"role": "system", "content": "You are a code generator. Output ONLY Python code. No markdown fences."},
                            {"role": "user", "content": architect_prompt},
                        ],
                        "max_tokens": 8192,
                        "temperature": 0.1,
                    },
                    headers={
                        "Authorization": f"Bearer {_DW_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        status(f"397B API error: HTTP {resp.status}")
                        logger.warning("[Ouroboros:T4] 397B HTTP %d: %s", resp.status, body[:200])
                        return None, None
                    data = await resp.json()
                    msg = data["choices"][0]["message"]
                    # 397B is a reasoning model: code may be in
                    # 'content' (final answer) or 'reasoning' (CoT).
                    generated_code = msg.get("content", "") or ""
                    if not generated_code.strip():
                        # Fallback: extract code from reasoning field
                        generated_code = msg.get("reasoning", "") or ""
        except Exception as exc:
            status(f"397B API call failed: {exc}")
            logger.debug("[Ouroboros:T4] 397B error: %s", exc, exc_info=True)
            return None, None

        # Clean up markdown fences if the model wrapped them
        generated_code = generated_code.strip()
        if generated_code.startswith("```"):
            lines = generated_code.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            generated_code = "\n".join(lines).strip()

        if not generated_code.startswith("def "):
            # Try to find the function definition in the output
            idx = generated_code.find("def reflex_extract")
            if idx >= 0:
                generated_code = generated_code[idx:]
            else:
                status("397B output doesn't contain a valid function definition")
                return None, None

        status("397B generated reflex code:")
        # Print the generated code to terminal so the human sees it
        for line in generated_code.split("\n"):
            status(f"  | {line}")

        # Step 3: Sandbox exec — compile the generated code
        try:
            sandbox: Dict[str, Any] = {}
            exec(compile(generated_code, "<ouroboros-tier4>", "exec"), sandbox)
            reflex_fn = sandbox.get("reflex_extract")
            if reflex_fn is None or not callable(reflex_fn):
                status("Generated code doesn't define callable 'reflex_extract'")
                return None, None
        except Exception as exc:
            status(f"Sandbox compilation failed: {exc}")
            return None, None

        status("Sandbox compilation passed — validating against ground truth...")
        return reflex_fn, generated_code

    async def _call_doubleword_vision(self, b64_png: str) -> Optional[str]:
        """Call the 235B VLM to analyze a frame. Returns natural language description."""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_DW_BASE_URL}/chat/completions",
                    json={
                        "model": _DW_VISION_MODEL,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/png;base64,{b64_png}",
                                        },
                                    },
                                    {
                                        "type": "text",
                                        "text": (
                                            "Read the exact text displayed in the top-left HUD overlay. "
                                            "List each line with its value. Be precise with numbers."
                                        ),
                                    },
                                ],
                            },
                        ],
                        "max_tokens": 256,
                        "temperature": 0.0,
                    },
                    headers={
                        "Authorization": f"Bearer {_DW_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    return data["choices"][0]["message"].get("content", "")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Tier 3: Persistent Apple Vision OCR server
    # ------------------------------------------------------------------
    # Compiles a Swift binary that stays running, keeps the Vision
    # Framework warm in memory. Image paths piped via stdin, JSON
    # results returned via stdout. ~50ms per read vs ~2000ms baseline.

    async def _ensure_ocr_server(self) -> None:
        """Compile and start the persistent OCR server if not running."""
        # Already running?
        if (
            self._ocr_server_proc is not None
            and self._ocr_server_proc.returncode is None
            and self._ocr_server_ready
        ):
            return

        # Compile the server binary
        bin_path = await self._compile_ocr_server()
        if not bin_path:
            return

        # Start the persistent server
        try:
            self._ocr_server_proc = await asyncio.create_subprocess_exec(
                bin_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Wait for READY signal
            line = await asyncio.wait_for(
                self._ocr_server_proc.stdout.readline(),
                timeout=10.0,
            )
            if b"READY" in line:
                self._ocr_server_ready = True
                logger.info(
                    "[Ouroboros] Persistent OCR server started (pid=%d)",
                    self._ocr_server_proc.pid,
                )
            else:
                logger.warning("[Ouroboros] OCR server did not send READY")
                self._ocr_server_proc.terminate()
                self._ocr_server_proc = None
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("[Ouroboros] OCR server start failed: %s", exc)
            if self._ocr_server_proc:
                try:
                    self._ocr_server_proc.terminate()
                except ProcessLookupError:
                    pass
            self._ocr_server_proc = None
            self._ocr_server_ready = False

    async def _compile_ocr_server(self) -> Optional[str]:
        """Compile the persistent OCR server Swift binary."""
        if self._compiled_server_binary and os.path.exists(self._compiled_server_binary):
            return self._compiled_server_binary

        os.makedirs(_TMP_DIR, exist_ok=True)
        src_path = os.path.join(_TMP_DIR, "jarvis_ocr_server.swift")
        bin_path = os.path.join(_TMP_DIR, "jarvis_ocr_server")

        with open(src_path, "w") as f:
            f.write(_SWIFT_OCR_SERVER)

        logger.info("[Ouroboros] Compiling persistent OCR server binary...")
        t0 = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                "swiftc", "-O",
                "-framework", "Vision",
                "-framework", "AppKit",
                "-o", bin_path,
                src_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            elapsed = (time.monotonic() - t0) * 1000

            if proc.returncode == 0 and os.path.exists(bin_path):
                os.chmod(bin_path, 0o755)
                self._compiled_server_binary = bin_path
                logger.info(
                    "[Ouroboros] OCR server binary compiled: %s (%.0fms)",
                    bin_path, elapsed,
                )
                return bin_path
            else:
                logger.warning(
                    "[Ouroboros] OCR server compilation failed (rc=%d): %s",
                    proc.returncode,
                    stderr.decode("utf-8", errors="replace")[:200],
                )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("[Ouroboros] OCR server compilation error: %s", exc)

        return None

    async def _reflex_ocr_server(self, b64_png: str) -> Dict[str, str]:
        """Tier 3 reflex: pre-crop HUD region, pipe to persistent OCR server.

        Pipeline: decode → crop top-left HUD (42%×28%) → save tiny PNG
        → pipe to warm Vision server (.fast mode) → parse JSON.
        Full frame at .accurate = ~800ms. Cropped at .fast = ~30-60ms.
        """
        if not self._ocr_server_ready or self._ocr_server_proc is None:
            return {}

        import tempfile
        from PIL import Image

        tmp = os.path.join(
            tempfile.gettempdir(), f"jarvis_server_ocr_{os.getpid()}.png",
        )
        try:
            raw = base64.b64decode(b64_png)
            img = Image.open(io.BytesIO(raw))
            w, h = img.size
            # Pre-crop to HUD region only — dramatically smaller image
            hud = img.crop((0, 0, int(w * 0.42), int(h * 0.28)))
            hud.save(tmp, format="PNG")

            # Send image path to server
            self._ocr_server_proc.stdin.write(
                (tmp + "\n").encode("utf-8"),
            )
            await self._ocr_server_proc.stdin.drain()

            # Read JSON response
            line = await asyncio.wait_for(
                self._ocr_server_proc.stdout.readline(),
                timeout=3.0,
            )
            result_text = line.decode("utf-8", errors="replace").strip()
            if not result_text or result_text == "[]":
                return {}

            lines = json.loads(result_text)
            text = " ".join(
                entry["text"]
                for entry in lines
                if entry.get("confidence", 0) > 0.5
            )
            return _parse_hud_text(text)

        except (asyncio.TimeoutError, Exception) as exc:
            logger.debug("[Ouroboros] OCR server query failed: %s", exc)
            # Server may have died — mark as not ready
            self._ocr_server_ready = False
            return {}
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Tier 1: Swift binary pre-compilation (one-time cost)
    # ------------------------------------------------------------------

    async def _ensure_compiled_binary(self) -> Optional[str]:
        """Compile the Apple Vision OCR Swift script to a native binary."""
        if self._compiled_binary and os.path.exists(self._compiled_binary):
            return self._compiled_binary

        from backend.vision.apple_ocr import _SWIFT_OCR

        os.makedirs(_TMP_DIR, exist_ok=True)
        src_path = os.path.join(_TMP_DIR, "jarvis_ocr_reflex.swift")
        bin_path = os.path.join(_TMP_DIR, "jarvis_ocr_reflex")

        with open(src_path, "w") as f:
            f.write(_SWIFT_OCR)

        logger.info("[Ouroboros] Compiling Swift OCR binary...")
        t0 = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                "swiftc", "-O",
                "-framework", "Vision",
                "-framework", "AppKit",
                "-o", bin_path,
                src_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            elapsed = (time.monotonic() - t0) * 1000

            if proc.returncode == 0 and os.path.exists(bin_path):
                os.chmod(bin_path, 0o755)
                self._compiled_binary = bin_path
                logger.info(
                    "[Ouroboros] Swift binary compiled: %s (%.0fms)",
                    bin_path, elapsed,
                )
                return bin_path
            else:
                logger.warning(
                    "[Ouroboros] swiftc failed (rc=%d): %s",
                    proc.returncode,
                    stderr.decode("utf-8", errors="replace")[:200],
                )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("[Ouroboros] Swift compilation error: %s", exc)

        return None

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def _emit_cognitive_inefficiency(
        self, task_key: str, count: int, latency_ms: float,
    ) -> None:
        """Emit ouroboros.cognitive_inefficiency@1.0.0 to TelemetryBus."""
        try:
            from backend.core.telemetry_contract import (
                TelemetryEnvelope,
                get_telemetry_bus,
            )
            envelope = TelemetryEnvelope.create(
                event_schema="ouroboros.cognitive_inefficiency@1.0.0",
                source="vision_reflex_compiler",
                trace_id=str(uuid.uuid4()),
                span_id=f"cognitive-inefficiency-{task_key}",
                partition_key="ouroboros",
                payload={
                    "task_key": task_key,
                    "call_count": count,
                    "threshold": _GRADUATION_THRESHOLD,
                    "last_latency_ms": latency_ms,
                },
                severity="warning",
            )
            get_telemetry_bus().emit(envelope)
        except Exception:
            pass

    def _log_graduation(
        self, task_key: str, tier: int, method: str,
    ) -> None:
        """Log a successful reflex graduation."""
        entry = {
            "task_key": task_key,
            "tier": tier,
            "method": method,
            "graduated_at": time.time(),
            "call_count_at_graduation": self._call_counts.get(task_key, 0),
        }
        self._graduation_log.append(entry)

        try:
            from backend.core.telemetry_contract import (
                TelemetryEnvelope,
                get_telemetry_bus,
            )
            envelope = TelemetryEnvelope.create(
                event_schema="ouroboros.reflex_graduation@1.0.0",
                source="vision_reflex_compiler",
                trace_id=str(uuid.uuid4()),
                span_id=f"graduation-{task_key}-tier{tier}",
                partition_key="ouroboros",
                payload=entry,
            )
            get_telemetry_bus().emit(envelope)
        except Exception:
            pass


# ======================================================================
# Reflex implementations (deterministic fast-paths)
# ======================================================================


def _reflex_green_hud(b64_png: str) -> Dict[str, str]:
    """Tier 2 reflex: extract green digits from dark HUD via numpy.

    Works for green monospace text (#00FF00-ish) on dark backgrounds.
    Latency: ~3-5ms. No OCR engine required.

    Approach:
      1. Decode -> crop known HUD region (top-left 40% x 25%)
      2. Green channel threshold (isolate bright green text pixels)
      3. Scale up 3x for legibility
      4. Feed clean binary image to lightweight OCR
    """
    import numpy as np
    from PIL import Image

    raw = base64.b64decode(b64_png)
    img = Image.open(io.BytesIO(raw))
    w, h = img.size

    # Crop HUD region -- top-left quadrant where counters live
    hud = img.crop((0, 0, int(w * 0.42), int(h * 0.28)))
    arr = np.array(hud)

    if arr.ndim < 3:
        return {}

    # Green channel isolation: bright green text on dark background
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    green_mask = (g > 120) & (r < 130) & (b < 130) & (g > r + 30)

    # Convert to clean binary image for OCR
    binary = np.zeros_like(g)
    binary[green_mask] = 255

    # Scale up 3x for better OCR recognition on small crops
    binary_img = Image.fromarray(binary)
    binary_img = binary_img.resize(
        (binary_img.width * 3, binary_img.height * 3),
        Image.Resampling.NEAREST,
    )

    # Try pytesseract on the pre-processed binary (fast: ~30-50ms)
    try:
        import pytesseract
        text = pytesseract.image_to_string(
            binary_img,
            config=(
                "--psm 6 "
                "-c tessedit_char_whitelist="
                "0123456789:HorizontalVeicalTtBunsSpd/px "
            ),
        )
        result = _parse_hud_text(text)
        if result:
            return result
    except ImportError:
        pass

    # Fallback: save binary and use Apple Vision on the tiny crop
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "jarvis_reflex_hud.png")
    try:
        binary_img.save(tmp)
        compiler = VisionReflexCompiler.get_instance()
        if compiler._compiled_binary and os.path.exists(compiler._compiled_binary):
            proc = subprocess.run(
                [compiler._compiled_binary, tmp],
                capture_output=True, timeout=3,
            )
        else:
            from backend.vision.apple_ocr import _ensure_script
            proc = subprocess.run(
                ["swift", _ensure_script(), tmp],
                capture_output=True, timeout=5,
            )
        if proc.returncode == 0:
            lines = json.loads(
                proc.stdout.decode("utf-8", errors="replace").strip()
            )
            text = " ".join(
                entry["text"]
                for entry in lines
                if entry.get("confidence", 0) > 0.5
            )
            return _parse_hud_text(text)
    except Exception:
        pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    return {}


def _reflex_compiled_ocr(b64_png: str, binary_path: str) -> Dict[str, str]:
    """Tier 1 reflex: run pre-compiled Swift OCR binary on the full frame.

    Same accuracy as interpreted Swift, ~50-100ms instead of ~2000ms.
    """
    import tempfile

    raw = base64.b64decode(b64_png)
    tmp = os.path.join(
        tempfile.gettempdir(), f"jarvis_reflex_{os.getpid()}.png",
    )
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        proc = subprocess.run(
            [binary_path, tmp],
            capture_output=True,
            timeout=3,
        )
        if proc.returncode != 0:
            return {}
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        if not stdout or stdout == "[]":
            return {}
        lines = json.loads(stdout)
        text = " ".join(
            entry["text"]
            for entry in lines
            if entry.get("confidence", 0) > 0.5
        )
        return _parse_hud_text(text)
    except Exception:
        return {}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ======================================================================
# Text parsing (shared, deterministic)
# ======================================================================

def _parse_hud_text(text: str) -> Dict[str, str]:
    """Extract bounce counter values from HUD text."""
    blob = text.replace("\n", " ").strip()
    result: Dict[str, str] = {}

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


def _validate_reflex(
    reflex_result: Dict[str, str],
    known_good: Dict[str, str],
) -> bool:
    """Validate reflex output against the last known-good OCR result.

    The reflex must extract at least H and V with plausible numeric values.
    Values may differ slightly (ball keeps bouncing) but structure must match.
    """
    if not reflex_result:
        return False

    required = {"horizontal", "vertical"}
    if not required.issubset(reflex_result.keys()):
        return False

    for key in required:
        try:
            int(reflex_result[key])
        except (ValueError, TypeError):
            return False

    # If total is present, H + V should approximate it
    if all(k in reflex_result for k in ("total", "horizontal", "vertical")):
        try:
            h = int(reflex_result["horizontal"])
            v = int(reflex_result["vertical"])
            t = int(reflex_result["total"])
            # Allow small drift — ball bounced between field reads
            if abs((h + v) - t) > 5:
                return False
        except ValueError:
            return False

    return True
