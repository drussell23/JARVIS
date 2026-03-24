"""
Silent Actuator -- async client for the CGEvent worker subprocess.

Replaces pyautogui with a persistent CGEvent worker that posts
clicks, keystrokes, and typing without visible cursor hijack.

Usage::

    actuator = SilentActuator.get_instance()
    await actuator.start()
    await actuator.click(500, 300)
    await actuator.key("return")
    await actuator.type_text("hello world")
    await actuator.scroll(-3)
    await actuator.stop()

All tunables are environment-variable driven.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_COMMAND_TIMEOUT_S = float(os.environ.get("ACTUATOR_COMMAND_TIMEOUT_S", "5.0"))
_WORKER_START_TIMEOUT_S = float(os.environ.get("ACTUATOR_START_TIMEOUT_S", "10.0"))


class SilentActuator:
    """Async interface to the CGEvent worker subprocess."""

    _instance: Optional["SilentActuator"] = None

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._started = False

    @classmethod
    def get_instance(cls) -> "SilentActuator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Start the CGEvent worker subprocess."""
        if self._started and self._proc and self._proc.returncode is None:
            return True

        worker_path = os.path.join(
            os.path.dirname(__file__), "cgevent_worker.py",
        )

        try:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, worker_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Wait for "ready" message
            ready_line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=_WORKER_START_TIMEOUT_S,
            )
            ready = json.loads(ready_line.decode().strip())

            if ready.get("ok") and ready.get("status") == "ready":
                self._started = True
                logger.info(
                    "[SilentActuator] CGEvent worker started (pid=%s)",
                    ready.get("pid"),
                )
                return True
            else:
                logger.error("[SilentActuator] Worker failed to start: %s", ready)
                return False

        except asyncio.TimeoutError:
            logger.error("[SilentActuator] Worker start timed out")
            await self._kill_proc()
            return False
        except Exception as exc:
            logger.error("[SilentActuator] Worker start error: %s", exc)
            await self._kill_proc()
            return False

    async def stop(self) -> None:
        """Stop the worker subprocess."""
        await self._kill_proc()
        self._started = False

    async def _kill_proc(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass
        self._proc = None

    async def _ensure_running(self) -> bool:
        """Ensure worker is alive, restart if needed."""
        if self._proc and self._proc.returncode is None:
            return True
        logger.warning("[SilentActuator] Worker died, restarting...")
        self._started = False
        return await self.start()

    # ------------------------------------------------------------------
    # Command interface
    # ------------------------------------------------------------------

    async def _send_command(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Send a command to the worker and return the response."""
        async with self._lock:
            if not await self._ensure_running():
                return {"ok": False, "error": "Worker not running"}

            try:
                line = json.dumps(cmd) + "\n"
                self._proc.stdin.write(line.encode())
                await self._proc.stdin.drain()

                resp_line = await asyncio.wait_for(
                    self._proc.stdout.readline(),
                    timeout=_COMMAND_TIMEOUT_S,
                )
                return json.loads(resp_line.decode().strip())

            except asyncio.TimeoutError:
                logger.error("[SilentActuator] Command timed out: %s", cmd.get("cmd"))
                return {"ok": False, "error": "timeout"}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[SilentActuator] Command error: %s", exc)
                return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------

    async def click(self, x: int, y: int) -> bool:
        """Click at screen coordinates."""
        result = await self._send_command({"cmd": "click", "x": x, "y": y})
        if result.get("ok"):
            logger.info("[SilentActuator] CLICK (%d, %d)", x, y)
        else:
            logger.error("[SilentActuator] CLICK failed: %s", result.get("error"))
        return result.get("ok", False)

    async def key(self, name: str) -> bool:
        """Press a key by name (return, tab, escape, etc.)."""
        result = await self._send_command({"cmd": "key", "name": name})
        if result.get("ok"):
            logger.info("[SilentActuator] KEY '%s'", name)
        else:
            logger.error("[SilentActuator] KEY failed: %s", result.get("error"))
        return result.get("ok", False)

    async def type_text(self, text: str) -> bool:
        """Type text via clipboard paste."""
        result = await self._send_command({"cmd": "type", "text": text})
        if result.get("ok"):
            logger.info("[SilentActuator] TYPE '%s'", text[:50])
        else:
            logger.error("[SilentActuator] TYPE failed: %s", result.get("error"))
        return result.get("ok", False)

    async def scroll(self, amount: int, x: int = 0, y: int = 0) -> bool:
        """Scroll at position."""
        result = await self._send_command({
            "cmd": "scroll", "amount": amount, "x": x, "y": y,
        })
        if result.get("ok"):
            logger.info("[SilentActuator] SCROLL %d", amount)
        else:
            logger.error("[SilentActuator] SCROLL failed: %s", result.get("error"))
        return result.get("ok", False)

    async def ping(self) -> bool:
        """Health check."""
        result = await self._send_command({"cmd": "ping"})
        return result.get("ok", False)
