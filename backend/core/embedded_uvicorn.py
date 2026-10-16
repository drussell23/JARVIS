"""
embedded_uvicorn - a uvicorn server that runs inside a process it does not own.

``uvicorn.Server.serve()`` wraps the whole serve in ``capture_signals()``:
for as long as it runs it replaces the process's SIGINT, SIGTERM (and on
Windows SIGBREAK) handlers with its own ``handle_exit`` -- which only stops
*that server*, quietly -- and restores them when it returns. That is right for
a process that IS the web server. The kernel is not: its backend API is one
task on the kernel's own loop, and the kernel's shutdown is owned by the
central SignalDispatcher (backend/kernel/signals.py).

On 2026-10-16 00:13 that is exactly what happened: Ctrl+C reached the kernel
while its backend was serving, so uvicorn took it, began stopping the backend
at log level "warning" (nothing logged), and the kernel's graceful shutdown
never ran -- no "Received SIGINT", no teardown, until the window was closed
on it. The earlier guard in async_system_manager
(``server.install_signal_handlers = lambda: None``) had been dead since
uvicorn 0.29 replaced that hook with ``capture_signals``.

``EmbeddedServer`` leaves signals to the process that owns them. Its owner
stops it the way the kernel already does: ``server.should_exit = True``.
Both hooks are neutralised, so the class is correct on every uvicorn version
the venvs may carry. A uvicorn server that is its own process (the
telemetry broker's gateway) keeps uvicorn's handling -- there the process and
the server are the same thing.
"""
from __future__ import annotations

import contextlib
from typing import Any, Dict, Iterator, Type

import uvicorn


class _LeavesSignalsAlone:
    """Mixin: neither uvicorn hook touches the process's signal handlers."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:  # uvicorn >= 0.29
        yield

    def install_signal_handlers(self) -> None:  # uvicorn < 0.29
        return None


_embedded: Dict[type, Type[Any]] = {}


def embedded_class(base: type) -> Type[Any]:
    """*base* (a ``uvicorn.Server`` class) with signals left to the host."""
    cls = _embedded.get(base)
    if cls is None:
        cls = _embedded[base] = type(f"Embedded{base.__name__}", (_LeavesSignalsAlone, base), {})
    return cls


def embedded_server(config: Any) -> Any:
    """A server for *config* that leaves Ctrl+C and SIGTERM to the host process.

    Built on whichever ``uvicorn.Server`` is in effect when called, so a
    substituted server class (a test double, a wrapped server) is the one
    embedded."""
    return embedded_class(uvicorn.Server)(config)


EmbeddedServer = embedded_class(uvicorn.Server)

__all__ = ["EmbeddedServer", "embedded_class", "embedded_server"]
