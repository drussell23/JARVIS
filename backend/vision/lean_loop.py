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
_SETTLE_S = float(os.environ.get("VISION_LEAN_SETTLE_S", "0.3"))
_OVERALL_TIMEOUT_S = float(os.environ.get("VISION_LEAN_TIMEOUT_S", "180"))
_VISION_TIMEOUT_S = float(os.environ.get("VISION_LEAN_TIMEOUT_S", "30"))
_CAPTURE_TIMEOUT_S = float(os.environ.get("VISION_LEAN_CAPTURE_TIMEOUT_S", "5"))
_MAX_IMAGE_DIM = int(os.environ.get("VISION_LEAN_MAX_IMAGE_DIM", "1024"))
_JPEG_QUALITY = int(os.environ.get("VISION_LEAN_JPEG_QUALITY", "70"))

# --- Vision model routing (3-tier cascade) ---
# TIER 0: Doubleword API (direct, always available, 30x cheaper)
# TIER 1: Claude API (fallback when Doubleword fails)
# TIER 2: J-Prime GCP (last resort, only when VM is running)
_DOUBLEWORD_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
_DOUBLEWORD_BASE_URL = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
_DOUBLEWORD_VISION_MODEL = os.environ.get("DOUBLEWORD_VISION_MODEL", "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8")
_DOUBLEWORD_TIMEOUT_S = float(os.environ.get("VISION_DOUBLEWORD_TIMEOUT_S", "30"))
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
            result = await asyncio.wait_for(
                self._loop(goal),
                timeout=_OVERALL_TIMEOUT_S,
            )
            logger.info(
                "[LeanVision] === END === success=%s turns=%s result=%s",
                result.get("success"), result.get("turns"),
                str(result.get("result", ""))[:120],
            )
            return result
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
        capture_failures = 0

        for turn in range(1, _MAX_TURNS + 1):
            turn_start = time.monotonic()
            logger.info(
                "[LeanVision] --- Turn %d/%d --- goal: %s",
                turn, _MAX_TURNS, goal[:60],
            )

            # ---- 1. CAPTURE ----
            screenshot_b64, img_w, img_h = await self._capture_screen()
            if screenshot_b64 is None:
                capture_failures += 1
                logger.error("[LeanVision] Turn %d: CAPTURE failed (%d consecutive)", turn, capture_failures)
                if capture_failures >= 3:
                    logger.error("[LeanVision] Screen capture broken after %d failures — aborting", capture_failures)
                    return {
                        "success": False,
                        "result": "Cannot capture screen — screencapture failed 3 times. Check screen recording permissions in System Settings > Privacy & Security.",
                        "turns": turn,
                        "action_log": action_log,
                    }
                await asyncio.sleep(1.0)
                continue
            capture_failures = 0  # reset on success
            logger.info("[LeanVision] Turn %d: CAPTURE OK (%dx%d)", turn, img_w, img_h)

            # ---- 2. THINK ----
            response = await self._ask_claude(
                goal, screenshot_b64, img_w, img_h, action_log, turn,
            )
            reasoning = response.get("reasoning", "(no reasoning)")
            logger.info("[LeanVision] Turn %d: THINK -> %s", turn, reasoning[:120])

            # Goal achieved?
            if response.get("goal_achieved"):
                # Safety: reject premature goal_achieved on turn 1 with no actions
                # if the goal clearly involves multi-step interaction (messaging, typing, etc.)
                if turn == 1 and len(action_log) == 0 and self._goal_requires_interaction(goal):
                    logger.warning(
                        "[LeanVision] Turn 1: model claims goal_achieved but no actions taken "
                        "and goal requires interaction — overriding to continue. "
                        "Reasoning: %s", response.get("reasoning", "")[:120],
                    )
                    # Force the model to propose an action by treating as not achieved
                    response["goal_achieved"] = False
                    # Fall through to next_action check below
                else:
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
        """Capture screenshot — tries FramePipeline first, falls back to screencapture.

        Returns (base64_jpeg, width, height) or (None, 0, 0) on failure.
        Image is downscaled to logical screen resolution so Claude coords
        map directly to the actuator's coordinate space.
        """
        # --- Primary: FramePipeline.latest_frame (sub-10ms if running) ---
        frame_data = self._try_frame_pipeline()
        if frame_data is not None:
            return self._process_frame(frame_data)

        # --- Fallback: screencapture subprocess ---
        return await self._capture_via_subprocess()

    def _try_frame_pipeline(self):
        """Try to get latest frame from the existing FramePipeline/VisionCortex."""
        for import_path in (
            "backend.vision.realtime.frame_pipeline",
            "vision.realtime.frame_pipeline",
        ):
            try:
                import importlib
                mod = importlib.import_module(import_path)
                # Check if VisionActionLoop has an instance with a frame_pipeline
                try:
                    from backend.vision.realtime.vision_action_loop import VisionActionLoop
                    val = VisionActionLoop.get_instance() if hasattr(VisionActionLoop, "get_instance") else None
                    if val and hasattr(val, "frame_pipeline") and val.frame_pipeline:
                        frame = val.frame_pipeline.latest_frame
                        if frame is not None:
                            logger.info("[LeanVision] Frame from FramePipeline (sub-10ms)")
                            return frame
                except Exception:
                    pass

                # Try VisionCortex singleton
                try:
                    cortex_mod = importlib.import_module(import_path.replace("frame_pipeline", "vision_cortex"))
                    cortex_cls = getattr(cortex_mod, "VisionCortex", None)
                    if cortex_cls and hasattr(cortex_cls, "get_instance"):
                        cortex = cortex_cls.get_instance()
                        if cortex and hasattr(cortex, "_pipeline") and cortex._pipeline:
                            frame = cortex._pipeline.latest_frame
                            if frame is not None:
                                logger.info("[LeanVision] Frame from VisionCortex (sub-10ms)")
                                return frame
                except Exception:
                    pass

                break  # Import succeeded but no frame available
            except ImportError:
                continue
        return None

    def _process_frame(self, frame_data) -> Tuple[Optional[str], int, int]:
        """Convert a FrameData (numpy RGB array) to base64 JPEG."""
        try:
            from PIL import Image
            img = Image.fromarray(frame_data.data)
            if img.mode == "RGBA":
                img = img.convert("RGB")
            return self._downscale_and_encode(img)
        except Exception as exc:
            logger.warning("[LeanVision] FramePipeline frame processing failed: %s", exc)
            return None, 0, 0

    async def _capture_via_subprocess(self) -> Tuple[Optional[str], int, int]:
        """Fallback: capture via screencapture subprocess."""
        os.makedirs(_TMP_DIR, exist_ok=True)
        tmp_path = os.path.join(_TMP_DIR, f"lean_{uuid.uuid4().hex[:8]}.png")

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
                logger.error("[LeanVision] screencapture exit code %d", returncode)
                return None, 0, 0

            from PIL import Image
            img = Image.open(tmp_path)
            if img.mode == "RGBA":
                img = img.convert("RGB")
            return self._downscale_and_encode(img)

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

    def _downscale_and_encode(self, img) -> Tuple[Optional[str], int, int]:
        """Downscale PIL image to logical screen coords and encode as base64 JPEG."""
        from PIL import Image as _Image
        capture_w, capture_h = img.size

        # Downscale to logical screen resolution so Claude coords = actuator coords
        logical_w, logical_h = self._get_logical_screen_size()
        if logical_w > 0 and logical_h > 0 and (capture_w, capture_h) != (logical_w, logical_h):
            img = img.resize((logical_w, logical_h), _Image.LANCZOS)

        # If still over max dimension, downscale further
        cur_w, cur_h = img.size
        if max(cur_w, cur_h) > _MAX_IMAGE_DIM:
            ratio = _MAX_IMAGE_DIM / max(cur_w, cur_h)
            img = img.resize((int(cur_w * ratio), int(cur_h * ratio)), _Image.LANCZOS)
            self._last_coord_scale = 1.0 / ratio
        else:
            self._last_coord_scale = 1.0

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        final_w, final_h = img.size
        return b64, final_w, final_h

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
        """Send screenshot to vision model — Claude PRIMARY for vision (best
        coordinate precision), Doubleword available as fallback."""

        # Build shared prompt components
        system_prompt, user_text = self._build_vision_prompt(
            goal, img_w, img_h, action_log, turn,
        )

        # PRIMARY: Claude Vision (best at UI automation, trained for computer use)
        result = await self._ask_claude_vision(
            system_prompt, user_text, screenshot_b64,
        )
        if result is not None and "error" not in result.get("reasoning", "").lower():
            return result

        claude_reason = result.get("reasoning", "unknown") if result else "no response"
        logger.warning("[LeanVision] Claude Vision failed/errored: %s", claude_reason[:120])

        # FALLBACK: Doubleword VL-235B (if Claude fails/unavailable)
        if _DOUBLEWORD_API_KEY:
            logger.info("[LeanVision] Claude failed, trying Doubleword VL-235B")
            result = await self._ask_doubleword_vision(
                system_prompt, user_text, screenshot_b64,
            )
            if result is not None:
                return result

        return {"goal_achieved": False, "reasoning": "All vision providers failed"}

    def _build_vision_prompt(
        self, goal: str, img_w: int, img_h: int,
        action_log: List[Dict[str, Any]], turn: int,
    ) -> tuple:
        """Build the system prompt and user text (shared by both providers)."""
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
            "TASK: Look at the screenshot and decide the single next action to achieve the COMPLETE goal.\n\n"
            f"The image is {img_w}x{img_h} pixels and maps directly to screen coordinates.\n"
            "Return coordinates as [x, y] in this image's pixel space.\n\n"
            "Respond with ONLY a JSON object (no markdown fences, no explanation outside JSON):\n"
            "{\n"
            '  "goal_achieved": boolean,\n'
            '  "next_action": {                    // null if goal_achieved is true\n'
            '    "action_type": "click"|"type"|"scroll"|"key",\n'
            '    "target": "human description of the element",\n'
            '    "text": "text to type or key name", // required for type and key\n'
            '    "coords": [x, y]                  // required for click, optional for type/scroll\n'
            "  },\n"
            '  "reasoning": "one-line explanation of your decision",\n'
            '  "confidence": 0.0 to 1.0,\n'
            '  "scene_summary": "brief description of what you see on screen"\n'
            "}\n\n"
            "CRITICAL — GOAL COMPLETION RULES:\n"
            "- Read the ENTIRE goal carefully. Multi-part goals (e.g., 'open X AND message Y saying Z') are NOT done until ALL parts are complete.\n"
            "- Opening an app is NEVER the final step if the goal includes sending a message, clicking something, or any further interaction.\n"
            "- goal_achieved=true ONLY when the ENTIRE goal is satisfied — e.g., message was SENT (visible in chat as sent), search results are showing, etc.\n"
            "- If you are on turn 1 and the goal involves messaging/typing/clicking, goal_achieved MUST be false — you haven't done anything yet.\n\n"
            "ACTION RULES:\n"
            "- For CLICK: return precise [x, y] pixel coordinates of the element center\n"
            "- For TYPE: if the target text field is already focused (from a prior click), omit coords\n"
            "- For KEY: press a key like 'return', 'tab', 'escape', 'space', etc. Put the key name in 'text'\n"
            "- For SCROLL: coords optional, scrolls at current mouse position\n"
            "- If previous action failed, try a different approach (different coords, different element)\n"
            "- Be precise with coordinates — look carefully at the actual element position\n\n"
            "CHAT APP RULES (WhatsApp, Messages, Slack, etc.):\n"
            "- To message someone: first find and click their name/chat in the sidebar or search for them\n"
            "- Then click the message input field at the bottom of the conversation\n"
            "- Then type the message text\n"
            "- Then press 'return' (key action) to SEND it\n"
            "- After pressing return, the message appears as a sent bubble. ONLY THEN set goal_achieved=true\n"
            "- If you see your message already in the conversation (as a sent bubble), the goal IS achieved\n"
            "- Do NOT type or send again after the message is visible as sent\n"
        )

        user_text = (
            f"GOAL: {goal}\n\n"
            f"TURN: {turn}/{_MAX_TURNS}\n\n"
            f"ACTION HISTORY:\n{history}\n\n"
            "Look at the screenshot and return the next action as JSON."
        )

        return system_prompt, user_text

    # ------------------------------------------------------------------
    # Tier 0: Doubleword Vision (direct API, always available)
    # ------------------------------------------------------------------

    async def _ask_doubleword_vision(
        self, system_prompt: str, user_text: str, screenshot_b64: str,
    ) -> Optional[Dict[str, Any]]:
        """Send screenshot to Doubleword's VL-235B model directly.

        Calls api.doubleword.ai/v1/chat/completions (OpenAI-compatible).
        No J-Prime needed — this is a direct cloud API call.
        Returns None if Doubleword is unavailable (triggers Claude fallback).
        """
        if not hasattr(self, "_dw_session") or self._dw_session is None:
            try:
                import aiohttp
                self._dw_session = aiohttp.ClientSession()
            except ImportError:
                logger.warning("[LeanVision] aiohttp not installed for Doubleword")
                return None

        payload = {
            "model": _DOUBLEWORD_VISION_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                    },
                    {"type": "text", "text": user_text},
                ]},
            ],
            # Qwen3.5 is a reasoning model — it uses ~400-800 tokens for
            # chain-of-thought BEFORE producing the JSON output. At 512,
            # the JSON gets truncated. 2048 gives plenty of room for both
            # reasoning and a complete JSON response.
            "max_tokens": 2048,
            "temperature": 0.1,
        }

        try:
            import aiohttp
            async with self._dw_session.post(
                f"{_DOUBLEWORD_BASE_URL}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {_DOUBLEWORD_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=_DOUBLEWORD_TIMEOUT_S),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("[LeanVision] Doubleword HTTP %d: %s", resp.status, body[:200])
                    return None

                data = await resp.json()
                content = data["choices"][0]["message"].get("content", "")
                logger.info("[LeanVision] THINK via Doubleword VL-235B")
                return self._parse_response(content)

        except asyncio.TimeoutError:
            logger.warning("[LeanVision] Doubleword timed out after %.0fs", _DOUBLEWORD_TIMEOUT_S)
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[LeanVision] Doubleword error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Tier 1: Claude Vision (SECONDARY fallback)
    # ------------------------------------------------------------------

    async def _ask_claude_vision(
        self, system_prompt: str, user_text: str, screenshot_b64: str,
    ) -> Dict[str, Any]:
        """Send screenshot to Claude Vision (Anthropic API)."""
        client = self._get_client()
        if client is None:
            return {"goal_achieved": False, "reasoning": "No Anthropic API key"}

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
                timeout=_VISION_TIMEOUT_S,
            )
            raw = response.content[0].text if response.content else ""
            logger.info("[LeanVision] THINK via Claude %s", _CLAUDE_MODEL)
            return self._parse_response(raw)

        except asyncio.TimeoutError:
            logger.error("[LeanVision] Claude timed out after %.0fs", _VISION_TIMEOUT_S)
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
        """Extract JSON from vision model response, repairing common errors."""

        def _try_parse(text: str) -> Optional[Dict[str, Any]]:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return None

        # Try direct parse
        result = _try_parse(raw)
        if result:
            return result

        # Strip markdown fences
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```\w*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
            result = _try_parse(cleaned)
            if result:
                return result

        # Extract first JSON object
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            json_text = match.group()

            result = _try_parse(json_text)
            if result:
                return result

            # --- Repair common model JSON errors ---

            # Fix missing closing quotes: "click,  → "click",
            repaired = re.sub(r'"(\w+),\s*\n', r'"\1",\n', json_text)
            result = _try_parse(repaired)
            if result:
                logger.info("[LeanVision] Repaired missing quote in JSON")
                return result

            # Fix truncated response: add missing closing braces
            open_braces = json_text.count("{") - json_text.count("}")
            open_brackets = json_text.count("[") - json_text.count("]")
            if open_braces > 0 or open_brackets > 0:
                # Truncate to last complete value
                # Find last complete key-value pair
                last_comma = json_text.rfind(",")
                last_colon = json_text.rfind(":")
                if last_comma > last_colon:
                    truncated = json_text[:last_comma]
                else:
                    truncated = json_text

                # Close open structures
                truncated += "]" * max(0, open_brackets)
                truncated += "}" * max(0, open_braces)
                result = _try_parse(truncated)
                if result:
                    logger.info("[LeanVision] Repaired truncated JSON")
                    return result

            # Fix unquoted values: click → "click"
            repaired2 = re.sub(
                r':\s*([a-zA-Z_]\w*)\s*([,}\]])',
                lambda m: f': "{m.group(1)}"{m.group(2)}' if m.group(1) not in ("true", "false", "null") else m.group(0),
                json_text,
            )
            result = _try_parse(repaired2)
            if result:
                logger.info("[LeanVision] Repaired unquoted values in JSON")
                return result

        logger.warning("[LeanVision] Could not parse response: %s", raw[:300])
        return {"goal_achieved": False, "reasoning": f"Unparseable response: {raw[:100]}"}

    # ------------------------------------------------------------------
    # Step 3: ACT
    # ------------------------------------------------------------------

    async def _execute_action(self, action: Dict[str, Any]) -> bool:
        """Dispatch action via BackgroundActuator (Ghost Hands) with pyautogui fallback."""
        action_type = action.get("action_type", "click")

        try:
            # Try Ghost Hands (CGEvent + FocusGuard) first
            gh_result = await self._try_ghost_hands(action)
            if gh_result is not None:
                return gh_result

            # Fallback: pyautogui
            logger.info("[LeanVision] Ghost Hands unavailable, falling back to pyautogui")
            if action_type == "click":
                return await self._pyautogui_click(action)
            elif action_type == "type":
                return await self._pyautogui_type(action)
            elif action_type == "key":
                return await self._pyautogui_key(action)
            elif action_type == "scroll":
                return await self._pyautogui_scroll(action)
            else:
                logger.warning("[LeanVision] Unknown action_type: %s", action_type)
                return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[LeanVision] Action '%s' error: %s", action_type, exc)
            return False

    # ------------------------------------------------------------------
    # Ghost Hands integration (primary actuator)
    # ------------------------------------------------------------------

    async def _try_ghost_hands(self, action: Dict[str, Any]) -> Optional[bool]:
        """Try executing via BackgroundActuator. Returns None if unavailable."""
        try:
            from backend.ghost_hands.background_actuator import (
                BackgroundActuator, Action, ActionType, ActionResult,
            )
        except ImportError:
            try:
                from ghost_hands.background_actuator import (
                    BackgroundActuator, Action, ActionType, ActionResult,
                )
            except ImportError:
                return None  # Not available

        # Get or create actuator
        if not hasattr(self, "_ghost_hands") or self._ghost_hands is None:
            try:
                self._ghost_hands = BackgroundActuator()
                started = await self._ghost_hands.start()
                if not started:
                    self._ghost_hands = None
                    return None
                logger.info("[LeanVision] Ghost Hands actuator started")
            except Exception as exc:
                logger.warning("[LeanVision] Ghost Hands init failed: %s", exc)
                self._ghost_hands = None
                return None

        action_type = action.get("action_type", "click")
        coords = action.get("coords")
        screen_coords = None
        if coords and len(coords) >= 2:
            screen_coords = (
                int(float(coords[0]) * self._last_coord_scale),
                int(float(coords[1]) * self._last_coord_scale),
            )

        target = action.get("target", "")

        try:
            if action_type == "click":
                if not screen_coords:
                    return None
                gh_action = Action(
                    action_type=ActionType.CLICK,
                    coordinates=screen_coords,
                )
                logger.info("[LeanVision] CLICK via Ghost Hands (%d, %d) on '%s'",
                           screen_coords[0], screen_coords[1], target[:50])

            elif action_type == "type":
                text = action.get("text", "")
                if not text:
                    return False
                # Click target first if coords provided
                if screen_coords:
                    click_action = Action(action_type=ActionType.CLICK, coordinates=screen_coords)
                    await self._ghost_hands.execute(click_action)
                    await asyncio.sleep(0.15)
                gh_action = Action(action_type=ActionType.TYPE, text=text)
                logger.info("[LeanVision] TYPE via Ghost Hands '%s'", text[:50])

            elif action_type == "key":
                key_name = action.get("text", "").lower().strip()
                if not key_name:
                    return False
                gh_action = Action(action_type=ActionType.KEY, key=key_name)
                logger.info("[LeanVision] KEY via Ghost Hands '%s'", key_name)

            elif action_type == "scroll":
                amount = action.get("scroll_amount") or action.get("amount") or -3
                gh_action = Action(
                    action_type=ActionType.SCROLL,
                    coordinates=screen_coords,
                    text=str(amount),
                )
                logger.info("[LeanVision] SCROLL via Ghost Hands %s", amount)

            else:
                return None

            report = await asyncio.wait_for(
                self._ghost_hands.execute(gh_action),
                timeout=5.0,
            )
            success = report.result == ActionResult.SUCCESS
            if not success:
                logger.warning("[LeanVision] Ghost Hands returned: %s (%s)",
                             report.result.name, getattr(report, "error", ""))
            return success

        except asyncio.TimeoutError:
            logger.warning("[LeanVision] Ghost Hands timed out")
            return None  # Fall back to pyautogui
        except Exception as exc:
            logger.warning("[LeanVision] Ghost Hands error: %s", exc)
            return None  # Fall back to pyautogui

    # ------------------------------------------------------------------
    # pyautogui fallback actions
    # ------------------------------------------------------------------

    async def _pyautogui_click(self, action: Dict[str, Any]) -> bool:
        """Fallback click via pyautogui."""
        coords = action.get("coords")
        if not coords or len(coords) < 2:
            return False
        screen_x = int(float(coords[0]) * self._last_coord_scale)
        screen_y = int(float(coords[1]) * self._last_coord_scale)
        logger.info("[LeanVision] CLICK via pyautogui (%d, %d)", screen_x, screen_y)
        import pyautogui
        await asyncio.to_thread(pyautogui.click, screen_x, screen_y)
        return True

    async def _pyautogui_type(self, action: Dict[str, Any]) -> bool:
        """Fallback type via clipboard paste."""
        text = action.get("text", "")
        if not text:
            return False
        coords = action.get("coords")
        if coords and len(coords) >= 2:
            await self._pyautogui_click({"coords": coords, "target": "text field"})
            await asyncio.sleep(0.2)
        logger.info("[LeanVision] TYPE via pyautogui '%s'", text[:50])
        proc = await asyncio.create_subprocess_exec(
            "pbcopy", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate(text.encode("utf-8"))
        paste = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "System Events" to keystroke "v" using command down',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await paste.wait()
        return True

    async def _pyautogui_key(self, action: Dict[str, Any]) -> bool:
        """Fallback key press via pyautogui."""
        key_name = action.get("text", "").lower().strip()
        if not key_name:
            return False
        logger.info("[LeanVision] KEY via pyautogui '%s'", key_name)
        import pyautogui
        await asyncio.to_thread(pyautogui.press, key_name)
        return True

    async def _pyautogui_scroll(self, action: Dict[str, Any]) -> bool:
        """Fallback scroll via pyautogui."""
        amount = action.get("scroll_amount") or action.get("amount") or -3
        logger.info("[LeanVision] SCROLL via pyautogui %s", amount)
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

    # ------------------------------------------------------------------
    # Multi-step goal detection
    # ------------------------------------------------------------------

    @staticmethod
    def _goal_requires_interaction(goal: str) -> bool:
        """Detect if a goal involves interaction beyond just opening an app.

        Used to prevent premature goal_achieved on turn 1 when the model
        sees the app already open but hasn't performed the actual task.
        """
        goal_lower = goal.lower()
        interaction_signals = (
            "message", "send", "type", "write", "reply", "text",
            "search", "click", "navigate", "play", "post",
            "saying", "tell", "ask",
        )
        return any(signal in goal_lower for signal in interaction_signals)
