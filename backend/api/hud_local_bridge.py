"""HUD ↔ unified_supervisor local-first bridge (Phase 9).

Operator authorization 2026-07-19. Vercel is blocked, so the native
JARVIS HUD (macOS) must talk DIRECTLY to the local ``unified_supervisor``
backend on ``localhost:8010`` instead of the cloud relay.

The Swift client (``JARVISKit``) speaks a cloud-shaped contract against a
``baseURL``:
  1. ``POST /api/stream/token``      → ``{token, stream_url}``
  2. ``GET  /api/stream/{deviceId}`` → the SSE stream (already served)
  3. ``POST /api/command``           → send a command

Endpoints #1 and #3 didn't exist locally (they were cloud concepts), and
#3's handler lives at ``/api/stream/command``. This module adds the two
missing shapes as LOOPBACK-TRUSTED local endpoints so the entire tested
Swift networking stack connects UNCHANGED once its ``baseURL`` points at
localhost — no cloud auth, no Redis, no relay.

DRY (mandate 3): the command endpoint bridges to the EXISTING
``EventStream.handle_post_command`` (the same handler ``/api/stream/
command`` uses) — zero duplicated command logic. The SSE endpoint is the
device-stream multiplexer already built.

These are pure, side-effect-free helpers so the routing layer stays thin
and the translation logic is unit-testable without booting FastAPI. Every
function NEVER raises.
"""
from __future__ import annotations

import secrets
from typing import Any, Dict, Optional

#: Hosts we consider loopback — these endpoints are local-only by design
#: (no cloud auth; the machine trusts its own supervisor).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


def is_loopback_host(host: Optional[str]) -> bool:
    """True when the request originates from this machine. The local
    bridge is loopback-only — a remote peer must go through the
    authenticated cloud path, never this trust-the-localhost shortcut."""
    try:
        h = (host or "").strip().lower()
        # Strip an IPv6 zone/port artifact if present.
        if h.startswith("::ffff:"):
            h = h[7:]
        return h in _LOOPBACK_HOSTS
    except Exception:  # noqa: BLE001
        return False


def build_stream_token_response(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Issue a trivial local stream token (Phase 9). The device SSE
    endpoint on this host is loopback-trusted and does not validate the
    token, so any opaque value satisfies the Swift ``StreamTokenResponse``
    contract ``{token, stream_url}`` and lets the client connect. NEVER
    raises."""
    try:
        p = payload or {}
        device_id = str(
            p.get("device_id") or p.get("deviceId") or p.get("commandId")
            or "mac-local"
        ).strip() or "mac-local"
        token = "local-" + secrets.token_urlsafe(18)
        return {
            "token": token,
            "stream_url": f"/api/stream/{device_id}",
            "expires_in": 3600,
            "mode": "local",
        }
    except Exception:  # noqa: BLE001
        return {"token": "local-fallback", "stream_url": "/api/stream/mac-local",
                "expires_in": 3600, "mode": "local"}


def translate_hud_command(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map the Swift ``CommandRequest`` JSON onto the WS command frame that
    ``EventStream.handle_post_command`` → ``ws_manager.handle_message``
    routes to command processing. Accepts either snake_case (wire) or
    camelCase (defensive) keys. NEVER raises."""
    try:
        p = payload or {}
        text = p.get("text") or p.get("command") or ""
        command_id = p.get("command_id") or p.get("commandId")
        return {
            # 'command'/'jarvis_command' route to the command channel.
            "type": "command",
            "command": text,
            "text": text,
            "command_id": command_id,
            "device_id": p.get("device_id") or p.get("deviceId") or "mac-local",
            "device_type": p.get("device_type") or p.get("deviceType") or "mac",
            "intent_hint": p.get("intent_hint") or p.get("intentHint"),
            "response_mode": p.get("response_mode") or p.get("responseMode")
            or "stream",
            "priority": p.get("priority") or "realtime",
            "context": p.get("context") or {},
            "source": "hud_local",
        }
    except Exception:  # noqa: BLE001
        return {"type": "command", "command": "", "text": "",
                "command_id": None, "source": "hud_local"}


def shape_command_response(
    handler_response: Any, command_id: Optional[str],
) -> Dict[str, Any]:
    """Normalize the handler's return into the Swift ``CommandResponse``
    shape (``status`` + ``command_id``). The real payload streams over the
    SSE channel; this POST just acknowledges acceptance. NEVER raises."""
    try:
        status = "accepted"
        success = True
        if isinstance(handler_response, dict):
            if handler_response.get("status"):
                status = str(handler_response["status"])
            elif handler_response.get("success") is False:
                status, success = "error", False
        return {"status": status, "command_id": command_id, "success": success}
    except Exception:  # noqa: BLE001
        return {"status": "accepted", "command_id": command_id, "success": True}


__all__ = [
    "is_loopback_host", "build_stream_token_response",
    "translate_hud_command", "shape_command_response",
]
