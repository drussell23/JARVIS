"""Command Sender — signs and sends commands to Vercel."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp

from brainstem.auth import BrainstemAuth
from brainstem.config import BrainstemConfig

logger = logging.getLogger("jarvis.brainstem.sender")


class CommandSender:
    def __init__(self, config: BrainstemConfig, auth: BrainstemAuth) -> None:
        self._config = config
        self._auth = auth
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def build_payload(
        self,
        text: str,
        priority: str = "realtime",
        response_mode: str = "stream",
        intent_hint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "command_id": str(uuid.uuid4()),
            "device_id": self._config.device_id,
            "device_type": self._config.device_type,
            "text": text,
            "priority": priority,
            "response_mode": response_mode,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if intent_hint:
            payload["intent_hint"] = intent_hint
        if context:
            payload["context"] = context
        payload["signature"] = self._auth.sign(payload)
        return payload

    async def send_command(
        self,
        text: str,
        priority: str = "realtime",
        response_mode: str = "stream",
        intent_hint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = self.build_payload(
            text=text,
            priority=priority,
            response_mode=response_mode,
            intent_hint=intent_hint,
            context=context,
        )
        session = await self._get_session()
        url = f"{self._config.vercel_url}/api/command"
        logger.info("[Sender] %s -> %s", text[:50], url)
        async with session.post(url, json=payload) as resp:
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                return {"status": "streaming", "command_id": payload["command_id"]}
            else:
                return await resp.json()

    async def get_frontmost_app(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                'tell application "System Events" to get name of first application process whose frontmost is true',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode().strip() if stdout else "unknown"
        except Exception:
            return "unknown"

    async def close(self) -> None:
        if self._session:
            await self._session.close()
