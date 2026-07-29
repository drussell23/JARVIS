"""Cockpit Attach Bridge — hydration + bi-directional + BrokenPipe spine.

CLI item #6 mandates:
  1. Native IPC, no tail -f cosplay — the tests run the REAL UDS server
     + client pair.
  2. **State Hydration Protocol** — the FIRST frame carries FSM status,
     active ops, and liquidity; the client renders instantly.
  3. Bi-directional — operator input frames reach the daemon's on_input
     sink (wired to _handle_repl_command → verbs + chat bridge).
  4. **BrokenPipe resilience (headline)** — a SIGKILL'd / vanished
     attach terminal is a dropped subscriber; the daemon's publisher
     never raises, never blocks, and keeps serving remaining clients.
"""
from __future__ import annotations

import ast

import asyncio
import json
import os
import stat

import pytest

from backend.core.ouroboros.battle_test import cockpit_attach as ca


@pytest.fixture()
def sock():
    """Short socket path (AF_UNIX ~104-char sun_path cap on macOS)."""
    import shutil
    import tempfile
    from pathlib import Path
    d = tempfile.mkdtemp(prefix="catt")
    try:
        yield Path(d) / "a.sock"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _providers():
    return dict(
        status_provider=lambda: {
            "phase": "GENERATE", "phase_detail": "47s",
            "cost_spent_usd": 0.42, "cost_budget_usd": 3.0,
        },
        ops_provider=lambda: ["op-019f-alpha", "op-019f-beta"],
        liquidity_provider=lambda: {
            "providers": {"anthropic": {"tokens_remaining": 11_000_000}},
            "any_exhausted": False,
        },
    )


async def _server(sock, on_input=None):
    b = ca.CockpitAttachBridge(
        path=sock, on_input=on_input, **_providers(),
    )
    assert await b.start() is True
    return b


class _Sink:
    def __init__(self) -> None:
        self.hydrations = []
        self.lines = []

    def on_hydration(self, m):
        self.hydrations.append(m)

    def on_line(self, t):
        self.lines.append(t)


async def _client(sock, sink):
    c = ca.CockpitAttachClient(
        path=sock, on_hydration=sink.on_hydration, on_line=sink.on_line,
    )
    return c, await c.connect()


# ---------------------------------------------------------------------------
# (1) Hydration protocol — instant state, never a blank screen
# ---------------------------------------------------------------------------


async def test_hydration_is_first_frame_with_full_state(sock):
    b = await _server(sock)
    try:
        sink = _Sink()
        c, ok = await _client(sock, sink)
        assert ok is True
        assert len(sink.hydrations) == 1          # BEFORE any FSM tick
        h = sink.hydrations[0]
        assert h["schema_version"] == ca.COCKPIT_ATTACH_SCHEMA_VERSION
        assert h["status"]["phase"] == "GENERATE"
        assert h["status"]["cost_spent_usd"] == 0.42
        assert h["ops"] == ["op-019f-alpha", "op-019f-beta"]
        assert h["liquidity"]["providers"]["anthropic"][
            "tokens_remaining"] == 11_000_000
        await c.close()
    finally:
        await b.stop()


async def test_hydration_providers_pulled_fresh_per_connect(sock):
    calls = {"n": 0}

    def _status():
        calls["n"] += 1
        return {"phase": f"TICK-{calls['n']}"}

    b = ca.CockpitAttachBridge(path=sock, status_provider=_status)
    assert await b.start()
    try:
        s1, s2 = _Sink(), _Sink()
        c1, _ = await _client(sock, s1)
        c2, _ = await _client(sock, s2)
        assert s1.hydrations[0]["status"]["phase"] == "TICK-1"
        assert s2.hydrations[0]["status"]["phase"] == "TICK-2"   # not cached
        await c1.close()
        await c2.close()
    finally:
        await b.stop()


async def test_broken_provider_degrades_not_dies(sock):
    def _boom():
        raise RuntimeError("provider exploded")

    b = ca.CockpitAttachBridge(path=sock, status_provider=_boom)
    assert await b.start()
    try:
        sink = _Sink()
        c, ok = await _client(sock, sink)
        assert ok is True                          # handshake survived
        assert sink.hydrations[0]["status"] == {}
        await c.close()
    finally:
        await b.stop()


# ---------------------------------------------------------------------------
# (2) Downstream streaming — the _repl_print mirror
# ---------------------------------------------------------------------------


async def test_published_lines_stream_to_client(sock):
    b = await _server(sock)
    try:
        sink = _Sink()
        c, ok = await _client(sock, sink)
        assert ok
        b.publish_line("⏺ apply — 2 file(s)")
        b.publish_line("⎿ verify: 4/4")
        for _ in range(50):
            if len(sink.lines) >= 2:
                break
            await asyncio.sleep(0.02)
        assert sink.lines == ["⏺ apply — 2 file(s)", "⎿ verify: 4/4"]
        await c.close()
    finally:
        await b.stop()


# ---------------------------------------------------------------------------
# (3) Bi-directional — operator input reaches the daemon sink
# ---------------------------------------------------------------------------


async def test_input_frames_reach_daemon_sink(sock):
    received = []
    b = await _server(sock, on_input=received.append)
    try:
        sink = _Sink()
        c, ok = await _client(sock, sink)
        assert ok
        assert c.send_input("/liquidity") is True
        assert c.send_input("what are you working on?") is True
        for _ in range(50):
            if len(received) >= 2:
                break
            await asyncio.sleep(0.02)
        assert received == ["/liquidity", "what are you working on?"]
        await c.close()
    finally:
        await b.stop()


async def test_input_sink_exception_never_kills_session(sock):
    def _bad(_t):
        raise RuntimeError("sink exploded")

    b = await _server(sock, on_input=_bad)
    try:
        sink = _Sink()
        c, ok = await _client(sock, sink)
        assert ok
        c.send_input("boom")
        await asyncio.sleep(0.1)
        # Session survived — a publish still arrives.
        b.publish_line("still alive")
        for _ in range(50):
            if sink.lines:
                break
            await asyncio.sleep(0.02)
        assert sink.lines == ["still alive"]
        await c.close()
    finally:
        await b.stop()


async def test_malformed_input_frame_ignored(sock):
    received = []
    b = await _server(sock, on_input=received.append)
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(sock))
        await reader.readline()                    # consume hydration
        writer.write(b"{never json\n")
        writer.write(
            json.dumps({"type": "input", "text": "ok"}).encode() + b"\n"
        )
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.02)
        assert received == ["ok"]
        writer.close()
    finally:
        await b.stop()


# ---------------------------------------------------------------------------
# (4) HEADLINE — BrokenPipe / ConnectionReset resilience
# ---------------------------------------------------------------------------


async def test_vanished_terminal_is_dropped_and_organism_continues(sock):
    """A SIGKILL'd `ov attach` (abrupt socket close) must cost the
    daemon exactly one subscriber — publishes keep flowing to the
    survivors and NOTHING raises into the FSM loop."""
    b = await _server(sock)
    try:
        survivor = _Sink()
        c_live, ok1 = await _client(sock, survivor)
        reader, writer = await asyncio.open_unix_connection(path=str(sock))
        await reader.readline()                    # raw client hydrated
        assert ok1 and b.client_count == 2

        # Abrupt death — no goodbye, transport torn down.
        writer.transport.abort()
        await asyncio.sleep(0.05)

        # The organism keeps publishing; the survivor keeps receiving.
        for i in range(3):
            b.publish_line(f"line-{i}")
        for _ in range(50):
            if len(survivor.lines) >= 3:
                break
            await asyncio.sleep(0.02)
        assert survivor.lines == ["line-0", "line-1", "line-2"]
        for _ in range(50):
            if b.client_count == 1:
                break
            await asyncio.sleep(0.02)
        assert b.client_count == 1                 # corpse reaped
        assert b.stats["dropped"] >= 1
        await c_live.close()
    finally:
        await b.stop()


async def test_broken_pipe_writer_dropped_without_raise(sock):
    """Direct fault injection: a writer whose write() raises
    BrokenPipeError is dropped mid-broadcast; the publish call NEVER
    raises and remaining clients still receive."""
    b = await _server(sock)
    try:
        sink = _Sink()
        c, ok = await _client(sock, sink)
        assert ok

        class _BrokenWriter:
            def is_closing(self):
                return False

            def write(self, _data):
                raise BrokenPipeError("gone")

            def close(self):
                pass

        b._clients.add(_BrokenWriter())            # type: ignore[arg-type]
        b.publish_line("survives")                 # must not raise
        for _ in range(50):
            if sink.lines:
                break
            await asyncio.sleep(0.02)
        assert sink.lines == ["survives"]
        assert b.client_count == 1                 # broken writer gone
        await c.close()
    finally:
        await b.stop()


async def test_connection_reset_writer_dropped_without_raise(sock):
    b = await _server(sock)
    try:
        class _ResetWriter:
            def is_closing(self):
                return False

            def write(self, _data):
                raise ConnectionResetError("reset by peer")

            def close(self):
                pass

        b._clients.add(_ResetWriter())             # type: ignore[arg-type]
        b.publish_line("no raise")                 # must not raise
        assert b.client_count == 0
    finally:
        await b.stop()


async def test_publish_with_no_clients_is_free(sock):
    b = await _server(sock)
    try:
        b.publish_line("into the void")            # no clients, no raise
        assert b.stats["lines_published"] == 0     # short-circuited
    finally:
        await b.stop()


# ---------------------------------------------------------------------------
# (5) Client degradation + hygiene
# ---------------------------------------------------------------------------


async def test_dead_socket_degrades_fast(sock):
    import time as _time
    c = ca.CockpitAttachClient(path=sock.parent / "nope.sock")
    t0 = _time.monotonic()
    assert await c.connect() is False
    assert _time.monotonic() - t0 < 1.5


async def test_daemon_exit_marks_client_disconnected(sock):
    b = await _server(sock)
    sink = _Sink()
    c, ok = await _client(sock, sink)
    assert ok and c.connected
    await b.stop()
    for _ in range(50):
        if not c.connected:
            break
        await asyncio.sleep(0.02)
    assert c.connected is False
    assert c.send_input("late") is False           # detached pipe refuses
    await c.close()


async def test_socket_perms_and_unlink(sock):
    b = await _server(sock)
    assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600
    await b.stop()
    assert not sock.exists()


# ---------------------------------------------------------------------------
# (6) Wiring pins
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / rel).read_text()


def test_harness_mounts_bridge_and_mirrors_chokepoint():
    src = _read("backend/core/ouroboros/battle_test/harness.py")
    assert "_start_cockpit_attach_bridge" in src
    body = src[src.index("def _repl_print"):][:2000]
    assert "publish_line" in body                  # chokepoint mirror
    # Structural, not positional. This previously asserted the name
    # appeared within an arbitrary 4000-character window after the `def`,
    # so adding a comment block to the function broke it while the wiring
    # was intact. The claim is "attached input reaches the REPL handler" —
    # which is a fact about the FUNCTION, not about byte offsets.
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_start_cockpit_attach_bridge"
    )
    assert "_handle_repl_command" in ast.dump(fn), (
        "attached operator input no longer reaches the REPL handler")


def test_ov_attach_is_real_not_stub():
    src = _read("backend/core/ouroboros/cli/ov.py")
    assert "CockpitAttachClient" in src
    assert "run_attach" in src
    assert "coming soon" not in src
    assert "follow-up sprint" not in src
    assert "no organism awake" in src              # degradation message


# ---------------------------------------------------------------------------
# Socket self-heal sentinel (the unlinked-live-socket class, 2026-07-23)
# ---------------------------------------------------------------------------


async def test_sentinel_rebinds_vanished_socket(sock, monkeypatch):
    """If ANY confused peer unlinks the live socket inode (a CLI
    misclassifying a starved organism as a ghost, an operator rm), the
    sentinel must REBIND at the same path — the organism never becomes
    permanently unattachable."""
    monkeypatch.setenv("JARVIS_ATTACH_SENTINEL_S", "0.05")
    b = await _server(sock)
    try:
        assert sock.exists()
        sock.unlink()                          # the confused peer strikes
        for _ in range(100):                   # ≤5s for the heal
            await asyncio.sleep(0.05)
            if sock.exists():
                break
        assert sock.exists(), "sentinel never rebound the socket"
        # And the reborn socket genuinely SERVES (full client handshake).
        sink = _Sink()
        c = ca.CockpitAttachClient(
            path=sock, on_hydration=sink.on_hydration, on_line=sink.on_line,
        )
        assert await c.connect() is True
        c.close()
    finally:
        await b.stop()


async def test_sentinel_stops_with_bridge(sock, monkeypatch):
    """stop() must end the sentinel — no immortal task, and no resurrection
    of a deliberately-stopped bridge's socket."""
    monkeypatch.setenv("JARVIS_ATTACH_SENTINEL_S", "0.05")
    b = await _server(sock)
    await b.stop()
    assert b._sentinel_task is None
    await asyncio.sleep(0.2)
    assert not sock.exists()                   # stop() unlinked; stayed gone


def test_autonomy_chain_mounted_in_harness_not_repl():
    """Wiring invariant (the wired-but-inert class): the AWE Trigger +
    Autonomous Supervisor mount must live on the HARNESS boot path (both
    modes), not inside SerpentREPL.start() where --headless never goes."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    harness_src = (
        root / "backend/core/ouroboros/battle_test/harness.py"
    ).read_text()
    serpent_src = (
        root / "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    assert "def _start_autonomy_chain" in harness_src
    assert "self._start_autonomy_chain()" in harness_src
    assert "start_awe_trigger()" not in serpent_src
    assert "start_autonomous_supervisor()" not in serpent_src


def test_autonomy_chain_env_gated_inert(monkeypatch):
    """With both masters off, the chain mounts as None (inert-by-default)."""
    monkeypatch.setenv("JARVIS_AWE_TRIGGER_ENABLED", "false")
    monkeypatch.setenv("JARVIS_AUTONOMOUS_SUPERVISOR_ENABLED", "false")
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness
    h = BattleTestHarness.__new__(BattleTestHarness)   # no heavy __init__
    h._start_autonomy_chain()
    assert h._awe_trigger is None
    assert h._autonomous_supervisor is None


# ---------------------------------------------------------------------------
# Escalating-patience attach (post-boot-storm hydration lag, 2026-07-23)
# ---------------------------------------------------------------------------


async def test_connect_survives_slow_hydration(sock, monkeypatch):
    """A live organism whose loop lags hydration past one 0.5s shot must
    still attach — timeout-class failures retry with escalating bounds."""
    monkeypatch.setenv("JARVIS_ATTACH_CONNECT_TIMEOUT_S", "0.3")
    monkeypatch.setenv("JARVIS_ATTACH_CONNECT_PATIENCE_S", "10")

    async def _laggy(reader, writer):
        await asyncio.sleep(1.2)               # the post-boot storm
        writer.write(b'{"type":"hydration","status":{}}\n')
        await writer.drain()
        try:
            await reader.read(64)
        except Exception:
            pass

    server = await asyncio.start_unix_server(_laggy, path=str(sock))
    try:
        sink = _Sink()
        c = ca.CockpitAttachClient(
            path=sock, on_hydration=sink.on_hydration, on_line=sink.on_line,
        )
        assert await c.connect() is True       # single-shot would have died
        assert sink.hydrations
        c.close()
    finally:
        server.close()


async def test_connect_fails_fast_on_dead_socket(sock, monkeypatch):
    """Waiting cannot conjure a dead listener: refused/absent must fail
    in well under the patience budget."""
    import time as _time
    monkeypatch.setenv("JARVIS_ATTACH_CONNECT_PATIENCE_S", "30")
    sock.write_bytes(b"")                      # plain file → refused
    c = ca.CockpitAttachClient(path=sock)
    t0 = _time.monotonic()
    assert await c.connect() is False
    assert _time.monotonic() - t0 < 3.0        # fast, not 30s


# ---------------------------------------------------------------------------
# Tool-activity markup channel (CC-style ⏺/⎿ blocks + diffs → ov cockpit)
# ---------------------------------------------------------------------------


async def test_markup_frame_roundtrip(sock):
    """publish_markup → typed frame → client on_markup, verbatim styled."""
    b = await _server(sock)
    try:
        got_markup, got_lines = [], []
        c = ca.CockpitAttachClient(
            path=sock,
            on_line=got_lines.append,
            on_markup=got_markup.append,
        )
        assert await c.connect() is True
        styled = "  [green]⏺ Bash[/green]([cyan]pytest tests/[/cyan])"
        b.publish_markup(styled)
        b.publish_line("plain chatter")
        for _ in range(100):
            await asyncio.sleep(0.02)
            if got_markup and got_lines:
                break
        assert got_markup == [styled]          # styled channel, verbatim
        assert got_lines == ["plain chatter"]  # untyped channel untouched
        c.close()
    finally:
        await b.stop()


async def test_markup_gate_off_publishes_nothing(sock, monkeypatch):
    monkeypatch.setenv("JARVIS_ATTACH_TOOL_ACTIVITY_ENABLED", "0")
    b = await _server(sock)
    try:
        got = []
        c = ca.CockpitAttachClient(path=sock, on_markup=got.append)
        assert await c.connect() is True
        b.publish_markup("[red]x[/red]")
        b.publish_line("beacon")               # proves the pipe flows
        lines = []
        c._on_line = lines.append
        b.publish_line("beacon2")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if lines:
                break
        assert got == []                       # gated channel silent
        c.close()
    finally:
        await b.stop()


async def test_markup_degrades_to_on_line_without_handler(sock):
    """A conservative client (no on_markup) still SEES the content —
    delivered through on_line where it renders escaped, never dropped."""
    b = await _server(sock)
    try:
        got_lines = []
        c = ca.CockpitAttachClient(path=sock, on_line=got_lines.append)
        assert await c.connect() is True
        b.publish_markup("[green]+ added[/green]")
        for _ in range(100):
            await asyncio.sleep(0.02)
            if got_lines:
                break
        assert got_lines == ["[green]+ added[/green]"]
        c.close()
    finally:
        await b.stop()


def test_serpent_flow_op_line_mirrors_markup():
    """The ONE render chokepoint mirrors every op-scoped line to the
    bridge sink — and a sink fault can never break the local render."""
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    sf = SerpentFlow(session_id="t", branch_name="b")
    mirrored = []
    sf.markup_mirror = mirrored.append
    sf._op_line("", "[dim]system banner[/dim]")     # out-of-band line
    assert mirrored == ["  [dim]system banner[/dim]"]
    # A crashing sink is swallowed — local render (and caller) unaffected.
    sf.markup_mirror = lambda _l: (_ for _ in ()).throw(RuntimeError("gone"))
    sf._op_line("", "still fine")                    # must not raise
    # None (default) = zero-cost no-op
    sf2 = SerpentFlow()
    assert sf2.markup_mirror is None
    sf2._op_line("", "local only")


def test_harness_wires_mirror_to_bridge():
    """Wiring invariant: the harness connects SerpentFlow.markup_mirror to
    the bridge's publish_markup at the attach-bridge mount."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    harness_src = (
        root / "backend/core/ouroboros/battle_test/harness.py"
    ).read_text()
    assert "sf.markup_mirror = bridge.publish_markup" in harness_src


def test_ov_markup_renderer_fail_soft():
    """Client-side: valid markup passes through; MALFORMED markup renders
    escaped (inert) — the canvas can never crash on a bad frame."""
    from backend.core.ouroboros.cli.ov import _render_markup_frame
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout,
        set_active_canvas,
    )
    mux = BipartiteLayout(width=100, height=20)
    set_active_canvas(mux)
    try:
        _render_markup_frame("  [green]⏺ Bash[/green](ls)")
        _render_markup_frame("  [bold]mismatched[/red]")   # raises in Rich
        snap = mux._buffer.snapshot()
        assert snap[0] == "  [green]⏺ Bash[/green](ls)"    # trusted verbatim
        assert "\\[" in snap[1]                            # escaped, inert
        ansi = mux.render_canvas_ansi()                    # renders w/o raising
        assert "Bash" in ansi
    finally:
        set_active_canvas(None)
