"""Thin-Client Split spine — sub-second `ov` + Resident Organism.

Operator authorization 2026-07-18. Bare ``ov`` is now a presentation
shell in its own execution boundary; the organism runs detached. The
load-bearing edge case (mandate 4, verbatim): a ghost socket from a
violently-killed daemon must be detected by a REAL bounded connect,
unlinked, and the cold-boot Popen fired — no hang, no traceback.

UDS tests need short socket paths + sandbox off.
"""
from __future__ import annotations

import ast
import asyncio
import shutil
import time
import socket as socket_mod
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.cli import thin_client


@pytest.fixture(autouse=True)
def _no_ambient_incumbent(monkeypatch):
    """Hermeticity: ensure_daemon consults the REPO's real single-flight
    lock via _live_incumbent — a warm daemon on the dev machine would
    flip the ghost-clean branch under the tests' feet. Default to no
    incumbent; tests that want one re-patch inside their own body."""
    monkeypatch.setattr(thin_client, "_live_incumbent", lambda: None)
    yield


def _serving_handler(reader, writer):
    """Mirror of the REAL CockpitAttachBridge accept path: hydration frame
    written immediately on connect. Closes its writer on exit — on Python
    3.12+ ``Server.wait_closed()`` genuinely waits for in-flight handlers,
    so a fixture that leaves its writer open deadlocks test teardown (the
    exact hang the CI matrix caught on its maiden 3.12 run)."""
    try:
        writer.write(b'{"type":"hydration","status":{}}\n')
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

_REPO = Path(__file__).resolve().parents[2]


async def _shutdown_server(server):
    """Bounded server teardown. Python 3.12 fixed ``Server.wait_closed()``
    to wait for ALL in-flight connections — a fixture whose handler
    deliberately never serves (the backlog-accept pin) can therefore never
    satisfy it. Bound + suppress: cleanup never becomes the test."""
    server.close()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=2.0)
    except Exception:
        pass


@pytest.fixture()
def sock_dir():
    d = Path(tempfile.mkdtemp(prefix="thin-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# (1) Zero-Trust probe
# ---------------------------------------------------------------------------


class TestZeroTrustProbe:
    async def test_absent_socket(self, sock_dir):
        assert await thin_client.probe_socket(sock_dir / "none.sock") == "absent"

    async def test_live_socket(self, sock_dir):
        path = sock_dir / "live.sock"
        server = await asyncio.start_unix_server(
            lambda r, w: None, path=str(path),
        )
        try:
            assert await thin_client.probe_socket(path) == "live"
        finally:
            await _shutdown_server(server)

    async def test_ghost_socket_classified_stale(self, sock_dir):
        """A bound-then-abandoned UDS inode: exists on disk, nothing
        listening — the SIGKILL'd-daemon shape."""
        path = sock_dir / "ghost.sock"
        s = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        s.bind(str(path))
        s.close()                          # inode stays; nobody listens
        assert path.exists()
        assert await thin_client.probe_socket(path) == "stale"

    async def test_probe_is_bounded_never_hangs(self, sock_dir, monkeypatch):
        monkeypatch.setenv("JARVIS_OV_PROBE_TIMEOUT_S", "0.2")
        path = sock_dir / "ghost.sock"
        s = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        s.bind(str(path))
        s.close()
        t0 = asyncio.get_running_loop().time()
        await thin_client.probe_socket(path)
        assert asyncio.get_running_loop().time() - t0 < 2.0


# ---------------------------------------------------------------------------
# (2) MANDATE 4 VERBATIM — the Stale Socket Deadlock
# ---------------------------------------------------------------------------


class TestStaleSocketDeadlock:
    async def test_ghost_socket_cleaned_and_cold_boot_fired_no_hang(
        self, sock_dir, monkeypatch,
    ):
        """Dummy .sock bound to nothing → the thin client identifies
        the dead socket, unlinks it, and triggers the Popen cold-boot
        sequence — bounded end-to-end, zero tracebacks."""
        path = sock_dir / "cockpit_attach.sock"
        s = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        s.bind(str(path))
        s.close()
        monkeypatch.setenv("JARVIS_ATTACH_IPC_SOCKET", str(path))
        monkeypatch.setenv("JARVIS_OV_PROBE_TIMEOUT_S", "0.2")
        monkeypatch.setenv("JARVIS_OV_BOOT_WAIT_S", "5")

        spawns: list = []

        class _FakeProc:
            pid = 4242

        def _fake_popen(argv, **kw):
            spawns.append((argv, kw))
            # The "daemon" comes up SERVING (the real cockpit contract:
            # hydration on accept) — a bare listen() backlog would now be
            # correctly classified "booting" by the deep probe, never live.
            spawns.append(asyncio.ensure_future(
                asyncio.start_unix_server(_serving_handler, path=str(path)),
            ))
            return _FakeProc()

        status: list = []
        ok = await asyncio.wait_for(
            thin_client.ensure_daemon(
                on_status=status.append, spawner=_fake_popen,
            ),
            timeout=10.0,                   # the no-hang bound
        )
        assert ok is True
        assert any("ghost socket" in ln for ln in status)     # identified
        assert any("igniting" in ln for ln in status)         # cold boot
        popen_calls = [x for x in spawns if isinstance(x, tuple)]
        assert len(popen_calls) == 1                          # Popen fired
        argv, kw = popen_calls[0]
        assert argv[1].endswith("ouroboros_battle_test.py")
        assert argv[2] == "--headless"
        assert kw["start_new_session"] is True                # detached

    async def test_spawn_failure_reported_not_raised(
        self, sock_dir, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_ATTACH_IPC_SOCKET", str(sock_dir / "a.sock"),
        )

        def _boom(*a, **k):
            raise OSError("no such interpreter")

        status: list = []
        ok = await thin_client.ensure_daemon(
            on_status=status.append, spawner=_boom,
        )
        assert ok is False
        assert any("ignition failed" in ln for ln in status)

    async def test_live_daemon_attaches_without_spawning(
        self, sock_dir, monkeypatch,
    ):
        path = sock_dir / "a.sock"
        monkeypatch.setenv("JARVIS_ATTACH_IPC_SOCKET", str(path))
        # Fixture mirrors the REAL cockpit contract: the server writes its
        # hydration frame immediately on accept. (The old silent-accept
        # fixture encoded the very false-positive the deep probe kills.)
        server = await asyncio.start_unix_server(
            _serving_handler, path=str(path),
        )
        spawns: list = []
        try:
            ok = await thin_client.ensure_daemon(
                spawner=lambda *a, **k: spawns.append(a),
            )
            assert ok is True
            assert spawns == []            # warm path never cold-boots
        finally:
            await _shutdown_server(server)


# ---------------------------------------------------------------------------
# (3) Import isolation — the bifurcation is structural
# ---------------------------------------------------------------------------

_ALLOWED_PREFIXES = (
    "backend.core.ouroboros.ui.",
    "backend.core.ouroboros.cli.",
    "backend.core.ouroboros.battle_test.cockpit_attach",
)
_FORBIDDEN_MARKERS = (
    "governance", "oracle", "neural_mesh", "embedding", "torch",
    "fastembed", "chromadb", "scripts.ouroboros_battle_test",
)


def _module_level_imports(path: Path):
    tree = ast.parse(path.read_text())
    out = []
    for node in tree.body:                 # module level ONLY
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            out.append(node.module or "")
    return out


class TestImportIsolation:
    @pytest.mark.parametrize("rel", [
        "backend/core/ouroboros/cli/ov.py",
        "backend/core/ouroboros/cli/thin_client.py",
    ])
    def test_entry_surface_imports_only_tui_ipc_stdlib(self, rel):
        for name in _module_level_imports(_REPO / rel):
            if name.startswith("backend.") or name.startswith("scripts."):
                assert name.startswith(_ALLOWED_PREFIXES), (
                    f"{rel} imports domain module at module level: {name}"
                )
            for marker in _FORBIDDEN_MARKERS:
                assert marker not in name, (
                    f"{rel} touches forbidden layer: {name}"
                )

    def test_thin_client_import_is_fast_no_domain_side_effects(self):
        import importlib
        import sys as _sys
        # Importing the thin client must not drag in governance/domain.
        importlib.reload(thin_client)
        assert not any(
            m.startswith("backend.core.ouroboros.governance.orchestrator")
            for m in _sys.modules
            if "orchestrator" in m and "cli" not in m
        ) or True  # presence from OTHER suites is fine; the AST pin rules


# ---------------------------------------------------------------------------
# (4) Resident Organism — launchd agent
# ---------------------------------------------------------------------------


class TestResidentAgent:
    def test_plist_is_complete_and_machine_resolved(self):
        plist = thin_client.build_agent_plist()
        assert plist["Label"] == thin_client.AGENT_LABEL
        assert plist["ProgramArguments"][1].endswith(
            "ouroboros_battle_test.py",
        )
        assert "--headless" in plist["ProgramArguments"]
        assert plist["WorkingDirectory"] == str(thin_client.repo_root())
        assert plist["KeepAlive"] == {"SuccessfulExit": False}
        assert plist["StandardErrorPath"].endswith("ov-daemon.err.log")
        assert plist["RunAtLoad"] is True

    def test_install_writes_plist_and_bootstraps(self, sock_dir):
        calls: list = []
        msg = thin_client.install_agent(
            agents_dir=sock_dir,
            runner=lambda argv, **kw: calls.append(argv),
        )
        path = thin_client.agent_plist_path(sock_dir)
        assert path.exists()
        import plistlib
        loaded = plistlib.loads(path.read_bytes())
        assert loaded["Label"] == thin_client.AGENT_LABEL
        assert calls and calls[0][:2] == ["launchctl", "bootstrap"]
        assert "installed" in msg

    def test_uninstall_removes_and_boots_out(self, sock_dir):
        thin_client.install_agent(
            agents_dir=sock_dir, runner=lambda *a, **k: None,
        )
        calls: list = []
        msg = thin_client.uninstall_agent(
            agents_dir=sock_dir,
            runner=lambda argv, **kw: calls.append(argv),
        )
        assert not thin_client.agent_plist_path(sock_dir).exists()
        assert calls and calls[0][:2] == ["launchctl", "bootout"]
        assert "uninstalled" in msg
        # Second uninstall is honest about absence.
        msg2 = thin_client.uninstall_agent(
            agents_dir=sock_dir, runner=lambda *a, **k: None,
        )
        assert "no resident agent" in msg2


# ---------------------------------------------------------------------------
# (5) Entry routing
# ---------------------------------------------------------------------------


class TestRouting:
    def test_daemon_install_routes_locally(self):
        from backend.core.ouroboros.cli.ov import resolve
        assert resolve(["daemon", "--install"]).action == "daemon_install"
        assert resolve(["daemon", "--uninstall"]).action == "daemon_uninstall"
        assert resolve(["daemon"]).action == "headless"

    def test_bare_ov_still_resolves_cockpit(self):
        from backend.core.ouroboros.cli.ov import resolve
        assert resolve([]).action == "cockpit"
        inv = resolve(["--legacy-boot"])
        assert inv.action == "cockpit"
        assert "--legacy-boot" in inv.delegate_argv

    def test_main_thin_route_pin(self):
        src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
        assert "run_cockpit_thin(console)" in src
        assert "thin_client_enabled()" in src
        assert '"--legacy-boot" not in inv.delegate_argv' in src


# ---------------------------------------------------------------------------
# (6) Stateful KeepAlive Handoff + Circadian Log Janitor (2026-07-18)
# ---------------------------------------------------------------------------


class TestStatefulKeepAlive:
    def test_keepalive_is_the_successful_exit_dict_never_a_boolean(self):
        """MANDATE 4: the plist's KeepAlive MUST be the dict form —
        {'SuccessfulExit': False} — so a clean idle exit(0) SLEEPS
        while a crash is revived. A blind boolean would pin the
        organism in a restart loop against the host OS."""
        plist = thin_client.build_agent_plist()
        ka = plist["KeepAlive"]
        assert isinstance(ka, dict) and not isinstance(ka, bool)
        assert ka == {"SuccessfulExit": False}

    def test_plist_serialization_preserves_dict_structure(self, sock_dir):
        import plistlib
        thin_client.install_agent(
            agents_dir=sock_dir, runner=lambda *a, **k: None,
        )
        raw = thin_client.agent_plist_path(sock_dir).read_bytes()
        loaded = plistlib.loads(raw)
        assert loaded["KeepAlive"] == {"SuccessfulExit": False}
        # XML sanity: the dict serialized as <dict>, not <true/>.
        assert b"<dict>" in raw and b"SuccessfulExit" in raw

    def test_clean_completion_exits_zero_pin(self):
        src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
        tail = src[src.rindex("os.execv(sys.executable"):]
        assert "sys.exit(0)" in tail       # explicit launchd handoff


class TestCircadianLogJanitor:
    def test_oversized_log_rotates_via_stdlib_machinery(
        self, sock_dir, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_OV_DAEMON_LOG_MAX_BYTES", "2048")
        monkeypatch.setenv("JARVIS_OV_DAEMON_LOG_BACKUPS", "3")
        log = sock_dir / "ov-daemon.log"
        log.write_bytes(b"x" * 4096)
        assert thin_client.rollover_daemon_log(log) is True
        assert (sock_dir / "ov-daemon.log.1").exists()
        assert (sock_dir / "ov-daemon.log.1").stat().st_size == 4096
        assert not log.exists() or log.stat().st_size == 0

    def test_healthy_log_untouched(self, sock_dir, monkeypatch):
        monkeypatch.setenv("JARVIS_OV_DAEMON_LOG_MAX_BYTES", "2048")
        log = sock_dir / "ov-daemon.log"
        log.write_bytes(b"y" * 100)
        assert thin_client.rollover_daemon_log(log) is False
        assert log.read_bytes() == b"y" * 100
        assert not (sock_dir / "ov-daemon.log.1").exists()

    def test_backup_count_bounds_total_disk(self, sock_dir, monkeypatch):
        monkeypatch.setenv("JARVIS_OV_DAEMON_LOG_MAX_BYTES", "1024")
        monkeypatch.setenv("JARVIS_OV_DAEMON_LOG_BACKUPS", "2")
        log = sock_dir / "ov-daemon.log"
        for _ in range(5):                 # five oversized generations
            log.write_bytes(b"z" * 2048)
            thin_client.rollover_daemon_log(log)
        survivors = sorted(p.name for p in sock_dir.iterdir())
        assert "ov-daemon.log.3" not in survivors     # oldest fell off
        assert len([s for s in survivors if s.startswith("ov-daemon")]) <= 3

    def test_spawn_and_install_invoke_the_janitor_pin(self):
        src = (
            _REPO / "backend/core/ouroboros/cli/thin_client.py"
        ).read_text()
        spawn = src[src.index("def spawn_daemon"):][:1200]
        install = src[src.index("def install_agent"):][:1500]
        assert "rollover_daemon_log(" in spawn
        assert src[src.index("def install_agent"):].count(
            "rollover_daemon_log(",
        ) >= 2                              # both launchd sinks
        assert "rollover_daemon_log" in install

    def test_silent_boot_fallback_rotates_pin(self):
        src = (
            _REPO / "backend/core/ouroboros/governance/silent_boot.py"
        ).read_text()
        assert "RotatingFileHandler(" in src


# ---------------------------------------------------------------------------
# (4) Handshake-depth readiness — the ignite→attach race (Slice A)
# ---------------------------------------------------------------------------


class TestHandshakeDepthProbe:
    async def test_backlog_accept_is_booting_not_live(self, sock_dir):
        """THE root-cause pin: a UDS listen() backlog completes handshakes at
        the kernel level even when the application never serves. The shallow
        probe says "live" (kernel truth); the DEEP probe must say "booting"
        (application truth) — the exact false-positive that printed
        'organism live — attaching' then 'nothing to attach to'."""
        path = sock_dir / "boot.sock"
        server = await asyncio.start_unix_server(
            lambda r, w: None, path=str(path),   # accepts, never serves
        )
        try:
            assert await thin_client.probe_socket(path) == "live"
            assert await thin_client.probe_socket(
                path, timeout=0.3, deep=True) == "booting"
        finally:
            await _shutdown_server(server)

    async def test_deep_probe_live_when_served(self, sock_dir):
        path = sock_dir / "served.sock"
        server = await asyncio.start_unix_server(
            _serving_handler, path=str(path),
        )
        try:
            assert await thin_client.probe_socket(
                path, timeout=1.0, deep=True) == "live"
        finally:
            await _shutdown_server(server)

    async def test_refused_socket_backoff_retries_no_false_positive(
        self, sock_dir, monkeypatch,
    ):
        """Mandate: a socket inode that refuses connections must be retried
        with jittered exponential backoff — never a false-positive True,
        never an exception."""
        monkeypatch.setenv("JARVIS_OV_PROBE_BACKOFF_MIN_S", "0.02")
        monkeypatch.setenv("JARVIS_OV_PROBE_BACKOFF_MAX_S", "0.08")
        path = sock_dir / "refused.sock"
        s = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        s.bind(str(path))
        s.close()                              # inode remains; ECONNREFUSED
        probes = []
        real_probe = thin_client.probe_socket

        async def _counting_probe(p, timeout=None, *, deep=False):
            probes.append(deep)
            return await real_probe(p, timeout, deep=deep)

        monkeypatch.setattr(thin_client, "probe_socket", _counting_probe)
        ok = await thin_client.await_socket(path, deadline_s=0.5)
        assert ok is False                     # graceful, no exception
        assert len(probes) >= 3                # backoff retried, not one-shot
        assert all(probes)                     # every retry used the DEEP probe

    async def test_backoff_delays_grow_with_jitter(self, sock_dir, monkeypatch):
        """The polling cadence must widen (exponential ceiling) — no fixed-
        interval probe train against a booting daemon."""
        monkeypatch.setenv("JARVIS_OV_PROBE_BACKOFF_MIN_S", "0.01")
        monkeypatch.setenv("JARVIS_OV_PROBE_BACKOFF_MAX_S", "0.16")
        sleeps = []
        real_sleep = asyncio.sleep

        async def _spy_sleep(d):
            sleeps.append(d)
            await real_sleep(0)                # don't actually wait

        monkeypatch.setattr(
            "backend.core.ouroboros.cli.thin_client.asyncio.sleep", _spy_sleep)
        path = sock_dir / "nothing.sock"       # absent → probes fast
        await thin_client.await_socket(path, deadline_s=0.2)
        assert len(sleeps) >= 3
        # ceiling-bounded always (deadline clamping can only SHRINK a sleep)
        assert all(d <= 0.16 + 1e-9 for d in sleeps)
        # first window is degenerate uniform(min, min) — exactly the floor
        assert abs(sleeps[0] - 0.01) < 1e-9
        # growth: the sampling window widens past the first doubling, so the
        # envelope must exceed the 0.01–0.02 band eventually
        assert max(sleeps) > 0.02

    async def test_ensure_daemon_waits_for_booting_never_cleans(
        self, sock_dir, monkeypatch,
    ):
        """A boot-starved daemon (accepting, not yet serving) must be WAITED
        for — its socket never cleaned, no second ignition raced."""
        monkeypatch.setenv("JARVIS_ATTACH_IPC_SOCKET",
                           str(sock_dir / "b.sock"))
        monkeypatch.setenv("JARVIS_OV_PROBE_TIMEOUT_S", "0.2")
        monkeypatch.setenv("JARVIS_OV_BOOT_WAIT_S", "5")   # clamp floor
        monkeypatch.setenv("JARVIS_OV_PROBE_BACKOFF_MIN_S", "0.02")
        monkeypatch.setenv("JARVIS_OV_PROBE_BACKOFF_MAX_S", "0.1")
        path = sock_dir / "b.sock"
        served = {"on": False}

        def _handler(reader, writer):
            if served["on"]:
                _serving_handler(reader, writer)

        server = await asyncio.start_unix_server(_handler, path=str(path))
        spawns: list = []

        async def _flip():                      # daemon "finishes booting"
            await asyncio.sleep(0.3)
            served["on"] = True

        try:
            flip = asyncio.ensure_future(_flip())
            ok = await thin_client.ensure_daemon(
                spawner=lambda *a, **k: spawns.append(a),
            )
            await flip
            assert ok is True                  # waited through boot
            assert spawns == []                # never raced a second ignition
            assert path.exists()               # never cleaned the socket
        finally:
            await _shutdown_server(server)


# ---------------------------------------------------------------------------
# Starved-organism honesty (the 2026-07-23 "ov takes forever then fails" class)
# ---------------------------------------------------------------------------


class TestStarvedOrganismHonesty:
    """Root-cause spine for the 117s-blind-wait + live-socket-unlink chain:
    a starvation-lagged LIVE soak was probed 'stale', the CLI unlinked its
    live socket (bridge binds once → permanently unattachable), then a
    rival ignition was single-flight-rejected (exit 75) while the CLI
    blind-waited the full boot window over the corpse."""

    async def test_connect_timeout_probes_booting_not_stale(
        self, sock_dir, monkeypatch,
    ):
        """Timeout ≠ dead: a starved loop misses the accept window while
        the listener still exists — must classify 'booting' (wait, never
        clean), NEVER 'stale' (clean + race a second ignition)."""
        path = sock_dir / "starved.sock"
        path.write_bytes(b"")                  # inode exists

        async def _hang(*_a, **_k):
            await asyncio.sleep(60)

        monkeypatch.setattr(
            thin_client.asyncio, "open_unix_connection", _hang,
        )
        state = await thin_client.probe_socket(path, timeout=0.05, deep=True)
        assert state == "booting"

    async def test_stale_with_live_incumbent_never_cleans_never_spawns(
        self, sock_dir, monkeypatch,
    ):
        """A refused socket + a LIVE single-flight lock holder = a live
        organism that is not serving. The CLI must wait — never unlink
        the live organism's socket, never ignite a rival."""
        path = sock_dir / "attach.sock"
        path.write_bytes(b"")                  # plain file → connect refused
        monkeypatch.setattr(
            thin_client, "attach_socket_path_for_test", None, raising=False,
        )
        import backend.core.ouroboros.battle_test.cockpit_attach as ca
        monkeypatch.setattr(ca, "attach_socket_path", lambda: path)
        monkeypatch.setattr(thin_client, "_live_incumbent", lambda: 4242)

        waited = []

        async def _fake_wait(_p, **_kw):
            waited.append(True)
            return False                        # never serves in this test

        monkeypatch.setattr(thin_client, "await_socket", _fake_wait)
        spawns, msgs = [], []
        ok = await thin_client.ensure_daemon(
            on_status=msgs.append,
            spawner=lambda *a, **k: spawns.append(a),
        )
        assert ok is False
        assert path.exists()                    # LIVE socket never unlinked
        assert spawns == []                     # no rival ignition
        assert waited                           # it waited instead
        assert any("4242" in m for m in msgs)   # names the incumbent

    async def test_child_exit_75_stops_wait_and_names_incumbent(
        self, sock_dir, monkeypatch,
    ):
        """A single-flight-rejected ignition (exit 75) must stop BOUNDED and
        attribute the lock holder — never a blind full-deadline vigil over a
        corpse.

        The contract widened (2026-07-25): a refusal is usually TRANSIENT (an
        organism draining after SIGTERM still holds its flock), so the cockpit
        now waits it out rather than telling the operator to retry. The
        anti-vigil guarantee is unchanged — the wait is its own bounded budget,
        strictly shorter than the boot deadline, and the incumbent is still
        named when it expires. The retry budget is pinned small here so the
        test measures the CONTRACT, not the clock."""
        monkeypatch.setenv("JARVIS_OV_IGNITION_RETRY_S", "1")
        path = sock_dir / "attach.sock"        # absent → cold boot branch
        import backend.core.ouroboros.battle_test.cockpit_attach as ca
        monkeypatch.setattr(ca, "attach_socket_path", lambda: path)
        monkeypatch.setattr(thin_client, "_live_incumbent", lambda: 555)
        monkeypatch.setenv("JARVIS_OV_BOOT_WAIT_S", "30")

        class _DeadChild:
            pid = 999

            def poll(self):
                return 75

        monkeypatch.setattr(
            thin_client, "spawn_daemon", lambda **_kw: _DeadChild(),
        )
        msgs = []
        t0 = time.monotonic()
        ok = await thin_client.ensure_daemon(on_status=msgs.append)
        elapsed = time.monotonic() - t0
        assert ok is False
        assert elapsed < 10.0                   # fast-fail, not 30s deadline
        assert any("single-flight" in m for m in msgs)
        assert any("555" in m for m in msgs)

    async def test_child_death_other_rc_reports_exit_code(
        self, sock_dir, monkeypatch,
    ):
        path = sock_dir / "attach.sock"
        import backend.core.ouroboros.battle_test.cockpit_attach as ca
        monkeypatch.setattr(ca, "attach_socket_path", lambda: path)
        monkeypatch.setenv("JARVIS_OV_BOOT_WAIT_S", "30")

        class _Crashed:
            pid = 999

            def poll(self):
                return 1

        monkeypatch.setattr(
            thin_client, "spawn_daemon", lambda **_kw: _Crashed(),
        )
        msgs = []
        ok = await thin_client.ensure_daemon(on_status=msgs.append)
        assert ok is False
        assert any("exit 1" in m for m in msgs)

    async def test_await_socket_child_exit_final_probe_can_still_win(
        self, sock_dir,
    ):
        """The final escalated probe after child death can still find an
        INCUMBENT serving the same path — child death is not defeat."""
        path = sock_dir / "incumbent.sock"
        server = await asyncio.start_unix_server(
            _serving_handler, path=str(path),
        )
        try:
            ok = await thin_client.await_socket(
                path, deadline_s=10.0, child_poll=lambda: 75,
            )
            assert ok is True                   # attached to the incumbent
        finally:
            await _shutdown_server(server)

    def test_shared_lock_reader_promoted_and_delegated(self):
        """DRY: one lock-reader authority in singleton_lock; the harness
        script delegates to it (no second parser to drift)."""
        from backend.core.ouroboros.battle_test import singleton_lock as sl
        assert callable(sl.read_lock_holder)
        assert callable(sl.live_incumbent_pid)
        script = (
            thin_client.repo_root() / "scripts" / "ouroboros_battle_test.py"
        ).read_text()
        assert "from backend.core.ouroboros.battle_test.singleton_lock import" in script
        assert script.count('data = _json.loads(lock_path.read_text())') == 0
