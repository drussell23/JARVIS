"""Action Dispatcher — routes SSE events to local hardware."""
import asyncio
import logging
import os
import re
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

logger = logging.getLogger("jarvis.brainstem.dispatch")

_DANGEROUS_COMMANDS = frozenset(["rm -rf /", "rm -rf ~", "mkfs", "> /dev/", "dd if=", ":(){", "chmod -R 777 /"])

# ---------------------------------------------------------------------------
# Message signature — appended to outgoing messages so recipients know
# the message was sent by JARVIS, not the user typing manually.
# Disable via JARVIS_MSG_SIGNATURE="" in env.
# ---------------------------------------------------------------------------
_MSG_SIGNATURE = os.environ.get("JARVIS_MSG_SIGNATURE", "🤖 - sent via JARVIS")

# ---------------------------------------------------------------------------
# Tier 0: Deterministic fast-path patterns (no model call, <100ms)
# ---------------------------------------------------------------------------

# "open WhatsApp", "launch Safari", "open the terminal"
_OPEN_APP_PATTERN = re.compile(
    r"^(?:open|launch|start|run)\s+(?:the\s+)?(.+?)(?:\s+app)?$",
    re.IGNORECASE,
)

# "open WhatsApp and message Zach" → app="WhatsApp", remainder="message Zach"
_OPEN_APP_AND_PATTERN = re.compile(
    r"^(?:open|launch|start)\s+(?:the\s+)?(.+?)\s+and\s+(.+)$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Messaging intent patterns — detect "message X" WITHOUT an app name.
# These fire BEFORE Tier 0 to resolve which app to use via MessagingRouter.
# ---------------------------------------------------------------------------

# "message Delia on WhatsApp saying ..." — explicit app, just rewrite goal
_MSG_WITH_APP_PATTERN = re.compile(
    r"^(?:message|msg|text)\s+(.+?)\s+on\s+(\S+)\s+(?:saying\s+)?(.*)$",
    re.IGNORECASE,
)

# Patterns matched top-to-bottom; first match wins.
_MESSAGING_PATTERNS = [
    # With "saying" delimiter (handles multi-word contact names)
    re.compile(r"^(?:message|msg)\s+(.+?)\s+saying\s+(.+)$", re.IGNORECASE),
    re.compile(r"^send\s+(?:a\s+)?message\s+to\s+(.+?)\s+saying\s+(.+)$", re.IGNORECASE),
    re.compile(r"^send\s+(.+?)\s+a\s+message\s+saying\s+(.+)$", re.IGNORECASE),
    re.compile(r"^text\s+(.+?)\s+saying\s+(.+)$", re.IGNORECASE),
    # Without "saying" — single-word contact, rest is message
    re.compile(r"^(?:message|msg)\s+(\S+)\s+(.+)$", re.IGNORECASE),
    re.compile(r"^text\s+(\S+)\s+(.+)$", re.IGNORECASE),
    # No message body — just open conversation
    re.compile(r"^(?:message|msg|text)\s+(\S+(?:\s+\S+)?)$", re.IGNORECASE),
    re.compile(r"^send\s+(\S+(?:\s+\S+)?)\s+a\s+message$", re.IGNORECASE),
    re.compile(r"^send\s+(?:a\s+)?message\s+to\s+(\S+(?:\s+\S+)?)$", re.IGNORECASE),
]


def _parse_messaging_intent(goal: str) -> Optional[Tuple[str, str]]:
    """Extract (contact, message_body) from a messaging command without app name.

    Returns None when the goal already specifies an app or isn't messaging.
    """
    g = goal.strip()

    # Already handled by Tier 0 app-launch patterns
    if _OPEN_APP_AND_PATTERN.match(g) or _OPEN_APP_PATTERN.match(g):
        return None

    # Already specifies "on <app>" — handled separately
    if _MSG_WITH_APP_PATTERN.match(g):
        return None

    for pattern in _MESSAGING_PATTERNS:
        m = pattern.match(g)
        if m:
            contact = m.group(1).strip()
            body = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else ""
            return contact, body

    return None


def _sign_message(body: str) -> str:
    """Append the JARVIS signature to an outgoing message body.

    The signature tells recipients the message was sent by JARVIS,
    not the user typing manually.  Disabled when JARVIS_MSG_SIGNATURE="".
    """
    if not _MSG_SIGNATURE or not body:
        return body
    return f"{body} {_MSG_SIGNATURE}"


def _rewrite_msg_with_app(goal: str) -> Optional[str]:
    """Rewrite 'message X on APP saying Y' → 'open APP and message X saying Y'."""
    m = _MSG_WITH_APP_PATTERN.match(goal.strip())
    if not m:
        return None
    contact = m.group(1).strip()
    app = m.group(2).strip()
    body = m.group(3).strip()
    if body:
        body = _sign_message(body)
        return f"open {app} and message {contact} saying {body}"
    return f"open {app} and message {contact}"


def _parse_app_launch(goal: str) -> Optional[Tuple[str, Optional[str]]]:
    """Extract app name and optional remainder from a goal string.

    Returns (app_name, remainder) or None if not an app-launch command.
    Examples:
        "open WhatsApp"                        → ("WhatsApp", None)
        "open WhatsApp and message Zach"       → ("WhatsApp", "message Zach saying what's up")
        "launch Safari"                        → ("Safari", None)
        "what's the weather?"                  → None
    """
    m = _OPEN_APP_AND_PATTERN.match(goal.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = _OPEN_APP_PATTERN.match(goal.strip())
    if m:
        return m.group(1).strip(), None
    return None


class ActionDispatcher:
    def __init__(
        self,
        hud: Any,
        ghost_hands: Any = None,
        tts_speak: Optional[Callable[..., Coroutine]] = None,
        jarvis_cu: Any = None,
    ) -> None:
        self.hud = hud
        self.ghost_hands = ghost_hands
        self.tts_speak = tts_speak
        self.jarvis_cu = jarvis_cu
        self._active_streams: Dict[str, list[str]] = {}

    async def dispatch(self, event_type: str, data: Dict[str, Any]) -> None:
        handler = self._HANDLERS.get(event_type)
        if handler:
            await handler(self, data)
        else:
            logger.debug("[Dispatch] Unknown event: %s", event_type)

    async def _handle_token(self, data: Dict[str, Any]) -> None:
        cmd_id = data.get("command_id", "")
        token = data.get("token", "")
        if cmd_id not in self._active_streams:
            self._active_streams[cmd_id] = []
            self.hud.begin_stream(cmd_id, source=data.get("source_brain", "claude"))
        self._active_streams[cmd_id].append(token)
        self.hud.append_token(cmd_id, token)

    async def _handle_action(self, data: Dict[str, Any]) -> None:
        action_type = data.get("action_type", "")
        payload = data.get("payload", {})
        cmd_id = data.get("command_id", "")
        logger.info("[Dispatch] Action: %s (cmd=%s)", action_type, cmd_id)
        self.hud.show_action(action_type, payload)
        try:
            if action_type == "ghost_hands":
                await self._exec_ghost_hands(payload)
            elif action_type == "file_edit":
                await self._exec_file_edit(payload)
            elif action_type == "terminal":
                await self._exec_terminal(payload)
            elif action_type == "notification":
                await self._exec_notification(payload)
            elif action_type == "vision_task":
                await self._exec_vision_task(payload)
            else:
                logger.warning("[Dispatch] Unknown action: %s", action_type)
        except Exception as e:
            logger.error("[Dispatch] Action failed: %s — %s", action_type, e)
            self.hud.show_error(f"Action failed: {e}")

    async def _handle_daemon(self, data: Dict[str, Any]) -> None:
        text = data.get("narration_text", "")
        priority = data.get("narration_priority", "ambient")
        source = data.get("source_brain", "unknown")
        if priority == "ambient":
            self.hud.show_daemon(text, source=source)
        elif priority == "informational":
            self.hud.show_daemon(text, source=source)
            if self.tts_speak:
                await self.tts_speak(text)
        elif priority == "urgent":
            self.hud.show_daemon(text, source=source, urgent=True)
            if self.tts_speak:
                await self.tts_speak(text)
            await self._exec_notification({"title": "JARVIS — Urgent", "body": text})

    async def _handle_status(self, data: Dict[str, Any]) -> None:
        self.hud.show_progress(
            data.get("command_id", ""),
            phase=data.get("phase", ""),
            progress=data.get("progress"),
            message=data.get("message", ""),
        )

    async def _handle_complete(self, data: Dict[str, Any]) -> None:
        cmd_id = data.get("command_id", "")
        self._active_streams.pop(cmd_id, None)
        self.hud.complete_stream(
            cmd_id,
            source=data.get("source_brain"),
            latency_ms=data.get("latency_ms"),
            artifacts=data.get("artifacts"),
        )

    _HANDLERS = {
        "token": _handle_token,
        "action": _handle_action,
        "daemon": _handle_daemon,
        "status": _handle_status,
        "complete": _handle_complete,
    }

    async def _exec_ghost_hands(self, payload: dict) -> None:
        if self.ghost_hands is None:
            logger.warning("[Dispatch] Ghost Hands not initialized")
            return
        if "click" in payload:
            x, y = payload["click"]
            await self.ghost_hands.click(
                app_name=payload.get("app"),
                coordinates=(float(x), float(y)),
            )
        elif "type_text" in payload:
            await self.ghost_hands.type_text(payload["type_text"])

    async def _exec_file_edit(self, payload: dict) -> None:
        import aiofiles
        path = payload.get("path", "")
        if not path:
            return
        if "content" in payload:
            async with aiofiles.open(path, "w") as f:
                await f.write(payload["content"])
            logger.info("[Dispatch] File written: %s", path)
        elif "diff" in payload:
            proc = await asyncio.create_subprocess_exec(
                "patch", "-p0", path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate(input=payload["diff"].encode())
            logger.info("[Dispatch] Diff applied: %s (exit=%d)", path, proc.returncode)

    async def _exec_terminal(self, payload: dict) -> None:
        cmd = payload.get("command", "")
        if not cmd:
            return
        for danger in _DANGEROUS_COMMANDS:
            if danger in cmd:
                logger.error("[Dispatch] BLOCKED dangerous command: %s", cmd)
                self.hud.show_error(f"Blocked dangerous: {cmd}")
                return
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=payload.get("cwd"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        logger.info("[Dispatch] Terminal: %s -> exit %d", cmd[:50], proc.returncode)

    async def _exec_notification(self, payload: dict) -> None:
        title = payload.get("title", "JARVIS").replace('"', '\\"')
        body = payload.get("body", "").replace('"', '\\"')
        await asyncio.create_subprocess_exec(
            "osascript", "-e",
            f'display notification "{body}" with title "{title}"',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _exec_vision_task(self, payload: dict) -> None:
        goal = payload.get("goal", "")
        if not goal:
            logger.warning("[Dispatch] vision_task with empty goal — skipped")
            return

        screenshot_b64 = payload.get("screenshot")

        # Activate vision pipeline on demand if not already running
        if self.jarvis_cu is None:
            logger.warning("[Dispatch] JarvisCU not initialized — vision task skipped")
            if self.tts_speak:
                await self.tts_speak("Vision backend is not available. I can't execute screen actions right now.")
            return

        source = payload.get("source", "")
        logger.info("[Dispatch] VLA executing goal: %s (screenshot=%s, source=%s)", goal[:80], "yes" if screenshot_b64 else "no", source or "sse")

        # Track messaging context for learn() after success
        _msg_contact: Optional[str] = None
        _msg_app: Optional[str] = None

        # ----- Rewrite "message X on APP saying Y" → "open APP and ..." -----
        rewritten = _rewrite_msg_with_app(goal)
        if rewritten is not None:
            logger.info("[Dispatch] Rewrote messaging goal → '%s'", rewritten)
            goal = rewritten

        # ----- Messaging Router: resolve app for app-less messaging -----
        messaging_intent = _parse_messaging_intent(goal)
        if messaging_intent is not None:
            contact, body = messaging_intent
            routing = await self._resolve_messaging_app(contact, body)
            if routing is not None:
                _msg_contact = contact
                _msg_app = routing.app_name
                if body:
                    signed_body = _sign_message(body)
                    goal = f"open {routing.app_name} and message {contact} saying {signed_body}"
                else:
                    goal = f"open {routing.app_name} and message {contact}"
                logger.info(
                    "[Dispatch] MessagingRouter: '%s' → %s (source=%s, conf=%.2f, %.0fms)",
                    contact, routing.app_name, routing.source,
                    routing.confidence, routing.routing_time_ms,
                )
                if self.tts_speak:
                    if body:
                        await self.tts_speak(
                            f"Sending your message to {contact} on {routing.app_name}."
                        )
                    else:
                        await self.tts_speak(
                            f"Opening a conversation with {contact} on {routing.app_name}."
                        )

        # ----- Tier 0: Deterministic fast-path for app launch (<100ms) -----
        app_launch = _parse_app_launch(goal)
        if app_launch is not None:
            app_name, remainder = app_launch
            logger.info("[Dispatch] Tier 0 fast-path: launching '%s' via macOS open", app_name)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "open", "-a", app_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr_data = await proc.communicate()
                if proc.returncode == 0:
                    logger.info("[Dispatch] Tier 0: '%s' launched (exit 0)", app_name)
                    if remainder:
                        logger.info("[Dispatch] Tier 0 → vision: remainder goal: %s", remainder)
                        await asyncio.sleep(1.0)
                        goal = remainder
                        screenshot_b64 = None
                    else:
                        if self.tts_speak:
                            await self.tts_speak(f"Opened {app_name}.")
                        return
                else:
                    err = stderr_data.decode("utf-8", errors="replace").strip() if stderr_data else "unknown"
                    logger.warning("[Dispatch] Tier 0: 'open -a %s' failed (exit %d): %s", app_name, proc.returncode, err)
            except Exception as e:
                logger.warning("[Dispatch] Tier 0 error: %s — falling back to vision", e)

        # Narrate what we're about to do (skip if messaging router already spoke)
        if self.tts_speak and source != "local_fast_path" and not _msg_contact:
            await self.tts_speak(f"On it. Executing: {goal[:60]}")

        try:
            result = await self.jarvis_cu.execute_goal(goal, screenshot_b64=screenshot_b64)
        except Exception as e:
            logger.error("[Dispatch] VLA execution failed: %s", e)
            if self.tts_speak:
                await self.tts_speak(f"Sorry, that action failed. {e}")
            return

        if result is None:
            if self.tts_speak:
                await self.tts_speak("Vision pipeline couldn't start. Check screen recording permissions.")
            return

        # --- Narrate result (human-friendly, messaging-aware) ---
        completed = result.get("steps_completed", 0)
        total = result.get("steps_total", 0)
        elapsed = result.get("elapsed_s", 0)

        if result.get("success"):
            layers = result.get("layers_used", {})
            layer_summary = ", ".join(f"{k}: {v}" for k, v in layers.items()) if layers else "none"
            logger.info(
                "[Dispatch] VLA success: %d/%d steps, %.1fs, layers: %s",
                completed, total, elapsed, layer_summary,
            )

            # Learn messaging route on success
            if _msg_contact and _msg_app:
                self._learn_messaging_route(_msg_contact, _msg_app)

            # Human-friendly TTS narration
            if self.tts_speak:
                if _msg_contact:
                    narration = f"Done. Your message to {_msg_contact} has been sent on {_msg_app}."
                else:
                    narration = f"Done. Completed in {elapsed:.1f} seconds."
                await self.tts_speak(narration)
        else:
            error = result.get("error", "unknown error")
            logger.warning(
                "[Dispatch] VLA partial: %d/%d steps. Error: %s",
                completed, total, error,
            )
            if self.tts_speak:
                if _msg_contact:
                    narration = (
                        f"I had trouble sending that message to {_msg_contact}. "
                        f"Got through {completed} of {total} steps. {error}"
                    )
                else:
                    narration = (
                        f"Action incomplete. Finished {completed} of {total} steps. {error}"
                    )
                await self.tts_speak(narration)

    # ------------------------------------------------------------------
    # Messaging Router integration
    # ------------------------------------------------------------------

    async def _resolve_messaging_app(self, contact: str, message: str) -> Optional[Any]:
        """Route a messaging command to the correct app via MessagingRouter."""
        try:
            from backend.system.messaging_router import get_messaging_router
            router = get_messaging_router()
            return await router.route(contact, message)
        except Exception as exc:
            logger.warning("[Dispatch] MessagingRouter failed: %s", exc)
            return None

    def _learn_messaging_route(self, contact: str, app: str) -> None:
        """Record a successful messaging route for future lookups."""
        try:
            from backend.system.messaging_router import get_messaging_router
            get_messaging_router().learn(contact, app)
        except Exception as exc:
            logger.debug("[Dispatch] MessagingRouter learn failed: %s", exc)
