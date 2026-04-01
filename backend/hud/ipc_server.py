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
from typing import Any, Callable, Coroutine, Dict, Optional

logger = logging.getLogger("jarvis.hud.ipc")

DEFAULT_IPC_PORT = 8742


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
    ipc_port = port or int(os.environ.get("JARVIS_IPC_PORT", str(DEFAULT_IPC_PORT)))

    def _dispatch_in_thread(event_type: str, data: dict) -> None:
        """Run async dispatch in a fresh event loop on a daemon thread.

        macOS subprocess contexts make call_soon_threadsafe unreliable,
        so each dispatch gets its own short-lived loop.
        """
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(dispatch(event_type, data))
        finally:
            loop.close()

    async def _handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("[IPC] Client connected: %s", peer)
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
                    logger.info("[IPC] Received event: %s (%d bytes)", event_type, len(line))
                    threading.Thread(
                        target=_dispatch_in_thread,
                        args=(event_type, data),
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
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("[IPC] Client disconnected: %s", peer)

    server = await asyncio.start_server(
        _handle_client,
        "127.0.0.1",
        ipc_port,
        reuse_address=True,
    )
    logger.info("[IPC] TCP server listening on localhost:%d", ipc_port)
    return server
