"""Environment configuration for the brainstem."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BrainstemConfig:
    vercel_url: str
    device_id: str
    device_secret: str
    device_type: str = "mac"
    poll_interval_ms: int = 100
    heartbeat_interval_s: int = 15
    token_refresh_s: int = 240
    reconnect_backoff_base: float = 1.0
    reconnect_backoff_max: float = 30.0

    @classmethod
    def from_env(cls) -> "BrainstemConfig":
        vercel_url = os.environ.get("JARVIS_VERCEL_URL", "")
        if not vercel_url:
            raise ValueError("JARVIS_VERCEL_URL is required")
        device_id = os.environ.get("JARVIS_DEVICE_ID", "")
        if not device_id:
            raise ValueError("JARVIS_DEVICE_ID is required")
        device_secret = os.environ.get("JARVIS_DEVICE_SECRET", "")
        if not device_secret or len(device_secret) < 64:
            raise ValueError("JARVIS_DEVICE_SECRET is required (64-char hex from pairing)")
        return cls(
            vercel_url=vercel_url.rstrip("/"),
            device_id=device_id,
            device_secret=device_secret,
            poll_interval_ms=int(os.environ.get("JARVIS_POLL_INTERVAL_MS", "100")),
            heartbeat_interval_s=int(os.environ.get("JARVIS_HEARTBEAT_S", "15")),
            token_refresh_s=int(os.environ.get("JARVIS_TOKEN_REFRESH_S", "240")),
            reconnect_backoff_base=float(os.environ.get("JARVIS_RECONNECT_BACKOFF_BASE", "1.0")),
            reconnect_backoff_max=float(os.environ.get("JARVIS_RECONNECT_BACKOFF_MAX", "30.0")),
        )
