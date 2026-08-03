"""
HUD IPC Server — TCP socket for Swift HUD ↔ backend communication.

v351.0: Extracted from brainstem/main.py into the unified backend so the
JARVIS HUD gets the full stack (Ouroboros, Doubleword, Claude, Vision,
Ghost Hands) instead of a lightweight duplicate.

Protocol: newline-delimited JSON on localhost:8742
Message format: {"event_type": str, "data": dict}

The HUD sends action events (voice commands, vision tasks) over this
socket. The backend dispatches them through the same ActionDispatcher
/ UnifiedCommandProcessor that handles SSE events.
"""
import asyncio
import json
import logging
import os
import threading
from typing import Any, Callable, Coroutine, Dict, Optional, Set

logger = logging.getLogger("jarvis.hud.ipc")

DEFAULT_IPC_PORT = 8742

# ---------------------------------------------------------------------------
# Backend -> HUD.
#
# The socket was read-only for its whole life: `_handle_client` consumed lines
# and never wrote one. That was fine while every conversation started at the
# HUD — and it is exactly why the consent gate was decorative. `SecureConsent`
# on the Swift side is complete, waiting for a challenge that nothing could
# send, so every gated capability failed closed with "no approval provider
# available" and the operator was never asked anything.
#
# Writers are held here rather than passed around because the thing that needs
# to ask (the capability router, on a dispatch thread, several frames deep) has
# no path to a StreamWriter and should not grow one.
# ---------------------------------------------------------------------------

_CLIENTS: Set[asyncio.StreamWriter] = set()
#: The loop the server runs on. Captured because `publish` is called from the
#: per-dispatch threads, and touching a StreamWriter from a foreign loop is
#: undefined behaviour that usually presents as a silently dropped write.
_SERVER_LOOP: Optional[asyncio.AbstractEventLoop] = None


def connected_clients() -> int:
    """How many HUDs are listening. NEVER raises."""
    return len(_CLIENTS)


def publish(event_type: str, data: Dict[str, Any]) -> int:
    """Push one event to every connected HUD. Returns the number reached.

    Thread-safe and NON-BLOCKING: schedules the write onto the server's loop
    and returns immediately. NEVER raises — a failed push must degrade the
    thing that wanted to speak, not kill it.

    Returning a COUNT rather than a bool is what lets a caller fail closed
    honestly. Zero means nobody heard the question, which for a consent request
    is a denial and not a silence to interpret optimistically.
    """
    try:
        loop = _SERVER_LOOP
        if loop is None or not _CLIENTS:
            return 0
        payload = (json.dumps({"event_type": event_type, "data": data})
                   + "\n").encode("utf-8")
        reached = len(_CLIENTS)
        loop.call_soon_threadsafe(_write_all, payload)
        return reached
    except Exception:  # noqa: BLE001
        logger.debug("[IPC] publish(%s) failed", event_type, exc_info=True)
        return 0


def _write_all(payload: bytes) -> None:
    """Write to every client. Runs ON the server loop. NEVER raises."""
    for writer in list(_CLIENTS):
        try:
            if writer.is_closing():
                _CLIENTS.discard(writer)
                continue
            writer.write(payload)
        except Exception:  # noqa: BLE001
            _CLIENTS.discard(writer)


def _principal_of(msg: dict, peer: Any) -> str:
    """Who this connection is acting for. NEVER raises.

    A HUD may declare a stable `client_id`; otherwise the peer address is used.
    The difference matters for exactly one thing: a peer address changes on
    every reconnect, so a HUD that is rebuilt and relaunched comes back as a
    NEW principal and its previous sessions are reaped. That is the correct
    default — a quit HUD's screen capture should not outlive it — but a client
    that intends to survive a restart can say so and keep its stream.
    """
    try:
        msg = msg or {}
        # Read the envelope AND the payload. `BrainstemLauncher.sendEvent`
        # builds the envelope itself and only lets a caller populate `data`, so
        # `data.client_id` is the only place the real Swift client can actually
        # put one. Reading solely the top level made the declared-identity path
        # unreachable from the one client it exists for.
        payload = msg.get("data")
        declared = str(msg.get("client_id")
                       or (payload.get("client_id") if isinstance(payload, dict)
                           else "")
                       or "").strip()
        if declared:
            return f"hud:{declared[:64]}"
        return f"peer:{peer[0]}:{peer[1]}" if peer else "peer:unknown"
    except Exception:  # noqa: BLE001
        return "peer:unknown"


async def start_ipc_server(
    dispatch: Callable[[str, Dict[str, Any]], Coroutine],
    shutdown: asyncio.Event,
    port: Optional[int] = None,
) -> asyncio.Server:
    """Start the HUD IPC TCP server.

    Args:
        dispatch: Async callable(event_type, data) to handle incoming events.
        shutdown: Event that signals graceful shutdown.
        port: TCP port (default: JARVIS_IPC_PORT env or 8742).

    Returns:
        The asyncio.Server instance (caller should manage lifetime).
    """
    # `port is None` rather than `port or ...`: 0 is a MEANINGFUL value — it
    # asks the OS for an ephemeral port — and the falsy test silently turned it
    # back into 8742, which is the one port a second instance is guaranteed to
    # collide on.
    ipc_port = (int(os.environ.get("JARVIS_IPC_PORT", str(DEFAULT_IPC_PORT)))
                if port is None else int(port))

    def _dispatch_in_thread(event_type: str, data: dict, principal: str) -> None:
        """Run async dispatch in a fresh event loop on a daemon thread.

        macOS subprocess contexts make call_soon_threadsafe unreliable,
        so each dispatch gets its own short-lived loop.

        The principal is stamped INSIDE this function rather than passed down
        through dispatch: a new thread starts with an empty context, so setting
        the contextvar here is what makes it visible to everything the dispatch
        awaits — and scoping it to this thread is what stops two clients'
        sessions from being attributed to each other.
        """
        try:
            from backend.system_control.capability_leases import set_principal
            set_principal(principal)
        except Exception:  # noqa: BLE001 — an unowned lease still has a TTL
            pass
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(dispatch(event_type, data))
        finally:
            loop.close()

    def _note(present: bool, principal: str) -> None:
        """Tell the lease book this principal came or went. NEVER raises."""
        try:
            from backend.system_control.capability_leases import get_lease_book
            book = get_lease_book()
            if present:
                book.note_arrival(principal)
            else:
                book.note_departure(principal)
        except Exception:  # noqa: BLE001
            logger.debug("[IPC] lease presence update failed", exc_info=True)

    async def _handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("[IPC] Client connected: %s", peer)
        # Provisional until a message declares a `client_id`. Held in a list so
        # the `finally` block reaps whatever the principal turned out to BE,
        # rather than the address it first connected from.
        principal = _principal_of({}, peer)
        _note(True, principal)
        _CLIENTS.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                    event_type = msg.get("event_type", "")
                    data = msg.get("data", {})
                    declared = _principal_of(msg, peer)
                    if declared != principal:
                        # The connection just told us who it really is. The
                        # provisional identity must stop being treated as
                        # present, or its (empty) departure never fires and the
                        # book accumulates ghosts of every reconnect.
                        _note(False, principal)
                        principal = declared
                        _note(True, principal)
                    logger.info("[IPC] Received event: %s (%d bytes)", event_type, len(line))
                    threading.Thread(
                        target=_dispatch_in_thread,
                        args=(event_type, data, principal),
                        daemon=True,
                    ).start()
                except json.JSONDecodeError as je:
                    logger.warning("[IPC] Bad JSON from client: %s", je)
                except Exception as de:
                    logger.error("[IPC] Dispatch error: %s", de)
        except asyncio.CancelledError:
            pass
        except ConnectionResetError:
            logger.info("[IPC] Client disconnected (reset): %s", peer)
        except Exception as exc:
            logger.error("[IPC] Client handler error: %s", exc)
        finally:
            _CLIENTS.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            # A dropped socket starts a GRACE window, it does not reap. A HUD
            # rebuild and a one-second blip look identical from here, and
            # killing a screen capture for either would be worse than the leak
            # this is guarding against. The reaper decides, on its own clock.
            _note(False, principal)
            logger.info("[IPC] Client disconnected: %s", peer)

    # 2MB limit per line — screenshots from the HUD can be 200-500KB as base64 JPEG.
    # Default asyncio limit is 64KB which truncates screenshot payloads.
    server = await asyncio.start_server(
        _handle_client,
        "127.0.0.1",
        ipc_port,
        reuse_address=True,
        limit=2 * 1024 * 1024,
    )
    global _SERVER_LOOP
    _SERVER_LOOP = asyncio.get_running_loop()
    logger.info("[IPC] TCP server listening on localhost:%d", ipc_port)
    return server
