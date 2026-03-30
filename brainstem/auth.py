"""HMAC authentication for the brainstem."""
import asyncio
import hashlib
import hmac
import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("jarvis.brainstem.auth")

CANONICAL_FIELDS = [
    "command_id", "device_id", "device_type",
    "priority", "response_mode", "text", "timestamp",
]

# Token request timeout in seconds
_TOKEN_TIMEOUT_S = 15


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

    async def get_stream_token(self, session: Any, vercel_url: str) -> str:
        """Get a stream token from Vercel.

        Uses urllib (system SSL stack) in a thread instead of aiohttp,
        because aiohttp's TLS handshake hangs on macOS when launched
        as a subprocess from Xcode.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        body: Dict[str, Any] = {
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
        logger.info("[Auth] Requesting stream token from %s", url)

        def _do_request() -> str:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_TOKEN_TIMEOUT_S) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Stream token request failed ({resp.status}): "
                        f"{resp.read().decode()}"
                    )
                result = json.loads(resp.read().decode())
                return result["token"]

        token = await asyncio.to_thread(_do_request)
        logger.info("[Auth] Token received: %s...", token[:8])
        self._stream_token = token
        return token

    async def refresh_stream_token(self, session: Any, vercel_url: str) -> str:
        return await self.get_stream_token(session, vercel_url)
