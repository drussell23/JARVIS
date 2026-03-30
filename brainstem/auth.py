"""HMAC authentication for the brainstem."""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger("jarvis.brainstem.auth")

CANONICAL_FIELDS = [
    "command_id", "device_id", "device_type",
    "priority", "response_mode", "text", "timestamp",
]


class BrainstemAuth:
    def __init__(self, device_id: str, device_secret: str):
        self.device_id = device_id
        self._secret_bytes = bytes.fromhex(device_secret)
        self._stream_token: Optional[str] = None

    def canonicalize(self, payload: Dict[str, Any]) -> str:
        parts = [f"{k}={payload[k]}" for k in CANONICAL_FIELDS]
        if "intent_hint" in payload and payload["intent_hint"]:
            parts.insert(3, f"intent_hint={payload['intent_hint']}")
        if "context" in payload and payload["context"]:
            sorted_ctx = json.dumps(payload["context"], sort_keys=True)
            parts.append(f"context={sorted_ctx}")
        return "&".join(parts)

    def sign(self, payload: Dict[str, Any]) -> str:
        canonical = self.canonicalize(payload)
        return hmac.new(
            self._secret_bytes,
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def get_stream_token(self, session: aiohttp.ClientSession, vercel_url: str) -> str:
        timestamp = datetime.now(timezone.utc).isoformat()
        body = {
            "device_id": self.device_id,
            "timestamp": timestamp,
            "command_id": "stream-token",
            "device_type": "mac",
            "text": "stream-token-request",
            "priority": "realtime",
            "response_mode": "stream",
        }
        body["signature"] = self.sign(body)
        url = f"{vercel_url}/api/stream/token"
        timeout = aiohttp.ClientTimeout(total=15, connect=10)
        logger.info("[Auth] Requesting stream token from %s", url)
        async with session.post(url, json=body, timeout=timeout) as resp:
            logger.info("[Auth] Token response: %d", resp.status)
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Stream token request failed ({resp.status}): {text}")
            data = await resp.json()
            self._stream_token = data["token"]
            return self._stream_token

    async def refresh_stream_token(self, session: aiohttp.ClientSession, vercel_url: str) -> str:
        return await self.get_stream_token(session, vercel_url)
