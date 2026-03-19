"""
JARVIS Neural Mesh — NativeAppControlAgent

Drives installed macOS applications using a vision-action loop:

  1. Verify app is installed via AppInventoryService (optional, graceful).
  2. Activate the target app via osascript.
  3. Capture a screenshot (screencapture -x -C).
  4. Send screenshot + goal to J-Prime vision server (free) for next-action
     inference; falls back to Claude API (paid) when J-Prime is unavailable.
  5. Execute the decided action (click / type / key / scroll).
  6. Repeat until the goal is achieved or max steps is reached.

Configuration (all via environment variables — no hardcoding):
  JARVIS_NATIVE_CONTROL_MAX_STEPS   — maximum loop iterations (default 10)
  JARVIS_NATIVE_CONTROL_STEP_DELAY  — seconds to wait between steps (default 1.0)
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

_VISION_PROMPT_TEMPLATE = """You are a macOS automation assistant controlling the application: {app_name}.

Current goal: {goal}

Steps taken so far:
{previous_actions_text}

Look at the screenshot and decide the SINGLE next action to achieve the goal.

Respond ONLY with valid JSON in this exact format:
{{
  "done": false,
  "action_type": "click",
  "detail": {{"x": 150, "y": 200}},
  "message": "Clicking the submit button"
}}

action_type must be one of: click, type, key, scroll, done
- click: requires detail.x (int) and detail.y (int)
- type: requires detail.text (str)
- key: requires detail.key (str, e.g. "enter", "tab", "escape")
- scroll: requires detail.direction (str: "up" or "down") and detail.amount (int, pixels)
- done: set "done": true and omit action_type / detail

If the goal is already accomplished set "done": true.
If you cannot determine a useful next action set "done": true with an explanatory message."""


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
        max_steps: int = int(os.getenv("JARVIS_NATIVE_CONTROL_MAX_STEPS", "10"))
        step_delay: float = float(os.getenv("JARVIS_NATIVE_CONTROL_STEP_DELAY", "1.0"))

        # --- Activate the app -------------------------------------------------
        await self._activate_app(app_name)
        await asyncio.sleep(step_delay)

        # --- Vision-action loop -----------------------------------------------
        actions_taken: List[Dict[str, Any]] = []
        final_message = f"Reached maximum steps ({max_steps}) without completing goal."
        success = False

        for step in range(max_steps):
            logger.debug(
                "[NativeAppControlAgent] Step %d/%d -- app=%s goal=%s",
                step + 1,
                max_steps,
                app_name,
                goal,
            )

            # Capture screenshot
            screenshot_b64 = await self._take_screenshot()
            if screenshot_b64 is None:
                logger.warning(
                    "[NativeAppControlAgent] Screenshot failed at step %d.", step + 1
                )
                final_message = "Screenshot capture failed."
                break

            # Ask vision model for next action
            decision = await self._ask_jprime_for_action(
                screenshot_b64, goal, app_name, actions_taken
            )

            msg = decision.get("message", "")
            action_type = decision.get("action_type", "")
            detail = decision.get("detail", {})
            done = bool(decision.get("done", False))

            actions_taken.append(
                {
                    "step": step + 1,
                    "action_type": action_type if not done else "done",
                    "detail": detail,
                    "message": msg,
                }
            )

            if done:
                success = True
                final_message = msg or "Goal achieved."
                logger.info(
                    "[NativeAppControlAgent] Goal achieved after %d step(s): %s",
                    step + 1,
                    final_message,
                )
                break

            # Execute the decided action
            try:
                if action_type == "click":
                    await self._click(int(detail.get("x", 0)), int(detail.get("y", 0)))
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
                        "[NativeAppControlAgent] Unknown action_type '%s' at step %d.",
                        action_type,
                        step + 1,
                    )
            except Exception as exc:
                logger.warning(
                    "[NativeAppControlAgent] Action execution error at step %d: %s",
                    step + 1,
                    exc,
                )

            await asyncio.sleep(step_delay)

        return {
            "success": success,
            "goal": goal,
            "app": app_name,
            "steps_taken": len(actions_taken),
            "actions": actions_taken,
            "final_message": final_message,
        }

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
    ) -> Dict[str, Any]:
        """Determine the next UI action using J-Prime vision or Claude API.

        Decision hierarchy:
          1. J-Prime vision server (free, GCP LLaVA) -- tried first.
          2. Claude API vision (paid) -- fallback when J-Prime unavailable.
          3. No-op return -- when both are unavailable.

        Args:
            screenshot_b64: Base64-encoded PNG of the current screen.
            goal: The automation goal.
            app_name: Name of the target application.
            previous_actions: List of dicts describing actions already taken.

        Returns:
            Dict with keys: done, action_type, detail, message.
        """
        previous_actions_text = (
            "\n".join(
                f"  Step {a['step']}: [{a['action_type']}] {a['message']}"
                for a in previous_actions
            )
            if previous_actions
            else "  (none yet)"
        )

        prompt = _VISION_PROMPT_TEMPLATE.format(
            app_name=app_name,
            goal=goal,
            previous_actions_text=previous_actions_text,
        )

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

            claude_model = os.getenv(
                "JARVIS_NATIVE_CONTROL_CLAUDE_MODEL",
                "claude-sonnet-4-20250514",
            )

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
