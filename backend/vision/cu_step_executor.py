"""
CU Step Executor -- 3-Layer Cascade for JARVIS Computer Use

Runs a single CUStep through a cascading resolution pipeline:
  Layer 1: Accessibility API   (<5ms, deterministic, handles ~80% of steps)
  Layer 2: Doubleword 235B VL  (~2-3s, visual grounding via Qwen3-VL)
  Layer 3: Claude Vision        (~5-15s, deep reasoning fallback)

Design principles (Symbiotic Manifesto):
  - No hardcoding: all thresholds, timeouts, models from env vars
  - Graceful degradation: any layer can fail without crashing
  - try/except around every external import (works with partial deps)
  - Async throughout for event-loop safety

Env vars:
  DOUBLEWORD_API_KEY            -- enables Layer 2
  DOUBLEWORD_VISION_MODEL       -- VL model slug (default Qwen3-VL-235B)
  DOUBLEWORD_BASE_URL           -- API base (default https://api.doubleword.ai/v1)
  ANTHROPIC_API_KEY             -- enables Layer 3
  JARVIS_CU_DW_TIMEOUT_S       -- Doubleword request timeout (default 10)
  JARVIS_CU_VISION_MODEL       -- Claude model for vision (default claude-3-5-sonnet-20241022)
  JARVIS_CU_VERIFY_DELAY_S     -- post-action verification delay (default 0.3)
  JARVIS_CU_JPEG_QUALITY       -- JPEG quality for frame encoding (default 80)
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env-driven config (Manifesto: no hardcoding)
# ---------------------------------------------------------------------------

def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# CUStep import (Task 1 may not be committed yet -- soft import)
# ---------------------------------------------------------------------------

try:
    from backend.vision.cu_task_planner import CUStep
except ImportError:
    # Forward reference -- callers or tests provide their own CUStep
    CUStep = None  # type: ignore[misc,assignment]

# ---------------------------------------------------------------------------
# Optional dependency imports (graceful degradation)
# ---------------------------------------------------------------------------

def _get_ax_resolver() -> Any:
    """Try to get the accessibility resolver singleton. Returns None on failure."""
    try:
        from backend.neural_mesh.agents.accessibility_resolver import (
            get_accessibility_resolver,
        )
        return get_accessibility_resolver()
    except Exception as exc:
        logger.debug("[CUExec] Accessibility resolver unavailable: %s", exc)
        return None


def _get_shm_reader() -> Any:
    """Try to create a SHM frame reader. Returns None on failure."""
    try:
        from backend.vision.shm_frame_reader import ShmFrameReader
        reader = ShmFrameReader()
        if reader.open():
            return reader
        logger.debug("[CUExec] SHM reader failed to open")
        return None
    except Exception as exc:
        logger.debug("[CUExec] SHM reader unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Action execution (isolated so it's patchable in tests)
# ---------------------------------------------------------------------------

def _execute_action_impl(
    action: str,
    coords: Optional[Tuple[int, int]],
    value: Optional[str],
) -> None:
    """Execute a single UI action via pyautogui / clipboard.

    Raises on failure -- caller wraps in try/except.
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
    except ImportError:
        raise RuntimeError("pyautogui not installed -- cannot execute UI actions")

    if action == "click":
        if coords:
            pyautogui.click(x=coords[0], y=coords[1])
        else:
            pyautogui.click()

    elif action == "type":
        if value:
            # Use clipboard for reliability with special chars / unicode
            _clipboard_type(value)
        else:
            logger.warning("[CUExec] type action with no value")

    elif action == "key":
        if value:
            pyautogui.press(value.lower())

    elif action == "hotkey":
        if value:
            keys = [k.strip().lower() for k in value.replace("+", ",").split(",")]
            # Map common names
            mapped = []
            for k in keys:
                if k in ("cmd", "command"):
                    mapped.append("command")
                elif k in ("ctrl", "control"):
                    mapped.append("ctrl")
                elif k in ("alt", "option"):
                    mapped.append("option")
                elif k in ("shift",):
                    mapped.append("shift")
                else:
                    mapped.append(k)
            pyautogui.hotkey(*mapped)

    elif action == "scroll":
        clicks = int(value) if value else -3
        if coords:
            pyautogui.scroll(clicks, x=coords[0], y=coords[1])
        else:
            pyautogui.scroll(clicks)

    else:
        logger.warning("[CUExec] Unknown action: %s", action)


def _clipboard_type(text: str) -> None:
    """Type text via clipboard (pbcopy + cmd+v) for reliability."""
    try:
        import pyautogui
        proc = subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=5,
        )
        if proc.returncode != 0:
            # Fallback: direct typing
            pyautogui.typewrite(text, interval=0.02)
            return
        time.sleep(0.05)
        pyautogui.hotkey("command", "v")
    except Exception:
        # Final fallback
        try:
            import pyautogui
            pyautogui.typewrite(text, interval=0.02)
        except Exception as exc:
            raise RuntimeError(f"Cannot type text: {exc}") from exc


# ---------------------------------------------------------------------------
# StepResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """Result of executing a single CU step."""
    success: bool
    layer_used: str  # "accessibility" | "doubleword" | "claude" | "direct" | "none"
    step_index: int
    coords: Optional[Tuple[int, int]] = None
    confidence: float = 0.0
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    verified: bool = False


# ---------------------------------------------------------------------------
# Vision prompt template
# ---------------------------------------------------------------------------

_VISION_PROMPT = (
    "You are a UI element locator. Given the screenshot, find the element "
    "described below and return its center coordinates as JSON.\n\n"
    "Target element: {target}\n"
    "Context: {description}\n\n"
    "Respond with ONLY a JSON object: "
    '{{\"x\": <int>, \"y\": <int>, \"confidence\": <0.0-1.0>, \"element\": \"<description>\"}}\n'
    "If you cannot find the element, respond with: "
    '{{\"x\": 0, \"y\": 0, \"confidence\": 0.0, \"element\": \"not_found\"}}'
)


# ---------------------------------------------------------------------------
# CUStepExecutor
# ---------------------------------------------------------------------------

class CUStepExecutor:
    """Executes a single CUStep via 3-layer cascade.

    Layer 1: Accessibility API  -- <5ms, deterministic, ~80% hit rate
    Layer 2: Doubleword VL      -- ~2-3s, visual grounding
    Layer 3: Claude Vision      -- ~5-15s, deep reasoning fallback
    """

    def __init__(self) -> None:
        # Accessibility permissions check — cache at construction so we only
        # pay the ctypes cost once and can warn loudly if not trusted.
        self._ax_trusted: bool = self._check_ax_trusted()
        if not self._ax_trusted:
            logger.warning(
                "[CUExec] *** macOS Accessibility NOT GRANTED ***\n"
                "  pyautogui clicks will be SILENTLY DROPPED.\n"
                "  System Settings → Privacy & Security → Accessibility\n"
                "  Add: /opt/homebrew/bin/python3.12  AND  Xcode"
            )

        # Layer 1: Accessibility
        self._ax_resolver: Any = _get_ax_resolver()

        # Layer 2: Doubleword
        self._dw_api_key: str = _env_str("DOUBLEWORD_API_KEY", "")
        self._dw_base_url: str = _env_str(
            "DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1",
        )
        self._dw_model: str = _env_str(
            "DOUBLEWORD_VISION_MODEL",
            "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8",
        )
        self._dw_timeout: float = _env_float("JARVIS_CU_DW_TIMEOUT_S", 10.0)

        # Layer 3: Claude
        self._anthropic_key: str = _env_str("ANTHROPIC_API_KEY", "")
        self._claude_model: str = _env_str(
            "JARVIS_CU_VISION_MODEL",
            os.environ.get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022"),
        )

        # SHM frame reader
        self._shm_reader: Any = _get_shm_reader()

        # Verification / encoding config
        self._verify_delay: float = _env_float("JARVIS_CU_VERIFY_DELAY_S", 0.3)
        self._jpeg_quality: int = _env_int("JARVIS_CU_JPEG_QUALITY", 80)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_live_frame(self) -> Optional[np.ndarray]:
        """Read latest frame from SHM. Converts BGRA->RGB if needed."""
        if self._shm_reader is None:
            return None
        try:
            frame, _ = self._shm_reader.read_latest()
            if frame is None:
                return None
            # Convert BGRA (4 channels) -> RGB (3 channels)
            if frame.ndim == 3 and frame.shape[2] == 4:
                frame = frame[:, :, [2, 1, 0]]  # BGRA -> RGB (drop alpha)
            return frame
        except Exception as exc:
            logger.warning("[CUExec] SHM read failed: %s", exc)
            return None

    async def execute_step(
        self,
        step: Any,  # CUStep
        frame: Optional[np.ndarray] = None,
        step_index: int = 0,
    ) -> StepResult:
        """Execute a single CU step through the cascade.

        Args:
            step: CUStep with action, target, value, description, app_name.
            frame: Current screen frame (RGB np.ndarray). If None, tries SHM.
            step_index: Index of this step in the task plan.

        Returns:
            StepResult with execution outcome.
        """
        t0 = time.monotonic()
        action = getattr(step, "action", "").lower()
        target = getattr(step, "target", None) or ""
        value = getattr(step, "value", None) or ""
        description = getattr(step, "description", "") or ""
        app_name = getattr(step, "app_name", "") or ""

        # ------ Wait action ------
        if action == "wait":
            return await self._handle_wait(step, step_index, t0)

        # ------ Direct actions (no target needed) ------
        if not target and action in ("type", "key", "hotkey"):
            return await self._handle_direct(step, step_index, t0)

        # ------ Scroll without target ------
        if not target and action == "scroll":
            return await self._handle_direct(step, step_index, t0)

        # ------ Target-based actions: run cascade ------
        if frame is None:
            frame = self.get_live_frame()

        coords: Optional[Tuple[int, int]] = None
        layer_used = "none"
        confidence = 0.0

        # Layer 1: Accessibility
        try:
            coords, confidence = await self._resolve_accessibility(
                target, app_name,
            )
            if coords is not None:
                layer_used = "accessibility"
        except Exception as exc:
            logger.warning("[CUExec] Layer 1 (accessibility) error: %s", exc)

        # Layer 2: Doubleword VL
        if coords is None and self._dw_api_key and frame is not None:
            try:
                dw_result = await self._ask_doubleword_vision(
                    description, target, frame,
                )
                if dw_result and dw_result.get("confidence", 0) > 0.1:
                    coords = (int(dw_result["x"]), int(dw_result["y"]))
                    confidence = float(dw_result.get("confidence", 0.5))
                    layer_used = "doubleword"
            except Exception as exc:
                logger.warning("[CUExec] Layer 2 (Doubleword) error: %s", exc)

        # Layer 3: Claude Vision
        if coords is None and self._anthropic_key and frame is not None:
            try:
                claude_result = await self._ask_claude_vision(
                    description, target, frame,
                )
                if claude_result and claude_result.get("confidence", 0) > 0.1:
                    coords = (int(claude_result["x"]), int(claude_result["y"]))
                    confidence = float(claude_result.get("confidence", 0.5))
                    layer_used = "claude"
            except Exception as exc:
                logger.warning("[CUExec] Layer 3 (Claude) error: %s", exc)

        # All layers failed
        if coords is None:
            elapsed = (time.monotonic() - t0) * 1000
            return StepResult(
                success=False,
                layer_used="none",
                step_index=step_index,
                confidence=0.0,
                elapsed_ms=elapsed,
                error="All vision layers failed to locate target",
            )

        # Execute the action at resolved coords
        if not self._ax_trusted:
            logger.warning(
                "[CUExec] Executing %s at %s WITHOUT Accessibility permissions — "
                "macOS may silently drop this event. Grant access in System Settings.",
                action, coords,
            )
        logger.info("[CUExec] Executing %s at coords=%s via layer=%s", action, coords, layer_used)
        try:
            await asyncio.to_thread(
                _execute_action_impl, action, coords, value or None,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            return StepResult(
                success=False,
                layer_used=layer_used,
                step_index=step_index,
                coords=coords,
                confidence=confidence,
                elapsed_ms=elapsed,
                error=str(exc),
            )

        # Verification — check if screen actually changed
        verified = False
        if frame is not None:
            try:
                await asyncio.sleep(self._verify_delay)
                post_frame = self.get_live_frame()
                if post_frame is not None:
                    verified = self._verify_frames_changed(frame, post_frame)
            except Exception as exc:
                logger.debug("[CUExec] Verification failed: %s", exc)

        if not verified and not self._ax_trusted:
            # Screen didn't change AND we don't have AX permissions → almost
            # certainly the event was silently dropped by macOS.
            elapsed = (time.monotonic() - t0) * 1000
            return StepResult(
                success=False,
                layer_used=layer_used,
                step_index=step_index,
                coords=coords,
                confidence=confidence,
                elapsed_ms=elapsed,
                error=(
                    "Screen unchanged after action — event likely dropped by macOS "
                    "(no Accessibility permissions). Grant access in System Settings → "
                    "Privacy & Security → Accessibility → add python3.12 + Xcode."
                ),
            )

        elapsed = (time.monotonic() - t0) * 1000
        return StepResult(
            success=True,
            layer_used=layer_used,
            step_index=step_index,
            coords=coords,
            confidence=confidence,
            elapsed_ms=elapsed,
            verified=verified,
        )

    # ------------------------------------------------------------------
    # AX trust check
    # ------------------------------------------------------------------

    @staticmethod
    def _check_ax_trusted() -> bool:
        """Return True if this process has macOS Accessibility permissions."""
        try:
            import ctypes
            ax = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            ax.AXIsProcessTrusted.restype = ctypes.c_bool
            return bool(ax.AXIsProcessTrusted())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Direct / wait handlers
    # ------------------------------------------------------------------

    async def _handle_direct(
        self, step: Any, step_index: int, t0: float,
    ) -> StepResult:
        """Handle actions that don't need target resolution."""
        action = getattr(step, "action", "").lower()
        value = getattr(step, "value", None) or ""
        try:
            await asyncio.to_thread(
                _execute_action_impl, action, None, value or None,
            )
            elapsed = (time.monotonic() - t0) * 1000
            return StepResult(
                success=True,
                layer_used="direct",
                step_index=step_index,
                confidence=1.0,
                elapsed_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            return StepResult(
                success=False,
                layer_used="direct",
                step_index=step_index,
                elapsed_ms=elapsed,
                error=str(exc),
            )

    async def _handle_wait(
        self, step: Any, step_index: int, t0: float,
    ) -> StepResult:
        """Handle wait actions."""
        value = getattr(step, "value", None) or ""
        target = getattr(step, "target", None) or ""
        app_name = getattr(step, "app_name", "") or ""

        if target and app_name:
            # Wait for a specific condition (e.g., app window to appear)
            found = await self._wait_for_condition(step, timeout_s=10)
            elapsed = (time.monotonic() - t0) * 1000
            return StepResult(
                success=found,
                layer_used="direct",
                step_index=step_index,
                elapsed_ms=elapsed,
                error=None if found else "Timed out waiting for condition",
            )
        else:
            # Simple sleep
            try:
                wait_s = float(value) if value else 1.0
            except (ValueError, TypeError):
                wait_s = 1.0
            await asyncio.sleep(wait_s)
            elapsed = (time.monotonic() - t0) * 1000
            return StepResult(
                success=True,
                layer_used="direct",
                step_index=step_index,
                confidence=1.0,
                elapsed_ms=elapsed,
            )

    # ------------------------------------------------------------------
    # Layer 1: Accessibility
    # ------------------------------------------------------------------

    async def _resolve_accessibility(
        self, target: str, app_name: str,
    ) -> Tuple[Optional[Tuple[int, int]], float]:
        """Try to resolve target via Accessibility API.

        Returns (coords, confidence) or (None, 0.0).
        """
        if self._ax_resolver is None:
            return None, 0.0

        result = await self._ax_resolver.resolve(
            target, app_name=app_name,
        )
        if result is None:
            return None, 0.0

        x = int(result.get("x", 0))
        y = int(result.get("y", 0))
        if x == 0 and y == 0:
            return None, 0.0

        # AX is deterministic -- high confidence
        return (x, y), 0.98

    # ------------------------------------------------------------------
    # Layer 2: Doubleword Vision (Qwen3-VL-235B)
    # ------------------------------------------------------------------

    async def _ask_doubleword_vision(
        self,
        description: str,
        target: str,
        frame: np.ndarray,
    ) -> Optional[Dict[str, Any]]:
        """Send frame + target to Doubleword VL API, parse JSON coords.

        Returns {"x": int, "y": int, "confidence": float, "element": str}
        or None on failure.
        """
        try:
            import aiohttp
        except ImportError:
            logger.warning("[CUExec] aiohttp not installed -- Doubleword unavailable")
            return None

        b64_image = self._frame_to_b64(frame)
        prompt = _VISION_PROMPT.format(target=target, description=description)

        payload = {
            "model": self._dw_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_image}",
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                }
            ],
            "max_tokens": 256,
            "temperature": 0.0,
        }

        url = f"{self._dw_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._dw_api_key}",
            "Content-Type": "application/json",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=self._dw_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            "[CUExec] Doubleword API %d: %s", resp.status, body[:200],
                        )
                        return None
                    data = await resp.json()
        except asyncio.TimeoutError:
            logger.warning("[CUExec] Doubleword timed out (%.1fs)", self._dw_timeout)
            return None
        except Exception as exc:
            logger.warning("[CUExec] Doubleword request failed: %s", exc)
            return None

        return self._parse_vision_response(data)

    # ------------------------------------------------------------------
    # Layer 3: Claude Vision
    # ------------------------------------------------------------------

    async def _ask_claude_vision(
        self,
        description: str,
        target: str,
        frame: np.ndarray,
    ) -> Optional[Dict[str, Any]]:
        """Send frame + target to Anthropic Claude Vision API.

        Returns {"x": int, "y": int, "confidence": float, "element": str}
        or None on failure.
        """
        try:
            import anthropic
        except ImportError:
            logger.warning("[CUExec] anthropic SDK not installed -- Claude unavailable")
            return None

        b64_image = self._frame_to_b64(frame)
        prompt = _VISION_PROMPT.format(target=target, description=description)

        try:
            client = anthropic.AsyncAnthropic(api_key=self._anthropic_key)
            response = await client.messages.create(
                model=self._claude_model,
                max_tokens=256,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64_image,
                                },
                            },
                            {
                                "type": "text",
                                "text": prompt,
                            },
                        ],
                    }
                ],
            )
        except Exception as exc:
            logger.warning("[CUExec] Claude Vision failed: %s", exc)
            return None

        # Extract text from response (filter to TextBlock only)
        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text += block.text  # type: ignore[union-attr]
        if not text:
            return None

        return self._parse_json_from_text(text)

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def _verify_frames_changed(
        self, pre: np.ndarray, post: np.ndarray,
    ) -> bool:
        """Compare pre/post frames to verify the action had visible effect.

        Returns True if mean absolute pixel diff > 1.0 (threshold from env
        could be added later).
        """
        if pre.shape != post.shape:
            return True  # Shape change = something happened
        diff = np.mean(np.abs(pre.astype(np.float32) - post.astype(np.float32)))
        return float(diff) > 1.0

    async def _verify_with_frame(
        self, _step: Any, pre_frame: np.ndarray,
    ) -> bool:
        """Wait, capture post-frame, compare."""
        await asyncio.sleep(self._verify_delay)
        post = self.get_live_frame()
        if post is None:
            return False
        return self._verify_frames_changed(pre_frame, post)

    # ------------------------------------------------------------------
    # Wait-for-condition
    # ------------------------------------------------------------------

    async def _wait_for_condition(
        self, step: Any, timeout_s: float = 10.0,
    ) -> bool:
        """Poll accessibility for app window or element appearance."""
        target = getattr(step, "target", "") or ""
        app_name = getattr(step, "app_name", "") or ""
        if not target or not app_name or self._ax_resolver is None:
            return False

        deadline = time.monotonic() + timeout_s
        poll_interval = 0.5
        while time.monotonic() < deadline:
            try:
                result = await self._ax_resolver.resolve(
                    target, app_name=app_name,
                )
                if result is not None:
                    return True
            except Exception:
                pass
            await asyncio.sleep(poll_interval)
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _frame_to_b64(self, frame: np.ndarray) -> str:
        """Encode RGB frame as JPEG base64 string."""
        try:
            from PIL import Image
        except ImportError:
            # Fallback: raw encode via numpy (extremely rare path)
            logger.warning("[CUExec] PIL unavailable -- using raw frame encoding")
            return base64.b64encode(frame.tobytes()).decode("ascii")

        img = Image.fromarray(frame)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._jpeg_quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _parse_vision_response(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse OpenAI-compatible chat completion response into coord dict."""
        try:
            choices = data.get("choices", [])
            if not choices:
                return None
            message = choices[0].get("message", {})
            content = message.get("content", "")
            return self._parse_json_from_text(content)
        except Exception as exc:
            logger.debug("[CUExec] Failed to parse vision response: %s", exc)
            return None

    def _parse_json_from_text(self, text: str) -> Optional[Dict[str, Any]]:
        """Extract JSON object from text that may contain markdown fences."""
        text = text.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last fence lines
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines).strip()

        # Find JSON object boundaries
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None

        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict) and "x" in obj and "y" in obj:
                return obj
            return None
        except (json.JSONDecodeError, ValueError):
            return None
