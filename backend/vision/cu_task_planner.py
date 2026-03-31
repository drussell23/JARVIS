"""
CU Task Planner — Claude Vision goal decomposition into atomic CUSteps.

The planner is the "architect" of the JARVIS-CU (Computer Use) system.
It fires once per goal: given a natural language objective and a screenshot
of the current desktop state, it calls Claude Vision to decompose the goal
into a sequence of atomic, independently executable UI steps.

The executor (cu_task_executor) then runs each step in order, using visual
grounding to resolve targets to screen coordinates.

All tunables are environment-variable driven — zero hardcoding.

Usage::

    planner = CUTaskPlanner()
    steps = await planner.plan_goal(
        "Open WhatsApp and send Zach 'what's up!'",
        current_frame,  # numpy RGB array
    )
    for step in steps:
        print(step.index, step.action, step.description)
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven tunables — no hardcoding
# ---------------------------------------------------------------------------
_MODEL = os.environ.get("JARVIS_CU_PLANNER_MODEL", "claude-sonnet-4-6-20250514")
_MAX_TOKENS = int(os.environ.get("JARVIS_CU_PLANNER_MAX_TOKENS", "2048"))
_JPEG_QUALITY = int(os.environ.get("JARVIS_CU_PLANNER_JPEG_QUALITY", "80"))

# Maximum image dimension before resizing (keeps API costs and latency sane)
_MAX_IMAGE_DIM = int(os.environ.get("JARVIS_CU_PLANNER_MAX_IMAGE_DIM", "1280"))

# ---------------------------------------------------------------------------
# Planning prompt — instructs Claude how to decompose goals
# ---------------------------------------------------------------------------
_PLANNING_PROMPT = """\
You are JARVIS, an AI assistant with REAL-TIME VISION controlling a macOS desktop.

You are given a LIVE SCREENSHOT of the current screen state and the user's goal.
LOOK at the screenshot carefully. Plan your steps based on what you can SEE.

VISION-FIRST PRINCIPLE:
If the element you need (a contact, button, app, field) is VISIBLE on screen,
interact with it directly. Do NOT search for things you can already see.
Only use search bars or Spotlight when the target is genuinely not on screen.

Each step must use exactly ONE of these action types:
- click: Click on a UI element. Include "target" describing what to click.
- type: Type text. Include "text" with the string to type. Optionally include "target" if a specific field must be clicked first.
- key: Press a single key. Include "key" (e.g. "Return", "Escape", "Tab").
- hotkey: Press a key combination. Include "keys" as a list (e.g. ["command", "space"]).
- scroll: Scroll the view. Include "direction" ("up"/"down") and "amount" (integer).
- wait: Wait for a condition. Include "condition" describing what to wait for. Include "app" if waiting for a specific app to become visible.

Rules:
1. LOOK at the screenshot first. Plan based on what is visible.
2. Use Spotlight (Cmd+Space) to launch apps only if the app is not already open.
3. Each step must be independently executable by looking at the screen.
4. Be specific about targets — describe the UI element precisely.
5. Include wait steps after launching apps or navigating to new screens.

For CHAT APPS (WhatsApp, Messages, Slack, Telegram):
- If the contact is VISIBLE in the sidebar or chat list, click them directly.
- Only use the search bar if the contact is NOT visible on screen.
- After opening the conversation, click the message input field at the bottom.
- Type the message, then press Return to send.

Return ONLY a JSON array of step objects. Each object has:
- "action": one of click/type/key/hotkey/scroll/wait
- "description": human-readable description of what this step does
- Optional fields depending on action type: "target", "text", "key", "keys", "condition", "app", "direction", "amount"

Do NOT include any text outside the JSON array. No markdown, no explanation.

User's goal: {goal}
"""

# Fields that CUStep accepts (used to filter unknown keys from Claude response)
_CUSTEP_FIELDS = frozenset({
    "action", "description", "target", "text", "keys", "key",
    "condition", "app", "direction", "amount",
})


# ---------------------------------------------------------------------------
# CUStep dataclass
# ---------------------------------------------------------------------------

@dataclass
class CUStep:
    """A single atomic UI step in a CU task plan.

    Attributes
    ----------
    index:
        Sequential position in the plan (0-based).
    action:
        One of: click, type, key, hotkey, scroll, wait.
    description:
        Human-readable description of what this step does.
    target:
        (Optional) Natural language description of the UI element to interact
        with. Used by the executor's visual grounding to find coordinates.
    text:
        (Optional) Text to type for 'type' actions.
    keys:
        (Optional) List of keys for 'hotkey' actions (e.g. ["command", "space"]).
    key:
        (Optional) Single key name for 'key' actions (e.g. "Return").
    condition:
        (Optional) Condition to wait for in 'wait' actions.
    app:
        (Optional) Application name for 'wait' actions.
    direction:
        (Optional) Scroll direction: "up" or "down".
    amount:
        (Optional) Scroll amount (integer, number of scroll clicks).
    """

    index: int
    action: str
    description: str
    target: Optional[str] = None
    text: Optional[str] = None
    keys: Optional[List[str]] = None
    key: Optional[str] = None
    condition: Optional[str] = None
    app: Optional[str] = None
    direction: Optional[str] = None
    amount: Optional[int] = None

    # ------------------------------------------------------------------
    # Executor-compatible computed fields
    # ------------------------------------------------------------------

    @property
    def value(self) -> str:
        """Single-string value for the executor's _execute_action_impl.

        The executor reads step.value generically across all action types.
        This property translates CUStep's typed fields into that format:
          type    → text content to type
          key     → key name to press
          hotkey  → comma-joined key list (e.g. "command,space")
          scroll  → scroll clicks as signed integer string (negative = down)
          others  → empty string
        """
        if self.action == "type":
            return self.text or ""
        elif self.action == "key":
            return self.key or ""
        elif self.action == "hotkey":
            if self.keys:
                return ",".join(str(k) for k in self.keys)
            return ""
        elif self.action == "scroll":
            amount = self.amount or 3
            if (self.direction or "down") == "up":
                return str(amount)
            return str(-amount)
        return ""

    @property
    def app_name(self) -> str:
        """Executor-compatible alias for step.app."""
        return self.app or ""

    @property
    def step_id(self) -> str:
        """Executor-compatible step identifier."""
        return f"step-{self.index}"

    @property
    def needs_visual_grounding(self) -> bool:
        """Whether this step requires visual grounding to resolve a target.

        Returns True for:
        - click with a target (need to find the element on screen)
        - type with a target (need to find the input field)
        - wait with an app (need to detect when the app is visible)
        """
        if self.action == "click" and self.target is not None:
            return True
        if self.action == "type" and self.target is not None:
            return True
        if self.action == "wait" and self.app is not None:
            return True
        return False


# ---------------------------------------------------------------------------
# CUTaskPlanner
# ---------------------------------------------------------------------------

class CUTaskPlanner:
    """Decomposes a natural language goal into atomic CUStep objects.

    Uses Claude Vision to analyze the current screen state and produce
    a step-by-step plan. The plan is a list of CUStep dataclass instances
    that the CU executor can run sequentially.

    All configuration is sourced from environment variables at construction
    time, so different planner instances can have different settings.
    """

    def __init__(self) -> None:
        # Lazy import to avoid hard dependency at module level
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

        # Read tunables at construction (not at call time) so they are
        # stable for the lifetime of this planner instance.
        self._model: str = os.environ.get(
            "JARVIS_CU_PLANNER_MODEL", "claude-sonnet-4-6-20250514"
        )
        self._max_tokens: int = int(
            os.environ.get("JARVIS_CU_PLANNER_MAX_TOKENS", "2048")
        )
        self._jpeg_quality: int = int(
            os.environ.get("JARVIS_CU_PLANNER_JPEG_QUALITY", "80")
        )
        self._max_image_dim: int = int(
            os.environ.get("JARVIS_CU_PLANNER_MAX_IMAGE_DIM", "1280")
        )

        logger.info(
            "[CUTaskPlanner] initialized — model=%s max_tokens=%d jpeg_q=%d",
            self._model,
            self._max_tokens,
            self._jpeg_quality,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def plan_goal(
        self,
        goal: str,
        current_frame: np.ndarray,
    ) -> List[CUStep]:
        """Decompose a goal into atomic CUStep objects.

        Parameters
        ----------
        goal:
            Natural language description of what to accomplish.
        current_frame:
            Screenshot of the current desktop state as a numpy RGB array.

        Returns
        -------
        List of CUStep objects in execution order.

        Raises
        ------
        Exception:
            If the Claude API call fails (network, auth, rate limit, etc.).
        """
        logger.info("[CUTaskPlanner] planning goal: %s", goal[:120])

        raw_steps = await self._call_claude_vision(goal, current_frame)
        steps = self._parse_steps(raw_steps)

        logger.info(
            "[CUTaskPlanner] plan complete — %d steps, %d need grounding",
            len(steps),
            sum(1 for s in steps if s.needs_visual_grounding),
        )
        return steps

    # ------------------------------------------------------------------
    # Claude Vision API call
    # ------------------------------------------------------------------

    async def _call_claude_vision(
        self,
        goal: str,
        frame: np.ndarray,
    ) -> List[Dict[str, Any]]:
        """Call Claude Vision with the screenshot and planning prompt.

        Returns the parsed JSON list of step dicts from Claude's response.
        """
        b64_image = self._frame_to_b64(frame)

        prompt_text = _PLANNING_PROMPT.format(goal=goal)

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
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
                            "text": prompt_text,
                        },
                    ],
                }
            ],
        )

        # Extract text content from the response
        raw_text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                raw_text += block.text

        # Parse JSON — handle markdown code fence wrapping
        raw_text = raw_text.strip()
        json_text = self._extract_json(raw_text)

        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as exc:
            logger.error(
                "[CUTaskPlanner] Failed to parse Claude response as JSON: %s — raw: %s",
                exc,
                raw_text[:500],
            )
            raise

        if not isinstance(parsed, list):
            logger.warning(
                "[CUTaskPlanner] Expected list, got %s — wrapping in list",
                type(parsed).__name__,
            )
            parsed = [parsed] if isinstance(parsed, dict) else []

        return parsed

    # ------------------------------------------------------------------
    # Step parsing
    # ------------------------------------------------------------------

    def _parse_steps(self, raw_steps: List[Dict[str, Any]]) -> List[CUStep]:
        """Convert a list of raw dicts into CUStep objects.

        Assigns sequential indices starting from 0. Unknown fields in
        the raw dicts are silently ignored.
        """
        steps: List[CUStep] = []
        for i, raw in enumerate(raw_steps):
            # Filter to only known CUStep fields
            filtered = {k: v for k, v in raw.items() if k in _CUSTEP_FIELDS}
            step = CUStep(index=i, **filtered)
            steps.append(step)
        return steps

    # ------------------------------------------------------------------
    # Frame encoding
    # ------------------------------------------------------------------

    def _frame_to_b64(self, frame: np.ndarray) -> str:
        """Convert a numpy frame to a base64-encoded JPEG string.

        Handles:
        - Grayscale (2D) arrays by converting to RGB
        - Large frames by resizing to fit within _max_image_dim
        """
        from PIL import Image

        # Handle grayscale
        if frame.ndim == 2:
            img = Image.fromarray(frame, mode="L").convert("RGB")
        else:
            img = Image.fromarray(frame)

        # Resize if needed
        w, h = img.size
        max_dim = self._max_image_dim
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # Encode as JPEG
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._jpeg_quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # ------------------------------------------------------------------
    # JSON extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_json(text: str) -> str:
        """Extract JSON from text that may be wrapped in markdown code fences.

        Handles:
        - ```json ... ```
        - ``` ... ```
        - Raw JSON (no wrapping)
        """
        # Try markdown code fence with optional language tag
        match = re.search(r"```(?:\w+)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # Already raw JSON
        return text
