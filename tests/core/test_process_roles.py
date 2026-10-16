"""A kernel's cleanup never takes a client of it for an orphan (2026-10-16 00:47).

The viewer following a restart was terminated by the booting kernel's lock
takeover: its command line matched "unified_supervisor" and it was neither the
lock holder nor the kernel's child. These tests drive the kill guard's rule with
real processes and role records."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

psutil = pytest.importorskip("psutil")


@pytest.fixture
def roles(tmp_path, monkeypatch):
    from backend.core import process_roles as pr

    monkeypatch.setattr(pr, "roles_dir", lambda: tmp_path)
    return tmp_path


def _record(directory: Path, pid: int, role: str) -> None:
    from backend.core.process_identity import process_started_at
    (directory / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "started_at": process_started_at(pid), "role": role}), encoding="utf-8")


@pytest.fixture
def chain():
    """A launcher process and the client it started (as the venv launcher
    starts the real interpreter)."""
    code = ("import subprocess, sys, time;"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            "print(p.pid, flush=True); time.sleep(60)")
    launcher = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    client = int(launcher.stdout.readline())
    yield launcher.pid, client
    # The guard (installed by any earlier test that imported the kernel)
    # protects these processes while their record stands: the fixture ends
    # them with the unguarded call, as only a test may.
    kill = getattr(psutil.Process.kill, "__wrapped__", psutil.Process.kill)
    for pid in (client, launcher.pid):
        try:
            kill(psutil.Process(pid))
        except psutil.Error:
            pass
    launcher.wait(timeout=10)


def test_a_declared_role_is_live_and_withdrawn(roles):
    from backend.core import process_roles as pr
    import os

    assert pr.declare_role(pr.VIEWER)
    assert pr.live_roles()[os.getpid()]["role"] == pr.VIEWER
    pr._withdraw(roles / f"{os.getpid()}.json")
    assert os.getpid() not in pr.live_roles()


def test_a_dead_declarer_s_record_is_pruned(roles):
    from backend.core import process_roles as pr

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)
    (roles / f"{dead.pid}.json").write_text(json.dumps(
        {"pid": dead.pid, "started_at": time.time() - 5, "role": "viewer"}), encoding="utf-8")
    assert dead.pid not in pr.live_roles()
    assert not (roles / f"{dead.pid}.json").exists()


def test_the_guard_never_signals_a_client_or_its_launcher(roles, chain):
    from backend.core import process_identity as pi

    launcher, client = chain
    _record(roles, client, "viewer")
    assert pi._refusal(client) == "a live JARVIS viewer"
    assert f"launcher of a live JARVIS viewer (PID {client})" in pi._refusal(launcher)
    assert not pi.may_signal(client) or not pi.kill_guard_enabled()


def test_a_kernel_s_record_protects_nothing(roles, chain):
    from backend.core import process_identity as pi

    launcher, client = chain
    _record(roles, client, "kernel")
    assert pi._refusal(client) is None      # this test's own descendant: ownable
    assert pi._refusal(launcher) is None


def test_every_launch_declares_what_it_is():
    src = Path("unified_supervisor.py").read_text(encoding="utf-8")
    assert "_process_roles.declare_role(" in src
    assert "declare_role, _KERNEL_ROLE" in src or "await asyncio.to_thread(declare_role, _KERNEL_ROLE)" in src
    assert "orphans = [o for o in orphans if may_signal(o.pid)]" in src
    assert "declare_role(VIEWER)" in Path("backend/core/supervisor_ipc.py").read_text(encoding="utf-8")


def test_the_restart_runner_can_import_the_checkout(tmp_path):
    """It runs from the temp directory; its settle step is an import away."""
    import os
    import re
    src = Path("unified_supervisor.py").read_text(encoding="utf-8")
    template = re.search(r"_script_content = (f'''.*?''')\n", src, re.S).group(1)
    fake_sys = type("S", (), {"executable": sys.executable, "argv": ["unified_supervisor.py"]})
    script = eval(template, {"_reexec_marker": "X", "_early_sys": fake_sys, "_early_os": os,
                             "__file__": str(Path("unified_supervisor.py").resolve()),
                             "_result_path": "r", "_session_log": None})
    head = script.split("# Full signal immunity")[0]
    probe = tmp_path / "probe.py"
    probe.write_text(head + "\nimport backend.core.daemon_handover\nprint('importable')\n",
                     encoding="utf-8")
    done = subprocess.run([sys.executable, "-I", str(probe)], capture_output=True, text=True,
                          cwd=str(tmp_path), timeout=120)
    assert "importable" in done.stdout, done.stderr
