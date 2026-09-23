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


@pytest.mark.parametrize("attack", ["pkill", "proc_walk"])
def test_a_new_session_alone_does_not_protect_the_daemon(attack):
    """Why the seam is a PID namespace and not setsid/killpg.

    A session or process group decides who RECEIVES a group signal; it hides
    nothing from a process that picks its targets by name. pkill scans every
    pid in the namespace, and so does a candidate that walks /proc itself.
    """
    victim, word = _victim()
    code = {
        "pkill": f"import subprocess; subprocess.run(['pkill', '-f', '{word}'])",
        "proc_walk": (
            "import os, signal\n"
            "for p in filter(str.isdigit, os.listdir('/proc')):\n"
            "    try:\n"
            f"        if b'{word}' in open(f'/proc/{{p}}/cmdline', 'rb').read() "
            "and int(p) != os.getpid():\n"
            "            os.kill(int(p), signal.SIGKILL)\n"
            "    except OSError:\n"
            "        pass\n"
        ),
    }[attack]
    try:
        subprocess.run([sys.executable, "-c", code], preexec_fn=os.setsid, timeout=30)
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

_G = "backend/core/ouroboros/governance/"
# Every function that spawns candidate code -- model-authored tests, patched
# modules, mutants, a candidate's build, a model's shell command.
_SEAMS = [
    (_G + "test_runner.py", "_exec_with_timeout"),
    (_G + "test_runner.py", "_exec_with_streaming"),
    (_G + "test_runner.py", "_default_cmake_build"),
    (_G + "test_runner.py", "_default_abi_probe"),
    (_G + "test_runner.py", "_default_ctest"),
    (_G + "repair_sandbox.py", "run_tests"),
    (_G + "test_subprocess_helper.py", "run_pytest_subprocess"),
    (_G + "test_subprocess_helper.py", "run_pytest_subprocess_sync"),
    (_G + "interactive_repair.py", "_run_and_capture"),
    (_G + "mutation_tester.py", "_run_pytest"),
    (_G + "hybrid_teammate_executor.py", "run"),
    # accumulation_promotion_gate._check_coverage delegates to
    # run_pytest_subprocess (above); the Slice 9 cage keeps it that way.
    (_G + "saga/cross_repo_verifier.py", "_verify_single_repo"),
    (_G + "saga/cross_repo_verifier.py", "_tier2_cross_repo_contracts"),
    (_G + "saga/cross_repo_verifier.py", "_tier3_integration_tests"),
    (_G + "live_kernel_validator.py", "_default_runner"),
    (_G + "forensic_inoculation.py", "_run_probe"),
    (_G + "tools/bash_tool.py", "execute"),
]

# Tools that read candidate files without executing them.
_STATIC_TOOLS = {"ruff"}
_SPAWNERS = {"create_subprocess_exec", "create_subprocess_shell", "run", "Popen",
             "check_output", "check_call", "call"}


def _callee(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _is_contained(expr: ast.AST) -> bool:
    if isinstance(expr, ast.Starred):
        expr = expr.value
    if isinstance(expr, ast.Call) and _callee(expr) in ("tuple", "list") and expr.args:
        expr = expr.args[0]
    if isinstance(expr, ast.Await):
        expr = expr.value
    return isinstance(expr, ast.Call) and _callee(expr) in {"contain_argv", "contain_argv_async"}


def _is_static(expr: ast.AST) -> bool:
    if isinstance(expr, ast.BinOp):
        expr = expr.left
    return (isinstance(expr, ast.List) and expr.elts and isinstance(expr.elts[0], ast.Constant)
            and expr.elts[0].value in _STATIC_TOOLS)


def _spawn_argvs(fn: ast.AST):
    """The argv expression of every process spawn inside *fn*."""
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        name = _callee(call)
        if name == "BackgroundMonitor":  # spawns its ``cmd=`` on __aenter__
            for kw in call.keywords:
                if kw.arg == "cmd":
                    yield call.lineno, kw.value
            continue
        if not call.args:
            continue
        if name == "to_thread" and len(call.args) >= 2:
            inner = call.args[0]
            if isinstance(inner, ast.Attribute) and inner.attr in _SPAWNERS:
                yield call.lineno, call.args[1]
        elif name in _SPAWNERS and not (
            name == "run" and isinstance(call.func, ast.Attribute)
            and getattr(call.func.value, "id", "") not in ("subprocess", "_subprocess")
        ):
            yield call.lineno, call.args[0]


@pytest.mark.parametrize("path,func", _SEAMS, ids=[f"{p.split('/')[-1]}::{f}" for p, f in _SEAMS])
def test_every_candidate_spawn_is_contained(path, func):
    tree = ast.parse((_REPO / path).read_text())
    fns = [n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func]
    assert fns, f"{path}::{func} no longer exists -- update the seam list"
    spawns = [(ln, a) for fn in fns for ln, a in _spawn_argvs(fn)]
    assert spawns, f"{path}::{func} spawns nothing -- update the seam list"
    loose = [ln for ln, a in spawns if not (_is_contained(a) or _is_static(a))]
    assert not loose, f"{path}::{func} spawns uncontained at line(s) {loose}"


# ---------------------------------------------------------------------------
# The model's shell: the most direct path a candidate has to the host
# ---------------------------------------------------------------------------

async def test_a_models_shell_command_cannot_reach_the_daemon(monkeypatch, contained):
    monkeypatch.setenv("JARVIS_BASH_TOOL_ENABLED", "true")
    from backend.core.ouroboros.governance.tools.bash_tool import SandboxedBashTool as BashTool

    victim, word = _victim()
    try:
        got = await BashTool().execute(
            # The pattern is assembled at run time: spelled out, it would match
            # the command's own shell and kill that (as it would uncontained).
            f"python3 -c \"import subprocess; subprocess.run(['pkill', '-f', '{word[:4]}' + '{word[4:]}'])\""
        )
        assert got.exit_code == 0, got.stderr
        assert victim.poll() is None, "a model's shell command killed a host process"
    finally:
        victim.kill()
        victim.wait()


async def test_a_shell_timeout_ends_the_whole_tree(monkeypatch, contained):
    monkeypatch.setenv("JARVIS_BASH_TOOL_ENABLED", "true")
    """The timeout used to kill only the shell; what it started lived on."""
    from backend.core.ouroboros.governance.tools.bash_tool import SandboxedBashTool as BashTool

    word = f"ovshell{uuid.uuid4().hex[:10]}"
    got = await BashTool().execute(
        "python3 -c \"import subprocess, sys, time; "
        f"subprocess.Popen(['setsid', sys.executable, '-c', 'import time; time.sleep(120)', '{word}']); "
        "time.sleep(60)\"",
        timeout=2,
    )
    assert got.timed_out
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        left = subprocess.run(["pgrep", "-f", word], capture_output=True, text=True).stdout.split()
        if not left:
            break
        await asyncio.sleep(0.1)
    assert not left, "the shell's descendants outlived its timeout"


async def test_a_promotion_gate_timeout_no_longer_abandons_its_run(tmp_path, monkeypatch, contained):
    from backend.core.ouroboros.governance import accumulation_promotion_gate as APG

    word = f"ovgate{uuid.uuid4().hex[:10]}"
    test = tmp_path / "tests" / f"test_{word}.py"
    test.parent.mkdir()
    test.write_text("import time\ndef test_slow():\n    time.sleep(60)\n")
    # The file-to-test mapping is the gate's business; how its run ENDS is ours.
    monkeypatch.setattr(APG, "_touched_files", lambda *a, **k: ["mod.py"])
    monkeypatch.setattr(APG, "_test_paths_for", lambda *a, **k: [str(test)])
    got = await APG._check_coverage("HEAD", tmp_path, python_bin=sys.executable, timeout_s=3.0)
    assert not got.passed and "exceeded" in got.detail
    await asyncio.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", word], capture_output=True, text=True).stdout.split()
    assert not left, "the timed-out coverage run was abandoned still running"
