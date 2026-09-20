"""A test run cannot outlive itself, lie about how it ended, or be asked of a
subject that cannot be imported.

2026-09-20, Sentinel soak bt-2026-09-20-173220. The organism chose, unprompted,
to write tests for a 43-line launcher script whose import starts a backend
server and then blocks on ``tail -f``. What followed, per attempt:

  * 3 seconds of generation, 14 minutes of VALIDATE;
  * the kill at the time cap filed as an ordinary ``test`` failure, because
    the flake-retry merge rebuilt the result and dropped ``timed_out`` -- so
    the hang was LEARNED as the cost of the work (next allowance: 332s) and
    the model was told its code was wrong;
  * the hang re-run by that same retry, which exists to detect flakes;
  * one leaked backend server and two leaked ``tail``s per candidate, because
    no spawn site reaped a session whose leader had exited by itself.

Every test here drives the real ``TestRunner`` / ``BackgroundMonitor`` against
real processes. The leak was invisible to anything that mocked the subprocess.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import environment_integrity as ei
from backend.core.ouroboros.governance.background_monitor import BackgroundMonitor
from backend.core.ouroboros.governance.process_session import (
    reap_session,
    session_survivors,
)
from backend.core.ouroboros.governance.test_runner import TestRunner

pytestmark = pytest.mark.skipif(
    not Path("/proc").is_dir() or sys.platform == "win32",
    reason="process-session enumeration needs /proc",
)

#: A marker unique per test so a leak is attributable, never a guess.
def _marker() -> str:
    return f"{int(time.time() * 1000) % 100000 + 40000}"


def _alive_with(marker: str) -> list:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv and os.path.basename(argv[0].decode(errors="replace")) == "sleep" \
                and len(argv) > 1 and argv[1].decode(errors="replace") == marker:
            found.append(int(entry.name))
    return found


def _hanging_repo(root: Path, marker: str) -> Path:
    (root / "hangscript.py").write_text(
        "import subprocess\n"
        f"subprocess.run(['sleep', '{marker}'])\n"
    )
    (root / "tests").mkdir()
    test = root / "tests" / "test_hangscript.py"
    test.write_text("def test_import_smoke():\n    import hangscript  # noqa: F401\n")
    (root / "conftest.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n"
    )
    return test


# ---------------------------------------------------------------------------
# The run tells the truth about how it ended
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hung_import_is_a_timeout_not_a_test_failure(tmp_path):
    marker = _marker()
    test = _hanging_repo(tmp_path, marker)
    runner = TestRunner(repo_root=tmp_path, timeout=60.0, per_test_timeout_s=2)
    result = await runner.run(test_files=(test,), sandbox_dir=None)
    assert result.passed is False
    assert result.timed_out is True, (
        "a run killed at its cap was reported as an ordinary failure — its "
        "duration gets learned and the model gets blamed"
    )


@pytest.mark.asyncio
async def test_a_hang_is_not_retried_as_a_flake(tmp_path):
    marker = _marker()
    test = _hanging_repo(tmp_path, marker)
    runner = TestRunner(repo_root=tmp_path, timeout=60.0, per_test_timeout_s=2)
    t0 = time.monotonic()
    result = await runner.run(test_files=(test,), sandbox_dir=None)
    elapsed = time.monotonic() - t0
    assert "--- RETRY ---" not in (result.stdout or "")
    assert elapsed < 2 * 2 + 3, f"ran the hang twice ({elapsed:.1f}s)"


@pytest.mark.asyncio
async def test_a_genuine_failure_is_still_retried(tmp_path):
    """The flake retry itself is untouched."""
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests" / "test_red.py"
    test.write_text("def test_red():\n    assert 1 == 2\n")
    runner = TestRunner(repo_root=tmp_path, timeout=60.0, per_test_timeout_s=20)
    result = await runner.run(test_files=(test,), sandbox_dir=None)
    assert result.passed is False and result.timed_out is False
    assert "--- RETRY ---" in (result.stdout or "")


# ---------------------------------------------------------------------------
# The run cannot outlive itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_killed_run_leaves_nothing_behind(tmp_path):
    marker = _marker()
    test = _hanging_repo(tmp_path, marker)
    runner = TestRunner(repo_root=tmp_path, timeout=60.0, per_test_timeout_s=2)
    await runner.run(test_files=(test,), sandbox_dir=None)
    await asyncio.sleep(0.3)
    assert _alive_with(marker) == [], "the test's subprocess outlived the run"


@pytest.mark.asyncio
async def test_monitor_reaps_descendants_of_a_leader_that_exited_cleanly(tmp_path):
    """The blind spot every spawn site shared: exit code 0, group not empty."""
    marker = _marker()
    script = tmp_path / "spawn_and_leave.py"
    script.write_text(
        "import subprocess\n"
        f"subprocess.Popen(['sleep', '{marker}'])\n"
        "print('leader done')\n"
    )
    async with BackgroundMonitor([sys.executable, str(script)], op_id="t") as mon:
        async for _event in mon.events():
            pass
    await asyncio.sleep(0.3)
    assert mon.exit_code == 0
    assert _alive_with(marker) == []


def test_reaping_an_empty_session_signals_nobody():
    """An empty group's id may already belong to a stranger."""
    import subprocess
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait()
    assert session_survivors(proc.pid) == ()
    assert reap_session(proc.pid, owner="t") == ()


def test_reap_refuses_our_own_group():
    assert reap_session(os.getpgrp(), owner="t") == ()
    assert reap_session(0, owner="t") == ()
    assert reap_session(1, owner="t") == ()


def test_reap_reports_what_it_found():
    import subprocess
    marker = _marker()
    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"import subprocess; subprocess.Popen(['sleep','{marker}'])"],
        start_new_session=True,
    )
    proc.wait()
    time.sleep(0.2)
    survivors = reap_session(proc.pid, owner="t")
    time.sleep(0.2)
    assert len(survivors) == 1
    assert _alive_with(marker) == []


# ---------------------------------------------------------------------------
# The subject that should never have been asked about
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_a_launcher_script_is_a_hazard(tmp_path):
    path = _write(tmp_path, "backend/start_thing.py", (
        "import subprocess\nfrom pathlib import Path\n"
        "log = Path('x.log')\nprint('starting')\n"
        "with open(log, 'w') as fh:\n"
        "    subprocess.run(['tail', '-f', str(log)])\n"
    ))
    why = ei.import_execution_hazard(path, tmp_path)
    assert "defines nothing importable" in why
    assert "__main__" in why, "the reason must say how to fix the subject"


@pytest.mark.parametrize("body", [
    "import logging\nlogging.basicConfig()\n\ndef f():\n    return 1\n",
    "X = 1\nY = {'a': X}\n",
    "import warnings\nwarnings.warn('moved')\n__all__ = ['X']\nX = 1\n",
    "import subprocess\nif __name__ == '__main__':\n    subprocess.run(['sleep', '9'])\n",
    "import subprocess\nif '__main__' == __name__:\n    subprocess.run(['sleep', '9'])\n",
    "def main(:\n",
    "",
])
def test_ordinary_modules_are_never_accused(tmp_path, body):
    path = _write(tmp_path, "backend/mod.py", body)
    assert ei.import_execution_hazard(path, tmp_path) == ""


def test_a_relative_reexporter_has_an_api(tmp_path):
    _write(tmp_path, "backend/pkg/__init__.py", "")
    _write(tmp_path, "backend/pkg/real.py", "def f():\n    return 1\n")
    shim = _write(tmp_path, "backend/pkg/shim.py", (
        "import warnings\nwarnings.warn('moved', DeprecationWarning)\n"
        "from .real import f  # noqa: F401\n"
    ))
    assert ei.import_execution_hazard(shim, tmp_path) == ""


def test_a_package_init_is_never_a_script(tmp_path):
    init = _write(tmp_path, "backend/pkg/__init__.py", "print('hello')\n")
    assert ei.import_execution_hazard(init, tmp_path) == ""


def test_the_verdict_makes_the_goal_undispatchable(tmp_path):
    _write(tmp_path, "backend/start_thing.py", (
        "import subprocess\nprint('go')\nsubprocess.run(['tail', '-f', 'x'])\n"
    ))
    verdict = ei.target_import_verdict(
        ["tests/test_start_thing.py"],
        "`backend/start_thing.py` has no test module. CREATE `tests/test_start_thing.py`",
        tmp_path,
    )
    assert verdict.importable is False
    assert verdict.impossible is True, "must quarantine, not merely demote"
    assert verdict.reason.startswith(ei.IMPORT_EXECUTES_PROGRAM)


def test_an_importable_subject_is_untouched(tmp_path):
    _write(tmp_path, "backend/good.py", "def f():\n    return 1\n")
    verdict = ei.target_import_verdict(
        ["backend/good.py"], "harden `backend/good.py`", tmp_path,
    )
    assert verdict.importable is True and verdict.reason == ""
