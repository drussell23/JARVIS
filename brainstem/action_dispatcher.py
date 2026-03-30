"""Action Dispatcher — routes SSE events to local hardware."""
import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, Optional

logger = logging.getLogger("jarvis.brainstem.dispatch")

_DANGEROUS_COMMANDS = frozenset(["rm -rf /", "rm -rf ~", "mkfs", "> /dev/", "dd if=", ":(){", "chmod -R 777 /"])


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

        logger.info("[Dispatch] VLA executing goal: %s (screenshot=%s)", goal[:80], "yes" if screenshot_b64 else "no")
        if self.tts_speak:
            await self.tts_speak(f"On it. Executing: {goal[:60]}")

        try:
            result = await self.jarvis_cu.execute_goal(goal, screenshot_b64=screenshot_b64)
        except Exception as e:
            logger.error("[Dispatch] VLA execution failed: %s", e)
            if self.tts_speak:
                await self.tts_speak(f"Action failed: {e}")
            return

        if result is None:
            if self.tts_speak:
                await self.tts_speak("Vision pipeline couldn't start. Check screen recording permissions.")
            return

        # Narrate result
        if result.get("success"):
            completed = result.get("steps_completed", 0)
            total = result.get("steps_total", 0)
            elapsed = result.get("elapsed_s", 0)
            layers = result.get("layers_used", {})
            layer_summary = ", ".join(f"{k}: {v}" for k, v in layers.items()) if layers else "none"
            narration = f"Done. Completed {completed} of {total} steps in {elapsed:.1f} seconds. Layers used: {layer_summary}."
            logger.info("[Dispatch] VLA success: %s", narration)
        else:
            error = result.get("error", "unknown error")
            completed = result.get("steps_completed", 0)
            total = result.get("steps_total", 0)
            narration = f"Action incomplete. Finished {completed} of {total} steps. Issue: {error}"
            logger.warning("[Dispatch] VLA partial: %s", narration)

        if self.tts_speak:
            await self.tts_speak(narration)
