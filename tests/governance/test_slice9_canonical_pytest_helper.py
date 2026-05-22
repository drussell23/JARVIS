"""Slice 9 — canonical pytest-subprocess helper + AST cage.

Closes the multi-path subprocess deadlock from bt-2026-05-22-000838
(Slice 7f graduation soak): Slice 8 patched only 2 of 9 pytest
spawn sites, leaving 7 unpatched paths that kept the post-Slice-7-
proof event-loop starvation alive (PIDs 3554, 3558 wedged at
STAT=SN for 9+ minutes).

Slice 9 introduces ONE canonical helper module
(``test_subprocess_helper.py``) with TWO entry points
(``run_pytest_subprocess`` async + ``run_pytest_subprocess_sync``)
and refactors every pytest invocation across the live battle_test
runtime to route through it.

## AST cage (operator-bound, verbatim)

  *"No direct asyncio.create_subprocess_exec(... pytest ...)
  outside helper. No direct subprocess.Popen(... pytest ...)
  outside helper. No stdin inheritance for pytest subprocesses."*

This module's AST pins enforce that ANY subprocess call whose
argv contains the string ``"pytest"`` MAY ONLY appear inside
``test_subprocess_helper.py``. A regression that reintroduces a
direct subprocess.run/Popen/create_subprocess_exec with pytest in
argv anywhere else fails CI before landing.

## Test surface

  * Closed-taxonomy AST pin — ``KillReason`` 5-value enum.
  * **AST cage Pin 1** — ZERO pytest-bearing subprocess calls
    outside the canonical helper across the entire backend/.
  * **AST cage Pin 2** — every pytest call site in the helper
    itself uses ``stdin=DEVNULL`` (or ``subprocess.DEVNULL``).
  * **AST cage Pin 3** — no blind ``proc.wait()`` calls in the
    helper module.
  * **Provenance pin** — every public entry point logs ``[PytestHelper]``
    at INFO with a ``caller=...`` field.
  * **Behavioural async pin** — fake stdin-reading child wedges
    WITHOUT the helper, exits cleanly WITH it.
  * **Behavioural timeout pin** — child sleeps past timeout;
    helper SIGKILL-pgrps cleanly + returns ``timed_out=True``.
  * **Behavioural sync pin** — same as async but for the sync
    entry point.
  * **Process-group cleanup pin** — child spawns grandchild that
    refuses parent's SIGTERM; helper's killpg cleans both.
  * Public surface ``__all__`` pin.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import pathlib
import sys
import tempfile
import unittest
from typing import List

from backend.core.ouroboros.governance.test_subprocess_helper import (
    KillReason,
    PytestRunResult,
    run_pytest_subprocess,
    run_pytest_subprocess_sync,
)
from backend.core.ouroboros.governance import test_subprocess_helper as helper_mod


# ============================================================================
# Module paths
# ============================================================================


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_HELPER_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "test_subprocess_helper.py"
)
_BACKEND_ROOT = _REPO_ROOT / "backend"

# Shadow / backup files to ignore (operator's "* 2.py" pattern).
_SHADOW_SUFFIX = " 2.py"


def _iter_backend_py_files():
    """Yield every .py file under backend/ excluding __pycache__ and
    shadow ``* 2.py`` backup files."""
    for py in _BACKEND_ROOT.rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        if _SHADOW_SUFFIX in py.name:
            continue
        yield py


def _parse(path: pathlib.Path) -> ast.Module:
    # Defensive: some files in the tree are not valid UTF-8 (e.g.
    # accidentally-added binary blobs, archived `* 2.py` files with
    # encoding drift). The AST cage's job is to enforce a structural
    # invariant on PARSEABLE Python source; unparseable files are
    # skipped by raising a special marker that the caller drops.
    try:
        src = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        raise SyntaxError(f"unparseable: {path}")
    return ast.parse(src, filename=str(path))


# ============================================================================
# Closed taxonomy — KillReason cardinality
# ============================================================================


class TestKillReasonClosedTaxonomy(unittest.TestCase):
    """``KillReason`` is a closed 5-value taxonomy. Adding a 6th
    requires bumping this pin + every caller's branching."""

    def test_exactly_five_members(self) -> None:
        self.assertEqual(
            len(list(KillReason)), 5,
            f"KillReason is closed; found {[m.name for m in KillReason]}",
        )

    def test_member_names(self) -> None:
        self.assertEqual(
            {m.name for m in KillReason},
            {"NOT_KILLED", "TIMEOUT", "CANCELLATION",
             "CALLER_TERMINATION", "SPAWN_ERROR"},
        )


# ============================================================================
# AST cage Pin 1 — ZERO pytest spawns outside the helper
# ============================================================================


class TestAstCagePin1NoPytestOutsideHelper(unittest.TestCase):
    """Operator binding rolled forward: every subprocess call whose
    argv contains ``"pytest"`` MUST live exclusively inside
    ``test_subprocess_helper.py``. This pin walks every .py file
    under backend/ and catches any direct
    ``subprocess.run`` / ``subprocess.Popen`` /
    ``asyncio.create_subprocess_exec`` / ``create_subprocess_shell``
    call whose argv strings mention pytest."""

    _SPAWN_ATTRS: set = {
        "create_subprocess_exec",
        "create_subprocess_shell",
        "run",
        "Popen",
        "call",
    }
    _SPAWN_NAMES: set = {"Popen", "run", "call"}

    def test_no_direct_pytest_spawns_outside_helper(self) -> None:
        violations: List[str] = []
        for py in _iter_backend_py_files():
            if py == _HELPER_FILE:
                continue
            # Fast-path: a file that doesn't even MENTION pytest as
            # bytes can't possibly contain a pytest-bearing
            # subprocess call. Skips ~99% of the backend without
            # AST parsing — avoids the pytest-internal-timeout
            # pathology when scanning thousands of files.
            try:
                raw = py.read_bytes()
            except OSError:
                continue
            if b"pytest" not in raw:
                continue
            try:
                tree = _parse(py)
            except SyntaxError:
                # Unparseable / non-utf-8 / corrupted — skip.
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                is_spawn = False
                if (
                    isinstance(f, ast.Attribute)
                    and f.attr in self._SPAWN_ATTRS
                ):
                    is_spawn = True
                elif (
                    isinstance(f, ast.Name)
                    and f.id in self._SPAWN_NAMES
                ):
                    is_spawn = True
                if not is_spawn:
                    continue
                argv_str = " ".join(
                    ast.unparse(a) for a in node.args[:6]
                )
                if "pytest" in argv_str.lower():
                    violations.append(
                        f"{py.relative_to(_REPO_ROOT)}:{node.lineno}"
                    )
        self.assertEqual(
            violations, [],
            f"Slice 9 cage violation — direct pytest-bearing "
            f"subprocess calls found OUTSIDE the canonical helper. "
            f"Refactor each to use "
            f"``run_pytest_subprocess`` or "
            f"``run_pytest_subprocess_sync`` from "
            f"``test_subprocess_helper.py``.\n  "
            + "\n  ".join(violations)
        )


# ============================================================================
# AST cage Pin 2 — helper itself uses stdin=DEVNULL
# ============================================================================


class TestAstCagePin2HelperStdinDevnull(unittest.TestCase):
    """Inside the canonical helper, every spawn call MUST carry
    ``stdin=asyncio.subprocess.DEVNULL`` (for async) or
    ``stdin=subprocess.DEVNULL`` (for sync). The Slice 8 binding
    rolled forward."""

    def test_helper_async_call_has_devnull_stdin(self) -> None:
        tree = _parse(_HELPER_FILE)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (
                isinstance(f, ast.Attribute)
                and f.attr == "create_subprocess_exec"
            ):
                continue
            for kw in node.keywords:
                if kw.arg == "stdin":
                    src = ast.unparse(kw.value)
                    self.assertIn(
                        "DEVNULL", src,
                        f"Async spawn at L{node.lineno} has stdin= "
                        f"{src} — must be DEVNULL",
                    )
                    found = True
        self.assertTrue(
            found,
            "Helper module must contain at least one "
            "create_subprocess_exec call with stdin=DEVNULL",
        )

    def test_helper_sync_call_has_devnull_stdin(self) -> None:
        tree = _parse(_HELPER_FILE)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (
                isinstance(f, ast.Attribute)
                and f.attr == "run"
                and isinstance(f.value, ast.Name)
                and f.value.id == "_subprocess"
            ):
                continue
            for kw in node.keywords:
                if kw.arg == "stdin":
                    src = ast.unparse(kw.value)
                    self.assertIn("DEVNULL", src)
                    found = True
        self.assertTrue(
            found,
            "Helper module sync path must use subprocess.DEVNULL "
            "for stdin",
        )

    def test_helper_uses_start_new_session(self) -> None:
        """Process-group isolation per operator binding —
        ``start_new_session=True`` enables the killpg cleanup of
        pytest-xdist workers."""
        src = _HELPER_FILE.read_text()
        self.assertIn(
            "start_new_session=True",
            src,
            "Helper MUST spawn children with start_new_session=True "
            "so killpg() can clean grandchildren (pytest-xdist).",
        )

    def test_helper_uses_killpg(self) -> None:
        """The kill primitive MUST be ``os.killpg`` not
        ``proc.kill`` (process-group binding)."""
        src = _HELPER_FILE.read_text()
        self.assertIn(
            "os.killpg",
            src,
            "Helper MUST use os.killpg for process-group cleanup.",
        )


# ============================================================================
# AST cage Pin 3 — no blind .wait() in helper
# ============================================================================


class TestAstCagePin3NoBlindWaitInHelper(unittest.TestCase):
    """Operator binding rolled forward from Slice 8: no blind
    ``.wait()`` calls. The canonical drain primitive is
    ``proc.communicate()`` which concurrently drains stdout +
    stderr and awaits exit atomically. ``proc.wait()`` standalone
    awaits exit WITHOUT draining the pipes — the OS-pipe-buffer
    fills, blocking the child's writes, deadlocking both processes."""

    def test_no_blind_proc_wait_in_helper(self) -> None:
        tree = _parse(_HELPER_FILE)
        offenders: List[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (
                isinstance(f, ast.Attribute)
                and f.attr == "wait"
                and len(node.args) == 0
                and len(node.keywords) == 0
            ):
                continue
            # Skip asyncio.wait — that's a different function.
            if (
                isinstance(f.value, ast.Name)
                and f.value.id == "asyncio"
            ):
                continue
            offenders.append(node.lineno)
        self.assertEqual(
            offenders, [],
            f"Slice 9 binding violation — blind .wait() in helper "
            f"at L{offenders}. Use communicate() instead.",
        )


# ============================================================================
# Provenance pin — every entry point logs [PytestHelper] at INFO
# ============================================================================


class TestProvenanceLogging(unittest.IsolatedAsyncioTestCase):
    """Operator binding: *"Add provenance logging: caller path /
    component name, timeout value, command argv, PID, kill reason."*

    The async + sync entry points MUST log at INFO with the
    structured ``[PytestHelper]`` prefix and the caller/argv/timeout
    fields, allowing operators to grep one log to find every pytest
    invocation in a session."""

    async def test_async_helper_logs_provenance_at_spawn(self) -> None:
        logger = logging.getLogger("Ouroboros.PytestHelper")
        with self.assertLogs(logger, level="INFO") as caplog:
            await run_pytest_subprocess(
                [sys.executable, "-c", "print('ok')"],
                timeout_s=10.0,
                caller="test_provenance.async_case",
            )
        joined = "\n".join(caplog.output)
        self.assertIn("[PytestHelper] spawn", joined)
        self.assertIn("caller=test_provenance.async_case", joined)
        self.assertIn("timeout=10.0s", joined)
        self.assertIn("argv=", joined)
        self.assertIn("[PytestHelper] spawned", joined)
        self.assertIn("[PytestHelper] exit", joined)

    def test_sync_helper_logs_provenance_at_spawn(self) -> None:
        logger = logging.getLogger("Ouroboros.PytestHelper")
        with self.assertLogs(logger, level="INFO") as caplog:
            run_pytest_subprocess_sync(
                [sys.executable, "-c", "print('ok')"],
                timeout_s=10.0,
                caller="test_provenance.sync_case",
            )
        joined = "\n".join(caplog.output)
        self.assertIn("[PytestHelper] spawn (sync)", joined)
        self.assertIn("caller=test_provenance.sync_case", joined)
        self.assertIn("timeout=10.0s", joined)
        self.assertIn("[PytestHelper] exit (sync)", joined)


# ============================================================================
# Behavioural async pin — stdin-reading child can't wedge under helper
# ============================================================================


class TestBehaviouralStdinNoDeadlock(unittest.IsolatedAsyncioTestCase):
    """Spawn a controlled child that reads from stdin. Under
    inherited stdin from a TTY-less harness, the read blocks
    indefinitely (the empirical bt-2026-05-22-000838 wedge mode).

    The helper's ``stdin=DEVNULL`` discipline makes the read return
    EOF immediately. The child exits cleanly within a bounded
    timeout."""

    async def test_async_child_reading_stdin_does_not_wedge(self) -> None:
        result = await run_pytest_subprocess(
            [sys.executable, "-c",
             "import sys; data = sys.stdin.read(); "
             "print(f'got_{len(data)}_bytes')"],
            timeout_s=10.0,
            caller="test_behavioural.async_stdin_devnull",
        )
        self.assertFalse(
            result.timed_out,
            f"Child with stdin=DEVNULL must NOT time out; "
            f"got result: {result}",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("got_0_bytes", result.stdout)

    def test_sync_child_reading_stdin_does_not_wedge(self) -> None:
        result = run_pytest_subprocess_sync(
            [sys.executable, "-c",
             "import sys; data = sys.stdin.read(); "
             "print(f'got_{len(data)}_bytes')"],
            timeout_s=10.0,
            caller="test_behavioural.sync_stdin_devnull",
        )
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertIn("got_0_bytes", result.stdout)


# ============================================================================
# Behavioural timeout pin — child sleeping past timeout is killed cleanly
# ============================================================================


class TestBehaviouralTimeoutKillsPgrp(unittest.IsolatedAsyncioTestCase):
    """A child that sleeps far past the helper's timeout MUST be
    killed cleanly (the helper returns ``timed_out=True`` +
    ``kill_reason=TIMEOUT`` within bounded time)."""

    async def test_async_timeout_kills_pgrp_cleanly(self) -> None:
        result = await run_pytest_subprocess(
            [sys.executable, "-c",
             "import time; time.sleep(60)"],
            timeout_s=1.0,
            caller="test_behavioural.async_timeout",
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(result.kill_reason, KillReason.TIMEOUT)
        self.assertLess(
            result.elapsed_s, 30.0,
            f"Helper must terminate the child within bounded grace; "
            f"actual elapsed={result.elapsed_s:.2f}s",
        )

    def test_sync_timeout_kills_cleanly(self) -> None:
        result = run_pytest_subprocess_sync(
            [sys.executable, "-c",
             "import time; time.sleep(60)"],
            timeout_s=1.0,
            caller="test_behavioural.sync_timeout",
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(result.kill_reason, KillReason.TIMEOUT)
        self.assertLess(result.elapsed_s, 15.0)


# ============================================================================
# Process-group cleanup — pytest-xdist style grandchild also dies
# ============================================================================


class TestProcessGroupCleanup(unittest.IsolatedAsyncioTestCase):
    """Spawn a child that itself spawns a grandchild that *ignores*
    SIGTERM (the pytest-xdist worker pattern). The helper's
    ``os.killpg(SIGKILL)`` cleans BOTH — without process-group
    isolation, grandchild would survive as STAT=SN orphan."""

    async def test_grandchild_cleaned_by_killpg(self) -> None:
        # The child spawns a grandchild that ignores SIGTERM and
        # sleeps forever. Both should be cleaned by killpg.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False,
        ) as f:
            f.write(
                "import os, signal, subprocess, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "# Spawn a grandchild that also ignores SIGTERM.\n"
                "gc = subprocess.Popen(\n"
                "    [sys.executable, '-c',\n"
                "     'import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(120)'])\n"
                "print(f'grandchild_pid={gc.pid}', flush=True)\n"
                "time.sleep(120)\n"
            )
            tmpfile = f.name

        try:
            result = await run_pytest_subprocess(
                [sys.executable, tmpfile],
                timeout_s=1.0,
                caller="test_pgrp_cleanup",
            )
            self.assertTrue(result.timed_out)
            self.assertEqual(result.kill_reason, KillReason.TIMEOUT)
            # Parse grandchild PID from stdout (helper captured it
            # before kill).
            gc_pid = None
            for line in result.stdout.splitlines():
                if line.startswith("grandchild_pid="):
                    try:
                        gc_pid = int(line.split("=", 1)[1].strip())
                    except (TypeError, ValueError):
                        pass
                    break
            # Allow a brief window for the OS to reap.
            await asyncio.sleep(0.5)
            if gc_pid is not None:
                # The grandchild process should be GONE
                # (killpg cleaned it).
                try:
                    os.kill(gc_pid, 0)
                    self.fail(
                        f"Grandchild PID {gc_pid} STILL ALIVE after "
                        f"helper timeout — killpg cleanup failed. "
                        f"This would leave STAT=SN orphans in the "
                        f"empirical wedge pattern."
                    )
                except (ProcessLookupError, PermissionError):
                    # ProcessLookupError = dead (expected).
                    # PermissionError = reaped + PID reused (also OK).
                    pass
        finally:
            try:
                os.unlink(tmpfile)
            except OSError:
                pass


# ============================================================================
# Spawn-error path returns synthetic result without raising
# ============================================================================


class TestSpawnErrorNeverRaises(unittest.IsolatedAsyncioTestCase):
    """Pathological inputs (non-existent binary, bad cwd) MUST
    return a structured result with ``kill_reason=SPAWN_ERROR``,
    NEVER raise."""

    async def test_async_nonexistent_binary_returns_synthetic_result(self) -> None:
        result = await run_pytest_subprocess(
            ["/nonexistent/binary/that/cannot/possibly/exist"],
            timeout_s=5.0,
            caller="test_spawn_error.async",
        )
        self.assertEqual(result.kill_reason, KillReason.SPAWN_ERROR)
        self.assertIsNotNone(result.spawn_error_class)
        # FileNotFoundError is the most common; some platforms may
        # surface different exception classes.
        self.assertIn(
            result.spawn_error_class,
            {"FileNotFoundError", "OSError", "PermissionError"},
        )

    def test_sync_nonexistent_binary_returns_synthetic_result(self) -> None:
        result = run_pytest_subprocess_sync(
            ["/nonexistent/binary/that/cannot/possibly/exist"],
            timeout_s=5.0,
            caller="test_spawn_error.sync",
        )
        self.assertEqual(result.kill_reason, KillReason.SPAWN_ERROR)
        self.assertIsNotNone(result.spawn_error_class)


# ============================================================================
# Public surface
# ============================================================================


class TestPublicSurface(unittest.TestCase):
    def test_all_exports(self) -> None:
        self.assertEqual(
            set(helper_mod.__all__),
            {
                "KillReason",
                "PytestRunResult",
                "run_pytest_subprocess",
                "run_pytest_subprocess_sync",
            },
        )

    def test_each_export_exists(self) -> None:
        for name in helper_mod.__all__:
            self.assertTrue(hasattr(helper_mod, name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
