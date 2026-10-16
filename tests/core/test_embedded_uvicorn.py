"""Ctrl+C belongs to the kernel, not to the web server it embeds (2026-10-16 00:13).

uvicorn's ``serve()`` replaced SIGINT/SIGTERM for as long as it ran, so a
Ctrl+C reached uvicorn -- which quietly stopped the backend -- and never the
kernel's SignalDispatcher. These tests serve a real uvicorn on the main thread
and raise a real SIGINT."""
from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest

uvicorn = pytest.importorskip("uvicorn")


async def _app(scope, receive, send):  # minimal ASGI app
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


def _serve_and_interrupt(server_factory):
    """Serve on this (main) thread, raise SIGINT mid-serve, and report who got
    it: the host's handler, and whether the server decided to exit."""
    received = []
    previous = signal.signal(signal.SIGINT, lambda signum, frame: received.append(signum))
    try:
        config = uvicorn.Config(_app, host="127.0.0.1", port=0, log_level="critical",
                                loop="asyncio", lifespan="on")
        server = server_factory(config)

        async def main():
            task = asyncio.ensure_future(server.serve())
            while not server.started:
                await asyncio.sleep(0.02)
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0.2)
            stole = server.should_exit
            server.should_exit = True  # the owner stops it, as the kernel does
            await asyncio.wait_for(task, timeout=10)
            return stole

        stole = asyncio.run(main())
        return received, stole, signal.getsignal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)


def test_a_plain_server_takes_ctrl_c_from_its_host():
    """The defect, pinned: if this stops holding, uvicorn changed and the
    embedded server may no longer be needed."""
    received, stole, _ = _serve_and_interrupt(uvicorn.Server)
    assert stole is True
    assert not received or received == [signal.SIGINT]  # re-raised only after serve ends


def test_an_embedded_server_leaves_ctrl_c_to_its_host():
    from backend.core.embedded_uvicorn import embedded_server

    received, stole, after = _serve_and_interrupt(embedded_server)
    assert received == [signal.SIGINT]
    assert stole is False
    assert callable(after)


def test_every_in_process_server_is_embedded():
    """The kernel's backend, the co-booted gateway and the async system manager
    all run inside a process whose signals belong to someone else."""
    for rel in ("unified_supervisor.py", "backend/api/observability_gateway.py",
                "backend/core/async_system_manager.py"):
        src = Path(rel).read_text(encoding="utf-8")
        assert "uvicorn.Server(" not in src, rel
        assert "embedded_server(" in src, rel
