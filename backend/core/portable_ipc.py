"""
portable_ipc - path-addressed local stream IPC on every platform.

JARVIS addresses its local servers (supervisor control, voice orchestrator,
cockpit attach, audio state, ...) by a filesystem path and talks to them with
``asyncio.start_unix_server`` / ``asyncio.open_unix_connection``. Windows
CPython has neither (and no ``socket.AF_UNIX``), so on Windows every one of
those servers failed to start.

POSIX: nothing changes; ``install()`` is a no-op and the asyncio functions
are the real ones.

Windows: ``install()`` provides both names on ``asyncio`` with the same
signatures, carried over loopback TCP:

* the server binds ``127.0.0.1`` on an ephemeral port and writes an ENDPOINT
  FILE at the path the socket would have occupied, holding
  ``{"transport": "tcp", "host", "port", "token"}``. Callers that test
  ``path.exists()``, unlink a stale path before binding, or chmod the socket
  keep working, because the path still names the endpoint;
* a client reads that file, connects, and sends the token as its first line;
* the server compares it in constant time and drops any connection that does
  not present it within ``JARVIS_PORTABLE_IPC_AUTH_TIMEOUT_S``. A loopback
  port is reachable by every local process; the token, readable only through
  the user's own profile directory, is what stands in for socket-file
  permissions.

A stale endpoint file (its server died) refuses connections exactly as a
stale Unix socket does: ``ConnectionRefusedError``.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Tuple, Union

logger = logging.getLogger(__name__)

_PathLike = Union[str, "os.PathLike[str]"]
_ClientCallback = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Optional[Awaitable[None]]]

_installed = False


def _auth_timeout_s() -> float:
    try:
        return float(os.environ.get("JARVIS_PORTABLE_IPC_AUTH_TIMEOUT_S", "") or 5.0)
    except ValueError:
        return 5.0


def read_endpoint(path: _PathLike) -> dict:
    """The endpoint a Windows path-addressed server published at ``path``."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        # Same failure a client sees connecting to a dead or foreign socket.
        raise ConnectionRefusedError(f"no IPC endpoint at {path}: {exc}") from exc
    if data.get("transport") != "tcp" or "port" not in data or "token" not in data:
        raise ConnectionRefusedError(f"no IPC endpoint at {path}")
    return data


def _write_endpoint(path: Path, endpoint: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(endpoint), encoding="utf-8")
    os.replace(tmp, path)


async def start_unix_server(
    client_connected_cb: _ClientCallback,
    path: Optional[_PathLike] = None,
    *,
    sock: Any = None,
    start_serving: bool = True,
    **kwargs: Any,
) -> asyncio.base_events.Server:
    """Windows stand-in for ``asyncio.start_unix_server`` (see module doc)."""
    if path is None:
        raise ValueError("portable_ipc.start_unix_server requires a path")
    endpoint_path = Path(path)
    token = secrets.token_hex(32)
    expected = (token + "\n").encode("ascii")

    async def _authenticated(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            presented = await asyncio.wait_for(reader.readline(), timeout=_auth_timeout_s())
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            presented = b""
        if not hmac.compare_digest(presented, expected):
            logger.warning("[PortableIPC] rejected unauthenticated connection to %s", endpoint_path)
            writer.close()
            return
        result = client_connected_cb(reader, writer)
        if asyncio.iscoroutine(result):
            await result

    server = await asyncio.start_server(
        _authenticated, host="127.0.0.1", port=0, start_serving=start_serving, **kwargs,
    )
    port = server.sockets[0].getsockname()[1]
    _write_endpoint(endpoint_path, {
        "transport": "tcp", "host": "127.0.0.1", "port": port,
        "token": token, "pid": os.getpid(),
    })
    logger.debug("[PortableIPC] %s -> 127.0.0.1:%d", endpoint_path, port)
    return server


async def open_unix_connection(
    path: Optional[_PathLike] = None,
    *,
    sock: Any = None,
    **kwargs: Any,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Windows stand-in for ``asyncio.open_unix_connection`` (see module doc)."""
    if path is None:
        raise ValueError("portable_ipc.open_unix_connection requires a path")
    endpoint = read_endpoint(path)
    reader, writer = await asyncio.open_connection(
        endpoint.get("host", "127.0.0.1"), int(endpoint["port"]), **kwargs,
    )
    writer.write((endpoint["token"] + "\n").encode("ascii"))
    await writer.drain()
    return reader, writer


def connect_unix_sync(path: _PathLike, timeout: Optional[float] = None) -> "socket.socket":
    """Blocking client connect to a path-addressed server, on any platform.

    Returns a connected stream socket (timeout applied) ready for the
    server's protocol: AF_UNIX on POSIX, authenticated loopback on Windows.
    Raises ``ConnectionRefusedError`` / ``FileNotFoundError`` / ``OSError``
    exactly where a Unix-socket connect would.
    """
    import socket

    if hasattr(socket, "AF_UNIX"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(os.fspath(path))
        except BaseException:
            sock.close()
            raise
        return sock
    endpoint = read_endpoint(path)
    sock = socket.create_connection(
        (endpoint.get("host", "127.0.0.1"), int(endpoint["port"])), timeout=timeout,
    )
    try:
        sock.sendall((endpoint["token"] + "\n").encode("ascii"))
    except BaseException:
        sock.close()
        raise
    return sock


def install() -> bool:
    """Provide the asyncio Unix-socket entry points where the platform lacks them."""
    global _installed
    if _installed:
        return True
    if hasattr(asyncio, "start_unix_server") and hasattr(asyncio, "open_unix_connection"):
        return False
    asyncio.start_unix_server = start_unix_server  # type: ignore[attr-defined]
    asyncio.open_unix_connection = open_unix_connection  # type: ignore[attr-defined]
    _installed = True
    return True


__all__ = [
    "connect_unix_sync", "install", "open_unix_connection", "read_endpoint", "start_unix_server",
]
