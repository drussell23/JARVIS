# [Ouroboros] Modified by Karen (op=cu-smoke-177) at 2026-04-02 00:58 UTC
# Reason: Add precise element descriptions for chat app input fields

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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

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

MINIMUM-STEP PRINCIPLE:
Generate the FEWEST steps necessary. Every unnecessary step is a chance for error.
If the screen already shows what you need, skip navigation steps entirely.

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

=== MESSAGING APPS — CRITICAL INSTRUCTIONS ===

STEP 0 — OBSERVE BEFORE ACTING:
Before generating ANY steps, answer these questions by examining the screenshot:
  Q1: Which messaging app is in the foreground? (Messages, WhatsApp, Telegram, Slack, etc.)
  Q2: Is a conversation with the target contact ALREADY OPEN and ACTIVE?
      → Check: Is the contact's name displayed at the TOP of the conversation pane?
      → If YES: The conversation is ALREADY ACTIVE — skip ALL navigation steps.
  Q3: Is the target contact visible in the sidebar/chat list?
      → If YES: Just click their name in the sidebar — do NOT use the search bar.
  Q4: Where is the message input field?
      → It is ALWAYS at the BOTTOM of the conversation area.

CONVERSATION ALREADY ACTIVE (Q2 = YES):
If the contact's conversation is already showing on screen, your ENTIRE plan is:
  1. Click the message input field at the bottom of the conversation
  2. Type the message text
  3. Press Return to send
That's it. Do NOT search, do NOT click the sidebar, do NOT use keyboard shortcuts.

NEED TO NAVIGATE TO CONTACT (Q2 = NO):
  - If contact is VISIBLE in sidebar (Q3 = YES): click them in the sidebar, then type + send.
  - If contact is NOT visible: click the search bar, type the contact name, click the matching result, then type + send.

APP-SPECIFIC UI LANDMARKS:
  Messages (iMessage):
    - Message input: text field labeled "iMessage" at the bottom of the conversation
    - Search bar: field with magnifying glass icon at the top-LEFT of the sidebar
    - Active contact: name shown in the header bar at the top-center of the conversation
    - DANGER: Do NOT press Cmd+N (creates a new message window and changes context)
    - DANGER: Do NOT type contact names into the message input — they will be SENT as messages

  WhatsApp:
    - Message input: text field labeled "Type a message" at the bottom
    - Search bar: field labeled "Search" at the top of the sidebar
    - Active contact: name shown at the top of the conversation pane
    - DANGER: Do NOT type contact names into the message input

  ENHANCED TARGET DESCRIPTIONS (auto-generated by Ouroboros):
    WhatsApp message input: "text input field labeled Type a message at the very bottom of the chat area, just above the keyboard bar"
    Messages/iMessage input: "text input field with placeholder text iMessage at the bottom of the conversation view, between the plus button and the audio button"
    Additional: WhatsApp: input has a smiley emoji button on the left and a microphone icon on the right. Messages: input has a plus (+) button on the left and an audio waveform button on the right.

  Telegram:
    - Message input: text field labeled "Message" at the bottom
    - Search bar: magnifying glass icon at the top of the chat list

  Slack:
    - Message input: field at the bottom labeled "Message #channel" or "Message @person"
    - Search bar: Cmd+K or search box at the top

ANTI-PATTERNS (things that CAUSE BUGS — never do these):
  ✗ Using Cmd+N in a messaging app (creates new message, loses context)
  ✗ Typing a contact name into the message input field (it gets SENT as a message!)
  ✗ Searching for a contact when their conversation is already open
  ✗ Using keyboard shortcuts for navigation in chat apps (unreliable)
  ✗ Clicking the search bar when the contact is visible in the sidebar

=== END MESSAGING INSTRUCTIONS ===

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

    System 1 / System 2 biological vision pipeline:

      System 1 (Peripheral/Scout): Doubleword Qwen3-VL-235B (~2-3s)
        Fast spatial planner. Handles basic screen geometry, element
        identification, and simple UI navigation natively.

      System 2 (Deep Fovea/Semantic): Claude Vision (~5-15s)
        Frontier-level semantic understanding. Activated ONLY when
        System 1 signals low confidence or the task requires deep
        pixel-level reasoning, complex multi-step UI workflows, or
        precise text reading from the screenshot.

    This prevents burning Claude's latency and tokens on simple spatial
    awareness while retaining frontier-level vision when needed.

    All configuration is sourced from environment variables at construction
    time, so different planner instances can have different settings.
    """

    def __init__(self) -> None:
        # System 2: Claude Vision (deep fovea fallback)
        self._anthropic_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
        self._claude_client: Any = None
        if self._anthropic_key:
            try:
                import anthropic
                self._claude_client = anthropic.AsyncAnthropic(api_key=self._anthropic_key)
            except ImportError:
                logger.warning("[CUTaskPlanner] anthropic SDK not installed — System 2 disabled")

        # System 1: Doubleword Qwen3-VL-235B (fast spatial scout)
        self._dw_api_key: str = os.environ.get("DOUBLEWORD_API_KEY", "")
        self._dw_base_url: str = os.environ.get(
            "DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1"
        )
        self._dw_model: str = os.environ.get(
            "DOUBLEWORD_PLANNER_MODEL",
            "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8",
        )
        self._dw_timeout: float = float(os.environ.get("JARVIS_CU_DW_PLANNER_TIMEOUT_S", "15"))

        # Read tunables at construction (not at call time) so they are
        # stable for the lifetime of this planner instance.
        self._claude_model: str = os.environ.get(
            "JARVIS_CU_PLANNER_MODEL",
            os.environ.get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022"),
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

        s1 = "Qwen3-VL" if self._dw_api_key else "disabled"
        s2 = "Claude" if self._claude_client else "disabled"
        logger.info(
            "[CUTaskPlanner] initialized — System1=%s System2=%s jpeg_q=%d",
            s1, s2, self._jpeg_quality,
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

        Tries System 1 (Qwen3-VL, ~2-3s) first. Falls back to System 2
        (Claude Vision, ~5-15s) when System 1 signals it needs help or
        is unavailable.

        Parameters
        ----------
        goal:
            Natural language description of what to accomplish.
        current_frame:
            Screenshot of the current desktop state as a numpy RGB array.

        Returns
        -------
        List of CUStep objects in execution order.
        """
        logger.info("[CUTaskPlanner] planning goal: %s", goal[:120])

        raw_steps = None
        planner_used = "none"

        # System 1: Qwen3-VL fast spatial planner (~2-3s)
        if self._dw_api_key:
            try:
                raw_steps, needs_escalation = await self._call_system1(goal, current_frame)
                if needs_escalation:
                    logger.info(
                        "[CUTaskPlanner] System 1 requested escalation → System 2"
                    )
                    raw_steps = None  # Discard and let System 2 handle
                elif raw_steps:
                    planner_used = "system1_qwen3vl"
            except Exception as exc:
                logger.warning("[CUTaskPlanner] System 1 failed: %s — escalating", exc)

        # System 2: Claude Vision deep fovea (~5-15s)
        if raw_steps is None and self._claude_client:
            try:
                raw_steps = await self._call_system2(goal, current_frame)
                planner_used = "system2_claude"
            except Exception as exc:
                logger.error("[CUTaskPlanner] System 2 failed: %s", exc)
                raise

        if raw_steps is None:
            logger.error("[CUTaskPlanner] Both systems failed — no plan generated")
            return []

        steps = self._parse_steps(raw_steps)

        logger.info(
            "[CUTaskPlanner] plan complete via %s — %d steps, %d need grounding",
            planner_used,
            len(steps),
            sum(1 for s in steps if s.needs_visual_grounding),
        )
        return steps

    # ------------------------------------------------------------------
    # System 1: Doubleword Qwen3-VL-235B (fast spatial planner, ~2-3s)
    # ------------------------------------------------------------------

    async def _call_system1(
        self,
        goal: str,
        frame: np.ndarray,
    ) -> tuple[Optional[List[Dict[str, Any]]], bool]:
        """Call Qwen3-VL-235B for fast spatial planning.

        Returns (raw_steps, needs_escalation).
        If needs_escalation is True, the caller should fall through to System 2.
        The model signals escalation by including {"escalate": true} in its
        response or returning an empty step list with a reason.
        """
        try:
            import aiohttp
        except ImportError:
            logger.warning("[CUTaskPlanner] aiohttp not installed — System 1 disabled")
            return None, True

        b64_image = self._frame_to_b64(frame)

        # Same planning prompt as System 2, plus escalation instruction
        prompt_text = _PLANNING_PROMPT.format(goal=goal) + (
            "\n\nIMPORTANT: If this task requires reading small or blurry text, "
            "complex multi-level menus, precise sub-pixel positioning, or you are "
            "not confident in your plan, respond with ONLY: "
            '{"escalate": true, "reason": "brief explanation"}\n'
            "Otherwise return the JSON step array as instructed above."
        )

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
                            "text": prompt_text,
                        },
                    ],
                }
            ],
            "max_tokens": self._max_tokens,
            "temperature": 0.0,
        }

        url = f"{self._dw_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._dw_api_key}",
            "Content-Type": "application/json",
        }

        logger.info("[CUTaskPlanner] System 1 (Qwen3-VL): calling Doubleword...")
        timeout = aiohttp.ClientTimeout(total=self._dw_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("[CUTaskPlanner] System 1 API %d: %s", resp.status, body[:200])
                    return None, True
                data = await resp.json()

        # Extract text from OpenAI-compatible response
        choices = data.get("choices", [])
        if not choices:
            return None, True
        raw_text = choices[0].get("message", {}).get("content", "")
        if not raw_text:
            return None, True

        raw_text = raw_text.strip()
        json_text = self._extract_json(raw_text)

        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            logger.warning("[CUTaskPlanner] System 1 returned non-JSON: %s", raw_text[:200])
            return None, True

        # Check for escalation signal
        if isinstance(parsed, dict):
            if parsed.get("escalate"):
                reason = parsed.get("reason", "unspecified")
                logger.info("[CUTaskPlanner] System 1 escalated: %s", reason)
                return None, True
            # Single step dict — wrap in list
            parsed = [parsed]

        if not isinstance(parsed, list):
            return None, True

        logger.info("[CUTaskPlanner] System 1 planned %d steps", len(parsed))
        return parsed, False

    # ------------------------------------------------------------------
    # System 2: Claude Vision (deep fovea, ~5-15s)
    # ------------------------------------------------------------------

    async def _call_system2(
        self,
        goal: str,
        frame: np.ndarray,
    ) -> List[Dict[str, Any]]:
        """Call Claude Vision with the screenshot and planning prompt.

        Returns the parsed JSON list of step dicts from Claude's response.
        """
        b64_image = self._frame_to_b64(frame)
        prompt_text = _PLANNING_PROMPT.format(goal=goal)

        logger.info("[CUTaskPlanner] System 2 (Claude): calling Anthropic...")
        response = await self._claude_client.messages.create(
            model=self._claude_model,
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
                raw_text += block.text  # type: ignore[union-attr]

        # Parse JSON — handle markdown code fence wrapping
        raw_text = raw_text.strip()
        json_text = self._extract_json(raw_text)

        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as exc:
            logger.error(
                "[CUTaskPlanner] System 2 failed to parse JSON: %s — raw: %s",
                exc, raw_text[:500],
            )
            raise

        if not isinstance(parsed, list):
            logger.warning(
                "[CUTaskPlanner] Expected list, got %s — wrapping",
                type(parsed).__name__,
            )
            parsed = [parsed] if isinstance(parsed, dict) else []

        logger.info("[CUTaskPlanner] System 2 planned %d steps", len(parsed))
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
            # Ensure required fields have defaults
            if "action" not in filtered:
                filtered["action"] = "click"
            if "description" not in filtered:
                # Synthesize description from action + target/text
                target = filtered.get("target", filtered.get("text", ""))
                filtered["description"] = f"{filtered['action']} {target}".strip()
            step = CUStep(index=i, **filtered)
            steps.append(step)

        # Post-parse safety: filter dangerous messaging patterns
        steps = self._filter_messaging_antipatterns(steps)
        return steps

    @staticmethod
    def _filter_messaging_antipatterns(steps: List[CUStep]) -> List[CUStep]:
        """Remove or fix steps that match known messaging bug patterns.

        Catches the exact bugs that have occurred in production:
          - Cmd+N in messaging apps (creates new message, loses context)
          - Typing a contact name followed by Return before the actual message
            (sends the contact name as a message)

        This is a deterministic safety net (Manifesto §5 Tier 0) — the planning
        prompt should prevent these, but defense-in-depth catches model errors.
        """
        if len(steps) < 2:
            return steps

        filtered: List[CUStep] = []
        i = 0
        while i < len(steps):
            step = steps[i]

            # Block Cmd+N in messaging context (creates new message window)
            if step.action == "hotkey" and step.keys:
                keys_lower = [k.lower() for k in step.keys]
                if "command" in keys_lower and "n" in keys_lower:
                    desc = step.description.lower()
                    if any(w in desc for w in ("message", "new", "compose", "chat")):
                        logger.warning(
                            "[CUTaskPlanner] BLOCKED Cmd+N anti-pattern at step %d: %s",
                            step.index, step.description,
                        )
                        i += 1
                        continue

            # Detect "type contact name → Return → type message" pattern.
            # If a type step is immediately followed by a key=Return, and then
            # another type step, the first type is likely a navigation search
            # that will accidentally send the contact name as a message.
            if (
                step.action == "type"
                and step.text
                and i + 2 < len(steps)
                and steps[i + 1].action == "key"
                and (steps[i + 1].key or "").lower() == "return"
                and steps[i + 2].action == "type"
            ):
                # Check if the first type text looks like a contact name
                # (short, no punctuation, capitalized) rather than a message
                text = step.text.strip()
                next_text = steps[i + 2].text or ""
                is_short = len(text.split()) <= 3
                has_no_punct = not any(c in text for c in ".!?,;:")
                if is_short and has_no_punct and next_text:
                    logger.warning(
                        "[CUTaskPlanner] BLOCKED type-name-then-send anti-pattern: "
                        "would have sent %r as a message before %r",
                        text, next_text[:40],
                    )
                    # Skip the contact-name type AND the return that follows it
                    i += 2
                    continue

            filtered.append(step)
            i += 1

        # Re-index if we removed steps
        if len(filtered) != len(steps):
            for idx, s in enumerate(filtered):
                s.index = idx  # type: ignore[misc]
            logger.info(
                "[CUTaskPlanner] Safety filter: %d → %d steps",
                len(steps), len(filtered),
            )

        return filtered

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

        # Handle grayscale and RGBA → RGB (JPEG doesn't support alpha)
        if frame.ndim == 2:
            img = Image.fromarray(frame, mode="L").convert("RGB")
        else:
            img = Image.fromarray(frame)
            if img.mode == "RGBA":
                img = img.convert("RGB")

        # Resize if needed
        w, h = img.size
        max_dim = self._max_image_dim
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

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
