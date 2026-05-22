"""Slice 8 — Subprocess pipe-discipline AST pins (TestRunner + TestWatcher).

Closes the empirical wedge from bt-2026-05-21-230025: two `pytest tests/`
subprocesses spawned by TestWatcher (PIDs 99509, 99511) were stuck at
``STAT=SN`` (sleeping, 0% CPU) for 11+ minutes. The smoke's main
asyncio loop starved while ``proc.communicate()`` polled for completion.

## Empirical root cause

Pytest inherits the parent's stdin when ``stdin=`` is not explicitly
set. The pytest child then issues an ``is_interactive_terminal()``
probe / ``--pdb`` fallback that does a blocking ``read()`` on stdin
— a read that NEVER returns when the parent's stdin is the harness's
terminal (or a stdin connected to a non-TTY pipe that the parent
won't write to). The child's stdin read holds the process at
``STAT=SN`` indefinitely; the parent's ``await proc.communicate()``
likewise never completes; the asyncio event loop starves.

## Architectural fix (operator-bound)

Operator binding (verbatim): *"You must explicitly route
``stdin=asyncio.subprocess.DEVNULL`` (to prevent blocking on input)
and asynchronously drain stdout and stderr concurrently (e.g., via
``await proc.communicate()``) so the OS pipes never fill up and
block the event loop. No blind ``.wait()`` calls anywhere."*

This module's AST pins enforce that discipline structurally:

  1. **Every ``asyncio.create_subprocess_exec(...)`` call in
     ``test_runner.py`` + ``test_watcher.py`` MUST carry an
     explicit ``stdin=asyncio.subprocess.DEVNULL`` kwarg.**
     Without this, regression silently reintroduces the wedge.

  2. **No blind ``.wait()`` calls in those files.** The canonical
     async drain primitive is ``proc.communicate()`` (which
     concurrently reads stdout + stderr and awaits exit in one
     atomic call). ``proc.wait()`` does NOT drain the pipes — it
     just awaits exit, allowing the OS pipe buffer (~64 KB on
     Darwin/Linux) to fill and block the child's writes.

The pins are AST-based: they parse the source file and walk the
tree without importing or executing the modules. A future refactor
that omits ``stdin=`` from any subprocess call fails CI before
landing.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from typing import List, Tuple


# ============================================================================
# Module paths
# ============================================================================


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TEST_RUNNER = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "test_runner.py"
)
_TEST_WATCHER = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "intent" / "test_watcher.py"
)


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _find_create_subprocess_exec_calls(
    tree: ast.Module,
) -> List[ast.Call]:
    """Yield every ``asyncio.create_subprocess_exec(...)`` call —
    Attribute path ``asyncio.create_subprocess_exec`` OR alias of
    ``create_subprocess_exec`` directly."""
    out: List[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "create_subprocess_exec"
        ):
            out.append(node)
        elif (
            isinstance(func, ast.Name)
            and func.id == "create_subprocess_exec"
        ):
            out.append(node)
    return out


def _stdin_kwarg_is_devnull(call: ast.Call) -> Tuple[bool, str]:
    """Return ``(ok, diag)`` — True iff the call carries a
    ``stdin=`` kwarg whose value is ``asyncio.subprocess.DEVNULL``
    (or ``subprocess.DEVNULL``). NEVER raises."""
    for kw in call.keywords:
        if kw.arg != "stdin":
            continue
        # Allowed shape: Attribute(Attribute(Name('asyncio'),
        # 'subprocess'), 'DEVNULL') or
        # Attribute(Name('subprocess'), 'DEVNULL').
        value = kw.value
        if isinstance(value, ast.Attribute) and value.attr == "DEVNULL":
            inner = value.value
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "subprocess"
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "asyncio"
            ):
                return True, "asyncio.subprocess.DEVNULL"
            if (
                isinstance(inner, ast.Name)
                and inner.id == "subprocess"
            ):
                return True, "subprocess.DEVNULL"
        return False, (
            f"stdin= kwarg present but value is "
            f"{ast.unparse(value) if hasattr(ast, 'unparse') else '<value>'} "
            f"— must be asyncio.subprocess.DEVNULL"
        )
    return False, "stdin= kwarg missing"


def _find_blind_wait_calls(tree: ast.Module) -> List[Tuple[int, str]]:
    """Find ``proc.wait()`` calls that are NOT wrapped in
    ``asyncio.wait_for`` (which is fine — wait_for is bounded).

    We define a "blind wait" as ``<obj>.wait(...)`` with no
    arguments AND ``<obj>`` being a local variable named like
    a process (``proc``, ``probe``, ``stream``, etc.) — heuristic
    that catches the empirical defect (``proc.wait()`` at L157 of
    the pre-Slice-8 test_watcher) without over-fitting.

    A wrapped wait — ``asyncio.wait_for(proc.wait(), ...)`` — is
    OK because wait_for bounds it. But ``proc.wait()`` standalone
    is a blind wait that does NOT drain pipes.

    Returns list of (lineno, descriptor) for offenders."""
    offenders: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match <obj>.wait() with no args.
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "wait"
            and len(node.args) == 0
            and len(node.keywords) == 0
        ):
            continue
        # Skip if receiver is asyncio (asyncio.wait is different).
        if (
            isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        ):
            continue
        # Heuristic: receiver names that indicate a Process handle.
        receiver = func.value
        receiver_repr = (
            receiver.id if isinstance(receiver, ast.Name)
            else (
                ast.unparse(receiver)
                if hasattr(ast, "unparse") else "<obj>"
            )
        )
        if isinstance(receiver, ast.Name) and receiver.id in {
            "proc", "process", "probe", "stream", "child",
            "subprocess_obj", "p",
        }:
            offenders.append((node.lineno, f"{receiver_repr}.wait()"))
    return offenders


# ============================================================================
# Pin 1 — every create_subprocess_exec carries stdin=DEVNULL
# ============================================================================


class TestStdinDevnullDiscipline(unittest.TestCase):
    """The structural cage that prevents the bt-2026-05-21-230025
    wedge from regressing. Every ``asyncio.create_subprocess_exec``
    call in ``test_runner.py`` + ``test_watcher.py`` MUST carry
    ``stdin=asyncio.subprocess.DEVNULL``."""

    def test_test_runner_every_subprocess_carries_stdin_devnull(self) -> None:
        tree = _parse(_TEST_RUNNER)
        calls = _find_create_subprocess_exec_calls(tree)
        self.assertGreater(
            len(calls), 0,
            "Sanity guard: test_runner.py MUST contain at least one "
            "asyncio.create_subprocess_exec call. If this fails, "
            "the AST pin is no longer protecting anything.",
        )
        offenders: List[str] = []
        for call in calls:
            ok, diag = _stdin_kwarg_is_devnull(call)
            if not ok:
                offenders.append(
                    f"test_runner.py:{call.lineno}  {diag}"
                )
        self.assertEqual(
            offenders, [],
            "Slice 8 invariant violated — at least one "
            "create_subprocess_exec in test_runner.py is missing "
            "the explicit stdin=asyncio.subprocess.DEVNULL kwarg. "
            "Without it, pytest children inherit the parent's "
            "stdin and block on is_interactive_terminal() / --pdb "
            "probes. The empirical wedge from "
            "bt-2026-05-21-230025 will recur.\n  "
            + "\n  ".join(offenders or ["(none)"])
        )

    def test_test_watcher_pytest_subprocess_carries_stdin_devnull(self) -> None:
        tree = _parse(_TEST_WATCHER)
        calls = _find_create_subprocess_exec_calls(tree)
        self.assertGreater(
            len(calls), 0,
            "test_watcher.py MUST contain at least one "
            "asyncio.create_subprocess_exec — its sole purpose "
            "is to invoke pytest.",
        )
        offenders: List[str] = []
        for call in calls:
            ok, diag = _stdin_kwarg_is_devnull(call)
            if not ok:
                offenders.append(
                    f"test_watcher.py:{call.lineno}  {diag}"
                )
        self.assertEqual(
            offenders, [],
            "Slice 8 invariant violated in test_watcher.py — the "
            "empirical wedge source from bt-2026-05-21-230025.\n  "
            + "\n  ".join(offenders or ["(none)"])
        )


# ============================================================================
# Pin 2 — no blind .wait() calls in either file
# ============================================================================


class TestNoBlindWaitCalls(unittest.TestCase):
    """Operator binding (verbatim): *"No blind ``.wait()`` calls
    anywhere."* The canonical drain primitive is
    ``proc.communicate()``, which concurrently reads stdout +
    stderr AND awaits exit. ``proc.wait()`` standalone awaits exit
    WITHOUT draining the pipe — the OS-pipe-buffer (~64 KB on
    Darwin/Linux) eventually fills and blocks the child's writes,
    deadlocking both processes."""

    def test_test_runner_no_blind_proc_wait(self) -> None:
        tree = _parse(_TEST_RUNNER)
        offenders = _find_blind_wait_calls(tree)
        self.assertEqual(
            offenders, [],
            f"Slice 8 invariant violated — blind .wait() calls in "
            f"test_runner.py: {offenders}. Use "
            f"asyncio.wait_for(proc.communicate(), timeout=N) "
            f"instead — bounded + drains pipes concurrently.",
        )

    def test_test_watcher_no_blind_proc_wait(self) -> None:
        tree = _parse(_TEST_WATCHER)
        offenders = _find_blind_wait_calls(tree)
        self.assertEqual(
            offenders, [],
            f"Slice 8 invariant violated — blind .wait() calls in "
            f"test_watcher.py: {offenders}. Use "
            f"asyncio.wait_for(proc.communicate(), timeout=N) "
            f"instead.",
        )


# ============================================================================
# Pin 3 — communicate() is the primary drain primitive
# ============================================================================


class TestUsesCommunicate(unittest.TestCase):
    """Positive-presence pin: each file MUST call ``proc.communicate()``
    at least once. The substitution for blind ``.wait()`` is
    ``communicate()`` — without it the structural fix is dead code."""

    def test_test_runner_uses_communicate(self) -> None:
        src = _TEST_RUNNER.read_text()
        self.assertIn(
            "proc.communicate()",
            src,
            "test_runner.py MUST call proc.communicate() — the "
            "canonical async pipe-drain primitive.",
        )

    def test_test_watcher_uses_communicate(self) -> None:
        src = _TEST_WATCHER.read_text()
        self.assertIn(
            "proc.communicate()",
            src,
            "test_watcher.py MUST call proc.communicate().",
        )


# ============================================================================
# Pin 4 — sanity: known site count
# ============================================================================


class TestSpawnSiteCardinality(unittest.TestCase):
    """At Slice 8 landing, test_runner.py had exactly 6
    ``create_subprocess_exec`` sites (cmake configure / cmake
    build / cmake install / ABI probe / ctest / pytest exec).
    test_watcher.py had exactly 1 (pytest invocation).

    A future refactor that ADDS a new subprocess site without
    paired ``stdin=DEVNULL`` is caught by Pin 1; this pin catches
    the inverse — a refactor that REMOVES all subprocess sites,
    which would render Pin 1 vacuously true."""

    def test_test_runner_has_at_least_six_subprocess_sites(self) -> None:
        tree = _parse(_TEST_RUNNER)
        calls = _find_create_subprocess_exec_calls(tree)
        self.assertGreaterEqual(
            len(calls), 6,
            f"test_runner.py: expected ≥6 subprocess sites at "
            f"Slice 8 landing; found {len(calls)}. If you "
            f"intentionally removed sites, lower this floor + "
            f"update the docstring; do NOT silently drop the "
            f"floor (it's a Pin-1-vacuity guard).",
        )

    def test_test_watcher_has_at_least_one_subprocess_site(self) -> None:
        tree = _parse(_TEST_WATCHER)
        calls = _find_create_subprocess_exec_calls(tree)
        self.assertGreaterEqual(
            len(calls), 1,
            f"test_watcher.py: expected ≥1 subprocess site; "
            f"found {len(calls)}.",
        )


# ============================================================================
# Pin 5 — behavioural smoke (the empirical proof)
# ============================================================================


class TestSubprocessNoDeadlock(unittest.IsolatedAsyncioTestCase):
    """**Behavioural pin** — exercises the actual pipe discipline
    with a controlled child that would HANG without ``stdin=DEVNULL``.

    The fake child reads from stdin and prints what it received.
    When stdin is the parent's terminal (or unset), the read blocks
    forever. When stdin is DEVNULL, the read returns EOF immediately
    and the child exits cleanly.

    This pin proves the fix at runtime, not just structurally."""

    async def test_devnull_prevents_stdin_read_deadlock(self) -> None:
        import asyncio
        import sys

        # Spawn a child that reads stdin. Without DEVNULL it blocks.
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            (
                "import sys; data = sys.stdin.read(); "
                "print(f'got {len(data)} bytes'); sys.stdout.flush()"
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # The Slice 8 fix — without this kwarg the child
            # blocks on sys.stdin.read() forever.
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=10.0,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            self.fail(
                "Slice 8 behavioural pin FAILED: child with "
                "stdin=DEVNULL still timed out after 10s. The "
                "DEVNULL discipline is not working as expected.",
            )
        output = out.decode("utf-8", errors="replace")
        self.assertIn(
            "got 0 bytes", output,
            f"Expected 'got 0 bytes' from stdin=DEVNULL child; "
            f"got: {output!r}",
        )
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
