"""The exit is bounded, not just the run.

Observed 2026-07-29: a soak ignored SIGTERM for 12+ seconds and needed
SIGKILL. The signal handler fired correctly — a 53KB summary.json landed —
and then graceful shutdown never finished.

`--max-wall-seconds` bounds the RUN. Nothing bounded the EXIT: every
subsystem's cleanup was awaited with no ceiling, so one hanging await
wedged the process permanently and an external SIGKILL was the only
recourse. A process that cannot be asked to stop is not shutting down.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import textwrap
import time

import pytest

_HARNESS = pathlib.Path("backend/core/ouroboros/battle_test/harness.py")


def _src() -> str:
    return _HARNESS.read_text()


def _code_of(fn_name: str) -> str:
    """A function's CODE, with its docstring removed.

    Every structural check here must read code, not prose. The docstrings
    deliberately NAME the things being avoided — "the partial-summary
    write", "the last phase reached", "the extend-condition" — so a
    substring match over the whole function flags the explanation as if it
    were the offence. That mistake has now cost four tests this session;
    stripping the docstring is the fix that generalises.
    """
    src = _src()
    node = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == fn_name
    )
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(
        (ast.get_source_segment(src, stmt) or "") for stmt in body
    )


class TestItIsWiredAtBothEnds:
    def test_armed_before_anything_that_can_wedge(self):
        """Everything after the arm can hang — the 2026-07-29 failure did
        exactly that, AFTER the summary landed. A deadline armed later
        would be armed by code that never runs.

        Asserted on STATEMENT ORDER via the AST rather than on string
        offsets: the docstring names the very things being ordered against
        ("the partial-summary write"), so a substring search finds the
        explanation before the code every time.
        """
        src = _src()
        node = next(
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_handle_shutdown_signal"
        )
        stmts = [s for s in node.body
                 if not (isinstance(s, ast.Expr)
                         and isinstance(s.value, ast.Constant))]
        arm_at = next(
            i for i, st in enumerate(stmts)
            if "_arm_shutdown_deadline" in (ast.dump(st))
        )
        assert arm_at == 0, (
            "the deadline must be the FIRST executable statement; "
            f"found at index {arm_at}"
        )

    def test_disarmed_ONLY_on_the_clean_path(self):
        """An exit that cannot confirm it finished should be forced, not
        trusted — so exactly one disarm site, on the full-summary path."""
        assert _src().count("self._disarm_shutdown_deadline(") == 1

    def test_it_is_idempotent_across_signals(self):
        """SIGINT then SIGTERM must not start two reapers."""
        src = _src()
        node = next(
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_arm_shutdown_deadline"
        )
        body = ast.get_source_segment(src, node) or ""
        assert "_shutdown_deadline_armed" in body
        assert "return" in body.split("_shutdown_deadline_armed")[1][:120]


class TestTheWatchdogIsIsolated:
    """The Watchdog Isolation Invariant this file already documents for the
    wall-clock cap: a watchdog that consults the inner state-ledger
    deadlocks WITH the system it guards."""

    def test_it_reads_only_a_clock(self):
        src = _src()
        node = next(
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_arm_shutdown_deadline"
        )
        body = ast.get_source_segment(src, node) or ""
        body = _code_of("_arm_shutdown_deadline")
        for forbidden in ("self._orchestrator", "_active_ops",
                          "self._phase"):
            assert forbidden not in body, forbidden
        assert "time.monotonic()" in body

    def test_it_captures_a_raw_fd_at_ARM_time(self):
        """A poisoned logging lock cannot wedge `os.write`, and the fd must
        be duped before the wedge, not during it."""
        src = _src()
        body = ast.get_source_segment(src, next(
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_arm_shutdown_deadline")) or ""
        assert "os.dup(2)" in body
        assert "os.write(" in body

    def test_it_kills_rather_than_re_entering_the_interpreter(self):
        """`sys.exit` runs atexit handlers and interpreter teardown — the
        very machinery suspected of being stuck."""
        body = ast.get_source_segment(_src(), next(
            n for n in ast.walk(ast.parse(_src()))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_arm_shutdown_deadline")) or ""
        assert "os.kill(" in body
        assert "sys.exit(" not in body

    def test_it_is_a_DAEMON_thread(self):
        body = ast.get_source_segment(_src(), next(
            n for n in ast.walk(ast.parse(_src()))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_arm_shutdown_deadline")) or ""
        assert "daemon=True" in body


class TestTheGraceIsBounded:
    def test_the_budget_is_a_knob_with_a_floor_and_a_ceiling(self):
        body = ast.get_source_segment(_src(), next(
            n for n in ast.walk(ast.parse(_src()))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_arm_shutdown_deadline")) or ""
        assert "JARVIS_SHUTDOWN_GRACE_S" in body
        assert "max(5.0" in body and "min(300.0" in body

    def test_no_activity_gated_extension(self):
        """Slice 47 rejected an adaptive waiver: a wedged phase keeps the
        extend-condition true forever, so the watchdog deadlocks WITH the
        system. The answer to "cleanup needs longer" is a larger STATIC
        budget."""
        body = ast.get_source_segment(_src(), next(
            n for n in ast.walk(ast.parse(_src()))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_arm_shutdown_deadline")) or ""
        code = _code_of("_arm_shutdown_deadline").lower()
        for waiver in ("extend", "waiver", "refresh", "reset_deadline"):
            assert waiver not in code, waiver


@pytest.mark.timeout(60)
class TestItActuallyKillsAWedge:
    def test_a_hung_cleanup_is_terminated(self, tmp_path):
        """The 2026-07-29 failure, reproduced in a real subprocess: the
        handler fires, writes its summary, and then never returns."""
        src = _src()
        tree = ast.parse(src)
        methods = "\n".join(
            textwrap.dedent(ast.get_source_segment(src, n) or "")
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name in ("_arm_shutdown_deadline",
                           "_disarm_shutdown_deadline")
        )
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import os, signal, threading, time, logging\n"
            "logger = logging.getLogger('p')\n"
            "from typing import Optional\n"
            "class H:\n"
            + textwrap.indent(methods, "    ")
            + "\nh = H()\n"
            "os.environ['JARVIS_SHUTDOWN_GRACE_S'] = '5'\n"
            "def on_term(*a):\n"
            "    h._arm_shutdown_deadline('sigterm')\n"
            "    while True: time.sleep(0.2)\n"
            "signal.signal(signal.SIGTERM, on_term)\n"
            "print('ready', flush=True)\n"
            "time.sleep(120)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, str(probe)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            assert proc.stdout is not None
            assert "ready" in (proc.stdout.readline() or "")
            proc.terminate()                  # SIGTERM into the wedge
            t0 = time.monotonic()
            rc = proc.wait(timeout=40)
            elapsed = time.monotonic() - t0
        finally:
            if proc.poll() is None:
                proc.kill()
        # Floor is 5s, so it must outlive a prompt exit and still die.
        assert 4.0 < elapsed < 30.0, elapsed
        assert rc != 0, "a SIGKILLed process should not report success"
