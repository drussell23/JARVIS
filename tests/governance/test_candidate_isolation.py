"""Candidate code cannot see, or signal, the daemon that runs it.

bt-2026-09-22-201845 was SIGTERMed by its own candidate: a generated test for
``backend/apply_performance_fixes.py`` ran ``pkill -f jarvis``, which matched
the daemon's own command line. Every seam that executes candidate code now
starts it in a private PID namespace (``process_session.contain_argv``).
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import process_session as PS
from backend.core.ouroboros.governance.test_subprocess_helper import (
    run_pytest_subprocess,
    run_pytest_subprocess_sync,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="PID namespaces are Linux")

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for var in ("JARVIS_CANDIDATE_ISOLATION_ENABLED", "JARVIS_CANDIDATE_ISOLATION_REQUIRED"):
        monkeypatch.delenv(var, raising=False)
    PS.reset_isolation_for_tests()
    yield
    PS.reset_isolation_for_tests()


@pytest.fixture
def contained():
    if not PS.isolation_status().available:
        pytest.skip(f"host cannot contain: {PS.isolation_status().reason}")


def _victim() -> tuple:
    """A bystander whose command line carries a unique word -- the daemon's
    stand-in."""
    word = f"ovvictim{uuid.uuid4().hex[:10]}"
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", word],
        start_new_session=True,
    )
    return proc, word


def _pkill_test(tmp_path: Path, word: str) -> Path:
    t = tmp_path / "test_candidate.py"
    t.write_text(
        "import subprocess, time\n"
        "def test_it():\n"
        f"    subprocess.run(['pkill', '-f', '{word}'])\n"
        "    time.sleep(0.5)\n"
    )
    return t


def _pytest_argv(test: Path) -> list:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test)]


# ---------------------------------------------------------------------------
# The soak, replayed through the real seam
# ---------------------------------------------------------------------------

async def test_a_candidates_pattern_kill_cannot_reach_the_daemon(tmp_path, contained):
    victim, word = _victim()
    try:
        got = await run_pytest_subprocess(
            _pytest_argv(_pkill_test(tmp_path, word)), cwd=str(tmp_path),
            timeout_s=60, caller="test",
        )
        assert got.returncode == 0, got.stdout[-2000:]
        assert victim.poll() is None, "the candidate's pkill killed a process outside its run"
    finally:
        victim.kill()
        victim.wait()


async def test_uncontained_the_same_candidate_does_kill_it(tmp_path, monkeypatch):
    """The replay discriminates: without the namespace the bystander dies."""
    monkeypatch.setenv("JARVIS_CANDIDATE_ISOLATION_ENABLED", "false")
    victim, word = _victim()
    try:
        await run_pytest_subprocess(
            _pytest_argv(_pkill_test(tmp_path, word)), cwd=str(tmp_path),
            timeout_s=60, caller="test",
        )
        assert victim.wait(timeout=5) is not None
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait()


def test_the_sync_helper_is_contained_too(tmp_path, contained):
    victim, word = _victim()
    try:
        got = run_pytest_subprocess_sync(
            _pytest_argv(_pkill_test(tmp_path, word)), cwd=str(tmp_path),
            timeout_s=60, caller="test",
        )
        assert got.returncode == 0, got.stdout[-2000:]
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


# ---------------------------------------------------------------------------
# Semantics a caller relies on
# ---------------------------------------------------------------------------

def _run(code: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(PS.contain_argv([sys.executable, "-c", code]), **kw)


@pytest.mark.parametrize("code,rc", [
    ("raise SystemExit(0)", 0),
    ("raise SystemExit(3)", 3),
    ("import os; os._exit(1)", 1),
    # The payload is not init: a signal it sends itself is delivered.
    ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", 128 + signal.SIGKILL),
])
def test_exit_status_is_the_payloads(code, rc, contained):
    assert _run(code, timeout=30).returncode == rc


def test_the_namespace_hides_the_host(contained):
    out = _run("import os; print(sum(p.isdigit() for p in os.listdir('/proc')))",
               capture_output=True, text=True, timeout=30).stdout
    assert int(out) <= 3, f"{out} processes visible inside the namespace"


def test_stdin_env_and_cwd_pass_through(tmp_path, contained):
    got = subprocess.run(
        PS.contain_argv([sys.executable, "-c",
                         "import os,sys; print(os.getcwd(), os.environ['OVX'], sys.stdin.read())"]),
        input="piped", capture_output=True, text=True, cwd=tmp_path,
        env={**os.environ, "OVX": "yes"}, timeout=30,
    ).stdout.split()
    assert got == [str(tmp_path), "yes", "piped"]


async def test_a_group_sigterm_ends_the_run_promptly(contained):
    proc = await asyncio.create_subprocess_exec(
        *PS.contain_argv([sys.executable, "-c", "import time; time.sleep(60)"]),
        start_new_session=True,
    )
    await asyncio.sleep(1.0)
    os.killpg(proc.pid, signal.SIGTERM)
    rc = await asyncio.wait_for(proc.wait(), 10)
    assert rc != 0


async def test_a_setsid_escapee_dies_with_the_run(contained):
    word = f"ovescapee{uuid.uuid4().hex[:10]}"
    proc = await asyncio.create_subprocess_exec(
        *PS.contain_argv([sys.executable, "-c",
                          "import subprocess, sys, time; "
                          f"subprocess.Popen(['setsid', sys.executable, '-c', 'import time; time.sleep(120)', '{word}']); "
                          "time.sleep(0.5)"]),
        start_new_session=True,
    )
    await proc.wait()
    PS.reap_session(proc.pid, owner="test")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        left = subprocess.run(["pgrep", "-f", word], capture_output=True, text=True).stdout.split()
        if not left:
            break
        await asyncio.sleep(0.1)
    assert not left, "a setsid'd descendant outlived its contained run"


async def test_dump_stacks_reaches_the_payload_not_the_wrapper(contained):
    proc = await asyncio.create_subprocess_exec(
        *PS.contain_argv([sys.executable, "-c",
                          "import faulthandler, time; faulthandler.enable(); "
                          "print('up', flush=True); time.sleep(60)"]),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    await proc.stdout.readline()
    assert PS._payload_pid(proc.pid) != proc.pid
    assert PS.dump_stacks(proc.pid)
    out = await asyncio.wait_for(proc.stdout.read(), 10)
    PS.reap_session(proc.pid, owner="test")
    await proc.wait()
    assert b"time.sleep" in out or b'File "<string>"' in out, out[-1500:]


# ---------------------------------------------------------------------------
# A host that cannot contain
# ---------------------------------------------------------------------------

def _no_unshare(monkeypatch):
    monkeypatch.setattr(PS, "_wrapper_prefix", lambda: None)


def test_uncontained_degrades_loudly_once(monkeypatch, caplog):
    _no_unshare(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=PS.logger.name):
        assert PS.contain_argv(["true"]) == ["true"]
        assert PS.contain_argv(["false"]) == ["false"]
    loud = [r for r in caplog.records if "UNCONTAINED" in r.getMessage()]
    assert len(loud) == 1


def test_required_refuses_as_a_spawn_failure(monkeypatch):
    _no_unshare(monkeypatch)
    monkeypatch.setenv("JARVIS_CANDIDATE_ISOLATION_REQUIRED", "true")
    with pytest.raises(PermissionError):
        PS.contain_argv(["true"])


async def test_a_required_refusal_reaches_the_caller_as_spawn_error(monkeypatch, tmp_path):
    _no_unshare(monkeypatch)
    monkeypatch.setenv("JARVIS_CANDIDATE_ISOLATION_REQUIRED", "true")
    got = await run_pytest_subprocess(["true"], timeout_s=5, caller="test")
    assert got.returncode == -1 and "spawn" in str(got.kill_reason).lower()


def test_switched_off_never_probes(monkeypatch):
    monkeypatch.setenv("JARVIS_CANDIDATE_ISOLATION_ENABLED", "false")
    assert PS.contain_argv(["true"]) == ["true"]
    assert PS.probed_isolation() is None


def test_the_summary_says_how_candidates_ran(tmp_path, monkeypatch):
    from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder

    def _summary():
        return json.loads(SessionRecorder(session_id="bt-iso").save_summary(
            output_dir=tmp_path, stop_reason="wall_clock_cap", duration_s=1.0,
            cost_total=0.0, cost_breakdown={}, branch_stats={},
            convergence_state="INSUFFICIENT_DATA", convergence_slope=0.0,
            convergence_r2=0.0,
        ).read_text())

    assert "candidate_isolation" not in _summary(), "a reader must not probe"
    _no_unshare(monkeypatch)
    PS.contain_argv(["true"])
    assert _summary()["candidate_isolation"]["contained"] is False


# ---------------------------------------------------------------------------
# Wiring: every candidate-executing spawn goes through the one seam
# ---------------------------------------------------------------------------

_SEAMS = [
    ("backend/core/ouroboros/governance/test_runner.py", "_exec_with_timeout"),
    ("backend/core/ouroboros/governance/test_runner.py", "_exec_with_streaming"),
    ("backend/core/ouroboros/governance/repair_sandbox.py", "run_tests"),
    ("backend/core/ouroboros/governance/test_subprocess_helper.py", "run_pytest_subprocess"),
    ("backend/core/ouroboros/governance/test_subprocess_helper.py", "run_pytest_subprocess_sync"),
    ("backend/core/ouroboros/governance/interactive_repair.py", "_run_and_capture"),
]


@pytest.mark.parametrize("path,func", _SEAMS)
def test_every_candidate_spawn_is_contained(path, func):
    tree = ast.parse((_REPO / path).read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func
    )
    names = {
        (c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", ""))
        for c in ast.walk(fn) if isinstance(c, ast.Call)
    }
    assert names & {"contain_argv", "contain_argv_async"}, f"{path}::{func} spawns uncontained"
