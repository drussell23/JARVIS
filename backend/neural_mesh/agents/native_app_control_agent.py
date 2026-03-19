"""
JARVIS Neural Mesh — NativeAppControlAgent

Drives installed macOS applications using a vision-action loop:

  1. Verify app is installed via AppInventoryService (optional, graceful).
  2. Activate the target app via osascript.
  3. Capture a screenshot (screencapture -x -C).
  4. Send screenshot + goal to J-Prime vision server (free) for next-action
     inference; falls back to Claude API (paid) when J-Prime is unavailable.
  5. Execute the decided action (click / type / key / scroll).
  6. Verify the action succeeded via a post-action screenshot + vision check.
  7. Repeat until the goal is achieved or max steps is reached.

Configuration (all via environment variables — no hardcoding):
  JARVIS_NATIVE_CONTROL_MAX_STEPS       — maximum loop iterations (default 10)
  JARVIS_NATIVE_CONTROL_STEP_DELAY      — seconds to wait between steps (default 1.0)
  JARVIS_VISION_VERIFY_ACTIONS          — enable post-action verification (default "true")
  JARVIS_VISION_VERIFY_MAX_RETRIES      — verification retry limit per action (default "2")
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent

logger = logging.getLogger(__name__)


def _get_brain_router():
    """Lazy import to avoid circular dependency at module load."""
    from backend.core.interactive_brain_router import get_interactive_brain_router
    return get_interactive_brain_router()


# ---------------------------------------------------------------------------
# Env-var helpers
# ---------------------------------------------------------------------------

def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key, "").lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Key code map for AppleScript key press — dynamically configurable via env
# ---------------------------------------------------------------------------

_DEFAULT_KEY_CODES: Dict[str, int] = {
    "enter": 36,
    "return": 36,
    "tab": 48,
    "escape": 53,
    "esc": 53,
    "space": 49,
    "delete": 51,
    "backspace": 51,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
}


def _load_key_codes() -> Dict[str, int]:
    """Return key-code map, allowing env override as JSON string."""
    raw = os.getenv("JARVIS_NATIVE_CONTROL_KEY_CODES", "").strip()
    if raw:
        try:
            overrides = json.loads(raw)
            merged = dict(_DEFAULT_KEY_CODES)
            merged.update({k.lower(): int(v) for k, v in overrides.items()})
            return merged
        except Exception as exc:
            logger.warning(
                "[NativeAppControlAgent] Failed to parse JARVIS_NATIVE_CONTROL_KEY_CODES: %s — "
                "using defaults.",
                exc,
            )
    return dict(_DEFAULT_KEY_CODES)


# ---------------------------------------------------------------------------
# Vision-action prompt template
# ---------------------------------------------------------------------------

_VISION_PROMPT_TEMPLATE = """You are JARVIS, an AI assistant physically controlling the macOS application: {app_name}.

You are executing step-by-step instructions like a human would.

CURRENT STEP: {current_step}
(This is step {step_number} of {total_steps} in the full plan)

FULL PLAN for context:
{plan_text}

Steps already completed:
{previous_actions_text}

Look at the screenshot carefully. Identify the UI element you need to interact with for the CURRENT STEP.

RULES:
- For "click" actions: describe the UI element by name, role, and nearby visible text. Do NOT guess pixel
  coordinates — the system will find the exact position automatically via the macOS Accessibility API.
- For "type" actions: provide the exact text to type.
- For "key" actions: provide the key name (e.g. "enter", "tab", "escape").
- Set "done": true ONLY when the CURRENT STEP is fully accomplished.
- Do NOT set "done": true just because you started the step — wait until you can see the result.
- If you need multiple actions for one step (e.g., click then type), do ONE action at a time.

Respond ONLY with valid JSON:
{{
  "done": false,
  "action_type": "click",
  "detail": {{"element": "search bar", "role": "text field", "near_text": "Chats"}},
  "message": "Clicking the search bar near the Chats heading"
}}

action_type must be one of: click, type, key, scroll
- click: detail must contain:
    "element"   (str) — short human-readable name of the element (e.g. "search bar", "Send button")
    "role"      (str, optional) — AX role hint: "button", "text field", "menu item", "checkbox", etc.
    "near_text" (str, optional) — visible label or text near the element to disambiguate
  Do NOT include pixel coordinates for click actions.
- type: detail.text (str) — text to type character by character
- key: detail.key (str) — "enter", "tab", "escape", "return"
- scroll: detail.direction ("up"/"down"), detail.amount (int pixels)

When the CURRENT STEP is complete (you can verify from the screenshot), respond:
{{
  "done": true,
  "action_type": "done",
  "detail": {{}},
  "message": "Step complete: [what was accomplished]"
}}"""


_STEP_PLANNER_PROMPT = """You are JARVIS, planning how to accomplish a task in the macOS application: {app_name}.

Task: {goal}

Break this into a numbered list of concrete, atomic steps that a screen automation tool can execute one at a time. Each step should be a single UI interaction (click something, type something, press a key).

Be very specific. For example, instead of "send a message", say:
1. Click on the message text input field at the bottom of the conversation
2. Type the message text: "Hello Zach"
3. Press Enter to send the message

Respond with ONLY a JSON array of step strings:
["Click on the search bar at the top", "Type 'Zach' in the search bar", "Click on Zach's conversation in the results list", ...]"""


class NativeAppControlAgent(BaseNeuralMeshAgent):
    """
    Neural Mesh agent that drives installed macOS apps via a vision-action loop.

    Uses J-Prime (GCP LLaVA vision server) as the free vision backbone and
    falls back to Claude API when J-Prime is unavailable.  All sub-process
    calls use asyncio.create_subprocess_exec to avoid blocking the event loop.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_name="native_app_control_agent",
            agent_type="autonomy",
            capabilities={"native_app_control", "interact_with_app"},
            version="1.0.0",
            description=(
                "Vision-action loop agent that drives installed macOS applications "
                "using J-Prime vision and AppleScript/cliclick for interaction."
            ),
        )
        self._app_inventory_service: Optional[Any] = None
        self._key_codes: Dict[str, int] = _load_key_codes()
        self._accessibility_resolver: Optional[Any] = None

    # ------------------------------------------------------------------
    # Lazy AccessibilityResolver accessor
    # ------------------------------------------------------------------

    def _get_resolver(self) -> Any:
        """Return a cached AccessibilityResolver instance (lazy init)."""
        if self._accessibility_resolver is None:
            from .accessibility_resolver import get_accessibility_resolver
            self._accessibility_resolver = get_accessibility_resolver()
        return self._accessibility_resolver

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_initialize(self, **kwargs) -> None:
        """Wire AppInventoryService — graceful if unavailable."""
        try:
            from backend.neural_mesh.agents.app_inventory_service import (
                AppInventoryService,
            )

            svc = AppInventoryService()
            await svc.initialize()
            self._app_inventory_service = svc
            logger.info(
                "[NativeAppControlAgent] AppInventoryService wired successfully."
            )
        except Exception as exc:
            logger.warning(
                "[NativeAppControlAgent] AppInventoryService unavailable (%s). "
                "App-installed checks will be skipped.",
                exc,
            )
            self._app_inventory_service = None

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """Route payload to the appropriate action handler.

        Supported actions:
          * interact_with_app -- run the vision-action loop for a goal

        Args:
            payload: Must contain 'action' (str).
                     'interact_with_app' also requires 'app_name' and 'goal'.

        Returns:
            Action-dependent dict.

        Raises:
            ValueError: If action is unknown or required fields are missing.
        """
        action = str(payload.get("action", "")).strip().lower()

        if not action:
            raise ValueError(
                "execute_task requires an 'action' key in the payload."
            )

        if action == "interact_with_app":
            return await self._interact_with_app(payload)

        raise ValueError(
            f"Unknown action '{action}'. Supported: 'interact_with_app'."
        )

    # ------------------------------------------------------------------
    # Primary handler
    # ------------------------------------------------------------------

    async def _interact_with_app(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Run the vision-action loop to achieve goal inside app_name.

        Args:
            payload: Must contain 'app_name' (str) and 'goal' (str).

        Returns:
            Dict with keys: success, goal, app, steps_taken, actions, final_message.
        """
        app_name: str = str(payload.get("app_name", "")).strip()
        goal: str = str(payload.get("goal", "")).strip()

        # --- Validation -------------------------------------------------------
        if not app_name:
            return {
                "success": False,
                "error": "app_name is required and must not be empty.",
                "goal": goal,
                "app": app_name,
                "steps_taken": 0,
                "actions": [],
                "final_message": "Validation failed: app_name is empty.",
            }

        if not goal:
            return {
                "success": False,
                "error": "goal is required and must not be empty.",
                "goal": goal,
                "app": app_name,
                "steps_taken": 0,
                "actions": [],
                "final_message": "Validation failed: goal is empty.",
            }

        # --- App installed check ----------------------------------------------
        if self._app_inventory_service is not None:
            try:
                check_result = await self._app_inventory_service.execute_task(
                    {"action": "check_app", "app_name": app_name}
                )
                if not check_result.get("found", False):
                    suggestion = (
                        f"'{app_name}' does not appear to be installed on this Mac. "
                        "Please install the application first or use the browser tier."
                    )
                    return {
                        "success": False,
                        "error": f"'{app_name}' is not installed.",
                        "suggestion": suggestion,
                        "goal": goal,
                        "app": app_name,
                        "steps_taken": 0,
                        "actions": [],
                        "final_message": suggestion,
                    }
            except Exception as exc:
                logger.warning(
                    "[NativeAppControlAgent] App check failed for '%s': %s -- proceeding anyway.",
                    app_name,
                    exc,
                )

        # --- Config -----------------------------------------------------------
        max_actions_per_step: int = int(os.getenv("JARVIS_NATIVE_CONTROL_MAX_ACTIONS_PER_STEP", "5"))
        step_delay: float = float(os.getenv("JARVIS_NATIVE_CONTROL_STEP_DELAY", "1.0"))

        # --- Activate the app -------------------------------------------------
        await self._activate_app(app_name)
        await asyncio.sleep(step_delay)

        # --- Decompose goal into micro-steps ----------------------------------
        plan_steps = await self._decompose_goal(goal, app_name)
        logger.info(
            "[NativeAppControlAgent] Decomposed '%s' into %d steps: %s",
            goal, len(plan_steps), plan_steps,
        )

        # --- Step-by-step vision-action loop ----------------------------------
        actions_taken: List[Dict[str, Any]] = []
        final_message = f"Completed all {len(plan_steps)} steps."
        success = False
        plan_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan_steps))

        for step_idx, current_step in enumerate(plan_steps):
            logger.info(
                "[NativeAppControlAgent] Step %d/%d: %s",
                step_idx + 1, len(plan_steps), current_step,
            )

            # Each step gets up to max_actions_per_step vision-action iterations
            step_done = False
            for action_num in range(max_actions_per_step):
                # Capture screenshot
                screenshot_b64 = await self._take_screenshot()
                if screenshot_b64 is None:
                    logger.warning(
                        "[NativeAppControlAgent] Screenshot failed at step %d action %d.",
                        step_idx + 1, action_num + 1,
                    )
                    final_message = "Screenshot capture failed."
                    break

                # Ask vision model — now with current step context
                decision = await self._ask_jprime_for_action(
                    screenshot_b64, goal, app_name, actions_taken,
                    current_step=current_step,
                    step_number=step_idx + 1,
                    total_steps=len(plan_steps),
                    plan_text=plan_text,
                )

                msg = decision.get("message", "")
                action_type = decision.get("action_type", "")
                detail = decision.get("detail", {})
                done = bool(decision.get("done", False))

                actions_taken.append({
                    "plan_step": step_idx + 1,
                    "action_num": action_num + 1,
                    "step_description": current_step,
                    "action_type": action_type if not done else "done",
                    "detail": detail,
                    "message": msg,
                })

                if done:
                    step_done = True
                    logger.info(
                        "[NativeAppControlAgent] Step %d complete: %s",
                        step_idx + 1, msg,
                    )
                    break

                # Execute the action
                try:
                    if action_type == "click":
                        await self._click_element(detail, app_name)
                    elif action_type == "type":
                        await self._type_text(str(detail.get("text", "")))
                    elif action_type == "key":
                        await self._press_key(str(detail.get("key", "")))
                    elif action_type == "scroll":
                        await self._scroll(
                            str(detail.get("direction", "down")),
                            int(detail.get("amount", 3)),
                        )
                    else:
                        logger.warning(
                            "[NativeAppControlAgent] Unknown action '%s'",
                            action_type,
                        )
                except Exception as exc:
                    logger.warning(
                        "[NativeAppControlAgent] Action error at step %d: %s",
                        step_idx + 1, exc,
                    )

                # --- Post-action verification --------------------------------
                if action_type != "done" and _env_bool(
                    "JARVIS_VISION_VERIFY_ACTIONS", True
                ):
                    max_retries = _env_int("JARVIS_VISION_VERIFY_MAX_RETRIES", 2)
                    verified = await self._verify_action(
                        app_name=app_name,
                        action_description=msg,
                        expected_result=current_step,
                        max_retries=max_retries,
                    )
                    if not verified:
                        logger.warning(
                            "[NativeAppControlAgent] Action verification failed for "
                            "step %d: %s",
                            step_idx + 1,
                            msg,
                        )
                        # Don't break — let the vision model see the current state
                        # and decide what to do next on the next iteration.

                await asyncio.sleep(step_delay)

            if not step_done:
                final_message = f"Step {step_idx + 1} did not complete: {current_step}"
                logger.warning("[NativeAppControlAgent] %s", final_message)
                break  # Stop plan execution if a step can't be completed

        else:
            # All steps completed successfully (for/else: no break)
            success = True
            final_message = f"All {len(plan_steps)} steps completed successfully."

        return {
            "success": success,
            "goal": goal,
            "app": app_name,
            "plan_steps": plan_steps,
            "steps_completed": step_idx + 1 if 'step_idx' in dir() else 0,
            "actions_taken": len(actions_taken),
            "actions": actions_taken,
            "final_message": final_message,
        }

    # ------------------------------------------------------------------
    # Goal Decomposition (Step Planner)
    # ------------------------------------------------------------------

    async def _decompose_goal(
        self, goal: str, app_name: str
    ) -> List[str]:
        """Break a high-level goal into atomic UI steps.

        Checks the StepPlanCache first (ChromaDB semantic search).  Only calls
        J-Prime or Claude when no sufficiently similar plan is cached.  Stores
        successful LLM decompositions back into the cache for next time.
        """
        # ------------------------------------------------------------------
        # 1. Cache lookup — fast, no LLM needed
        # ------------------------------------------------------------------
        try:
            from .step_plan_cache import get_step_plan_cache
            _cache = get_step_plan_cache()
            cached_steps = await _cache.get_cached_plan(goal, app_name)
            if cached_steps:
                logger.info(
                    "[NativeAppControlAgent] Plan cache HIT for '%s' (%d steps)",
                    goal[:60],
                    len(cached_steps),
                )
                return cached_steps
        except Exception as _exc:
            logger.debug(
                "[NativeAppControlAgent] Plan cache lookup failed: %s", _exc
            )

        # ------------------------------------------------------------------
        # 2. LLM decomposition — J-Prime then Claude
        # ------------------------------------------------------------------
        prompt = _STEP_PLANNER_PROMPT.format(app_name=app_name, goal=goal)
        steps: List[str] = []

        # Try J-Prime first (free)
        try:
            from backend.core.prime_client import get_prime_client
            client = await asyncio.wait_for(get_prime_client(), timeout=5.0)
            response = await asyncio.wait_for(
                client.send_vision_request(
                    image_base64="",  # No image needed for planning
                    prompt=prompt,
                    max_tokens=512,
                    temperature=0.2,
                ),
                timeout=30.0,
            )
            if response and response.content:
                steps = self._parse_plan_steps(response.content)
        except Exception as e:
            logger.debug("[NativeAppControlAgent] J-Prime planning failed: %s", e)

        # Try Claude (paid fallback)
        if not steps:
            try:
                import anthropic
                api_key = os.getenv("ANTHROPIC_API_KEY", "")
                if api_key:
                    client = anthropic.AsyncAnthropic(api_key=api_key)
                    _selection = _get_brain_router().select_for_task("step_decomposition", goal)
                    model = os.getenv("JARVIS_NATIVE_CONTROL_CLAUDE_MODEL") or _selection.claude_model
                    msg = await client.messages.create(
                        model=model,
                        max_tokens=512,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    if msg.content:
                        steps = self._parse_plan_steps(msg.content[0].text)
            except Exception as e:
                logger.debug("[NativeAppControlAgent] Claude planning failed: %s", e)

        # Heuristic fallback — generic steps based on goal keywords
        if not steps:
            steps = self._heuristic_decompose(goal, app_name)

        # ------------------------------------------------------------------
        # 3. Store successful decomposition in cache for next time
        # ------------------------------------------------------------------
        if len(steps) > 1:
            try:
                from .step_plan_cache import get_step_plan_cache
                _cache = get_step_plan_cache()
                await _cache.store_plan(goal, app_name, steps)
                logger.info(
                    "[NativeAppControlAgent] Plan cached for '%s' (%d steps)",
                    goal[:60],
                    len(steps),
                )
            except Exception as _exc:
                logger.debug(
                    "[NativeAppControlAgent] Plan cache store failed: %s", _exc
                )

        return steps

    def _parse_plan_steps(self, raw_text: str) -> List[str]:
        """Parse JSON array of step strings from LLM response."""
        import json as _json
        import re
        cleaned = raw_text.strip()
        # Try direct JSON parse
        try:
            result = _json.loads(cleaned)
            if isinstance(result, list) and all(isinstance(s, str) for s in result):
                return result
        except _json.JSONDecodeError:
            pass
        # Try extracting JSON array from text
        match = re.search(r'\[[\s\S]*\]', cleaned)
        if match:
            try:
                result = _json.loads(match.group())
                if isinstance(result, list):
                    return [str(s) for s in result]
            except _json.JSONDecodeError:
                pass
        # Fall back to line-by-line numbered list
        lines = [
            re.sub(r'^\d+[\.\)]\s*', '', line.strip())
            for line in cleaned.split('\n')
            if line.strip() and re.match(r'^\d+[\.\)]', line.strip())
        ]
        return lines if lines else [cleaned]

    def _heuristic_decompose(self, goal: str, app_name: str) -> List[str]:
        """Simple keyword-based step decomposition when no LLM available."""
        goal_lower = goal.lower()
        steps = []

        if "message" in goal_lower or "send" in goal_lower:
            # Extract recipient name if present
            import re
            name_match = re.search(r'(?:to|message)\s+(\w+)', goal_lower)
            name = name_match.group(1).title() if name_match else "the contact"

            # Extract message content
            msg_match = re.search(r'(?:say|send|type|message)[:\s]+"?([^"]+)"?', goal_lower)
            message_text = msg_match.group(1) if msg_match else "Testing with JARVIS"

            steps = [
                f"Click on the search bar at the top of {app_name}",
                f"Type '{name}' in the search bar",
                f"Click on {name}'s conversation in the search results",
                f"Click on the message text input field at the bottom",
                f"Type the message: '{message_text}'",
                "Press Enter to send the message",
            ]
        elif "open" in goal_lower:
            steps = [f"The app {app_name} is already open and active"]
        else:
            steps = [goal]  # Single step — let vision figure it out

        return steps

    # ------------------------------------------------------------------
    # App activation
    # ------------------------------------------------------------------

    async def _activate_app(self, app_name: str) -> None:
        """Bring app_name to the foreground via osascript.

        Uses asyncio.create_subprocess_exec (not shell=True) to prevent
        command injection.  app_name is passed as a literal argument string.
        """
        script = f'tell application "{app_name}" to activate'
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            if proc.returncode != 0:
                logger.warning(
                    "[NativeAppControlAgent] osascript activate returned %d: %s",
                    proc.returncode,
                    stderr.decode(errors="replace").strip(),
                )
        except asyncio.TimeoutError:
            logger.warning(
                "[NativeAppControlAgent] App activation timed out for '%s'.", app_name
            )
        except Exception as exc:
            logger.warning(
                "[NativeAppControlAgent] App activation error for '%s': %s", app_name, exc
            )

    # ------------------------------------------------------------------
    # Screenshot capture
    # ------------------------------------------------------------------

    async def _take_screenshot(self) -> Optional[str]:
        """Capture the current screen and return base64-encoded PNG.

        Uses screencapture -x -C (no sound, include cursor).

        Returns:
            Base64 PNG string, or None if capture fails.
        """
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".png", delete=False, prefix="jarvis_nac_"
            ) as tmp:
                tmp_path = tmp.name

            proc = await asyncio.create_subprocess_exec(
                "screencapture", "-x", "-C", tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15.0)

            if proc.returncode != 0 or not Path(tmp_path).exists():
                logger.warning(
                    "[NativeAppControlAgent] screencapture failed (rc=%s).",
                    proc.returncode,
                )
                return None

            raw_bytes = Path(tmp_path).read_bytes()

            # Compress: resize to max 1536px wide + JPEG quality 80
            # Keeps images under 5MB for both J-Prime LLaVA and Claude fallback
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(raw_bytes))
                max_dim = int(os.getenv("JARVIS_SCREENSHOT_MAX_DIM", "1536"))
                if max(img.size) > max_dim:
                    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=80)
                compressed = buf.getvalue()
                logger.debug(
                    "[NativeAppControlAgent] Screenshot compressed: %dKB → %dKB",
                    len(raw_bytes) // 1024,
                    len(compressed) // 1024,
                )
                return base64.b64encode(compressed).decode("ascii")
            except ImportError:
                # Pillow not available — send raw PNG
                return base64.b64encode(raw_bytes).decode("ascii")

        except asyncio.TimeoutError:
            logger.warning("[NativeAppControlAgent] Screenshot timed out.")
            return None
        except Exception as exc:
            logger.warning("[NativeAppControlAgent] Screenshot error: %s", exc)
            return None
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Vision / LLM action inference
    # ------------------------------------------------------------------

    async def _ask_jprime_for_action(
        self,
        screenshot_b64: str,
        goal: str,
        app_name: str,
        previous_actions: List[Dict[str, Any]],
        current_step: str = "",
        step_number: int = 1,
        total_steps: int = 1,
        plan_text: str = "",
    ) -> Dict[str, Any]:
        """Determine the next UI action using J-Prime vision or Claude API.

        Decision hierarchy:
          1. J-Prime vision server (free, GCP LLaVA) -- tried first.
          2. Claude API vision (paid) -- fallback when J-Prime unavailable.
          3. No-op return -- when both are unavailable.

        Returns:
            Dict with keys: done, action_type, detail, message.
        """
        previous_actions_text = (
            "\n".join(
                f"  [{a.get('action_type', '?')}] {a.get('message', '?')}"
                for a in previous_actions[-5:]  # Last 5 actions for context
            )
            if previous_actions
            else "  (none yet)"
        )

        prompt = _VISION_PROMPT_TEMPLATE.format(
            app_name=app_name,
            goal=goal,
            current_step=current_step or goal,
            step_number=step_number,
            total_steps=total_steps,
            plan_text=plan_text or goal,
            previous_actions_text=previous_actions_text,
        )

        # --- Ensure GPU VM is running (starts on-demand if needed) -----------
        try:
            from .vision_gpu_lifecycle import ensure_vision_available
            await ensure_vision_available()
        except Exception:
            pass  # Best-effort — will fall through to Claude if J-Prime unavailable

        # --- Attempt 1: J-Prime vision server ---------------------------------
        try:
            from backend.core.prime_client import get_prime_client

            client = await asyncio.wait_for(get_prime_client(), timeout=5.0)
            vision_healthy = await asyncio.wait_for(
                client.get_vision_health(), timeout=5.0
            )
            if vision_healthy:
                response = await asyncio.wait_for(
                    client.send_vision_request(
                        image_base64=screenshot_b64,
                        prompt=prompt,
                        max_tokens=512,
                        temperature=0.1,
                    ),
                    timeout=60.0,
                )
                return self._parse_vision_response(response.content)
        except Exception as exc:
            logger.debug(
                "[NativeAppControlAgent] J-Prime vision unavailable: %s -- trying Claude API.",
                exc,
            )

        # --- Attempt 2: Claude API (paid fallback) ----------------------------
        try:
            import anthropic

            anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
            if not anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")

            _selection = _get_brain_router().select_for_task("vision_action", goal)
            claude_model = os.getenv(
                "JARVIS_NATIVE_CONTROL_CLAUDE_MODEL",
            ) or _selection.claude_model

            async_client = anthropic.AsyncAnthropic(api_key=anthropic_api_key)
            message = await asyncio.wait_for(
                async_client.messages.create(
                    model=claude_model,
                    max_tokens=512,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/jpeg",
                                        "data": screenshot_b64,
                                    },
                                },
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                ),
                timeout=60.0,
            )
            raw_text = message.content[0].text if message.content else ""
            return self._parse_vision_response(raw_text)
        except Exception as exc:
            logger.warning(
                "[NativeAppControlAgent] Claude API vision fallback failed: %s", exc
            )

        # --- No vision model available ----------------------------------------
        return {
            "done": True,
            "action_type": None,
            "detail": {},
            "message": "No vision model available to determine next action.",
        }

    def _parse_vision_response(self, raw_text: str) -> Dict[str, Any]:
        """Parse the vision model's JSON response into an action dict.

        Tolerates markdown code fences and partial JSON fragments.

        Args:
            raw_text: Raw LLM output text.

        Returns:
            Dict with keys: done, action_type, detail, message.
        """
        default: Dict[str, Any] = {
            "done": True,
            "action_type": None,
            "detail": {},
            "message": "Could not parse vision model response.",
        }

        if not raw_text:
            return default

        # Strip markdown code fences if present
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            inner = [
                l for i, l in enumerate(lines)
                if not (i == 0 or l.strip() == "```")
            ]
            cleaned = "\n".join(inner).strip()

        # Find the first JSON object in the text
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start == -1 or end <= start:
            logger.debug(
                "[NativeAppControlAgent] No JSON object found in: %.200s", raw_text
            )
            return default

        try:
            data = json.loads(cleaned[start:end])
        except json.JSONDecodeError as exc:
            logger.debug(
                "[NativeAppControlAgent] JSON parse error (%s) in: %.200s", exc, raw_text
            )
            return default

        return {
            "done": bool(data.get("done", False)),
            "action_type": data.get("action_type"),
            "detail": data.get("detail", {}),
            "message": str(data.get("message", "")),
        }

    # ------------------------------------------------------------------
    # Action executors
    # ------------------------------------------------------------------

    async def _click_element(self, detail: Dict[str, Any], app_name: str) -> bool:
        """Click a UI element using AccessibilityResolver for exact coordinates.

        Fallback chain:
          1. AccessibilityResolver (AX tree) — exact position
          2. Pixel coordinates from vision model — approximate (if provided)
          3. AppleScript click by description — last resort

        Args:
            detail:   The detail dict from the vision model response.
                      Expected keys: element (str), role (str, opt), near_text (str, opt).
                      May also contain x/y as a legacy fallback.
            app_name: The macOS application process name for AppleScript fallback.

        Returns:
            True if a click was dispatched, False if all strategies failed.
        """
        element_desc = detail.get("element", "")
        role = detail.get("role")
        near_text = detail.get("near_text")

        # 1. AccessibilityResolver (exact position via AX tree)
        try:
            resolver = self._get_resolver()
            coords = await resolver.resolve(
                description=element_desc,
                app_name=app_name,
                role=role,
                near_text=near_text,
            )
            if coords:
                logger.info(
                    "[NativeAppControlAgent] AX resolved '%s' → (%d, %d)",
                    element_desc, coords["x"], coords["y"],
                )
                await self._cg_click(coords["x"], coords["y"])
                return True
        except Exception as exc:
            logger.debug("[NativeAppControlAgent] AX resolution failed: %s", exc)

        # 2. Fallback: pixel coordinates from vision model (if provided)
        if "x" in detail and "y" in detail:
            logger.info(
                "[NativeAppControlAgent] AX miss — using vision coordinates (%s, %s)",
                detail["x"], detail["y"],
            )
            await self._cg_click(int(detail["x"]), int(detail["y"]))
            return True

        # 3. Last resort: AppleScript click by element description
        if element_desc:
            logger.info(
                "[NativeAppControlAgent] Trying AppleScript click for '%s'",
                element_desc,
            )
            try:
                safe_desc = element_desc.replace('"', '\\"')
                safe_app = app_name.replace('"', '\\"')
                script = (
                    f'tell application "System Events" to tell process "{safe_app}" '
                    f'to click (first UI element of window 1 whose description contains "{safe_desc}")'
                )
                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", script,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=10.0)
                return proc.returncode == 0
            except Exception as exc:
                logger.debug(
                    "[NativeAppControlAgent] AppleScript click failed: %s", exc
                )

        logger.warning(
            "[NativeAppControlAgent] Could not click element — all strategies exhausted: %s",
            detail,
        )
        return False

    async def _cg_click(self, x: int, y: int) -> None:
        """Click at exact screen coordinates.

        Tries cliclick first (lightweight C tool, no AppleScript overhead).
        Falls back to the existing _click method which already has an AppleScript
        fallback baked in, so the behaviour is unchanged for callers that used
        the old direct-coordinate path.

        Args:
            x: Horizontal screen coordinate in points.
            y: Vertical screen coordinate in points.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "cliclick", f"c:{x},{y}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10.0)
            if proc.returncode == 0:
                logger.debug("[NativeAppControlAgent] _cg_click(%d, %d) via cliclick", x, y)
                return
        except (FileNotFoundError, asyncio.TimeoutError, Exception) as exc:
            logger.debug(
                "[NativeAppControlAgent] cliclick unavailable (%s) — falling back to AppleScript.",
                exc,
            )

        # AppleScript fallback (reuse existing _click internals)
        await self._click(x, y)

    async def _click(self, x: int, y: int) -> None:
        """Click at screen coordinates (x, y).

        Tries cliclick first (lightweight); falls back to AppleScript.
        All invocations use asyncio.create_subprocess_exec (no shell=True).
        """
        # Attempt 1: cliclick (preferred -- faster and more reliable)
        try:
            proc = await asyncio.create_subprocess_exec(
                "cliclick", f"c:{x},{y}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode == 0:
                logger.debug("[NativeAppControlAgent] click(%d, %d) via cliclick", x, y)
                return
        except (FileNotFoundError, asyncio.TimeoutError, Exception) as exc:
            logger.debug(
                "[NativeAppControlAgent] cliclick unavailable (%s) -- falling back to AppleScript.",
                exc,
            )

        # Attempt 2: AppleScript System Events
        script = f"tell application \"System Events\" to click at {{{x}, {y}}}"
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10.0)
        logger.debug("[NativeAppControlAgent] click(%d, %d) via AppleScript", x, y)

    async def _type_text(self, text: str) -> None:
        """Type text using AppleScript keystroke.

        Escapes backslashes and double-quotes so the generated AppleScript
        string literal is always valid.
        """
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "System Events" to keystroke "{escaped}"'
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=15.0)
        logger.debug(
            "[NativeAppControlAgent] type_text length=%d via AppleScript", len(text)
        )

    async def _press_key(self, key: str) -> None:
        """Press a named key using AppleScript key code.

        Key names are resolved via the dynamic key-code map.  Unknown key
        names fall back to a direct keystroke call.
        """
        key_lower = key.strip().lower()
        key_code = self._key_codes.get(key_lower)

        if key_code is not None:
            script = f"tell application \"System Events\" to key code {key_code}"
        else:
            escaped = key_lower.replace("\\", "\\\\").replace('"', '\\"')
            script = f'tell application "System Events" to keystroke "{escaped}"'
            logger.debug(
                "[NativeAppControlAgent] Unknown key name '%s' -- using keystroke fallback.",
                key,
            )

        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10.0)
        logger.debug("[NativeAppControlAgent] press_key('%s') via AppleScript", key)

    async def _scroll(self, direction: str, amount: int = 3) -> None:
        """Scroll in direction by amount units via AppleScript.

        Args:
            direction: "up" or "down".
            amount: Number of scroll units (default 3).
        """
        direction_lower = direction.strip().lower()
        # AppleScript scroll uses positive for down, negative for up
        scroll_amount = abs(amount) if direction_lower == "down" else -abs(amount)

        script = (
            "tell application \"System Events\" to scroll "
            "(the first window of (the first application process whose frontmost is true)) "
            f"by {scroll_amount}"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
            logger.debug(
                "[NativeAppControlAgent] scroll(direction=%s, amount=%d)", direction, amount
            )
        except Exception as exc:
            logger.warning(
                "[NativeAppControlAgent] Scroll via AppleScript failed: %s", exc
            )

    # ------------------------------------------------------------------
    # Post-action verification
    # ------------------------------------------------------------------

    async def _verify_action(
        self,
        app_name: str,
        action_description: str,
        expected_result: str,
        max_retries: int = 2,
    ) -> bool:
        """Verify that the previous action succeeded by taking a post-action screenshot.

        Queries J-Prime vision (free) first, then Claude API (paid fallback).
        If no vision model is reachable the method returns True so the main loop
        is never blocked by an unavailable verifier.

        Args:
            app_name:           The macOS application being controlled.
            action_description: Human-readable description of the action that was taken.
            expected_result:    The plan step whose completion is being verified.
            max_retries:        How many additional attempts to make if verification
                                returns False (not counting the initial check).

        Returns:
            True when verified (or when no model is available), False when all
            attempts conclude the action did not succeed.
        """
        await asyncio.sleep(0.5)  # Wait for UI to update

        import re as _re

        verify_prompt = (
            f"I just performed this action in {app_name}: {action_description}\n"
            f"The expected result is: {expected_result}\n\n"
            f"Look at the screenshot. Did the action succeed? "
            f'Respond with JSON: {{"verified": true/false, "reason": "..."}}'
        )

        for attempt in range(max_retries + 1):
            if attempt > 0:
                logger.debug(
                    "[NativeAppControlAgent] Verification retry %d/%d for: %s",
                    attempt, max_retries, action_description,
                )
                await asyncio.sleep(0.5)

            screenshot_b64 = await self._take_screenshot()
            if not screenshot_b64:
                logger.debug(
                    "[NativeAppControlAgent] Verification skipped — screenshot unavailable."
                )
                return True  # Can't verify; don't block the loop

            # --- Attempt 1: J-Prime vision server ---
            try:
                from backend.core.prime_client import get_prime_client

                client = await asyncio.wait_for(get_prime_client(), timeout=5.0)
                vision_ok = await asyncio.wait_for(client.get_vision_health(), timeout=5.0)
                if vision_ok:
                    response = await asyncio.wait_for(
                        client.send_vision_request(
                            image_base64=screenshot_b64,
                            prompt=verify_prompt,
                            max_tokens=100,
                            temperature=0.0,
                        ),
                        timeout=30.0,
                    )
                    if response and response.content:
                        match = _re.search(r'\{[\s\S]*\}', response.content)
                        if match:
                            data = json.loads(match.group())
                            verified = bool(data.get("verified", False))
                            logger.debug(
                                "[NativeAppControlAgent] J-Prime verification=%s reason=%s",
                                verified, data.get("reason", ""),
                            )
                            if verified:
                                return True
                            continue  # Retry
            except Exception as exc:
                logger.debug(
                    "[NativeAppControlAgent] Verification via J-Prime failed: %s", exc
                )

            # --- Attempt 2: Claude API fallback ---
            try:
                import anthropic

                api_key = os.getenv("ANTHROPIC_API_KEY", "")
                if api_key:
                    claude_client = anthropic.AsyncAnthropic(api_key=api_key)
                    _selection = _get_brain_router().select_for_task("vision_verification")
                    model = os.getenv(
                        "JARVIS_NATIVE_CONTROL_CLAUDE_MODEL",
                    ) or _selection.claude_model
                    claude_msg = await asyncio.wait_for(
                        claude_client.messages.create(
                            model=model,
                            max_tokens=100,
                            messages=[
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "image",
                                            "source": {
                                                "type": "base64",
                                                "media_type": "image/jpeg",
                                                "data": screenshot_b64,
                                            },
                                        },
                                        {"type": "text", "text": verify_prompt},
                                    ],
                                }
                            ],
                        ),
                        timeout=30.0,
                    )
                    if claude_msg.content:
                        match = _re.search(r'\{[\s\S]*\}', claude_msg.content[0].text)
                        if match:
                            data = json.loads(match.group())
                            verified = bool(data.get("verified", False))
                            logger.debug(
                                "[NativeAppControlAgent] Claude verification=%s reason=%s",
                                verified, data.get("reason", ""),
                            )
                            if verified:
                                return True
                            continue  # Retry
            except Exception as exc:
                logger.debug(
                    "[NativeAppControlAgent] Verification via Claude failed: %s", exc
                )

            # Neither model is available for this attempt — assume success
            logger.debug(
                "[NativeAppControlAgent] No vision model available for verification; "
                "assuming success."
            )
            return True

        # All retries exhausted without a positive verification
        return False
