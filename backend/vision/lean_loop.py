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
    "You are JARVIS, an AI assistant controlling a macOS desktop.\n\n"
    "MACOS TIPS:\n"
    "- Use Cmd+Space for Spotlight to launch apps quickly\n"
    "- Prefer keyboard shortcuts over mouse for menus and dropdowns\n"
    "- After each action, take a screenshot to verify the result before moving on\n\n"
    "CHAT APP TIPS (WhatsApp, Messages, Slack, Telegram):\n"
    "- Use the search bar to find contacts — don't scroll through the list\n"
    "- Click the message input field before typing\n"
    "- Press Return to send the message\n"
    "- After sending, take a screenshot and verify the message appears as a sent bubble\n"
    "- Only declare the task complete when the message is visibly sent\n"
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

            # Goal achieved?
            if response.get("goal_achieved"):
                if (
                    turn == 1
                    and not action_log
                    and self._goal_requires_interaction(goal)
                ):
                    logger.warning(
                        "[LeanVision:AG] Turn 1: premature goal_achieved — overriding",
                    )
                    response["goal_achieved"] = False
                else:
                    logger.info(
                        "[LeanVision:AG] === GOAL ACHIEVED turn %d ===", turn,
                    )
                    return {
                        "success": True,
                        "result": f"Goal achieved: {goal}",
                        "turns": turn,
                        "action_log": action_log,
                    }

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

            ok, err = await self._execute_cu_action(action_name, params)
            elapsed = time.monotonic() - turn_start

            await asyncio.sleep(_CU_SETTLE_S)

            action_log.append({
                "turn": turn,
                "action": action_name,
                "params": {
                    k: v for k, v in params.items() if k != "action"
                },
                "target": next_action.get("target", ""),
                "result": "success" if ok else f"error: {err}",
                "reasoning": reasoning,
                "elapsed_s": round(elapsed, 2),
            })

            logger.info(
                "[LeanVision:AG] Turn %d: %s → %s (%.1fs)",
                turn, action_name,
                "OK" if ok else f"FAIL: {err}",
                elapsed,
            )

        logger.warning(
            "[LeanVision:AG] Max turns (%d) exhausted", _CU_MAX_TURNS,
        )
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
    ) -> Optional[Dict[str, Any]]:
        """Send recent screenshots + action log to vision model cascade.

        Returns parsed response or None if all providers fail.
        """
        system_prompt = self._build_agentic_system_prompt()
        content = self._build_agentic_content(
            goal, frames, action_log, turn,
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
            "CRITICAL RULES:\n"
            "- COMPARE the screenshots — the LAST one is the CURRENT state.\n"
            "- Earlier screenshots show what the screen looked like BEFORE and "
            "AFTER your previous actions — use them to verify progress.\n"
            "- goal_achieved=true ONLY when the ENTIRE goal is satisfied.\n"
            "- For chat apps: message must be SENT (visible as sent bubble).\n"
            "- Use Cmd+Space (Spotlight) to launch apps.\n"
            "- Use search bars to find contacts, not scrolling.\n"
            "- If a previous action had no effect, try a different approach.\n"
            "- Be precise with [x, y] — look carefully at element centers.\n"
        )

    def _build_agentic_content(
        self,
        goal: str,
        frames: List[Tuple[str, str]],
        action_log: List[Dict[str, Any]],
        turn: int,
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
            parts.append(f"→ {result}")
            history_lines.append("  " + " ".join(parts))
        history = (
            "\n".join(history_lines) if history_lines
            else "  (first turn — no prior actions)"
        )

        user_text = (
            f"GOAL: {goal}\n\n"
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
                success = any(
                    kw in summary.lower()
                    for kw in (
                        "complete", "done", "sent", "success",
                        "achieved", "finished", "accomplished",
                    )
                )
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
    # Computer Use — screenshot capture (PNG at CU display resolution)
    # ------------------------------------------------------------------

    async def _capture_cu_screenshot(self) -> Optional[str]:
        """Capture screen as PNG at CU display resolution.

        Capture cascade (fastest first):
        1. Ferrari Engine (frame_server subprocess via Quartz CGWindowListCreateImage)
           — ~50ms, 15fps continuous, zero temp files
        2. screencapture subprocess — ~200ms, reliable fallback
        """
        # --- Try Ferrari Engine first (frame_server atomic file) ---
        result = await self._try_frame_server_capture()
        if result is not None:
            return result

        # --- Fallback: screencapture subprocess ---
        return await self._screencapture_fallback()

    async def _try_frame_server_capture(self) -> Optional[str]:
        """Read latest frame from frame_server subprocess (~50ms, Quartz CGWindowListCreateImage).

        frame_server.py runs as a persistent subprocess using Quartz (safe in its
        own process, no CFRunLoop conflict). It writes the latest JPEG to
        /tmp/claude/latest_frame.jpg via atomic rename.
        """
        frame_path = os.path.join(_TMP_DIR, "latest_frame.jpg")
        meta_path = os.path.join(_TMP_DIR, "frame_meta.json")

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
                "screencapture", "-x", "-C", tmp_path,
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
        """Execute a Computer Use action. Returns (success, error_msg)."""
        try:
            if action in ("screenshot", "cursor_position"):
                return True, None

            coord = params.get("coordinate")
            sx, sy = self._cu_to_screen(coord) if coord else (0, 0)

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
