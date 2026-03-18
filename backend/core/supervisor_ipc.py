"""
Supervisor IPC — Unix Domain Socket client-server multiplexing (v293.0).

Implements the Control Plane / UI Plane distinction described in the Unified
Control Plane engineering directive:

  Control Plane (daemon):
    - Binds ~/.jarvis/supervisor.sock
    - Broadcasts structured events (logs, health snapshots, status changes)
      to every connected client over newline-delimited JSON

  UI Plane (terminal client):
    - Connects to the socket if a daemon is already running
    - Renders the live event stream with ANSI formatting on TTY,
      or plain JSON when stdout is a pipe / launchd log file

Wire protocol:
  daemon → client:  {"type": "log"|"health"|"status"|"shutdown", ...}
  client → daemon:  {"cmd": "ping"|"shutdown"|"status"}

Usage (called from unified_supervisor.py main()):
  ipc = SupervisorIPC()
  is_daemon = ipc.try_bind_sync()      # sync probe, no asyncio needed
  if not is_daemon:
      asyncio.run(ipc.attach_as_client())
      sys.exit(0)
  # else: we are the daemon — bind async server in async_main, wire log handler
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket as _socket_mod
import sys
import time
from pathlib import Path
from typing import Optional, Set

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOCKET_PATH: Path = Path.home() / ".jarvis" / "supervisor.sock"

# ANSI escape codes for TTY rendering
_RESET   = "\033[0m"
_BOLD    = "\033[1m"
_DIM     = "\033[2m"
_RED     = "\033[91m"
_YELLOW  = "\033[93m"
_GREEN   = "\033[92m"
_CYAN    = "\033[96m"
_BLUE    = "\033[94m"
_MAGENTA = "\033[95m"
_WHITE   = "\033[97m"

_LEVEL_COLORS = {
    "DEBUG":    _DIM + _WHITE,
    "INFO":     _CYAN,
    "WARNING":  _YELLOW,
    "ERROR":    _RED,
    "CRITICAL": _BOLD + _RED,
}

_TYPE_COLORS = {
    "health":   _GREEN,
    "status":   _BLUE,
    "shutdown": _MAGENTA,
}


# ---------------------------------------------------------------------------
# Event rendering
# ---------------------------------------------------------------------------

def _render_event(event: dict, color: bool) -> None:
    """Render a structured daemon event to stdout."""
    etype = event.get("type", "log")
    ts    = event.get("ts", time.time())
    ts_str = time.strftime("%H:%M:%S", time.localtime(ts))

    if etype == "log":
        level  = event.get("level", "INFO")
        logger = event.get("logger", "")
        msg    = event.get("msg", "")
        if color:
            lc = _LEVEL_COLORS.get(level, _WHITE)
            print(f"{_DIM}{ts_str}{_RESET} {lc}{level:<8}{_RESET} {_DIM}{logger}{_RESET} {msg}",
                  flush=True)
        else:
            print(json.dumps(event), flush=True)

    elif etype == "health":
        components = event.get("components", {})
        if color:
            parts = []
            for name, state in components.items():
                sc = _GREEN if state in ("healthy", "ready") else _YELLOW if state == "degraded" else _RED
                parts.append(f"{name}={sc}{state}{_RESET}")
            print(f"{_DIM}{ts_str}{_RESET} {_GREEN}[health]{_RESET} " + "  ".join(parts),
                  flush=True)
        else:
            print(json.dumps(event), flush=True)

    elif etype == "status":
        phase = event.get("phase", "")
        pid   = event.get("pid", "")
        if color:
            print(f"{_DIM}{ts_str}{_RESET} {_BLUE}[status]{_RESET} "
                  f"phase={_BOLD}{phase}{_RESET}  pid={pid}", flush=True)
        else:
            print(json.dumps(event), flush=True)

    elif etype == "shutdown":
        if color:
            print(f"{_DIM}{ts_str}{_RESET} {_MAGENTA}[shutdown]{_RESET} "
                  f"{event.get('reason', 'daemon exiting')}", flush=True)
        else:
            print(json.dumps(event), flush=True)

    else:
        # Unknown event type — passthrough
        if color:
            print(f"{_DIM}{ts_str}{_RESET} {event}", flush=True)
        else:
            print(json.dumps(event), flush=True)


# ---------------------------------------------------------------------------
# IPC log handler — broadcasts all Python log records to connected clients
# ---------------------------------------------------------------------------

class _IPCLogHandler(logging.Handler):
    """Logging handler that forwards records to the IPC broadcast channel."""

    def __init__(self, ipc: "SupervisorIPC") -> None:
        super().__init__()
        self._ipc = ipc

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event: dict = {
                "type":   "log",
                "ts":     record.created,
                "level":  record.levelname,
                "logger": record.name,
                "msg":    record.getMessage(),
            }
            # Run in the supervisor's event loop without blocking the caller
            loop = self._ipc._loop
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(self._ipc.broadcast(event), loop)
        except Exception:
            pass  # Never let a log handler crash the supervisor


# ---------------------------------------------------------------------------
# Main IPC class
# ---------------------------------------------------------------------------

class SupervisorIPC:
    """
    Unix Domain Socket IPC for the JARVIS supervisor.

    Lifecycle:
      1. Call try_bind_sync() synchronously to test whether a daemon is
         already running before entering an asyncio event loop.
      2a. If NOT daemon: call asyncio.run(attach_as_client()) — blocks until
          the daemon exits or the user presses Ctrl+C.
      2b. If daemon: call await start_server() once the asyncio loop is
          running, then install_log_handler() to wire the broadcast channel
          into Python's logging system.
      3. Call await close() during graceful shutdown.
    """

    def __init__(self) -> None:
        self._clients: Set[asyncio.StreamWriter] = set()
        self._server: Optional[asyncio.Server] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._is_tty: bool = False
        try:
            self._is_tty = os.isatty(sys.stdout.fileno())
        except Exception:
            self._is_tty = False

    # ------------------------------------------------------------------
    # Sync probe (called BEFORE asyncio.run)
    # ------------------------------------------------------------------

    def try_bind_sync(self) -> bool:
        """
        Synchronously check whether a daemon socket already exists and is live.

        Returns True  → this process should become the daemon (socket was
                         stale or absent; stale file removed).
        Returns False → a live daemon exists; caller should attach as client.
        """
        sock_path = str(SOCKET_PATH)

        if SOCKET_PATH.exists():
            # Try connecting — if it succeeds the daemon is live
            probe = _socket_mod.socket(_socket_mod.AF_UNIX, _socket_mod.SOCK_STREAM)
            probe.settimeout(1.0)
            try:
                probe.connect(sock_path)
                probe.close()
                return False  # Live daemon found
            except (ConnectionRefusedError, OSError):
                # Stale socket — remove it so we can bind
                try:
                    SOCKET_PATH.unlink(missing_ok=True)
                except OSError:
                    pass
                return True
        return True

    # ------------------------------------------------------------------
    # Daemon path: start server and accept clients
    # ------------------------------------------------------------------

    async def start_server(self) -> None:
        """Bind the UDS and start accepting client connections (daemon only)."""
        self._loop = asyncio.get_running_loop()
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Remove any leftover socket file (should have been cleaned by try_bind_sync,
        # but defend against races in multi-instance edge cases)
        try:
            SOCKET_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(SOCKET_PATH)
        )
        logging.getLogger(__name__).info(
            "[IPC] Daemon socket bound at %s", SOCKET_PATH
        )

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single connected client: read commands, keep writing events."""
        self._clients.add(writer)
        try:
            while not reader.at_eof():
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Send a keepalive ping so idle clients don't time out
                    await self._write_event(writer, {"type": "ping", "ts": time.time()})
                    continue

                if not raw:
                    break

                try:
                    cmd = json.loads(raw.decode().strip())
                    await self._handle_command(cmd, writer)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass  # Ignore malformed commands

        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            self._clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_command(self, cmd: dict, writer: asyncio.StreamWriter) -> None:
        action = cmd.get("cmd", "")
        if action == "ping":
            await self._write_event(writer, {"type": "pong", "ts": time.time()})
        elif action == "status":
            await self._write_event(writer, {
                "type": "status_response",
                "ts":   time.time(),
                "pid":  os.getpid(),
            })
        elif action == "shutdown":
            # Route shutdown command to the supervisor via shutdown event
            try:
                from backend.core.shutdown_event import get_shutdown_event
                get_shutdown_event().set()
            except Exception:
                pass

    async def broadcast(self, event: dict) -> None:
        """Broadcast a structured event to all connected clients."""
        if not self._clients:
            return
        dead: Set[asyncio.StreamWriter] = set()
        for writer in list(self._clients):
            if not await self._write_event(writer, event):
                dead.add(writer)
        self._clients -= dead

    @staticmethod
    async def _write_event(writer: asyncio.StreamWriter, event: dict) -> bool:
        """Write one newline-delimited JSON event. Returns False if write failed."""
        try:
            writer.write((json.dumps(event) + "\n").encode())
            await writer.drain()
            return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            return False

    def install_log_handler(self, min_level: int = logging.INFO) -> None:
        """
        Install a logging.Handler on the root logger that forwards all records
        at or above min_level to every connected IPC client.
        Call this once the asyncio loop is running (after start_server).
        """
        handler = _IPCLogHandler(self)
        handler.setLevel(min_level)
        logging.getLogger().addHandler(handler)

    async def broadcast_health(self, components: dict) -> None:
        """Convenience: broadcast a health snapshot."""
        await self.broadcast({"type": "health", "ts": time.time(), "components": components})

    async def broadcast_status(self, phase: str) -> None:
        """Convenience: broadcast a phase transition."""
        await self.broadcast({"type": "status", "ts": time.time(),
                               "phase": phase, "pid": os.getpid()})

    async def broadcast_shutdown(self, reason: str = "graceful") -> None:
        """Notify all clients the daemon is exiting, then drain connections."""
        await self.broadcast({"type": "shutdown", "ts": time.time(), "reason": reason})
        # Give clients a moment to receive the shutdown event
        await asyncio.sleep(0.2)

    # ------------------------------------------------------------------
    # Client path: connect and stream events to terminal
    # ------------------------------------------------------------------

    async def attach_as_client(self) -> None:
        """
        Connect to the running daemon and stream events to stdout.
        Blocks until the daemon exits or the user presses Ctrl+C.
        """
        color = self._is_tty

        if color:
            print(f"{_CYAN}[JARVIS]{_RESET} Daemon already running — attaching to live stream. "
                  f"Press {_BOLD}Ctrl+C{_RESET} to detach (daemon keeps running).")

        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            print(f"[JARVIS] Could not connect to daemon socket: {exc}", file=sys.stderr)
            return

        try:
            async for raw in reader:
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    _render_event(event, color=color)
                    if event.get("type") == "shutdown":
                        break
                except (json.JSONDecodeError, UnicodeDecodeError):
                    print(line, flush=True)

        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            if color:
                print(f"\n{_DIM}[JARVIS] Daemon connection closed.{_RESET}")
        except asyncio.CancelledError:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Shut down the server and remove the socket file."""
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        try:
            SOCKET_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        logging.getLogger(__name__).debug("[IPC] Socket closed and unlinked.")


# ---------------------------------------------------------------------------
# Context-aware log formatter (Task 3)
# ---------------------------------------------------------------------------

class _AnsiFormatter(logging.Formatter):
    """ANSI-colored log formatter for interactive TTY sessions."""

    _FMT = "%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)s  %(message)s"
    _DATE = "%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelname, _WHITE)
        record.levelname = f"{color}{record.levelname}{_RESET}"
        record.name      = f"{_DIM}{record.name}{_RESET}"
        return super().format(record)


class _JsonFormatter(logging.Formatter):
    """Structured JSON log formatter for launchd / file output."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "ts":      record.created,
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
            "pid":     record.process,
        })


def configure_context_logging(root_level: int = logging.INFO) -> None:
    """
    Configure the root logger with a context-aware formatter (Task 3).

    TTY (interactive terminal) → ANSI colored human-readable output.
    Non-TTY (launchd / file redirect) → strict JSON, one object per line.
    """
    is_tty: bool = False
    try:
        is_tty = os.isatty(sys.stdout.fileno())
    except Exception:
        pass

    root = logging.getLogger()
    # Remove any existing StreamHandlers to avoid double-logging
    root.handlers = [h for h in root.handlers if not isinstance(h, logging.StreamHandler)
                     or isinstance(h, logging.FileHandler)]

    handler = logging.StreamHandler(sys.stdout)
    if is_tty:
        fmt = _AnsiFormatter(_AnsiFormatter._FMT, datefmt=_AnsiFormatter._DATE)
    else:
        fmt = _JsonFormatter()

    handler.setFormatter(fmt)
    handler.setLevel(root_level)
    root.addHandler(handler)
    root.setLevel(root_level)
