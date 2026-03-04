"""
JARVIS Notification Bridge
===========================

Unified multi-channel notification delivery for proactive JARVIS output.
Channels: Voice (RealTimeVoiceCommunicator), WebSocket (broadcast_router),
          macOS native (osascript).

All channels are best-effort and delivered in **parallel** — a slow or
failing channel never blocks delivery on the others.

Version: 1.1.0 (v252.1)

Fixes over v1.0.0:
- [Bug 1] Parallel channel delivery via asyncio.gather()
- [Bug 2] Per-channel timeout on voice speak() (not just acquire)
- [Bug 6] shutdown_notifications() is now reversible for warm restarts
- [Bug 7] hashlib.md5(usedforsecurity=False) for FIPS compat
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger("jarvis.notification_bridge")


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")


# ─────────────────────────────────────────────────────────
# Enums & Data Models
# ─────────────────────────────────────────────────────────

class NotificationUrgency(IntEnum):
    """Urgency levels for notifications (ascending severity)."""
    BACKGROUND = 0
    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4
    CRITICAL = 5


@dataclass
class NotificationRecord:
    """Immutable record of a delivered notification."""
    timestamp: float
    urgency: NotificationUrgency
    title: str
    message: str
    context: Dict[str, Any]
    channels_delivered: List[str] = field(default_factory=list)
    user_acknowledged: Optional[bool] = None


# ─────────────────────────────────────────────────────────
# Module-level State
# ─────────────────────────────────────────────────────────

_shutting_down: bool = False
_notifications_enabled: bool = True
_notification_history: Deque[NotificationRecord] = deque(
    maxlen=_env_int("JARVIS_NOTIFY_HISTORY_SIZE", 200),
)
_recent_notifications: Dict[str, float] = {}  # dedup_hash -> timestamp

_DEDUP_WINDOW: float = _env_float("JARVIS_NOTIFY_DEDUP_WINDOW", 60.0)
_VOICE_ACQUIRE_TIMEOUT: float = _env_float("JARVIS_NOTIFY_VOICE_TIMEOUT", 2.0)
_VOICE_SPEAK_TIMEOUT: float = _env_float("JARVIS_NOTIFY_VOICE_SPEAK_TIMEOUT", 8.0)
_MACOS_MIN_URGENCY: int = _env_int(
    "JARVIS_NOTIFY_MACOS_MIN_URGENCY", NotificationUrgency.HIGH,
)
_OSASCRIPT_TIMEOUT: float = _env_float("JARVIS_NOTIFY_OSASCRIPT_TIMEOUT", 5.0)


# ─────────────────────────────────────────────────────────
# Core: notify_user()
# ─────────────────────────────────────────────────────────

async def notify_user(
    message: str,
    urgency: NotificationUrgency = NotificationUrgency.NORMAL,
    title: str = "JARVIS",
    context: Optional[Dict[str, Any]] = None,
) -> bool:
    """Deliver a notification to the user across all available channels.

    All channels are dispatched in parallel via asyncio.gather().
    Returns True if at least one channel succeeded.
    """
    if _shutting_down:
        return False
    if not _notifications_enabled:
        logger.debug("[NotifyBridge] Notifications globally muted — skipping")
        return False
    if not _env_bool("JARVIS_NOTIFICATIONS_ENABLED", True):
        return False

    ctx = context or {}

    # ── Purge stale dedup entries ──
    now = time.time()
    stale_keys = [
        k for k, ts in _recent_notifications.items()
        if now - ts > _DEDUP_WINDOW
    ]
    for k in stale_keys:
        del _recent_notifications[k]

    # ── Cross-path dedup ──
    dedup_payload = f"{ctx.get('situation_type', '')}|{message[:80]}".encode()
    try:
        dedup_key = hashlib.md5(dedup_payload, usedforsecurity=False).hexdigest()
    except TypeError:
        # Python < 3.9 doesn't support usedforsecurity
        dedup_key = hashlib.md5(dedup_payload).hexdigest()  # noqa: S324
    if dedup_key in _recent_notifications:
        logger.debug("[NotifyBridge] Dedup hit — skipping duplicate notification")
        return False
    _recent_notifications[dedup_key] = now

    # ── Record to history ──
    record = NotificationRecord(
        timestamp=now,
        urgency=urgency,
        title=title,
        message=message,
        context=ctx,
    )
    _notification_history.append(record)

    # ── Deliver across ALL channels in parallel (best-effort) ──
    voice_coro = _deliver_voice(message, urgency)
    ws_coro = _deliver_websocket(message, urgency, title, ctx)
    macos_coro = _deliver_macos(message, urgency, title)

    results = await asyncio.gather(
        voice_coro, ws_coro, macos_coro,
        return_exceptions=True,
    )

    channel_names = ("voice", "websocket", "macos")
    delivered_any = False
    for name, result in zip(channel_names, results):
        if isinstance(result, BaseException):
            logger.debug("[NotifyBridge] %s channel raised: %s", name, result)
        elif result is True:
            record.channels_delivered.append(name)
            delivered_any = True

    if delivered_any:
        logger.info(
            "[NotifyBridge] Delivered '%s' [%s] via %s",
            title, urgency.name, ", ".join(record.channels_delivered),
        )
    else:
        logger.warning(
            "[NotifyBridge] All channels failed for: %s", message[:80],
        )

    return delivered_any


# ─────────────────────────────────────────────────────────
# Channel: Voice
# ─────────────────────────────────────────────────────────

async def _deliver_voice(message: str, urgency: NotificationUrgency) -> bool:
    """Deliver via RealTimeVoiceCommunicator.

    Two-phase timeout:
    1. Acquire communicator (2s default) — cold start protection
    2. speak()/speak_priority() call (8s default) — TTS hang protection
    """
    try:
        from agi_os.realtime_voice_communicator import (
            get_voice_communicator,
            VoiceMode,
            VoicePriority,
        )

        try:
            vc = await asyncio.wait_for(
                get_voice_communicator(), timeout=_VOICE_ACQUIRE_TIMEOUT,
            )
        except (asyncio.TimeoutError, Exception):
            logger.debug("[NotifyBridge] Voice communicator unavailable (timeout/error)")
            return False

        if vc is None:
            return False

        # Map urgency -> VoiceMode
        mode = VoiceMode.NOTIFICATION if urgency < NotificationUrgency.URGENT else VoiceMode.URGENT

        # Map urgency -> VoicePriority
        _priority_map = {
            NotificationUrgency.BACKGROUND: VoicePriority.BACKGROUND,
            NotificationUrgency.LOW: VoicePriority.BACKGROUND,
            NotificationUrgency.NORMAL: VoicePriority.NORMAL,
            NotificationUrgency.HIGH: VoicePriority.HIGH,
            NotificationUrgency.URGENT: VoicePriority.URGENT,
            NotificationUrgency.CRITICAL: VoicePriority.CRITICAL,
        }
        priority = _priority_map.get(urgency, VoicePriority.NORMAL)

        # Phase 2 timeout: cap the actual speak() call
        if urgency >= NotificationUrgency.URGENT:
            speak_coro = vc.speak_priority(message, priority=priority, mode=mode)
        else:
            speak_coro = vc.speak(message, mode=mode, priority=priority)

        await asyncio.wait_for(speak_coro, timeout=_VOICE_SPEAK_TIMEOUT)
        return True
    except asyncio.TimeoutError:
        logger.debug("[NotifyBridge] Voice speak() timed out after %.1fs", _VOICE_SPEAK_TIMEOUT)
        return False
    except Exception as e:
        logger.debug("[NotifyBridge] Voice delivery failed: %s", e)
        return False


# ─────────────────────────────────────────────────────────
# Channel: WebSocket
# ─────────────────────────────────────────────────────────

async def _deliver_websocket(
    message: str,
    urgency: NotificationUrgency,
    title: str,
    context: Dict[str, Any],
) -> bool:
    """Broadcast via both broadcast_router AND unified WebSocket manager.

    The broadcast_router reaches ``/api/broadcast/ws`` clients.
    The unified WS manager reaches ``/ws`` clients (JarvisVoice frontend).
    Both are best-effort — failure of one does not block the other.
    """
    payload = {
        "type": "proactive_notification",
        "title": title,
        "message": message,
        "urgency": urgency.name.lower(),
        "urgency_level": int(urgency),
        "context": context,
        "timestamp": time.time(),
    }

    delivered = False

    # Channel A: broadcast_router (/api/broadcast/ws clients)
    try:
        from api.broadcast_router import manager
        count = await manager.broadcast(payload)
        if count > 0:
            delivered = True
    except Exception as e:
        logger.debug("[NotifyBridge] broadcast_router delivery failed: %s", e)

    # Channel B: unified WebSocket manager (/ws clients — JarvisVoice frontend)
    try:
        from api.unified_websocket import get_ws_manager_if_initialized
        ws_mgr = get_ws_manager_if_initialized()
        if ws_mgr is not None:
            await ws_mgr.broadcast(payload)
            delivered = True
    except Exception as e:
        logger.debug("[NotifyBridge] unified WS delivery failed: %s", e)

    return delivered


# ─────────────────────────────────────────────────────────
# Channel: macOS Native
# ─────────────────────────────────────────────────────────

def _sanitize_applescript(text: str) -> str:
    """Escape text for safe embedding in osascript double-quoted strings."""
    # Escape backslash FIRST, then double quotes, then truncate
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return text[:200]


async def _deliver_macos(
    message: str,
    urgency: NotificationUrgency,
    title: str,
) -> bool:
    """Show macOS native notification via osascript (for HIGH+ urgency)."""
    if urgency < _MACOS_MIN_URGENCY:
        return False

    # Focus mode check — skip DND/SLEEP unless CRITICAL
    if urgency < NotificationUrgency.CRITICAL:
        try:
            focus_mode = _get_focus_mode()
            if focus_mode is not None and focus_mode.value in ("dnd", "sleep"):
                logger.debug(
                    "[NotifyBridge] macOS notification suppressed (focus: %s)",
                    focus_mode.value,
                )
                return False
        except Exception:
            pass  # No focus guard — proceed

    safe_title = _sanitize_applescript(title)
    safe_msg = _sanitize_applescript(message)
    script = f'display notification "{safe_msg}" with title "{safe_title}"'

    try:
        process = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=_OSASCRIPT_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.debug("[NotifyBridge] osascript timed out — killed")
            return False

        return process.returncode == 0
    except Exception as e:
        logger.debug("[NotifyBridge] macOS notification failed: %s", e)
        return False


def _get_focus_mode():
    """Best-effort focus mode detection. Returns None on any failure."""
    try:
        from macos_helper.intelligence.notification_triage import NotificationTriageSystem
        # Try the singleton if available
        triage = getattr(NotificationTriageSystem, '_instance', None)
        if triage and hasattr(triage, 'get_focus_mode'):
            return triage.get_focus_mode()
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────
# Public Helpers
# ─────────────────────────────────────────────────────────

def get_notification_history(limit: int = 50) -> List[NotificationRecord]:
    """Return recent notification records (newest first)."""
    items = list(_notification_history)
    items.reverse()
    return items[:limit]


def set_notifications_enabled(enabled: bool) -> None:
    """Runtime toggle for global notification mute."""
    global _notifications_enabled
    _notifications_enabled = enabled
    logger.info("[NotifyBridge] Notifications %s", "enabled" if enabled else "muted")


def shutdown_notifications() -> None:
    """Mark bridge as shutting down — all future notify_user() calls return False."""
    global _shutting_down
    _shutting_down = True
    logger.info("[NotifyBridge] Notification bridge shut down")


def reset_notifications() -> None:
    """Re-enable bridge after shutdown (for warm restarts without process exit).

    v253.1: Also clear history and dedup state to prevent stale data
    from previous session bleeding into the new one.
    """
    global _shutting_down
    _shutting_down = False
    _notification_history.clear()
    _recent_notifications.clear()
    logger.info("[NotifyBridge] Notification bridge reset (warm restart)")
