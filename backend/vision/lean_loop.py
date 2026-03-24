"""
Lean Vision Loop -- stripped-down 3-step see-think-act loop.

Replaces the 12-hop pipeline (FramePipeline -> VisionRouter -> L1/L2/L3 ->
ActionExecutor -> Verifier) with a tight loop:

    1. CAPTURE  -- async screencapture (no fork crash)
    2. THINK    -- direct Claude Vision API (no L1/L2 routing)
    3. ACT      -- pyautogui + clipboard (Retina-aware coords)

This is Path A: get vision working reliably NOW.  When a multimodal model
is deployed on GCP (Path B), the full tiered pipeline can be re-enabled.

Usage::

    loop = LeanVisionLoop()
    result = await loop.run("Open WhatsApp and message Zach 'what's up'")

All tunables are environment-variable driven -- zero hardcoding.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven tunables
# ---------------------------------------------------------------------------
_MAX_TURNS = int(os.environ.get("VISION_LEAN_MAX_TURNS", "10"))
_SETTLE_S = float(os.environ.get("VISION_LEAN_SETTLE_S", "0.5"))
_OVERALL_TIMEOUT_S = float(os.environ.get("VISION_LEAN_TIMEOUT_S", "180"))
_CLAUDE_TIMEOUT_S = float(os.environ.get("VISION_LEAN_CLAUDE_TIMEOUT_S", "60"))
_CAPTURE_TIMEOUT_S = float(os.environ.get("VISION_LEAN_CAPTURE_TIMEOUT_S", "5"))
_MAX_IMAGE_DIM = int(os.environ.get("VISION_LEAN_MAX_IMAGE_DIM", "1024"))
_JPEG_QUALITY = int(os.environ.get("VISION_LEAN_JPEG_QUALITY", "70"))
_CLAUDE_MODEL = os.environ.get("JARVIS_CLAUDE_VISION_MODEL", "claude-sonnet-4-20250514")
_STAGNATION_WINDOW = int(os.environ.get("VISION_LEAN_STAGNATION_WINDOW", "3"))
_TMP_DIR = os.environ.get("VISION_LEAN_TMP_DIR", "/tmp/claude")


class LeanVisionLoop:
    """Stripped-down 3-step vision loop: CAPTURE -> THINK -> ACT.

    Designed to be fast, reliable, and debuggable.  Every step logs
    clearly so failures are immediately visible.
    """

    _instance: Optional["LeanVisionLoop"] = None

    def __init__(self) -> None:
        self._client: Any = None  # anthropic.AsyncAnthropic (lazy)
        self._screen_size: Optional[Tuple[int, int]] = None  # logical px
        # Track the downscale ratio when image exceeds _MAX_IMAGE_DIM
        self._last_coord_scale: float = 1.0

    # ------------------------------------------------------------------
    # Singleton access (matches codebase convention)
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "LeanVisionLoop":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, goal: str) -> Dict[str, Any]:
        """Run the vision loop until the goal is achieved or turns exhaust.

        Returns a dict with keys: success, result, turns, action_log.
        """
        logger.info("[LeanVision] === START === goal: %s", goal[:100])
        try:
            return await asyncio.wait_for(
                self._loop(goal),
                timeout=_OVERALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[LeanVision] Overall timeout (%.0fs) exceeded for: %s",
                _OVERALL_TIMEOUT_S, goal[:80],
            )
            return {
                "success": False,
                "result": f"Overall timeout ({_OVERALL_TIMEOUT_S}s) exceeded",
                "turns": 0,
                "action_log": [],
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[LeanVision] Unexpected error: %s", exc)
            return {
                "success": False,
                "result": f"Unexpected error: {exc}",
                "turns": 0,
                "action_log": [],
            }

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    async def _loop(self, goal: str) -> Dict[str, Any]:
        action_log: List[Dict[str, Any]] = []

        for turn in range(1, _MAX_TURNS + 1):
            turn_start = time.monotonic()
            logger.info(
                "[LeanVision] --- Turn %d/%d --- goal: %s",
                turn, _MAX_TURNS, goal[:60],
            )

            # ---- 1. CAPTURE ----
            screenshot_b64, img_w, img_h = await self._capture_screen()
            if screenshot_b64 is None:
                logger.error("[LeanVision] Turn %d: CAPTURE failed, retrying...", turn)
                await asyncio.sleep(1.0)
                continue
            logger.info("[LeanVision] Turn %d: CAPTURE OK (%dx%d)", turn, img_w, img_h)

            # ---- 2. THINK ----
            response = await self._ask_claude(
                goal, screenshot_b64, img_w, img_h, action_log, turn,
            )
            reasoning = response.get("reasoning", "(no reasoning)")
            logger.info("[LeanVision] Turn %d: THINK -> %s", turn, reasoning[:120])

            # Goal achieved?
            if response.get("goal_achieved"):
                logger.info(
                    "[LeanVision] === GOAL ACHIEVED on turn %d ===", turn,
                )
                return {
                    "success": True,
                    "result": f"Goal achieved: {goal}",
                    "turns": turn,
                    "action_log": action_log,
                    "scene_summary": response.get("scene_summary", ""),
                }

            next_action = response.get("next_action")
            if not next_action:
                reason = response.get("reasoning", "No action proposed")
                logger.warning(
                    "[LeanVision] Turn %d: no action proposed: %s", turn, reason,
                )
                return {
                    "success": False,
                    "result": reason,
                    "turns": turn,
                    "action_log": action_log,
                }

            # Stagnation guard
            if self._is_stagnant(action_log, next_action):
                logger.warning("[LeanVision] Stagnation detected after %d turns", turn)
                return {
                    "success": False,
                    "result": f"Stagnation: repeating same action for {_STAGNATION_WINDOW} turns",
                    "turns": turn,
                    "action_log": action_log,
                }

            # ---- 3. ACT ----
            action_type = next_action.get("action_type", "click")
            success = await self._execute_action(next_action)
            elapsed = time.monotonic() - turn_start

            entry = {
                "turn": turn,
                "action_type": action_type,
                "target": next_action.get("target", ""),
                "text": next_action.get("text"),
                "coords": next_action.get("coords"),
                "result": "success" if success else "failure",
                "reasoning": reasoning,
                "elapsed_s": round(elapsed, 2),
            }
            action_log.append(entry)

            logger.info(
                "[LeanVision] Turn %d: ACT %s '%s' -> %s (%.1fs)",
                turn, action_type,
                next_action.get("target", "")[:40],
                "OK" if success else "FAIL",
                elapsed,
            )

            # Settle -- let UI update after action
            await asyncio.sleep(_SETTLE_S)

        # Max turns exhausted
        logger.warning("[LeanVision] Max turns (%d) exhausted", _MAX_TURNS)
        return {
            "success": False,
            "result": f"Max turns ({_MAX_TURNS}) exhausted",
            "turns": _MAX_TURNS,
            "action_log": action_log,
        }

    # ------------------------------------------------------------------
    # Step 1: CAPTURE
    # ------------------------------------------------------------------

    async def _capture_screen(
        self,
    ) -> Tuple[Optional[str], int, int]:
        """Capture screenshot via async subprocess (no fork crash).

        Returns (base64_jpeg, width, height) or (None, 0, 0) on failure.
        The image is downscaled to logical screen resolution so that
        pixel coordinates in the image map directly to pyautogui coords.
        """
        os.makedirs(_TMP_DIR, exist_ok=True)
        tmp_path = os.path.join(_TMP_DIR, f"lean_{uuid.uuid4().hex[:8]}.png")

        try:
            # Async subprocess -- safe alongside loaded C extensions
            proc = await asyncio.create_subprocess_exec(
                "screencapture", "-x", "-C", tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            returncode = await asyncio.wait_for(
                proc.wait(), timeout=_CAPTURE_TIMEOUT_S,
            )
            if returncode != 0:
                logger.error("[LeanVision] screencapture exit code %d", returncode)
                return None, 0, 0

            from PIL import Image

            img = Image.open(tmp_path)
            if img.mode == "RGBA":
                img = img.convert("RGB")

            capture_w, capture_h = img.size

            # Downscale to logical screen resolution so Claude coords
            # map directly to pyautogui's coordinate space.
            logical_w, logical_h = self._get_logical_screen_size()
            if logical_w > 0 and logical_h > 0 and (capture_w, capture_h) != (logical_w, logical_h):
                img = img.resize((logical_w, logical_h), Image.LANCZOS)

            # If still over max dimension, downscale further
            cur_w, cur_h = img.size
            if max(cur_w, cur_h) > _MAX_IMAGE_DIM:
                ratio = _MAX_IMAGE_DIM / max(cur_w, cur_h)
                new_w = int(cur_w * ratio)
                new_h = int(cur_h * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                self._last_coord_scale = 1.0 / ratio
            else:
                self._last_coord_scale = 1.0

            # JPEG compress
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")

            final_w, final_h = img.size
            return b64, final_w, final_h

        except asyncio.TimeoutError:
            logger.error("[LeanVision] screencapture timed out (%.1fs)", _CAPTURE_TIMEOUT_S)
            return None, 0, 0
        except Exception as exc:
            logger.error("[LeanVision] capture error: %s", exc)
            return None, 0, 0
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _get_logical_screen_size(self) -> Tuple[int, int]:
        """Get the logical (non-Retina) screen dimensions.

        Cached after first call.  Falls back to (0, 0) if unavailable.
        """
        if self._screen_size is not None:
            return self._screen_size
        try:
            import pyautogui
            w, h = pyautogui.size()
            self._screen_size = (w, h)
            logger.info("[LeanVision] Logical screen size: %dx%d", w, h)
            return (w, h)
        except Exception as exc:
            logger.warning("[LeanVision] Could not get screen size: %s", exc)
            self._screen_size = (0, 0)
            return (0, 0)

    # ------------------------------------------------------------------
    # Step 2: THINK
    # ------------------------------------------------------------------

    async def _ask_claude(
        self,
        goal: str,
        screenshot_b64: str,
        img_w: int,
        img_h: int,
        action_log: List[Dict[str, Any]],
        turn: int,
    ) -> Dict[str, Any]:
        """Send screenshot to Claude Vision and get structured action."""
        client = self._get_client()
        if client is None:
            return {"goal_achieved": False, "reasoning": "No Anthropic API key"}

        # Build action history
        history_lines = []
        for entry in action_log:
            t = entry.get("turn", "?")
            act = entry.get("action_type", "?")
            target = entry.get("target", "?")
            result = entry.get("result", "?")
            text = entry.get("text")
            text_note = f" (text: '{text}')" if text else ""
            coords = entry.get("coords")
            coord_note = f" at {coords}" if coords else ""
            history_lines.append(
                f"  Turn {t}: {act} '{target}'{coord_note}{text_note} -> {result}"
            )
        history = "\n".join(history_lines) if history_lines else "  (first turn -- no prior actions)"

        system_prompt = (
            "You are a precise UI automation agent. You can see the user's screen.\n\n"
            "TASK: Look at the screenshot and decide the single next action to achieve the goal.\n\n"
            f"The image is {img_w}x{img_h} pixels and maps directly to screen coordinates.\n"
            "Return coordinates as [x, y] in this image's pixel space.\n\n"
            "Respond with ONLY a JSON object (no markdown fences, no explanation outside JSON):\n"
            "{\n"
            '  "goal_achieved": boolean,\n'
            '  "next_action": {                    // null if goal_achieved is true\n'
            '    "action_type": "click"|"type"|"scroll",\n'
            '    "target": "human description of the element",\n'
            '    "text": "text to type",           // required for type, omit for click/scroll\n'
            '    "coords": [x, y]                  // required for click, optional for type/scroll\n'
            "  },\n"
            '  "reasoning": "one-line explanation of your decision",\n'
            '  "confidence": 0.0 to 1.0,\n'
            '  "scene_summary": "brief description of what you see on screen"\n'
            "}\n\n"
            "RULES:\n"
            "- For CLICK: return precise [x, y] pixel coordinates of the element center\n"
            "- For TYPE: if the target text field is already focused (from a prior click), omit coords\n"
            "- For SCROLL: coords optional, scrolls at current mouse position\n"
            "- If previous action failed, try a different approach (different coords, different element)\n"
            "- Be precise with coordinates -- look carefully at the actual element position\n"
            "- If the goal is fully achieved (e.g., message sent, page loaded), set goal_achieved=true\n"
        )

        user_text = (
            f"GOAL: {goal}\n\n"
            f"TURN: {turn}/{_MAX_TURNS}\n\n"
            f"ACTION HISTORY:\n{history}\n\n"
            "Look at the screenshot and return the next action as JSON."
        )

        content: list = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": screenshot_b64,
                },
            },
            {"type": "text", "text": user_text},
        ]

        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model=_CLAUDE_MODEL,
                    max_tokens=512,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content}],
                ),
                timeout=_CLAUDE_TIMEOUT_S,
            )

            raw = response.content[0].text if response.content else ""
            return self._parse_response(raw)

        except asyncio.TimeoutError:
            logger.error(
                "[LeanVision] Claude timed out after %.0fs", _CLAUDE_TIMEOUT_S,
            )
            return {"goal_achieved": False, "reasoning": "Claude API timed out"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[LeanVision] Claude API error: %s", exc)
            return {"goal_achieved": False, "reasoning": f"Claude error: {exc}"}

    def _get_client(self) -> Any:
        """Lazy-init Anthropic async client (singleton per loop instance)."""
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError:
            logger.error("[LeanVision] anthropic package not installed")
            return None

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.error("[LeanVision] ANTHROPIC_API_KEY not set")
            return None

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        return self._client

    @staticmethod
    def _parse_response(raw: str) -> Dict[str, Any]:
        """Extract JSON from Claude's response text."""
        # Try direct parse first
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass

        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```\w*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
            try:
                return json.loads(cleaned)
            except (json.JSONDecodeError, TypeError):
                pass

        # Regex fallback: find first JSON object
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            try:
                return json.loads(match.group())
            except (json.JSONDecodeError, TypeError):
                pass

        logger.warning("[LeanVision] Could not parse Claude response: %s", raw[:300])
        return {"goal_achieved": False, "reasoning": f"Unparseable response: {raw[:100]}"}

    # ------------------------------------------------------------------
    # Step 3: ACT
    # ------------------------------------------------------------------

    async def _execute_action(self, action: Dict[str, Any]) -> bool:
        """Dispatch action to the appropriate handler."""
        action_type = action.get("action_type", "click")

        try:
            if action_type == "click":
                return await self._do_click(action)
            elif action_type == "type":
                return await self._do_type(action)
            elif action_type == "scroll":
                return await self._do_scroll(action)
            else:
                logger.warning("[LeanVision] Unknown action_type: %s", action_type)
                return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[LeanVision] Action '%s' error: %s", action_type, exc)
            return False

    async def _do_click(self, action: Dict[str, Any]) -> bool:
        """Click at coordinates, scaling from image space to screen space."""
        coords = action.get("coords")
        if not coords or len(coords) < 2:
            logger.error("[LeanVision] Click missing coords: %s", action)
            return False

        img_x, img_y = float(coords[0]), float(coords[1])

        # Scale from image coords to logical screen coords
        # If we downscaled beyond logical size, _last_coord_scale > 1.0
        screen_x = int(img_x * self._last_coord_scale)
        screen_y = int(img_y * self._last_coord_scale)

        target = action.get("target", "unknown element")
        logger.info(
            "[LeanVision] CLICK (%d, %d) -> screen (%d, %d) on '%s'",
            int(img_x), int(img_y), screen_x, screen_y, target[:50],
        )

        import pyautogui
        await asyncio.to_thread(pyautogui.click, screen_x, screen_y)
        return True

    async def _do_type(self, action: Dict[str, Any]) -> bool:
        """Type text using clipboard paste (handles Unicode reliably)."""
        text = action.get("text", "")
        if not text:
            logger.error("[LeanVision] Type action has no text")
            return False

        # If coords provided, click the target field first
        coords = action.get("coords")
        if coords and len(coords) >= 2:
            await self._do_click({"coords": coords, "target": action.get("target", "text field")})
            await asyncio.sleep(0.2)  # Let field focus

        logger.info("[LeanVision] TYPE '%s'", text[:50])

        # Use clipboard paste for reliability (handles Unicode, special chars)
        proc = await asyncio.create_subprocess_exec(
            "pbcopy",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate(text.encode("utf-8"))

        import pyautogui
        await asyncio.to_thread(pyautogui.hotkey, "command", "v")
        return True

    async def _do_scroll(self, action: Dict[str, Any]) -> bool:
        """Scroll at current position or specified coords."""
        amount = action.get("scroll_amount") or action.get("amount") or -3

        # If coords provided, move mouse there first
        coords = action.get("coords")
        if coords and len(coords) >= 2:
            import pyautogui
            screen_x = int(float(coords[0]) * self._last_coord_scale)
            screen_y = int(float(coords[1]) * self._last_coord_scale)
            await asyncio.to_thread(pyautogui.moveTo, screen_x, screen_y)

        logger.info("[LeanVision] SCROLL amount=%s", amount)

        import pyautogui
        await asyncio.to_thread(pyautogui.scroll, int(amount))
        return True

    # ------------------------------------------------------------------
    # Stagnation detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_stagnant(
        action_log: List[Dict[str, Any]],
        next_action: Dict[str, Any],
    ) -> bool:
        """Detect if we're repeating the same action without progress."""
        if len(action_log) < _STAGNATION_WINDOW:
            return False

        recent = action_log[-_STAGNATION_WINDOW:]
        proposed_key = (
            next_action.get("action_type"),
            next_action.get("target"),
            str(next_action.get("coords")),
        )
        for entry in recent:
            entry_key = (
                entry.get("action_type"),
                entry.get("target"),
                str(entry.get("coords")),
            )
            if entry_key != proposed_key:
                return False

        return True
