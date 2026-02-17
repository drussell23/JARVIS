"""
HTTP Connection Pool v1.0 — Shared aiohttp session management.

Replaces 32+ ephemeral aiohttp.ClientSession() creations with a
singleton pool keyed by base URL. Each session has configurable
TTL, connection limits, and automatic cleanup.

Usage:
    from backend.core.http_pool import get_session, close_all_sessions

    # Get or create a session for a base URL
    session = await get_session("http://localhost:8001")
    async with session.get("/health") as resp:
        data = await resp.json()

    # Cleanup on shutdown
    await close_all_sessions()
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger("jarvis.http_pool")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PoolConfig:
    """Per-session configuration."""
    max_connections: int = int(os.getenv("JARVIS_HTTP_POOL_MAX_CONN", "30"))
    max_connections_per_host: int = int(os.getenv("JARVIS_HTTP_POOL_MAX_PER_HOST", "10"))
    ttl_seconds: float = float(os.getenv("JARVIS_HTTP_POOL_TTL", "600"))
    connect_timeout: float = float(os.getenv("JARVIS_HTTP_POOL_CONNECT_TIMEOUT", "10"))
    total_timeout: float = float(os.getenv("JARVIS_HTTP_POOL_TOTAL_TIMEOUT", "30"))
    keepalive_timeout: float = float(os.getenv("JARVIS_HTTP_POOL_KEEPALIVE", "30"))
    enable_cleanup_task: bool = True
    cleanup_interval: float = 60.0


# ---------------------------------------------------------------------------
# Pool entry
# ---------------------------------------------------------------------------

@dataclass
class _PoolEntry:
    session: aiohttp.ClientSession
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    request_count: int = 0

    def touch(self) -> None:
        self.last_used = time.monotonic()
        self.request_count += 1

    def is_expired(self, ttl: float) -> bool:
        return (time.monotonic() - self.created_at) > ttl


# ---------------------------------------------------------------------------
# Connection Pool
# ---------------------------------------------------------------------------

class HTTPConnectionPool:
    """
    Singleton pool of aiohttp.ClientSession instances keyed by base URL.

    Thread-safe via asyncio.Lock. Expired sessions are reaped by a
    background task or eagerly on next access.
    """

    _instance: Optional["HTTPConnectionPool"] = None

    def __init__(self) -> None:
        # Guard against re-init on singleton reuse
        if hasattr(self, "_initialized"):
            return
        self._sessions: Dict[str, _PoolEntry] = {}
        self._lock = asyncio.Lock()
        self._config = PoolConfig()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._closed = False
        self._initialized = True

    def __new__(cls) -> "HTTPConnectionPool":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # -- public API ---------------------------------------------------------

    async def get_session(
        self,
        base_url: str,
        *,
        config: Optional[PoolConfig] = None,
    ) -> aiohttp.ClientSession:
        """
        Get or create a session for *base_url*.

        If the existing session is expired, it is closed and replaced.
        """
        cfg = config or self._config

        async with self._lock:
            entry = self._sessions.get(base_url)

            if entry is not None and not entry.is_expired(cfg.ttl_seconds):
                entry.touch()
                return entry.session

            # Close stale session
            if entry is not None:
                await self._close_entry(entry, base_url)

            # Create fresh session
            connector = aiohttp.TCPConnector(
                limit=cfg.max_connections,
                limit_per_host=cfg.max_connections_per_host,
                keepalive_timeout=cfg.keepalive_timeout,
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(
                connect=cfg.connect_timeout,
                total=cfg.total_timeout,
            )
            session = aiohttp.ClientSession(
                base_url=base_url,
                connector=connector,
                timeout=timeout,
            )
            new_entry = _PoolEntry(session=session)
            self._sessions[base_url] = new_entry
            logger.debug(f"[HTTPPool] Created session for {base_url}")

            # Start cleanup task on first session creation
            self._ensure_cleanup_task(cfg)

            return session

    async def close_session(self, base_url: str) -> None:
        """Close and remove a specific session."""
        async with self._lock:
            entry = self._sessions.pop(base_url, None)
            if entry:
                await self._close_entry(entry, base_url)

    async def close_all(self) -> None:
        """Close all sessions. Call on shutdown."""
        async with self._lock:
            self._closed = True
            if self._cleanup_task and not self._cleanup_task.done():
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except asyncio.CancelledError:
                    pass
                self._cleanup_task = None

            for base_url, entry in list(self._sessions.items()):
                await self._close_entry(entry, base_url)
            self._sessions.clear()
            logger.info("[HTTPPool] All sessions closed")

    def get_stats(self) -> Dict[str, Any]:
        """Return pool statistics."""
        return {
            "active_sessions": len(self._sessions),
            "sessions": {
                url: {
                    "age_s": round(time.monotonic() - e.created_at, 1),
                    "idle_s": round(time.monotonic() - e.last_used, 1),
                    "requests": e.request_count,
                }
                for url, e in self._sessions.items()
            },
        }

    # -- internals ----------------------------------------------------------

    async def _close_entry(self, entry: _PoolEntry, label: str) -> None:
        try:
            if not entry.session.closed:
                await entry.session.close()
            logger.debug(f"[HTTPPool] Closed session for {label}")
        except Exception as exc:
            logger.warning(f"[HTTPPool] Error closing session {label}: {exc}")

    def _ensure_cleanup_task(self, cfg: PoolConfig) -> None:
        if not cfg.enable_cleanup_task:
            return
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(cfg),
                name="http-pool-cleanup",
            )

    async def _cleanup_loop(self, cfg: PoolConfig) -> None:
        """Periodically close expired sessions."""
        try:
            while not self._closed:
                await asyncio.sleep(cfg.cleanup_interval)
                async with self._lock:
                    expired = [
                        url for url, entry in self._sessions.items()
                        if entry.is_expired(cfg.ttl_seconds)
                    ]
                    for url in expired:
                        entry = self._sessions.pop(url)
                        await self._close_entry(entry, url)
                    if expired:
                        logger.debug(f"[HTTPPool] Reaped {len(expired)} expired sessions")
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

_pool: Optional[HTTPConnectionPool] = None


def _get_pool() -> HTTPConnectionPool:
    global _pool
    if _pool is None:
        _pool = HTTPConnectionPool()
    return _pool


async def get_session(
    base_url: str,
    *,
    config: Optional[PoolConfig] = None,
) -> aiohttp.ClientSession:
    """Get or create a pooled session for *base_url*."""
    return await _get_pool().get_session(base_url, config=config)


async def close_all_sessions() -> None:
    """Close all pooled sessions (call at shutdown)."""
    if _pool is not None:
        await _pool.close_all()


def get_pool_stats() -> Dict[str, Any]:
    """Return current pool statistics."""
    return _get_pool().get_stats()
