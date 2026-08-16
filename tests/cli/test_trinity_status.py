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


def _row(name, state, **kw):
    return st.DaemonHealth(name=name, title=kw.pop("title", name), state=state,
                           **kw)


def _fleet(*rows):
    async def _f(*a, **k):
        return st.FleetHealth(daemons=tuple(rows))
    return _f


def test_status_main_exit_code_nonzero_on_zombie(monkeypatch):
    """status_main is SYNC (it owns asyncio.run) — so this test is sync.

    Seam moved from `active_health_handshake` to `assess_fleet` when status
    stopped assuming a monolith; the CONTRACT it pins is unchanged — a
    present-and-wrong daemon exits non-zero and is named on screen.
    """
    class _C:
        def __init__(self): self.lines = []
        def print(self, t, **k): self.lines.append(str(t))
    monkeypatch.setattr(st, "assess_fleet", _fleet(
        _row("supervisor", st.Health.ZOMBIE, pid=5, detail="wedged",
             recommendation="tail log")))
    c = _C()
    rc = st.status_main(c)
    assert rc == 1                           # non-zero on unhealthy
    assert any("ZOMBIE" in l for l in c.lines)


def test_status_main_exit_zero_on_healthy(monkeypatch):
    class _C:
        def print(self, t, **k): pass
    monkeypatch.setattr(st, "assess_fleet", _fleet(
        _row("supervisor", st.Health.HEALTHY, pid=5, detail="ok")))
    assert st.status_main(_C()) == 0


def test_a_down_engine_is_not_a_failure(monkeypatch):
    """The multi-daemon consequence: a deployment may legitimately run only
    the supervisor. DOWN is a fact; ZOMBIE/DEGRADED is a fault."""
    class _C:
        def __init__(self): self.lines = []
        def print(self, t, **k): self.lines.append(str(t))
    monkeypatch.setattr(st, "assess_fleet", _fleet(
        _row("supervisor", st.Health.HEALTHY, pid=5, detail="ok"),
        _row("ov", st.Health.DOWN, detail="not running")))
    c = _C()
    assert st.status_main(c) == 0
    assert any("DOWN" in l for l in c.lines), "still REPORTED, just not fatal"


def test_both_daemons_are_rendered_separately(monkeypatch):
    """The point of the matrix: one row cannot borrow the other's verdict."""
    class _C:
        def __init__(self): self.lines = []
        def print(self, t, **k): self.lines.append(str(t))
    monkeypatch.setattr(st, "assess_fleet", _fleet(
        _row("supervisor", st.Health.HEALTHY, title="supervisor", detail="a"),
        _row("ov", st.Health.ZOMBIE, title="O+V engine", pid=9, detail="b")))
    c = _C()
    assert st.status_main(c) == 1
    joined = "\n".join(c.lines)
    assert "supervisor" in joined and "O+V engine" in joined


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


# ---------------------------------------------------------------------------
# Multi-daemon matrix — a daemon is judged by ITS OWN transports
# ---------------------------------------------------------------------------


class TestOneDaemonCannotBorrowAnothersVerdict:
    """THE regression. `trinity status` reported
    `ZOMBIE/DEADLOCKED (tcp=live, uds=stale)` about a supervisor that was
    serving HTTP 200 — because the `uds` it folded in was the COCKPIT's
    socket, which `ov` owns since it took the governed loop.
    """

    def test_the_supervisor_row_no_longer_probes_the_cockpit_socket(
            self, monkeypatch):
        monkeypatch.delenv("JARVIS_SUPERVISOR_IPC_SOCKET", raising=False)
        monkeypatch.setenv("JARVIS_ATTACH_IPC_SOCKET", "/tmp/cockpit-owned.sock")
        assert st._attach_socket() is None, "the cockpit socket is not the supervisor's"
        assert str(st.engine_socket()) == "/tmp/cockpit-owned.sock"

    def test_a_supervisor_with_a_live_port_is_healthy_whatever_the_cockpit_does(
            self):
        """tcp live + no uds of its own = HEALTHY. This exact state was
        being reported ZOMBIE."""
        assert st.classify(True, {"tcp": "live", "uds": "absent"}) is st.Health.HEALTHY

    def test_a_dedicated_supervisor_socket_is_honoured_when_declared(
            self, monkeypatch):
        monkeypatch.setenv("JARVIS_SUPERVISOR_IPC_SOCKET", "/tmp/sup.sock")
        assert str(st._attach_socket()) == "/tmp/sup.sock"

    async def test_each_row_probes_only_its_own_transports(self):
        seen = []

        def _t(label, value):
            def _r():
                seen.append(label)
                return value
            return st.Transport("tcp", label, _r)

        a = st.DaemonSpec(name="a", title="A", pid_source=lambda: None,
                          transports=(_t("a-tcp", ("127.0.0.1", 1)),))
        b = st.DaemonSpec(name="b", title="B", pid_source=lambda: None,
                          transports=(_t("b-tcp", ("127.0.0.1", 2)),))
        fleet = await st.assess_fleet((a, b), timeout=0.05)
        assert {d.name for d in fleet.daemons} == {"a", "b"}
        assert set(fleet.by_name("a").transports) == {"a-tcp"}
        assert set(fleet.by_name("b").transports) == {"b-tcp"}


class TestTheVerdictRuleIsSharedNotCopied:
    def test_down_zombie_healthy_degraded(self):
        assert st.classify(False, {"tcp": "refused"}) is st.Health.DOWN
        assert st.classify(True, {"tcp": "refused"}) is st.Health.ZOMBIE
        assert st.classify(True, {"tcp": "live"}) is st.Health.HEALTHY
        assert st.classify(True, {"tcp": "live", "uds": "stale"}) is st.Health.DEGRADED

    def test_it_never_raises(self):
        assert st.classify(True, None) in tuple(st.Health)


class TestDynamicDiscovery:
    """Zero hardcoded addresses: every probe target is read from the SAME
    configuration the daemons bind with, at probe time."""

    def test_the_supervisor_endpoint_follows_the_backend_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BACKEND_HOST", "127.0.0.1")
        monkeypatch.setenv("JARVIS_BACKEND_PORT", "9111")
        assert st.supervisor_endpoint() == ("127.0.0.1", 9111)

    def test_the_engine_bus_follows_the_channel_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CHANNEL_HOST", "127.0.0.1")
        monkeypatch.setenv("JARVIS_CHANNEL_PORT", "9199")
        assert st.engine_endpoint() == ("127.0.0.1", 9199)

    def test_the_cockpit_socket_comes_from_the_cockpit_contract(
            self, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_IPC_SOCKET", "/tmp/x.sock")
        assert str(st.engine_socket()) == "/tmp/x.sock"

    def test_the_engine_pid_comes_from_the_lock_the_engine_writes(
            self, tmp_path):
        """A NON-socket source, so a wedged socket cannot mask a live
        process — the O+V parallel to launchctl for the supervisor."""
        import json
        import os as _os
        (tmp_path / ".jarvis").mkdir()
        (tmp_path / ".jarvis" / "intake_router.lock").write_text(
            json.dumps({"pid": _os.getpid()}))
        assert st.engine_pid(root=tmp_path) == _os.getpid()

    def test_a_stale_lock_is_not_a_daemon(self, tmp_path):
        import json
        (tmp_path / ".jarvis").mkdir()
        (tmp_path / ".jarvis" / "intake_router.lock").write_text(
            json.dumps({"pid": 2_000_000}))       # cannot exist
        assert st.engine_pid(root=tmp_path) is None

    def test_a_missing_lock_is_not_an_error(self, tmp_path):
        assert st.engine_pid(root=tmp_path) is None


class TestTheDiagnosticNeverHangs:
    """A tool that freezes while reporting on a frozen daemon has become the
    problem it was run to describe."""

    def test_probe_timeouts_are_sub_second_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_FLEET_PROBE_TIMEOUT_S", raising=False)
        assert st.fleet_probe_timeout_s() < 1.0

    async def test_a_hanging_probe_is_reported_not_awaited(self):
        async def _never(*_a, **_k):
            await asyncio.sleep(3600)

        import backend.core.ouroboros.cli.thin_client as tc
        real = tc.probe_http
        tc.probe_http = _never
        try:
            spec = st.DaemonSpec(
                name="wedged", title="wedged", pid_source=lambda: 4242,
                transports=(st.Transport("tcp", "tcp",
                                         lambda: ("127.0.0.1", 1)),))
            row = await asyncio.wait_for(
                st.assess_daemon(spec, timeout=0.05), timeout=5)
            assert row.transports["tcp"] == "timeout"
            assert row.state is st.Health.ZOMBIE
            assert "UNRESPONSIVE" in row.detail
        finally:
            tc.probe_http = real

    async def test_the_whole_matrix_is_bounded_even_if_a_row_overruns(self):
        async def _slow(*_a, **_k):
            await asyncio.sleep(30)

        import backend.core.ouroboros.cli.thin_client as tc
        real = tc.probe_http
        tc.probe_http = _slow
        try:
            spec = st.DaemonSpec(
                name="slow", title="slow", pid_source=lambda: None,
                transports=(st.Transport("tcp", "tcp",
                                         lambda: ("127.0.0.1", 1)),))
            fleet = await asyncio.wait_for(
                st.assess_fleet((spec,), timeout=10, deadline=0.3), timeout=5)
            assert fleet.daemons[0].state is st.Health.UNKNOWN
            assert "deadline" in fleet.daemons[0].detail
        finally:
            tc.probe_http = real

    async def test_an_exploding_resolver_never_escapes(self):
        def _boom():
            raise RuntimeError("config exploded")

        spec = st.DaemonSpec(name="x", title="x", pid_source=_boom,
                             transports=(st.Transport("tcp", "tcp", _boom),))
        row = await st.assess_daemon(spec, timeout=0.05)
        assert row.transports["tcp"] == "error"


class TestAnOptionalTransportIsReportedNotFatal:
    """The cockpit socket is attach-on-demand — `ov daemon` runs headless and
    nobody is attached most of the time. Degrading a healthy engine for it is
    the same error as judging the supervisor by the cockpit's socket."""

    async def test_a_stale_cockpit_does_not_degrade_a_serving_engine(self):
        spec = st.DaemonSpec(
            name="ov", title="O+V", pid_source=lambda: 4242,
            transports=(
                st.Transport("uds", "cockpit", lambda: None, optional=True),
                st.Transport("tcp", "event-bus",
                             lambda: ("127.0.0.1", 1)),
            ))

        async def _live(*_a, **_k):
            return "live"

        import backend.core.ouroboros.cli.thin_client as tc
        real_http, real_sock = tc.probe_http, tc.probe_socket

        async def _stale(*_a, **_k):
            return "stale"

        tc.probe_http, tc.probe_socket = _live, _stale
        try:
            spec = st.DaemonSpec(
                name="ov", title="O+V", pid_source=lambda: 4242,
                transports=(
                    st.Transport("uds", "cockpit", lambda: Path("/tmp/x.sock"),
                                 optional=True),
                    st.Transport("tcp", "event-bus",
                                 lambda: ("127.0.0.1", 1)),
                ))
            row = await st.assess_daemon(spec, timeout=0.2)
            assert row.state is st.Health.HEALTHY, row.detail
            assert "cockpit=stale" in row.detail, "still REPORTED"
        finally:
            tc.probe_http, tc.probe_socket = real_http, real_sock

    async def test_a_required_transport_still_degrades(self):
        import backend.core.ouroboros.cli.thin_client as tc
        real = tc.probe_http

        async def _refused(*_a, **_k):
            return "refused"

        tc.probe_http = _refused
        try:
            spec = st.DaemonSpec(
                name="ov", title="O+V", pid_source=lambda: 4242,
                transports=(st.Transport("tcp", "event-bus",
                                         lambda: ("127.0.0.1", 1)),))
            row = await st.assess_daemon(spec, timeout=0.2)
            assert row.state is st.Health.ZOMBIE
        finally:
            tc.probe_http = real

    def test_each_daemon_points_at_its_own_log(self):
        sup, ov = st.default_fleet()
        assert "supervisor.err.log" in sup.log_hint()
        hint = ov.log_hint()
        assert hint == "" or "sessions" in hint, hint


class TestATrippedBreakerIsVisibleToTheOperator:
    """A breaker that only exists in the daemon's memory is invisible to the
    status CLI, which runs in its own process. A revoked Screen-Recording
    permission would present as "everything fine, the screenshots merely
    stopped" — the exact silent failure the breaker was built to end."""

    async def test_an_open_breaker_degrades_a_daemon_that_answers_everything(
            self):
        """THE point: every transport live, pid live — and still not
        HEALTHY, because its screen capture has been refused for minutes."""
        import backend.core.ouroboros.cli.thin_client as tc
        real = tc.probe_http

        async def _live(*_a, **_k):
            return "live"

        tc.probe_http = _live
        try:
            spec = st.DaemonSpec(
                name="sup", title="supervisor", pid_source=lambda: 1,
                transports=(st.Transport("tcp", "tcp",
                                         lambda: ("127.0.0.1", 1)),),
                conditions=lambda: ("TCC_BLOCKED:screencapture",))
            row = await st.assess_daemon(spec, timeout=0.2)
            assert row.state is st.Health.DEGRADED, row.detail
            assert "TCC_BLOCKED" in row.detail
            assert row.transports["tcp"] == "live", "the socket is fine"
            assert row.conditions
        finally:
            tc.probe_http = real

    async def test_a_healthy_daemon_with_no_conditions_stays_healthy(self):
        import backend.core.ouroboros.cli.thin_client as tc
        real = tc.probe_http

        async def _live(*_a, **_k):
            return "live"

        tc.probe_http = _live
        try:
            spec = st.DaemonSpec(
                name="sup", title="supervisor", pid_source=lambda: 1,
                transports=(st.Transport("tcp", "tcp",
                                         lambda: ("127.0.0.1", 1)),))
            row = await st.assess_daemon(spec, timeout=0.2)
            assert row.state is st.Health.HEALTHY
        finally:
            tc.probe_http = real

    async def test_a_down_daemon_is_not_relabelled_by_a_stale_breaker(self):
        """A tripped breaker inside a process that is not running is not the
        operator's problem; DOWN must survive."""
        spec = st.DaemonSpec(
            name="sup", title="supervisor", pid_source=lambda: None,
            transports=(), conditions=lambda: ("CIRCUIT_OPEN:yabai",))
        row = await st.assess_daemon(spec, timeout=0.05)
        assert row.state is st.Health.DOWN

    def test_the_condition_is_read_from_the_DURABLE_ledger(
            self, tmp_path, monkeypatch):
        """Cross-process by construction: the CLI reads what the daemon
        wrote, because its own memory is always empty here."""
        import json
        import time as _t
        led = tmp_path / "breakers.json"
        led.write_text(json.dumps({
            "schema_version": "bounded_subprocess.1",
            "open": {"screencapture": {"consecutive_failures": 3}},
            "updated_at": _t.time(), "cooldown_s": 300, "pid": 999,
        }))
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_LEDGER", str(led))
        conds = st.subprocess_breaker_conditions()
        assert any("TCC_BLOCKED" in c and "screencapture" in c for c in conds)
        assert any("Screen Recording" in c for c in conds), "name the FIX"

    def test_a_non_tcc_binary_reports_circuit_open(self, tmp_path, monkeypatch):
        import json
        import time as _t
        led = tmp_path / "b.json"
        led.write_text(json.dumps({
            "open": {"yabai": {}}, "updated_at": _t.time(), "cooldown_s": 300}))
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_LEDGER", str(led))
        assert st.subprocess_breaker_conditions() == ("CIRCUIT_OPEN:yabai",)

    def test_a_stale_ledger_is_ignored(self, tmp_path, monkeypatch):
        """The breaker would have re-probed by now; reporting a stale trip is
        its own kind of lie."""
        import json
        led = tmp_path / "b.json"
        led.write_text(json.dumps({
            "open": {"screencapture": {}}, "updated_at": 0, "cooldown_s": 1}))
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_LEDGER", str(led))
        assert st.subprocess_breaker_conditions() == ()

    def test_no_ledger_means_no_conditions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_SUBPROC_BREAKER_LEDGER",
                           str(tmp_path / "absent.json"))
        assert st.subprocess_breaker_conditions() == ()

    def test_the_supervisor_row_actually_carries_this_condition_source(self):
        """Wiring pin: the fleet's supervisor row must consult it, or the
        whole chain is a module nobody calls."""
        sup, _ = st.default_fleet()
        assert sup.conditions is st.subprocess_breaker_conditions
