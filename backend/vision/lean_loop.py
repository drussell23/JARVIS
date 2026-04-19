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

# --- Visual Telemetry (Principle 7: Absolute Observability) ---
# Every perception carries its frame artifact so the operator can verify
# what the agent saw without altering the host environment.
_TELEMETRY_DIR = os.environ.get(
    "VISION_TELEMETRY_DIR",
    os.path.join(_TMP_DIR, "vision_telemetry"),
)
_TELEMETRY_MAX_ARTIFACTS = int(os.environ.get("VISION_TELEMETRY_MAX_ARTIFACTS", "50"))

# --- Computer Use API (native, best accuracy for UI automation) ---
_CU_ENABLED = os.environ.get("VISION_CU_ENABLED", "true").lower() in ("true", "1", "yes")
_CU_DISPLAY_W = int(os.environ.get("VISION_CU_DISPLAY_W", "1280"))
_CU_DISPLAY_H = int(os.environ.get("VISION_CU_DISPLAY_H", "800"))
_CU_MAX_TOKENS = int(os.environ.get("VISION_CU_MAX_TOKENS", "4096"))
_CU_THINKING_BUDGET = int(os.environ.get("VISION_CU_THINKING_BUDGET", "1024"))
_CU_SETTLE_S = float(os.environ.get("VISION_CU_SETTLE_S", "0.5"))
_CU_PRUNE_AFTER = int(os.environ.get("VISION_CU_PRUNE_SCREENSHOTS", "10"))
_CU_MAX_TURNS = int(os.environ.get("VISION_CU_MAX_TURNS", "20"))
_CU_MODEL = os.environ.get("JARVIS_CU_MODEL", "claude-sonnet-4-6")
_CU_TOOL_VERSION = os.environ.get("VISION_CU_TOOL_VERSION", "computer_20251124")
_CU_BETA = os.environ.get("VISION_CU_BETA_FLAG", "computer-use-2025-11-24")
_CU_SYSTEM = (
    "You are JARVIS, an AI assistant with REAL-TIME VISION controlling a\n"
    "macOS desktop. You can SEE the screen at 60fps. You are NOT blind.\n"
    "Look at what is on screen and act on what you see — like a human would.\n\n"

    "VISION-FIRST PRINCIPLE:\n"
    "You receive live screenshots of the desktop. LOOK at them carefully.\n"
    "If you can SEE the element you need (a contact name, a button, a field),\n"
    "click it directly. Do NOT search for things you can already see.\n"
    "Only use search bars when the target is genuinely not visible on screen.\n\n"

    "INTERACTION DISCIPLINE:\n"
    "1. ONE action at a time. Take a screenshot after EVERY action.\n"
    "2. Verify the result before moving on. If the screen didn't change as\n"
    "   expected, diagnose why and retry — don't blindly continue.\n"
    "3. Before typing ANYTHING, verify the correct field has focus (cursor\n"
    "   blinking in the right input). If unsure, click the field first.\n\n"

    "MACOS TIPS:\n"
    "- Use Cmd+Space for Spotlight to launch apps quickly.\n"
    "- After launching an app, WAIT and take a screenshot to confirm it loaded.\n\n"

    "CHAT APPS (WhatsApp, Messages, Slack, Telegram):\n"
    "1. LOOK at the screen. If the contact is visible in the chat list or\n"
    "   sidebar, click their name DIRECTLY. Do not use the search bar.\n"
    "2. Only search if the contact is NOT visible after scrolling or looking.\n"
    "3. Once the conversation is open, click the message input field at the\n"
    "   bottom. Verify the cursor is in the message field, not the search bar.\n"
    "4. Type the message. Take a screenshot to verify the text is correct.\n"
    "5. Press Return to send. Take a screenshot to confirm the message\n"
    "   appears as a sent bubble. Only then is the task complete.\n"
)



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
        # Computer Use coordinate scale (logical_screen / cu_display)
        self._cu_scale: Optional[Tuple[float, float]] = None
        # Ferrari Engine: frame_server subprocess for real-time capture
        self._frame_server_proc: Any = None  # asyncio.subprocess.Process
        self._frame_server_ready: bool = False
        # Visual Telemetry: monotonic perception counter
        self._perception_seq: int = 0
        # SHM direct reader (in-process SCK capture, no subprocess TCC issues)
        self._shm_reader: Any = None

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
        """Cascade: CU native API → Agentic fallback (reverse-engineered CU arch)."""
        # PRIMARY: Claude Computer Use native API (best accuracy)
        if _CU_ENABLED and os.environ.get("ANTHROPIC_API_KEY"):
            try:
                result = await self._loop_computer_use(goal)
                if result is not None:
                    return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[LeanVision] CU API failed: %s — trying agentic fallback", exc,
                )

        # FALLBACK: Reverse-engineered CU architecture (any vision model)
        result = await self._loop_agentic(goal)
        if result is not None:
            return result

        return {
            "success": False,
            "result": "All vision paths exhausted (CU + Agentic)",
            "turns": 0,
            "action_log": [],
        }

    # ==================================================================
    # Agentic Fallback — reverse-engineered CU architecture for any model
    # ==================================================================

    async def _loop_agentic(self, goal: str) -> Optional[Dict[str, Any]]:
        """Multi-turn vision loop that mirrors Claude's CU architecture.

        Key differences from the old single-shot legacy loop:
        - VISUAL MEMORY: recent screenshots included in every prompt so the
          model can see what happened after its previous actions.
        - CU RESOLUTION: PNG at 1280x800 (not JPEG 1024).
        - CU ACTION VOCAB: full action set (double-click, drag, modifier keys).
        - CU INFRASTRUCTURE: shares capture, coords, actions, and settle time
          with the native CU path.
        - PROVIDER CASCADE: Claude Vision → Doubleword → J-Prime.

        Returns result dict or None if all providers fail on turn 1.
        """
        action_log: List[Dict[str, Any]] = []
        # Visual history: list of (b64_png, annotation) — the model sees
        # recent screenshots so it can verify what happened after each action.
        frames: List[Tuple[str, str]] = []
        capture_failures = 0
        # VisionCortex scene context (injected on first turn)
        scene_context = self._get_scene_context()
        # B.2: Bridge VisionActionLoop state machine
        self._bridge_val_state(active=True)

        for turn in range(1, _CU_MAX_TURNS + 1):
            turn_start = time.monotonic()
            logger.info(
                "[LeanVision:AG] --- Turn %d/%d --- goal: %s",
                turn, _CU_MAX_TURNS, goal[:60],
            )

            # ---- 1. CAPTURE (CU infrastructure: PNG 1280x800) ----
            b64_png = await self._capture_cu_screenshot()
            if b64_png is None:
                capture_failures += 1
                logger.error(
                    "[LeanVision:AG] Turn %d: CAPTURE failed (%d)",
                    turn, capture_failures,
                )
                if capture_failures >= 3:
                    return {
                        "success": False,
                        "result": "Screen capture broken after 3 failures",
                        "turns": turn,
                        "action_log": action_log,
                    }
                await asyncio.sleep(1.0)
                continue
            capture_failures = 0

            annotation = ""
            if action_log:
                last = action_log[-1]
                annotation = (
                    f"After: {last.get('action', '?')} "
                    f"→ {last.get('result', '?')}"
                )
            frames.append((b64_png, annotation))

            # ---- 2. THINK (multi-image visual history → model cascade) ----
            response = await self._agentic_think(
                goal, frames, action_log, turn,
                scene_context=scene_context if turn == 1 else "",
            )
            if response is None:
                if turn == 1:
                    return None  # Signal: all providers failed, outer loop handles
                return {
                    "success": False,
                    "result": "All vision providers failed",
                    "turns": turn,
                    "action_log": action_log,
                }

            reasoning = response.get("reasoning", "(no reasoning)")
            logger.info(
                "[LeanVision:AG] Turn %d: THINK → %s", turn, reasoning[:120],
            )

            # Goal achieved? (multi-signal fusion)
            if response.get("goal_achieved"):
                # v308.0: Guard against claiming success when no real actions
                # actually succeeded (e.g., all clicks/types failed due to
                # missing pyautogui, but model claims done anyway).
                _real_acts = [
                    a for a in action_log
                    if a.get("action") not in ("screenshot", "wait", "cursor_position")
                ]
                _any_real_ok = any(
                    a.get("result") == "success" for a in _real_acts
                )
                if (
                    turn == 1
                    and not action_log
                    and self._goal_requires_interaction(goal)
                ):
                    logger.warning(
                        "[LeanVision:AG] Turn 1: premature goal_achieved — overriding",
                    )
                    response["goal_achieved"] = False
                elif _real_acts and not _any_real_ok:
                    logger.warning(
                        "[LeanVision:AG] Model claims done but ALL %d real "
                        "actions failed — overriding to failure",
                        len(_real_acts),
                    )
                    response["goal_achieved"] = False
                else:
                    # Fuse model confidence with verification + history signals
                    fused_ok, fused_conf = self._fuse_goal_confidence(
                        model_says_done=True,
                        model_confidence=response.get("confidence", 0.8),
                        verification_status=action_log[-1].get("verification", "") if action_log else "",
                        turn=turn,
                        action_log=action_log,
                    )
                    if fused_ok:
                        logger.info(
                            "[LeanVision:AG] === GOAL ACHIEVED turn %d (fused_conf=%.2f) ===",
                            turn, fused_conf,
                        )
                        return {
                            "success": True,
                            "result": f"Goal achieved: {goal}",
                            "turns": turn,
                            "action_log": action_log,
                            "fused_confidence": fused_conf,
                        }
                    else:
                        logger.warning(
                            "[LeanVision:AG] Model claims done but fusion rejected "
                            "(fused_conf=%.2f < threshold) — continuing",
                            fused_conf,
                        )
                        response["goal_achieved"] = False

            next_action = response.get("next_action")
            if not next_action:
                return {
                    "success": False,
                    "result": reasoning,
                    "turns": turn,
                    "action_log": action_log,
                }

            # Stagnation guard (works with both legacy and CU key formats)
            if self._is_stagnant_agentic(action_log, next_action):
                logger.warning("[LeanVision:AG] Stagnation detected")
                return {
                    "success": False,
                    "result": "Stagnation: repeating same action",
                    "turns": turn,
                    "action_log": action_log,
                }

            # ---- 3. ACT (CU action executor: full vocabulary) ----
            action_name = next_action.get(
                "action", next_action.get("action_type", "left_click"),
            )
            params = self._translate_to_cu_params(next_action)

            # ---- PRECHECK (idempotency + freshness + risk) ----
            precheck_fail = self._precheck_action(action_name, params, turn)
            if precheck_fail:
                action_log.append({
                    "turn": turn, "action": action_name,
                    "params": {k: v for k, v in params.items() if k != "action"},
                    "target": next_action.get("target", ""),
                    "result": f"blocked: {precheck_fail}",
                    "verification": "skipped", "reasoning": reasoning,
                    "elapsed_s": round(time.monotonic() - turn_start, 2),
                })
                continue  # Skip to next turn — model will see "blocked" in history

            # Save pre-action frame for verification
            pre_frame = self._b64_to_numpy(b64_png)

            ok, err = await self._execute_cu_action(action_name, params)
            elapsed = time.monotonic() - turn_start

            await asyncio.sleep(_CU_SETTLE_S)

            # ---- 4. VERIFY (pixel-diff post-action check) ----
            verification = "skipped"
            post_b64 = await self._capture_cu_screenshot()
            if pre_frame is not None and post_b64 is not None:
                post_frame = self._b64_to_numpy(post_b64)
                if post_frame is not None:
                    verification = self._verify_cu_action(
                        action_name, pre_frame, post_frame, params,
                    )
                    if verification == "fail" and ok:
                        logger.warning(
                            "[LeanVision:AG] Turn %d: action reported success "
                            "but verification FAILED (no pixel change)",
                            turn,
                        )

            # ---- 5. SCENE WRITE-BACK (cache successful element coords) ----
            if ok and verification != "fail":
                self._write_scene_cache(action_name, params, next_action)

            action_log.append({
                "turn": turn,
                "action": action_name,
                "params": {
                    k: v for k, v in params.items() if k != "action"
                },
                "target": next_action.get("target", ""),
                "result": "success" if ok else f"error: {err}",
                "verification": verification,
                "reasoning": reasoning,
                "elapsed_s": round(elapsed, 2),
            })

            # ---- NARRATE (fire-and-forget voice feedback) ----
            asyncio.ensure_future(self._narrate_action(
                action_name, next_action.get("target", "")[:40],
                "success" if ok else f"error: {err}",
            ))

            # ---- B.2: METRICS (VisionActionLoop-compatible telemetry) ----
            self._emit_val_metric(
                action_name, params, next_action.get("target", ""),
                ok, verification, turn, elapsed * 1000,
            )

            logger.info(
                "[LeanVision:AG] Turn %d: %s → %s (%.1fs)",
                turn, action_name,
                "OK" if ok else f"FAIL: {err}",
                elapsed,
            )

        logger.warning(
            "[LeanVision:AG] Max turns (%d) exhausted", _CU_MAX_TURNS,
        )
        self._bridge_val_state(active=False)
        return {
            "success": False,
            "result": f"Max turns ({_CU_MAX_TURNS}) exhausted",
            "turns": _CU_MAX_TURNS,
            "action_log": action_log,
        }

    # ------------------------------------------------------------------
    # Agentic — multi-image THINK (provider cascade)
    # ------------------------------------------------------------------

    async def _agentic_think(
        self,
        goal: str,
        frames: List[Tuple[str, str]],
        action_log: List[Dict[str, Any]],
        turn: int,
        scene_context: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Send recent screenshots + action log to vision model cascade.

        Returns parsed response or None if all providers fail.
        """
        system_prompt = self._build_agentic_system_prompt()
        content = self._build_agentic_content(
            goal, frames, action_log, turn, scene_context=scene_context,
        )

        # --- Provider cascade ---
        # 1. Claude Vision (standard API, multi-image)
        client = self._get_client()
        if client:
            result = await self._agentic_ask_claude(system_prompt, content)
            if result is not None:
                return result

        # 2. Doubleword VL-235B
        if _DOUBLEWORD_API_KEY:
            result = await self._agentic_ask_doubleword(
                system_prompt, content,
            )
            if result is not None:
                return result

        # 3. J-Prime LLaVA (GCP GPU — zero API cost)
        result = await self._agentic_ask_jprime(system_prompt, content)
        if result is not None:
            return result

        return None

    def _build_agentic_system_prompt(self) -> str:
        """System prompt that teaches CU-compatible action vocabulary."""
        return (
            "You are JARVIS, a precise UI automation agent on macOS.\n\n"
            f"Screenshots are {_CU_DISPLAY_W}x{_CU_DISPLAY_H} px. "
            "Coordinates are [x, y] in this image's pixel space.\n\n"
            "AVAILABLE ACTIONS:\n"
            "- left_click: click at [x, y]\n"
            "- double_click: double-click at [x, y]\n"
            "- right_click: right-click at [x, y]\n"
            "- type: type text (uses clipboard paste)\n"
            "- key: press key or combo (e.g., 'return', 'command+v')\n"
            "- scroll: scroll at [x, y], set scroll_direction + scroll_amount\n"
            "- mouse_move: move cursor to [x, y]\n"
            "- wait: pause N seconds\n\n"
            "Return ONLY a JSON object (no markdown, no explanation):\n"
            "{\n"
            '  "goal_achieved": boolean,\n'
            '  "next_action": {\n'
            '    "action": "left_click",\n'
            '    "coordinate": [x, y],\n'
            '    "text": "...",\n'
            '    "target": "human description of element"\n'
            "  },\n"
            '  "reasoning": "one-line explanation",\n'
            '  "confidence": 0.0 to 1.0\n'
            "}\n\n"
            "VISION-FIRST PRINCIPLE:\n"
            "You have REAL-TIME VISION. You can SEE the screen. LOOK at the\n"
            "screenshots carefully. If the element you need (a contact, button,\n"
            "field) is VISIBLE on screen, click it directly at its coordinates.\n"
            "Do NOT search for things you can already see.\n\n"
            "CRITICAL RULES:\n"
            "- COMPARE the screenshots — the LAST one is the CURRENT state.\n"
            "- Earlier screenshots show what the screen looked like BEFORE and "
            "AFTER your previous actions — use them to verify progress.\n"
            "- goal_achieved=true ONLY when the ENTIRE goal is satisfied.\n"
            "- If a previous action had no effect, try a different approach.\n"
            "- Be precise with [x, y] — look carefully at element centers.\n\n"
            "CHAT APPS (WhatsApp, Messages, Slack, Telegram):\n"
            "1. LOOK at the screen. If the contact is visible in the chat list,\n"
            "   click their name directly. Do NOT use the search bar.\n"
            "2. Only search if the contact is NOT visible on screen.\n"
            "3. Click the message input field at the bottom. Verify focus.\n"
            "4. Type the message. Press Return. Verify the sent bubble.\n"
            "- goal_achieved=true ONLY when message is visibly sent.\n"
        )

    def _build_agentic_content(
        self,
        goal: str,
        frames: List[Tuple[str, str]],
        action_log: List[Dict[str, Any]],
        turn: int,
        scene_context: str = "",
    ) -> list:
        """Build multi-image content with annotated screenshot history."""
        # Include last 3 screenshots for visual context (≈3K-4.5K tokens)
        recent = frames[-3:]
        offset = max(0, len(frames) - 3)

        content: list = []
        for i, (b64_png, annotation) in enumerate(recent):
            label = f"[Screenshot {offset + i + 1}"
            if annotation:
                label += f" — {annotation}"
            if i == len(recent) - 1:
                label += " — CURRENT"
            label += "]"
            content.append({"type": "text", "text": label})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64_png,
                },
            })

        # Action history as text
        history_lines = []
        for entry in action_log:
            t = entry.get("turn", "?")
            act = entry.get("action", "?")
            params = entry.get("params", {})
            result = entry.get("result", "?")
            coord = params.get("coordinate", "")
            text = params.get("text", "")
            parts = [f"Turn {t}: {act}"]
            if coord:
                parts.append(f"at {coord}")
            if text:
                parts.append(f"text='{text[:30]}'")
            verification = entry.get("verification", "")
            parts.append(f"→ {result}")
            if verification and verification not in ("skipped", "success"):
                parts.append(f"[verify: {verification}]")
            history_lines.append("  " + " ".join(parts))
        history = (
            "\n".join(history_lines) if history_lines
            else "  (first turn — no prior actions)"
        )

        context_line = ""
        if scene_context:
            context_line = f"SCENE CONTEXT: {scene_context}\n\n"

        user_text = (
            f"GOAL: {goal}\n\n"
            f"{context_line}"
            f"TURN: {turn}/{_CU_MAX_TURNS}\n\n"
            f"ACTION HISTORY:\n{history}\n\n"
            "The screenshots above show your screen. The LAST is current.\n"
            "Analyze what you see and return the next action as JSON."
        )
        content.append({"type": "text", "text": user_text})
        return content

    @staticmethod
    def _translate_to_cu_params(next_action: dict) -> dict:
        """Translate agentic response to CU-compatible action params."""
        params: Dict[str, Any] = {}
        action = next_action.get(
            "action", next_action.get("action_type", "left_click"),
        )
        params["action"] = action

        # Handle both "coordinate" and legacy "coords" keys
        coord = next_action.get("coordinate", next_action.get("coords"))
        if coord:
            params["coordinate"] = coord

        for key in ("text", "scroll_direction", "scroll_amount", "duration"):
            if key in next_action:
                params[key] = next_action[key]
        return params

    @staticmethod
    def _is_stagnant_agentic(
        action_log: List[Dict[str, Any]],
        next_action: Dict[str, Any],
    ) -> bool:
        """Stagnation detection for agentic loop (handles both key formats)."""
        if len(action_log) < _STAGNATION_WINDOW:
            return False

        proposed_key = (
            next_action.get("action", next_action.get("action_type")),
            next_action.get("target"),
            str(next_action.get("coordinate", next_action.get("coords"))),
        )
        for entry in action_log[-_STAGNATION_WINDOW:]:
            entry_key = (
                entry.get("action", entry.get("action_type")),
                entry.get("target"),
                str(entry.get("params", {}).get("coordinate", "")),
            )
            if entry_key != proposed_key:
                return False
        return True

    # ------------------------------------------------------------------
    # Agentic — provider adapters (multi-image capable)
    # ------------------------------------------------------------------

    async def _agentic_ask_claude(
        self, system_prompt: str, content: list,
    ) -> Optional[Dict[str, Any]]:
        """Claude Vision with multi-image content (standard API)."""
        client = self._get_client()
        if client is None:
            return None

        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model=_CLAUDE_MODEL,
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content}],
                ),
                timeout=45,
            )
            raw = response.content[0].text if response.content else ""
            logger.info("[LeanVision:AG] THINK via Claude %s", _CLAUDE_MODEL)
            return self._parse_response(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[LeanVision:AG] Claude error: %s", exc)
            return None

    async def _agentic_ask_doubleword(
        self, system_prompt: str, content: list,
    ) -> Optional[Dict[str, Any]]:
        """Doubleword VL-235B with multi-image content (OpenAI-compat API)."""
        if not _DOUBLEWORD_API_KEY:
            return None

        if not hasattr(self, "_dw_session") or self._dw_session is None:
            try:
                import aiohttp
                self._dw_session = aiohttp.ClientSession()
            except ImportError:
                return None

        # Convert Anthropic content format → OpenAI-compatible
        oai_content: list = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "image":
                    oai_content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{item['source']['data']}",
                        },
                    })
                elif item.get("type") == "text":
                    oai_content.append({
                        "type": "text",
                        "text": item["text"],
                    })

        payload = {
            "model": _DOUBLEWORD_VISION_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": oai_content},
            ],
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
                    logger.warning(
                        "[LeanVision:AG] Doubleword HTTP %d: %s",
                        resp.status, body[:200],
                    )
                    return None
                data = await resp.json()
                raw = data["choices"][0]["message"].get("content", "")
                logger.info("[LeanVision:AG] THINK via Doubleword VL-235B")
                return self._parse_response(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[LeanVision:AG] Doubleword error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Agentic — J-Prime LLaVA provider (GCP GPU, zero API cost)
    # ------------------------------------------------------------------

    async def _agentic_ask_jprime(
        self, system_prompt: str, content: list,
    ) -> Optional[Dict[str, Any]]:
        """J-Prime LLaVA/32B on GCP GPU (zero API cost fallback)."""
        endpoint = os.environ.get("JARVIS_JPRIME_VISION_URL", "")
        if not endpoint:
            # Try default GCP VM
            endpoint = "http://136.113.252.164:8000"

        # Quick reachability check — don't burn 30s on a dead endpoint
        if not hasattr(self, "_jprime_reachable"):
            self._jprime_reachable = None
        if self._jprime_reachable is False:
            return None

        if not hasattr(self, "_jp_session") or self._jp_session is None:
            try:
                import aiohttp
                self._jp_session = aiohttp.ClientSession()
            except ImportError:
                return None

        # Convert content to OpenAI-compatible format (same as Doubleword)
        oai_content: list = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "image":
                    oai_content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{item['source']['data']}",
                        },
                    })
                elif item.get("type") == "text":
                    oai_content.append({"type": "text", "text": item["text"]})

        payload = {
            "model": os.environ.get("JARVIS_JPRIME_VISION_MODEL", "llava-v1.5"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": oai_content},
            ],
            "max_tokens": 2048,
            "temperature": 0.1,
        }

        try:
            import aiohttp
            async with self._jp_session.post(
                f"{endpoint}/v1/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    self._jprime_reachable = False
                    return None
                self._jprime_reachable = True
                data = await resp.json()
                raw = data["choices"][0]["message"].get("content", "")
                logger.info("[LeanVision:AG] THINK via J-Prime LLaVA")
                return self._parse_response(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[LeanVision:AG] J-Prime error: %s", exc)
            self._jprime_reachable = False
            return None

    # ------------------------------------------------------------------
    # Agentic — ActionVerifier integration
    # ------------------------------------------------------------------

    @staticmethod
    def _b64_to_numpy(b64_png: str) -> Optional["np.ndarray"]:
        """Decode base64 PNG to numpy array for verification."""
        try:
            import numpy as np
            from PIL import Image
            img = Image.open(io.BytesIO(base64.b64decode(b64_png)))
            return np.array(img.convert("RGB"))
        except Exception:
            return None

    @staticmethod
    def _verify_cu_action(
        action: str,
        pre_frame: "np.ndarray",
        post_frame: "np.ndarray",
        params: dict,
    ) -> str:
        """Run pixel-diff verification. Returns 'success', 'fail', or 'inconclusive'."""
        try:
            from backend.vision.realtime.verification import (
                ActionVerifier, VerificationStatus,
            )
        except ImportError:
            return "skipped"

        verifier = ActionVerifier()
        coord = params.get("coordinate")

        if action in (
            "left_click", "right_click", "double_click",
            "triple_click", "middle_click",
        ) and coord:
            result = verifier.verify_click(
                pre_frame, post_frame,
                coords=(int(coord[0]), int(coord[1])),
            )
        elif action == "scroll":
            result = verifier.verify_scroll(pre_frame, post_frame)
        else:
            # Type, key, etc. — check full-frame diff
            import numpy as _np
            diff = float(_np.mean(_np.abs(
                post_frame.astype(_np.float32) - pre_frame.astype(_np.float32),
            )))
            if diff > 2.0:
                return "success"
            return "fail"

        return result.status.value

    # ------------------------------------------------------------------
    # Agentic — L1 Scene Graph write-back
    # ------------------------------------------------------------------

    def _write_scene_cache(
        self, action: str, params: dict, next_action: dict,
    ) -> None:
        """Write successful element + coords to KnowledgeFabric scene graph."""
        target = next_action.get("target", "")
        coord = params.get("coordinate")
        if not target or not coord:
            return

        try:
            # Reuse VAL's fabric if available, else create/cache our own
            if not hasattr(self, "_knowledge_fabric") or self._knowledge_fabric is None:
                try:
                    from backend.vision.realtime.vision_action_loop import VisionActionLoop
                    val = VisionActionLoop.get_instance()
                    if val:
                        self._knowledge_fabric = val.knowledge_fabric
                except Exception:
                    pass
                if not hasattr(self, "_knowledge_fabric") or self._knowledge_fabric is None:
                    from backend.knowledge.fabric import KnowledgeFabric
                    self._knowledge_fabric = KnowledgeFabric()
            fabric = self._knowledge_fabric
            entity_id = f"kg://scene/element/{target.lower().replace(' ', '_')[:50]}"
            fabric.write(
                entity_id,
                {
                    "target": target,
                    "coordinates": coord,
                    "action": action,
                    "timestamp": time.time(),
                },
                ttl_seconds=30,  # Cache for 30s (UI can change)
            )
            logger.debug("[LeanVision:AG] Scene cache: %s → %s", target[:30], coord)
        except Exception:
            pass  # Non-critical — don't block on cache failure

    # ------------------------------------------------------------------
    # Agentic — VisionCortex scene context
    # ------------------------------------------------------------------

    @staticmethod
    def _get_scene_context() -> str:
        """Query VisionCortex for current foreground app / scene state."""
        try:
            from backend.vision.realtime.vision_cortex import VisionCortex
            cortex = VisionCortex.get_instance() if hasattr(VisionCortex, "get_instance") else None
            if cortex is None:
                return ""
            # Try to get the current foreground app
            if hasattr(cortex, "_current_app") and cortex._current_app:
                return f"Foreground app: {cortex._current_app}"
            if hasattr(cortex, "_last_scene_summary") and cortex._last_scene_summary:
                return cortex._last_scene_summary[:200]
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # B.2: VisionActionLoop Unification (shared infrastructure bridges)
    # ------------------------------------------------------------------

    def _bridge_val_state(self, active: bool) -> None:
        """Bridge VisionActionLoop state machine — update when lean_loop starts/stops.

        This keeps the TUI dashboard and telemetry aware of vision activity
        without merging control flows.
        """
        try:
            from backend.vision.realtime.vision_action_loop import VisionActionLoop
            from backend.vision.realtime.states import VisionEvent
            val = VisionActionLoop.get_instance()
            if val is None:
                return
            if active and val.state.value == "IDLE":
                val._state_machine.transition(VisionEvent.START)
                logger.debug("[LeanVision:VAL] Bridge: IDLE → WATCHING")
            elif not active and val.state.value != "IDLE":
                val._state_machine.transition(VisionEvent.STOP)
                logger.debug("[LeanVision:VAL] Bridge: → IDLE")
        except Exception:
            pass  # Non-critical

    def _emit_val_metric(
        self, action_name: str, params: dict, target: str,
        ok: bool, verification: str, turn: int, elapsed_ms: float,
        tier_used: str = "agentic",
    ) -> None:
        """Emit a VisionActionLoop-compatible metric record.

        Uses the same ``build_action_record`` format as VisionActionLoop
        so metrics appear in the same telemetry stream.
        """
        try:
            from backend.vision.realtime.metrics import build_action_record
            from backend.vision.realtime.vision_action_loop import VisionActionLoop

            record = build_action_record(
                action_id=f"ag-{turn}-{action_name}",
                target_description=target,
                coords=tuple(params["coordinate"]) if "coordinate" in params else None,
                confidence=0.8,
                precheck_passed=True,
                action_type=action_name,
                backend_used="ghost_hands+pyautogui",
                latency_ms=elapsed_ms,
                verification_result=verification,
                retry_count=0,
                tier_used=tier_used,
                success=ok,
            )

            # Emit via VAL callback if available
            val = VisionActionLoop.get_instance()
            if val and val.on_action_record:
                val.on_action_record(record)
            else:
                logger.debug("[LeanVision:VAL] Metric: %s %s → %s (%.0fms)",
                             action_name, target[:20], "ok" if ok else "fail", elapsed_ms)
        except Exception:
            pass  # Non-critical

    async def _try_val_frame_pipeline(self) -> Optional[str]:
        """Read latest frame from VisionActionLoop's FramePipeline (sub-10ms).

        VisionActionLoop starts a FramePipeline at boot (Zone 6.4b).
        Reading from it is faster than spawning frame_server or screencapture.
        Returns base64 PNG at CU resolution, or None if unavailable.
        """
        try:
            from backend.vision.realtime.vision_action_loop import VisionActionLoop
            val = VisionActionLoop.get_instance()
            if val is None or not val.frame_pipeline.is_running:
                return None

            frame = val.frame_pipeline.latest_frame
            if frame is None:
                return None

            from PIL import Image
            img = Image.fromarray(frame.data)
            if img.mode != "RGB":
                img = img.convert("RGB")

            logical_w, logical_h = self._get_logical_screen_size()
            if logical_w > 0 and logical_h > 0:
                self._cu_scale = (
                    logical_w / _CU_DISPLAY_W,
                    logical_h / _CU_DISPLAY_H,
                )
            else:
                self._cu_scale = (
                    frame.width / _CU_DISPLAY_W,
                    frame.height / _CU_DISPLAY_H,
                )

            img = img.resize(
                (_CU_DISPLAY_W, _CU_DISPLAY_H), Image.Resampling.LANCZOS,
            )
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            logger.debug("[LeanVision:VAL] Frame from FramePipeline (sub-10ms)")
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Agentic — PrecheckGate (idempotency + freshness + risk)
    # ------------------------------------------------------------------

    def _precheck_action(
        self, action_name: str, params: dict, turn: int,
    ) -> Optional[str]:
        """Run PrecheckGate guards before executing an action.

        Returns None if all guards pass, or a failure reason string.
        Lightweight checks only — no model calls, no I/O.
        """
        try:
            from backend.vision.realtime.precheck_gate import PrecheckGate
        except ImportError:
            return None  # Gate unavailable — pass through

        if not hasattr(self, "_precheck_gate"):
            self._precheck_gate = PrecheckGate()

        action_id = f"ag-{turn}-{action_name}-{id(params)}"
        result = self._precheck_gate.check(
            frame_age_ms=50.0,  # frame was just captured this turn (~50-200ms ago)
            fused_confidence=0.8,  # agentic loop uses model confidence
            action_id=action_id,
            action_type=action_name,
            target_task_type="browser_navigate",  # default safe
            intent_timestamp=time.time(),
            is_degraded=False,
        )

        if result.passed:
            # Commit so idempotency guard works on next check
            self._precheck_gate.commit_action(action_id)
            return None
        else:
            failed = ", ".join(result.failed_guards)
            logger.warning(
                "[LeanVision:AG] PrecheckGate BLOCKED: %s (action=%s)",
                failed, action_name,
            )
            return f"PrecheckGate: {failed}"

    # ------------------------------------------------------------------
    # Agentic — NarrationEngine (voice feedback during automation)
    # ------------------------------------------------------------------

    async def _narrate_action(
        self, action_name: str, target: str, result: str,
    ) -> None:
        """Fire-and-forget voice narration of vision actions.

        Non-blocking — narration failure never blocks the vision loop.
        Uses Ghost Hands NarrationEngine if available, falls back to safe_say.
        """
        try:
            from backend.ghost_hands.narration_engine import NarrationEngine
            engine = NarrationEngine.get_instance()
            if engine and engine._is_running:
                if result.startswith("error"):
                    await engine.narrate_error(
                        f"Action failed: {action_name} on {target}",
                    )
                else:
                    await engine.narrate_action(
                        f"{action_name} on {target}",
                    )
                return
        except Exception:
            pass

        # Fallback: use safe_say if NarrationEngine unavailable
        try:
            from backend.audio.safe_say import safe_say
            if action_name in ("left_click", "double_click"):
                safe_say(f"Clicking {target[:30]}")
            elif action_name == "type":
                safe_say("Typing text")
            elif action_name == "key":
                safe_say(f"Pressing {target[:20]}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Agentic — Fusion (multi-signal goal_achieved confidence)
    # ------------------------------------------------------------------

    @staticmethod
    def _fuse_goal_confidence(
        model_says_done: bool,
        model_confidence: float,
        verification_status: str,
        turn: int,
        action_log: List[Dict[str, Any]],
    ) -> Tuple[bool, float]:
        """Multi-signal fusion for goal_achieved decisions.

        Combines model confidence with verification signals and action
        history to produce a calibrated (achieved, confidence) tuple.
        Prevents premature goal_achieved claims.
        """
        if not model_says_done:
            return False, 0.0

        confidence = model_confidence

        # Penalty: if last action verification failed, distrust goal_achieved
        if action_log:
            last_verify = action_log[-1].get("verification", "")
            if last_verify == "fail":
                confidence *= 0.5
            elif last_verify == "success":
                confidence *= 1.1  # boost

        # Penalty: very early goal_achieved is suspicious
        if turn <= 2 and len(action_log) < 2:
            confidence *= 0.6

        # Penalty: no successful actions at all
        successful = sum(
            1 for e in action_log if e.get("result") == "success"
        )
        if successful == 0:
            confidence *= 0.3

        confidence = min(confidence, 1.0)

        # Only accept if fused confidence is above threshold
        threshold = float(os.environ.get("VISION_GOAL_CONFIDENCE_THRESHOLD", "0.5"))
        return confidence >= threshold, confidence

    # ==================================================================
    # Computer Use API — native multi-turn agent loop
    # ==================================================================

    async def _loop_computer_use(self, goal: str) -> Optional[Dict[str, Any]]:
        """Native Claude Computer Use agent loop (multi-turn, highest accuracy).

        Returns a result dict on success/failure, or ``None`` to signal
        that the caller should fall back to the legacy loop.
        """
        client = self._get_client()
        if client is None:
            return None

        tools: list = [
            {
                "type": _CU_TOOL_VERSION,
                "name": "computer",
                "display_width_px": _CU_DISPLAY_W,
                "display_height_px": _CU_DISPLAY_H,
            },
        ]

        messages: list = [{"role": "user", "content": goal}]
        action_log: List[Dict[str, Any]] = []

        for turn in range(1, _CU_MAX_TURNS + 1):
            logger.info("[LeanVision:CU] --- Turn %d/%d ---", turn, _CU_MAX_TURNS)

            try:
                kwargs: Dict[str, Any] = dict(
                    model=_CU_MODEL,
                    max_tokens=_CU_MAX_TOKENS,
                    system=_CU_SYSTEM,
                    tools=tools,
                    messages=messages,
                    betas=[_CU_BETA],
                )
                if _CU_THINKING_BUDGET > 0:
                    kwargs["thinking"] = {
                        "type": "enabled",
                        "budget_tokens": _CU_THINKING_BUDGET,
                    }

                response = await asyncio.wait_for(
                    client.beta.messages.create(**kwargs),
                    timeout=60,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[LeanVision:CU] API error turn %d: %s", turn, exc)
                if turn == 1:
                    return None  # Signal fallback to legacy
                return {
                    "success": False,
                    "result": f"Computer Use API error on turn {turn}: {exc}",
                    "turns": turn,
                    "action_log": action_log,
                }

            # Append Claude's full response (including thinking blocks)
            messages.append({"role": "assistant", "content": response.content})

            # Collect tool_use blocks
            tool_use_blocks = [
                b for b in response.content
                if getattr(b, "type", None) == "tool_use"
            ]

            # If no tool calls, Claude considers the task done
            if not tool_use_blocks:
                text_parts = []
                for b in response.content:
                    if getattr(b, "type", None) == "text":
                        text_parts.append(b.text)
                summary = " ".join(text_parts)
                logger.info(
                    "[LeanVision:CU] Task complete on turn %d: %s",
                    turn, summary[:120],
                )

                # v308.0: Action-based success detection.  The old keyword
                # matcher ("sent", "done", …) produced false positives when
                # Claude described a *hypothetical* plan containing those
                # words.  Instead, success requires that at least one REAL
                # action (not screenshot/wait/cursor_position) actually
                # succeeded during the session.
                _real_actions = [
                    a for a in action_log
                    if a.get("action") not in ("screenshot", "wait", "cursor_position")
                ]
                _any_real_succeeded = any(
                    a.get("result") == "success" for a in _real_actions
                )

                summary_lower = summary.lower()
                _failure_signals = any(
                    phrase in summary_lower
                    for phrase in (
                        "unable to", "can't", "cannot", "couldn't",
                        "not able to", "failed to", "unresponsive",
                        "would do", "here's what i",
                    )
                )

                if _failure_signals or not _any_real_succeeded:
                    success = False
                    logger.warning(
                        "[LeanVision:CU] Overriding to failure: "
                        "failure_signals=%s real_actions_succeeded=%s",
                        _failure_signals, _any_real_succeeded,
                    )
                else:
                    success = True

                return {
                    "success": success,
                    "result": summary or f"Goal completed: {goal}",
                    "turns": turn,
                    "action_log": action_log,
                }

            # Process each tool call
            tool_results: list = []
            for block in tool_use_blocks:
                if block.name != "computer":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Unknown tool: {block.name}",
                        "is_error": True,
                    })
                    continue

                action = block.input.get("action", "screenshot")
                logger.info(
                    "[LeanVision:CU] Turn %d: %s %s",
                    turn, action,
                    str({k: v for k, v in block.input.items() if k != "action"})[:100],
                )

                # Execute the action
                ok, err = await self._execute_cu_action(action, block.input)

                # Settle after non-screenshot actions
                if action not in ("screenshot", "wait", "cursor_position"):
                    await asyncio.sleep(_CU_SETTLE_S)

                # Capture post-action screenshot and return as tool_result
                b64_png = await self._capture_cu_screenshot()

                if b64_png is None:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Error: Screenshot capture failed.",
                        "is_error": True,
                    })
                elif not ok:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": [
                            {"type": "text", "text": f"Error: {action} failed: {err}"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64_png,
                                },
                            },
                        ],
                    })
                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64_png,
                                },
                            },
                        ],
                    })

                action_log.append({
                    "turn": turn,
                    "action": action,
                    "params": {
                        k: v for k, v in block.input.items() if k != "action"
                    },
                    "result": "success" if ok else f"error: {err}",
                })

            messages.append({"role": "user", "content": tool_results})
            self._prune_cu_screenshots(messages)

        return {
            "success": False,
            "result": f"Max turns ({_CU_MAX_TURNS}) exhausted",
            "turns": _CU_MAX_TURNS,
            "action_log": action_log,
        }

    # ------------------------------------------------------------------
    # Visual Telemetry (Principle 7: Absolute Observability)
    # ------------------------------------------------------------------
    # Every perception saves the exact frame the agent reasoned over.
    # The operator verifies OCR/model output against that artifact —
    # no host freezing, no temporal guessing.

    def _emit_perception_artifact(
        self, b64_png: str, source: str,
    ) -> Optional[str]:
        """Save the exact frame the agent perceived. Returns artifact path.

        Args:
            b64_png: The base64-encoded PNG the agent will reason over.
            source: Capture source identifier (val_pipeline, frame_server, screencapture).

        Returns:
            Absolute path to the saved artifact, or None on failure.
        """
        try:
            os.makedirs(_TELEMETRY_DIR, exist_ok=True)
            self._perception_seq += 1
            epoch_ms = int(time.time() * 1000)
            filename = f"{epoch_ms}_p{self._perception_seq:04d}.png"
            artifact_path = os.path.join(_TELEMETRY_DIR, filename)
            latest_path = os.path.join(_TELEMETRY_DIR, "vision_last_perception.png")

            raw = base64.b64decode(b64_png)

            # Atomic write: tmp → rename
            tmp_artifact = artifact_path + ".tmp"
            with open(tmp_artifact, "wb") as f:
                f.write(raw)
            os.replace(tmp_artifact, artifact_path)

            # Update latest pointer (atomic)
            tmp_latest = latest_path + ".tmp"
            with open(tmp_latest, "wb") as f:
                f.write(raw)
            os.replace(tmp_latest, latest_path)

            # Prune old artifacts beyond the rolling window
            self._prune_perception_artifacts()

            # Emit to TelemetryBus (non-blocking, optional)
            self._emit_perception_envelope(artifact_path, source)

            logger.info(
                "[VisionTelemetry] #%d | source=%s | %s",
                self._perception_seq, source, artifact_path,
            )
            return artifact_path

        except Exception as exc:
            logger.debug("[VisionTelemetry] artifact save failed: %s", exc)
            return None

    def _prune_perception_artifacts(self) -> None:
        """Keep only the last N timestamped perception PNGs."""
        try:
            entries = sorted(
                f for f in os.listdir(_TELEMETRY_DIR)
                if f.endswith(".png")
                and not f.startswith("vision_last_perception")
                and not f.endswith(".tmp")
            )
            for stale in entries[:-_TELEMETRY_MAX_ARTIFACTS]:
                try:
                    os.unlink(os.path.join(_TELEMETRY_DIR, stale))
                except OSError:
                    pass
        except OSError:
            pass

    def _emit_perception_envelope(
        self, artifact_path: str, source: str,
    ) -> None:
        """Emit a vision.perception@1.0.0 envelope to the TelemetryBus."""
        try:
            from backend.core.telemetry_contract import (
                TelemetryEnvelope,
                get_telemetry_bus,
            )
            envelope = TelemetryEnvelope.create(
                event_schema="vision.perception@1.0.0",
                source="lean_vision_loop",
                trace_id=str(uuid.uuid4()),
                span_id=f"perception-{self._perception_seq}",
                partition_key="vision",
                payload={
                    "perception_seq": self._perception_seq,
                    "artifact_path": artifact_path,
                    "capture_source": source,
                    "capture_epoch_ms": int(time.time() * 1000),
                },
            )
            get_telemetry_bus().emit(envelope)
        except Exception:
            pass  # TelemetryBus unavailable — observability degrades, agent continues

    # ------------------------------------------------------------------
    # Computer Use — screenshot capture (PNG at CU display resolution)
    # ------------------------------------------------------------------

    async def _capture_cu_screenshot(self) -> Optional[str]:
        """Capture screen as PNG at CU display resolution.

        Capture cascade (fastest first, in-process first):
        0. VisionActionLoop FramePipeline (if running) — sub-10ms
        1. SHM ring buffer direct read (VisionActivator's SCK)  — sub-10ms
        2. Ferrari Engine (frame_server subprocess via Quartz)   — ~50ms
        3. screencapture subprocess                              — ~200ms

        v308.0: Added SHM direct read at tier 1.  When JARVIS runs as a
        launchd agent (PPID=1), subprocess-based capture (tiers 2-3)
        fails because the child process inherits a different TCC context
        that lacks Screen Recording permission.  SHM is in-process — it
        reads from the ScreenCaptureKit pipeline that VisionActivator
        starts, which has the correct TCC grant.
        """
        # --- Tier 0: VisionActionLoop FramePipeline (sub-10ms) ---
        result = await self._try_val_frame_pipeline()
        if result is not None:
            self._emit_perception_artifact(result, "val_pipeline")
            return result

        # --- Tier 1: SHM direct read (in-process, SCK-backed) ---
        result = self._try_shm_direct()
        if result is not None:
            self._emit_perception_artifact(result, "shm_direct")
            return result

        # --- Tier 2: Ferrari Engine (frame_server subprocess) ---
        result = await self._try_frame_server_capture()
        if result is not None:
            self._emit_perception_artifact(result, "frame_server")
            return result

        # --- Tier 3: screencapture subprocess (last resort) ---
        result = await self._screencapture_fallback()
        if result is not None:
            self._emit_perception_artifact(result, "screencapture")
        return result

    def _try_shm_direct(self) -> Optional[str]:
        """Read latest frame directly from the SHM ring buffer.

        The SHM segment is written by ScreenCaptureKit (via FramePipeline
        or the native C++ capture path). Reading is in-process — no
        subprocess spawn, no TCC inheritance issue.  This is the
        structural fix for launchd-launched JARVIS where subprocess-based
        capture fails due to macOS TCC permission inheritance.
        """
        try:
            from backend.vision.shm_frame_reader import ShmFrameReader
        except ImportError:
            return None

        if self._shm_reader is None:
            reader = ShmFrameReader()
            if not reader.open():
                return None
            self._shm_reader = reader

        try:
            frame, counter = self._shm_reader.read_latest()
            if frame is None:
                return None

            from PIL import Image
            # SHM frames are BGRA (4ch) from SCK — convert to RGB
            if frame.ndim == 3 and frame.shape[2] == 4:
                frame = frame[:, :, [2, 1, 0]]  # BGRA → RGB (drop alpha)

            img = Image.fromarray(frame)
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Track coordinate scale
            logical_w, logical_h = self._get_logical_screen_size()
            if logical_w > 0 and logical_h > 0:
                self._cu_scale = (
                    logical_w / _CU_DISPLAY_W,
                    logical_h / _CU_DISPLAY_H,
                )
            else:
                self._cu_scale = (
                    img.width / _CU_DISPLAY_W,
                    img.height / _CU_DISPLAY_H,
                )

            img = img.resize(
                (_CU_DISPLAY_W, _CU_DISPLAY_H), Image.Resampling.LANCZOS,
            )
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            logger.debug("[LeanVision:SHM] Frame from SHM direct read (sub-10ms)")
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:
            logger.debug("[LeanVision:SHM] SHM read failed: %s", exc)
            return None

    async def _try_frame_server_capture(self) -> Optional[str]:
        """Read latest frame from frame_server subprocess (~50ms, Quartz CGWindowListCreateImage).

        frame_server.py runs as a persistent subprocess using Quartz (safe in its
        own process, no CFRunLoop conflict). It writes the latest JPEG to
        /tmp/claude/latest_frame.jpg via atomic rename.
        """
        frame_path = os.path.join(_TMP_DIR, "latest_frame.jpg")
        meta_path = os.path.join(_TMP_DIR, "latest_frame.json")

        # Auto-start frame_server if not running
        if not self._frame_server_ready:
            await self._ensure_frame_server()

        if not self._frame_server_ready:
            return None

        try:
            # Check freshness — skip if frame is older than 2 seconds
            if os.path.exists(meta_path):
                meta_stat = os.stat(meta_path)
                age = time.time() - meta_stat.st_mtime
                if age > 2.0:
                    logger.debug("[Ferrari] Frame stale (%.1fs old), falling back", age)
                    return None

            if not os.path.exists(frame_path):
                return None

            from PIL import Image
            img = Image.open(frame_path)
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Track scale for coordinate mapping
            logical_w, logical_h = self._get_logical_screen_size()
            if logical_w > 0 and logical_h > 0:
                self._cu_scale = (
                    logical_w / _CU_DISPLAY_W,
                    logical_h / _CU_DISPLAY_H,
                )
            else:
                cap_w, cap_h = img.size
                self._cu_scale = (
                    cap_w / _CU_DISPLAY_W, cap_h / _CU_DISPLAY_H,
                )

            # Resize to CU display resolution
            img = img.resize(
                (_CU_DISPLAY_W, _CU_DISPLAY_H), Image.Resampling.LANCZOS,
            )

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            logger.debug("[Ferrari] Frame captured via frame_server (sub-50ms)")
            return base64.b64encode(buf.getvalue()).decode("ascii")

        except Exception as exc:
            logger.debug("[Ferrari] Frame read error: %s", exc)
            return None

    async def _ensure_frame_server(self) -> None:
        """Auto-start the frame_server subprocess if not already running."""
        # Check if frame_server is already alive
        if (
            self._frame_server_proc is not None
            and self._frame_server_proc.returncode is None
        ):
            self._frame_server_ready = True
            return

        # Find the frame_server script
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "frame_server.py",
        )
        if not os.path.exists(script):
            logger.debug("[Ferrari] frame_server.py not found at %s", script)
            return

        try:
            import sys as _sys
            python = _sys.executable or "python3"
            self._frame_server_proc = await asyncio.create_subprocess_exec(
                python, script,
                "--fps", "10",
                "--quality", "0.85",
                "--max-dim", str(max(_CU_DISPLAY_W, _CU_DISPLAY_H)),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            # Wait for "ready" signal (first line of stdout)
            try:
                line = await asyncio.wait_for(
                    self._frame_server_proc.stdout.readline(),
                    timeout=5.0,
                )
                if line:
                    data = json.loads(line.decode().strip())
                    if data.get("ok"):
                        self._frame_server_ready = True
                        logger.info(
                            "[Ferrari] frame_server started (pid=%d, Quartz CGWindowListCreateImage)",
                            data.get("pid", 0),
                        )
                        return
            except (asyncio.TimeoutError, json.JSONDecodeError, Exception) as exc:
                logger.warning("[Ferrari] frame_server startup failed: %s", exc)

            # Startup failed — kill it
            try:
                self._frame_server_proc.terminate()
            except ProcessLookupError:
                pass
            self._frame_server_proc = None

        except Exception as exc:
            logger.debug("[Ferrari] Could not start frame_server: %s", exc)

    async def _screencapture_fallback(self) -> Optional[str]:
        """Fallback: capture via screencapture subprocess (~200ms)."""
        os.makedirs(_TMP_DIR, exist_ok=True)
        tmp_path = os.path.join(_TMP_DIR, f"cu_{uuid.uuid4().hex[:8]}.png")

        try:
            proc = await asyncio.create_subprocess_exec(
                "screencapture", "-x", "-m", "-C", tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await asyncio.wait_for(proc.wait(), timeout=_CAPTURE_TIMEOUT_S)
            if rc != 0:
                logger.error("[LeanVision:CU] screencapture exit code %d", rc)
                return None

            from PIL import Image
            img = Image.open(tmp_path)
            if img.mode == "RGBA":
                img = img.convert("RGB")

            logical_w, logical_h = self._get_logical_screen_size()
            if logical_w > 0 and logical_h > 0:
                self._cu_scale = (
                    logical_w / _CU_DISPLAY_W,
                    logical_h / _CU_DISPLAY_H,
                )
            else:
                cap_w, cap_h = img.size
                self._cu_scale = (
                    cap_w / _CU_DISPLAY_W, cap_h / _CU_DISPLAY_H,
                )

            img = img.resize(
                (_CU_DISPLAY_W, _CU_DISPLAY_H), Image.Resampling.LANCZOS,
            )

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("ascii")

        except asyncio.TimeoutError:
            logger.error("[LeanVision:CU] screencapture timed out")
            return None
        except Exception as exc:
            logger.error("[LeanVision:CU] capture error: %s", exc)
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Computer Use — coordinate mapping
    # ------------------------------------------------------------------

    def _cu_to_screen(self, coord) -> Tuple[int, int]:
        """Convert CU pixel coordinates to actual screen coordinates."""
        if not coord or len(coord) < 2:
            return (0, 0)
        x, y = float(coord[0]), float(coord[1])
        if self._cu_scale:
            return (int(x * self._cu_scale[0]), int(y * self._cu_scale[1]))
        return (int(x), int(y))

    # ------------------------------------------------------------------
    # Computer Use — action execution (full vocabulary)
    # ------------------------------------------------------------------

    async def _execute_cu_action(
        self, action: str, params: dict,
    ) -> Tuple[bool, Optional[str]]:
        """Execute a Computer Use action. Returns (success, error_msg).

        Execution cascade: Ghost Hands (focus-preserving) → pyautogui (fallback).
        """
        try:
            if action in ("screenshot", "cursor_position"):
                return True, None

            coord = params.get("coordinate")
            sx, sy = self._cu_to_screen(coord) if coord else (0, 0)

            # --- Try Ghost Hands first (focus-preserving) ---
            gh_result = await self._try_cu_ghost_hands(action, sx, sy, params)
            if gh_result is not None:
                return gh_result

            if action in (
                "left_click", "right_click", "middle_click",
                "double_click", "triple_click",
            ):
                return await self._cu_click(sx, sy, action, params.get("text"))
            elif action == "type":
                return await self._cu_type(params.get("text", ""))
            elif action == "key":
                return await self._cu_key(params.get("text", ""))
            elif action == "scroll":
                direction = params.get("scroll_direction", "down")
                amount = params.get("scroll_amount", 3)
                return await self._cu_scroll(sx, sy, direction, amount)
            elif action == "mouse_move":
                return await self._cu_mouse_move(sx, sy)
            elif action == "left_click_drag":
                start = params.get("start_coordinate", coord)
                s_sx, s_sy = (
                    self._cu_to_screen(start) if start else (sx, sy)
                )
                return await self._cu_drag(s_sx, s_sy, sx, sy)
            elif action == "wait":
                dur = min(float(params.get("duration", 1)), 5)
                await asyncio.sleep(dur)
                return True, None
            elif action == "hold_key":
                return await self._cu_hold_key(
                    params.get("text", ""),
                    min(float(params.get("duration", 1)), 3),
                )
            elif action in ("left_mouse_down", "left_mouse_up"):
                return await self._cu_mouse_button(action, sx, sy)
            else:
                return False, f"Unsupported action: {action}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return False, str(exc)

    async def _try_cu_ghost_hands(
        self, action: str, sx: int, sy: int, params: dict,
    ) -> Optional[Tuple[bool, Optional[str]]]:
        """Try executing via Ghost Hands BackgroundActuator (focus-preserving).

        Returns (success, error) or None if Ghost Hands unavailable.
        """
        try:
            from backend.ghost_hands.background_actuator import (
                BackgroundActuator, Action, ActionType, ActionResult,
            )
        except ImportError:
            return None

        if not hasattr(self, "_ghost_hands") or self._ghost_hands is None:
            try:
                self._ghost_hands = BackgroundActuator()
                started = await self._ghost_hands.start()
                if not started:
                    self._ghost_hands = None
                    return None
                logger.info("[LeanVision:GH] Ghost Hands started")
            except Exception as exc:
                logger.debug("[LeanVision:GH] Ghost Hands init failed: %s", exc)
                self._ghost_hands = None
                return None

        try:
            gh_action: Optional[Action] = None
            if action in ("left_click", "double_click", "right_click"):
                atype = {
                    "left_click": ActionType.CLICK,
                    "double_click": ActionType.DOUBLE_CLICK,
                    "right_click": ActionType.RIGHT_CLICK,
                }.get(action, ActionType.CLICK)
                gh_action = Action(action_type=atype, coordinates=(sx, sy))
            elif action == "type":
                gh_action = Action(
                    action_type=ActionType.TYPE, text=params.get("text", ""),
                )
            elif action == "key":
                gh_action = Action(
                    action_type=ActionType.KEY, key=params.get("text", ""),
                )

            if gh_action is None:
                return None  # Unsupported action — fall through to pyautogui

            report = await asyncio.wait_for(
                self._ghost_hands.execute(gh_action), timeout=5.0,
            )
            success = report.result == ActionResult.SUCCESS
            if success:
                logger.info(
                    "[LeanVision:GH] %s (%d,%d) focus_preserved=%s",
                    action, sx, sy, report.focus_preserved,
                )
                return True, None
            else:
                logger.debug(
                    "[LeanVision:GH] %s failed: %s", action, report.error,
                )
                return None  # Fall through to pyautogui
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

    async def _cu_click(
        self, x: int, y: int, action: str, modifier: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Execute click variants via pyautogui."""
        import pyautogui

        clicks_map = {
            "left_click": 1, "double_click": 2, "triple_click": 3,
            "right_click": 1, "middle_click": 1,
        }
        clicks = clicks_map.get(action, 1)
        button = (
            "right" if action == "right_click"
            else "middle" if action == "middle_click"
            else "left"
        )

        if modifier:
            mod_key = self._map_cu_key(modifier)
            await asyncio.to_thread(pyautogui.keyDown, mod_key)
            try:
                await asyncio.to_thread(
                    pyautogui.click, x, y, clicks=clicks, button=button,
                )
            finally:
                await asyncio.to_thread(pyautogui.keyUp, mod_key)
        else:
            await asyncio.to_thread(
                pyautogui.click, x, y, clicks=clicks, button=button,
            )

        logger.info("[LeanVision:CU] %s (%d, %d)", action, x, y)
        return True, None

    async def _cu_type(self, text: str) -> Tuple[bool, Optional[str]]:
        """Type text via clipboard paste (reliable for special chars)."""
        if not text:
            return True, None
        proc = await asyncio.create_subprocess_exec(
            "pbcopy",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate(text.encode("utf-8"))
        paste = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "System Events" to keystroke "v" using command down',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await paste.wait()
        logger.info("[LeanVision:CU] type '%s'", text[:50])
        return True, None

    async def _cu_key(self, key_combo: str) -> Tuple[bool, Optional[str]]:
        """Press key or combo (e.g. 'Return', 'ctrl+s')."""
        import pyautogui

        if not key_combo:
            return False, "Empty key"
        parts = [self._map_cu_key(k.strip()) for k in key_combo.split("+")]
        if len(parts) == 1:
            await asyncio.to_thread(pyautogui.press, parts[0])
        else:
            await asyncio.to_thread(pyautogui.hotkey, *parts)
        logger.info("[LeanVision:CU] key '%s'", key_combo)
        return True, None

    async def _cu_scroll(
        self, x: int, y: int, direction: str, amount: int,
    ) -> Tuple[bool, Optional[str]]:
        """Scroll at position with direction control."""
        import pyautogui

        if x > 0 or y > 0:
            await asyncio.to_thread(pyautogui.moveTo, x, y)
        scroll_val = int(amount)
        if direction in ("down", "right"):
            scroll_val = -scroll_val
        await asyncio.to_thread(pyautogui.scroll, scroll_val)
        logger.info(
            "[LeanVision:CU] scroll %s %d at (%d,%d)",
            direction, amount, x, y,
        )
        return True, None

    async def _cu_mouse_move(
        self, x: int, y: int,
    ) -> Tuple[bool, Optional[str]]:
        """Move mouse cursor to position."""
        import pyautogui

        await asyncio.to_thread(pyautogui.moveTo, x, y)
        return True, None

    async def _cu_drag(
        self, sx: int, sy: int, ex: int, ey: int,
    ) -> Tuple[bool, Optional[str]]:
        """Click-drag from start to end coordinates."""
        import pyautogui

        await asyncio.to_thread(pyautogui.moveTo, sx, sy)
        await asyncio.to_thread(
            pyautogui.drag, ex - sx, ey - sy, duration=0.5,
        )
        logger.info("[LeanVision:CU] drag (%d,%d)->(%d,%d)", sx, sy, ex, ey)
        return True, None

    async def _cu_hold_key(
        self, key: str, duration: float,
    ) -> Tuple[bool, Optional[str]]:
        """Hold a key for a specified duration."""
        import pyautogui

        mapped = self._map_cu_key(key)
        await asyncio.to_thread(pyautogui.keyDown, mapped)
        await asyncio.sleep(duration)
        await asyncio.to_thread(pyautogui.keyUp, mapped)
        return True, None

    async def _cu_mouse_button(
        self, action: str, x: int, y: int,
    ) -> Tuple[bool, Optional[str]]:
        """Fine-grained mouse down/up control."""
        import pyautogui

        if x > 0 or y > 0:
            await asyncio.to_thread(pyautogui.moveTo, x, y)
        if action == "left_mouse_down":
            await asyncio.to_thread(pyautogui.mouseDown)
        else:
            await asyncio.to_thread(pyautogui.mouseUp)
        return True, None

    @staticmethod
    def _map_cu_key(key: str) -> str:
        """Map Claude Computer Use key names to pyautogui key names."""
        m = {
            "return": "return", "enter": "return",
            "super": "command", "meta": "command", "cmd": "command",
            "command": "command",
            "ctrl": "ctrl", "control": "ctrl",
            "alt": "alt", "option": "alt",
            "shift": "shift", "space": "space", "tab": "tab",
            "escape": "escape", "esc": "escape",
            "backspace": "backspace", "delete": "delete",
            "up": "up", "down": "down", "left": "left", "right": "right",
            "page_up": "pageup", "page_down": "pagedown",
            "home": "home", "end": "end",
        }
        return m.get(key.lower(), key.lower())

    # ------------------------------------------------------------------
    # Computer Use — conversation management
    # ------------------------------------------------------------------

    @staticmethod
    def _prune_cu_screenshots(messages: list) -> None:
        """Replace old base64 screenshots with placeholders to manage tokens.

        Keeps only the most recent ``_CU_PRUNE_AFTER`` images.
        """
        images: list = []
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                inner = item.get("content")
                if not isinstance(inner, list):
                    continue
                for sub in inner:
                    if isinstance(sub, dict) and sub.get("type") == "image":
                        images.append(sub)

        excess = len(images) - _CU_PRUNE_AFTER
        if excess > 0:
            for img_ref in images[:excess]:
                img_ref.clear()
                img_ref["type"] = "text"
                img_ref["text"] = "[earlier screenshot omitted]"

    # ------------------------------------------------------------------
    # Legacy: Step 1: CAPTURE
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
                "screencapture", "-x", "-m", "-C", tmp_path,
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
            img = img.resize((logical_w, logical_h), _Image.Resampling.LANCZOS)

        # If still over max dimension, downscale further
        cur_w, cur_h = img.size
        if max(cur_w, cur_h) > _MAX_IMAGE_DIM:
            ratio = _MAX_IMAGE_DIM / max(cur_w, cur_h)
            img = img.resize((int(cur_w * ratio), int(cur_h * ratio)), _Image.Resampling.LANCZOS)
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
