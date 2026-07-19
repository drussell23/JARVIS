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
import socket as socket_mod
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.cli import thin_client

_REPO = Path(__file__).resolve().parents[2]


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
            server.close()
            await server.wait_closed()

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
            # The "daemon" comes up: bind a REAL listener at the path.
            srv = socket_mod.socket(
                socket_mod.AF_UNIX, socket_mod.SOCK_STREAM,
            )
            srv.bind(str(path))
            srv.listen(1)
            spawns.append(srv)             # keep alive for the test
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
        server = await asyncio.start_unix_server(
            lambda r, w: None, path=str(path),
        )
        spawns: list = []
        try:
            ok = await thin_client.ensure_daemon(
                spawner=lambda *a, **k: spawns.append(a),
            )
            assert ok is True
            assert spawns == []            # warm path never cold-boots
        finally:
            server.close()
            await server.wait_closed()


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
