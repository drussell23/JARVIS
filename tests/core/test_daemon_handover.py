"""A launch replaces a daemon running older code instead of attaching to it."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from backend.core import daemon_handover as dh
from backend.core import supervisor_ipc

REPO = Path(__file__).resolve().parents[2]


def test_the_daemon_names_the_sources_it_loaded():
    ident = dh.code_identity()
    assert ident["pid"] == os.getpid() and ident["started_at"]
    assert "backend/core/daemon_handover.py" in ident["sources"]


def _checkout(tmp_path, *names):
    for n in names:
        (tmp_path / n).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / n).write_text("x = 1\n")
    return tmp_path


def _daemon(monkeypatch, started_at, sources, root):
    monkeypatch.setattr(dh, "daemon_status", lambda _s: {
        "type": "status_response", "pid": 4242,
        "code": {"root": str(root), "started_at": started_at, "sources": sources}})


def test_a_daemon_running_this_code_is_attached_to(tmp_path, monkeypatch):
    root = _checkout(tmp_path, "a.py", "pkg/b.py")
    _daemon(monkeypatch, time.time() + 60, ["a.py", "pkg/b.py"], root)
    assert dh.running_daemon_staleness(Path("unused"), root) is None


def test_a_source_changed_after_it_started_makes_it_stale(tmp_path, monkeypatch):
    root = _checkout(tmp_path, "a.py", "pkg/b.py")
    started = time.time() - 60
    os.utime(root / "a.py", (started - 10, started - 10))
    _daemon(monkeypatch, started, ["a.py", "pkg/b.py", "gone.py"], root)
    stale = dh.running_daemon_staleness(Path("unused"), root)
    assert stale.pid == 4242 and set(stale.changed) == {"pkg/b.py", "gone.py"}
    assert "2 changed source file(s)" in stale.describe()


def test_a_daemon_from_another_checkout_is_stale(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "here", "a.py")
    _daemon(monkeypatch, time.time() + 60, ["a.py"], tmp_path / "elsewhere")
    assert dh.running_daemon_staleness(Path("unused"), root).other_root


def test_a_daemon_that_cannot_say_who_it_is_is_attached_to(monkeypatch):
    monkeypatch.setattr(dh, "daemon_status", lambda _s: None)
    assert dh.running_daemon_staleness(Path("unused")) is None


def test_an_old_daemon_is_judged_by_the_tracked_sources(tmp_path, monkeypatch):
    root = _checkout(tmp_path, "a.py", "tests/test_a.py", "conftest.py")
    monkeypatch.setattr(dh, "daemon_status", lambda _s: {"type": "status_response", "pid": os.getpid()})
    monkeypatch.setattr(dh, "_process_started_at", lambda _pid: time.time() - 60)
    monkeypatch.setattr(dh, "_tracked_sources",
                        lambda r: [n for n in ("a.py", "tests/test_a.py", "conftest.py")])
    assert dh.running_daemon_staleness(Path("unused"), root).changed  # a.py is newer


@pytest.mark.parametrize("git_on_path", [True, False])
def test_tracked_sources_leave_out_the_test_suite(monkeypatch, git_on_path):
    # A plain Windows PowerShell has no git on PATH: the 07:51 launch then saw
    # no sources at all and attached to the 04:52 kernel.
    if not git_on_path:
        monkeypatch.setattr(dh.shutil, "which", lambda _name: None)
    names = dh._tracked_sources(REPO)
    assert "unified_supervisor.py" in names and "backend/core/daemon_handover.py" in names
    assert not any(Path(n).name.startswith("test_") or Path(n).name == "conftest.py" for n in names)
    assert not any(part.startswith(".") or part in ("node_modules", "__pycache__")
                   for n in names for part in Path(n).parts[:-1])


# -- the handover, against real daemons in child processes ---------------------
_FAKE_DAEMON = textwrap.dedent("""
    import asyncio, json, os, sys
    sys.path.insert(0, {repo!r})
    from pathlib import Path
    from backend.core import supervisor_ipc
    supervisor_ipc.SOCKET_PATH = Path({stream!r})
    obeys = {obeys!r}

    async def main():
        ipc = supervisor_ipc.SupervisorIPC()
        await ipc.start_server()
        done = asyncio.Event()

        async def kernel(reader, writer):
            req = json.loads(await reader.readline())
            writer.write(json.dumps({{"success": True, "result": {{}}}}).encode() + b"\\n")
            await writer.drain(); writer.close()
            if req.get("command") == "shutdown" and obeys:
                for i in range(3):
                    await ipc.broadcast({{"type": "log", "ts": 0, "level": "INFO",
                                         "logger": "fake", "msg": f"stopping {{i}}"}})
                    await asyncio.sleep(0.1)
                await ipc.broadcast_shutdown("handover")
                done.set()

        server = await asyncio.start_unix_server(kernel, path={kernel!r})
        print("ready", flush=True)
        if obeys:
            await done.wait()
            await ipc.close()
        else:
            await asyncio.sleep(3600)

    asyncio.run(main())
""")


def _spawn(tmp_path, obeys):
    stream, kernel = tmp_path / "s.sock", tmp_path / "k.sock"
    src = _FAKE_DAEMON.format(repo=str(REPO), stream=str(stream), kernel=str(kernel), obeys=obeys)
    proc = subprocess.Popen([sys.executable, "-c", src], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    return proc, stream, kernel


@pytest.mark.skipif(not hasattr(asyncio, "start_unix_server"), reason="POSIX sockets")
def test_a_daemon_that_obeys_is_streamed_until_it_exits(tmp_path, monkeypatch, capsys):
    proc, stream, kernel = _spawn(tmp_path, obeys=True)
    monkeypatch.setattr(supervisor_ipc, "SOCKET_PATH", stream)
    try:
        said = []
        assert asyncio.run(dh.hand_over(proc.pid, kernel, stream, say=said.append)) is True
        assert proc.wait(timeout=10) == 0          # it exited on its own
        assert said == []                          # acknowledged, never escalated
    finally:
        proc.kill()


@pytest.mark.skipif(not hasattr(asyncio, "start_unix_server"), reason="POSIX sockets")
def test_a_daemon_that_goes_silent_is_terminated(tmp_path, monkeypatch):
    proc, stream, kernel = _spawn(tmp_path, obeys=False)
    monkeypatch.setattr(supervisor_ipc, "SOCKET_PATH", stream)
    monkeypatch.setenv("JARVIS_DAEMON_HANDOVER_SILENCE_S", "1")
    said = []
    try:
        started = time.monotonic()
        assert asyncio.run(dh.hand_over(proc.pid, kernel, stream, say=said.append)) is True
        assert time.monotonic() - started < 15
        assert any("went silent" in s for s in said)
        proc.wait(timeout=5)                       # gone, not left running
    finally:
        proc.kill()


@pytest.mark.skipif(not hasattr(asyncio, "start_unix_server"), reason="POSIX sockets")
def test_relay_ignores_keepalives_when_judging_silence(tmp_path, monkeypatch):
    stream = tmp_path / "s.sock"
    monkeypatch.setattr(supervisor_ipc, "SOCKET_PATH", stream)

    async def scenario():
        ipc = supervisor_ipc.SupervisorIPC()
        await ipc.start_server()

        async def pings():
            while True:
                await ipc.broadcast({"type": "ping", "ts": 0})
                await asyncio.sleep(0.1)
        task = asyncio.ensure_future(pings())
        try:
            return await supervisor_ipc.SupervisorIPC().relay(silence_s=0.6)
        finally:
            task.cancel()
            await ipc.close()

    assert asyncio.run(scenario()) == "silent"
