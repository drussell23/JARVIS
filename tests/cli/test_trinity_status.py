"""trinity status — Active IPC Health Handshake spine.

Mandate 4 (verbatim): simulate a Zombie Daemon — an active PID but a
probe_socket that raises TimeoutError. Assert trinity status synthesizes
ZOMBIE/DEADLOCKED, not a false HEALTHY.

Plus the full synthesis matrix, non-invasive probing, and the strict
timeout extension to probe_socket.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.cli import trinity_status as st


async def _live(*a, **k): return "live"
async def _refused(*a, **k): return "refused"
async def _timeout(*a, **k): return "timeout"
async def _absent(*a, **k): return "absent"


# ---------------------------------------------------------------------------
# MANDATE 4 — Zombie Daemon: active PID + probe that RAISES TimeoutError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zombie_active_pid_but_socket_probe_raises_timeout(tmp_path):
    """The core mandate-4 contract: launchctl says pid 4242 is alive, but
    the UDS probe RAISES TimeoutError (event loop wedged). Must be flagged
    ZOMBIE — never HEALTHY."""
    async def _raises_timeout(*a, **k):
        raise TimeoutError("no handshake — loop deadlocked")

    report = await st.active_health_handshake(
        port=8010, socket_path=tmp_path / "attach.sock",
        pid_fn=lambda: 4242,                 # launchctl → active PID
        tcp_probe=_raises_timeout,           # TCP also wedged
        socket_probe=_raises_timeout,        # UDS RAISES TimeoutError
    )
    assert report.state is st.Health.ZOMBIE
    assert report.ok is False                # NOT a false HEALTHY
    assert report.pid == 4242
    assert "deadlock" in report.detail.lower()
    assert "tail" in report.recommendation.lower()   # points at error log


@pytest.mark.asyncio
async def test_zombie_pid_alive_tcp_refused(tmp_path):
    """PID alive but TCP refused (multiplexer never bound) → ZOMBIE."""
    report = await st.active_health_handshake(
        socket_path=tmp_path / "s.sock", pid_fn=lambda: 999,
        tcp_probe=_refused, socket_probe=_absent)
    assert report.state is st.Health.ZOMBIE
    assert report.tcp_state == "refused"


# ---------------------------------------------------------------------------
# Synthesis matrix
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_healthy_pid_and_both_transports_live(tmp_path):
    report = await st.active_health_handshake(
        socket_path=tmp_path / "s.sock", pid_fn=lambda: 111,
        tcp_probe=_live, socket_probe=_live)
    assert report.state is st.Health.HEALTHY
    assert report.ok is True


@pytest.mark.asyncio
async def test_healthy_tcp_live_uds_absent_is_fine(tmp_path):
    """The cockpit UDS being absent (nobody attached) is NOT unhealthy so
    long as the supervisor's own TCP is live."""
    report = await st.active_health_handshake(
        socket_path=tmp_path / "s.sock", pid_fn=lambda: 111,
        tcp_probe=_live, socket_probe=_absent)
    assert report.state is st.Health.HEALTHY


@pytest.mark.asyncio
async def test_down_no_pid_no_transport(tmp_path):
    report = await st.active_health_handshake(
        socket_path=tmp_path / "s.sock", pid_fn=lambda: None,
        tcp_probe=_refused, socket_probe=_absent)
    assert report.state is st.Health.DOWN
    assert "install" in report.recommendation or "up" in report.recommendation


def test_status_main_exit_code_nonzero_on_zombie(monkeypatch):
    """status_main is SYNC (it owns asyncio.run) — so this test is sync."""
    class _C:
        def __init__(self): self.lines = []
        def print(self, t, **k): self.lines.append(str(t))
    async def _zombie():
        return st.HealthReport(state=st.Health.ZOMBIE, pid=5,
                               detail="wedged", recommendation="tail log")
    monkeypatch.setattr(st, "active_health_handshake", lambda **k: _zombie())
    c = _C()
    rc = st.status_main(c)
    assert rc == 1                           # non-zero on unhealthy
    assert any("ZOMBIE" in l for l in c.lines)


def test_status_main_exit_zero_on_healthy(monkeypatch):
    class _C:
        def print(self, t, **k): pass
    async def _ok():
        return st.HealthReport(state=st.Health.HEALTHY, pid=5, detail="ok")
    monkeypatch.setattr(st, "active_health_handshake", lambda **k: _ok())
    assert st.status_main(_C()) == 0


# ---------------------------------------------------------------------------
# MANDATE 1/3 — PID from a NON-socket source; DRY probe reuse
# ---------------------------------------------------------------------------

def test_supervisor_pid_parses_launchctl(monkeypatch):
    class _R:
        stdout = '{\n\t"PID" = 6789;\n\t"Label" = "com.jarvis.supervisor";\n}'
    pid = st.supervisor_pid(runner=lambda *a, **k: _R())
    assert pid == 6789


def test_supervisor_pid_none_when_unregistered(monkeypatch):
    class _R:
        stdout = ""
    # launchctl empty + pgrep empty → None
    monkeypatch.setattr(st.subprocess, "run", lambda *a, **k: _R())
    # force pgrep path to also be empty via the same runner
    pid = st.supervisor_pid(runner=lambda *a, **k: _R())
    assert pid is None


# ---------------------------------------------------------------------------
# MANDATE 2/3 — probe_socket strict-timeout extension + non-invasive probe_tcp
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_socket_accepts_strict_timeout(tmp_path):
    """DRY: probe_socket now takes a timeout override for live validation.
    An absent socket returns 'absent' fast under a strict bound."""
    from backend.core.ouroboros.cli.thin_client import probe_socket
    state = await probe_socket(tmp_path / "nope.sock", 0.5)
    assert state == "absent"


@pytest.mark.asyncio
async def test_probe_tcp_refused_on_dead_port():
    """probe_tcp against a definitely-closed port → 'refused', fast, no
    lingering connection (non-invasive)."""
    import socket
    from backend.core.ouroboros.cli.thin_client import probe_tcp
    s = socket.socket(); s.bind(("127.0.0.1", 0)); free = s.getsockname()[1]
    s.close()
    state = await probe_tcp("127.0.0.1", free, 1.0)
    assert state == "refused"


@pytest.mark.asyncio
async def test_probe_tcp_live_on_real_listener():
    import socket
    from backend.core.ouroboros.cli.thin_client import probe_tcp
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    try:
        state = await probe_tcp("127.0.0.1", port, 1.0)
        assert state == "live"
    finally:
        srv.close()


@pytest.mark.asyncio
async def test_probe_http_timeout_on_kernel_accept_but_no_app_response():
    """The root-cause fix: a socket bound + listening but never processing
    (wedged loop) completes the kernel handshake, so a bare connect says
    'live' — but probe_http sends a request and gets NO response → timeout
    (the honest ZOMBIE signal)."""
    import socket
    from backend.core.ouroboros.cli.thin_client import probe_http
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(1)           # listens, NEVER accepts/reads
    port = srv.getsockname()[1]
    try:
        state = await probe_http("127.0.0.1", port, 1.0)
        assert state == "timeout"                        # app-level dead
    finally:
        srv.close()


@pytest.mark.asyncio
async def test_probe_http_live_on_responding_server():
    """A server that accepts + replies with an HTTP line → live."""
    import asyncio
    from backend.core.ouroboros.cli.thin_client import probe_http

    async def _handle(reader, writer):
        await reader.read(256)
        writer.write(b"HTTP/1.0 200 OK\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        state = await probe_http("127.0.0.1", port, 2.0)
    assert state == "live"


@pytest.mark.asyncio
async def test_probe_http_refused_on_dead_port():
    import socket
    from backend.core.ouroboros.cli.thin_client import probe_http
    s = socket.socket(); s.bind(("127.0.0.1", 0)); free = s.getsockname()[1]; s.close()
    assert await probe_http("127.0.0.1", free, 1.0) == "refused"
