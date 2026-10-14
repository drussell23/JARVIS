"""
portable_sockets - one meaning for "may I reuse this address?" on every OS.

Every bind-test and server bind in JARVIS set SO_REUSEADDR, meaning the POSIX
rule: a port left in TIME_WAIT may be rebound. Windows gives that option a
different meaning -- a bind succeeds on top of a socket that is still
LISTENING -- so a free-port probe reported an occupied port free (the loading
server was handed 8080 while a WSL relay served there, and its health check
then talked to the relay), and two servers could share one port silently.
Windows spells the POSIX meaning SO_EXCLUSIVEADDRUSE.
"""
from __future__ import annotations

import socket


def allow_time_wait_rebind(sock: socket.socket) -> None:
    """Let *sock* bind a port in TIME_WAIT, never one another socket is using."""
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)  # Windows only
    if exclusive is not None:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


__all__ = ["allow_time_wait_rebind"]
