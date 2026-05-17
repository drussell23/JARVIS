"""§Phase D — the Resource-Zero Watchdog.

Closes the root cause diagnosed in the bt-2026-05-17-024509
postmortem: the wall-clock watchdog's *kill path* shared resources
with the very system it was guarding.

* Old Layer 3 sent ``SIGTERM`` to itself and trusted the harness
  signal handler to write a partial summary. PROVEN wedgeable —
  bt-2026-05-17-024509 ignored an *external* SIGTERM because signal
  delivery was queued behind the same starved interpreter.
* Old Layer 4 emitted a logging-module diagnostic and called
  ``_atexit_fallback_write`` before ``os._exit(75)``. Both acquire
  the logging lock; the postmortem proved that lock was the poison
  — a wedged producer held it, so the watchdog's own kill path
  blocked on lock acquisition and never reached the exit.

A watchdog that shares the signal path AND the logging lock with
the system it guards is not a watchdog. Phase D collapses both
escalation layers into a single **resource-zero** kill that touches
nothing a poisoned process can hold:

  1. A raw ``os.dup(2)`` panic fd captured at *arm time* (pre-wedge).
  2. ``os.write`` to that fd for every diagnostic — never ``logging``.
  3. ``os.kill(os.getpid(), signal.SIGKILL)`` — never SIGTERM, never
     the harness signal handler, never ``call_soon_threadsafe``.
  4. ``os._exit(137)`` as an absolute backstop.

The ``signal`` reference is the module-level import (already in
``sys.modules`` before the thread ran) — not an inner ``import``,
so not even the import lock is touched on the last line of defense.

The startup announce is held to the SAME bar: a poisoned logging
lock at arm time would deadlock the watchdog thread *before* it
established its tripwire, and ``try/except`` cannot rescue a
blocked lock acquire. So startup, too, uses raw ``os.write``.

These invariants are STRUCTURAL — a future edit that re-introduces
``logging``/``SIGTERM``/``_atexit_fallback_write`` onto the watchdog
path would silently re-open the exact poison vector. The AST pins
below are the load-bearing defense; the subprocess test proves the
raw-OS kill path actually fires and actually kills.
"""
from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from backend.core.ouroboros.battle_test.harness import BattleTestHarness


_HARNESS_FILE = Path(inspect.getfile(BattleTestHarness))
_HARNESS_SRC = _HARNESS_FILE.read_text(encoding="utf-8")


def _watchdog_fn() -> ast.FunctionDef:
    """Return the AST node for
    ``_start_wall_clock_hard_deadline_thread`` — the method whose
    nested ``_watch`` closure is the entire watchdog thread body."""
    tree = ast.parse(_HARNESS_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "_start_wall_clock_hard_deadline_thread"
        ),
        None,
    )
    assert fn is not None, (
        "_start_wall_clock_hard_deadline_thread must exist"
    )
    return fn


def _watch_closure(fn: ast.FunctionDef) -> ast.FunctionDef:
    """Return the nested ``_watch`` thread-target closure."""
    watch = next(
        (
            n for n in ast.walk(fn)
            if isinstance(n, ast.FunctionDef) and n.name == "_watch"
        ),
        None,
    )
    assert watch is not None, (
        "_watch thread-target closure must exist inside "
        "_start_wall_clock_hard_deadline_thread"
    )
    return watch


# ---------------------------------------------------------------------------
# AST pins — resource-zero structural invariants
# ---------------------------------------------------------------------------


def test_ast_pin_panic_fd_dupped_at_arm_time():
    """The panic fd MUST be captured via ``os.dup(2)`` in the
    OUTER method body (arm time, pre-wedge), NOT inside ``_watch``
    (which only runs once the thread starts — too late if the
    process is already wedging)."""
    fn = _watchdog_fn()
    watch = _watch_closure(fn)
    assert watch.end_lineno is not None
    watch_lines = set(range(watch.lineno, watch.end_lineno + 1))

    dup_calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "dup"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "os"
    ]
    assert dup_calls, (
        "watchdog MUST capture a panic fd via os.dup(...) — without "
        "a pre-dup'd fd the kill path has no poison-free output "
        "channel"
    )
    # The os.dup() call site is OUTSIDE the _watch closure.
    assert any(
        c.lineno not in watch_lines for c in dup_calls
    ), (
        "os.dup() MUST run at arm time (outer method body), NOT "
        "inside _watch — capturing it after the thread starts "
        "races the wedge it is meant to survive"
    )
    # Source-level: it dups fd 2 (stderr) and assigns _wd_panic_fd.
    src = ast.get_source_segment(_HARNESS_SRC, fn)
    assert src is not None
    assert "os.dup(2)" in src, (
        "panic fd MUST be a raw dup of fd 2 (stderr)"
    )
    assert "_wd_panic_fd" in src, (
        "panic fd MUST be stored as self._wd_panic_fd for the kill "
        "path to reference"
    )


def test_ast_pin_watch_body_never_imports_or_calls_logging():
    """The ENTIRE ``_watch`` closure — startup announce AND kill
    path — MUST NOT import or call the logging module. The logging
    lock was the proven poison; a single ``logging`` touch anywhere
    on the thread re-opens the vector. ``try/except`` cannot rescue
    a deadlocked lock acquire."""
    watch = _watch_closure(_watchdog_fn())

    for node in ast.walk(watch):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            assert not any(
                (n or "").split(".")[0] == "logging" for n in names
            ), (
                f"_watch MUST NOT import logging (line "
                f"{node.lineno}) — re-opens the bt-2026-05-17-"
                f"024509 poison vector"
            )
        # No _wd_log.* / logging.* attribute calls.
        if isinstance(node, ast.Attribute) and isinstance(
            node.value, ast.Name
        ):
            assert node.value.id not in ("logging", "_wd_log"), (
                f"_watch MUST NOT call the logging module (line "
                f"{getattr(node, 'lineno', '?')}: "
                f"{node.value.id}.{node.attr})"
            )


def test_ast_pin_watch_body_never_sends_sigterm_or_uses_signal_handler():
    """The watchdog path MUST NOT send SIGTERM/SIGINT/SIGHUP to
    itself, nor route through the asyncio signal handler. The kill
    is SIGKILL only — the one signal the wedged interpreter cannot
    swallow."""
    watch = _watch_closure(_watchdog_fn())
    src = ast.get_source_segment(_HARNESS_SRC, watch)
    assert src is not None

    for forbidden in ("SIGTERM", "SIGINT", "SIGHUP"):
        for node in ast.walk(watch):
            if isinstance(node, ast.Attribute) and node.attr == forbidden:
                raise AssertionError(
                    f"_watch MUST NOT reference signal.{forbidden} — "
                    f"the resource-zero kill is SIGKILL only "
                    f"(line {getattr(node, 'lineno', '?')})"
                )
    # The only os.kill(...) on the path targets SIGKILL.
    kill_calls = [
        n for n in ast.walk(watch)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "kill"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "os"
    ]
    assert kill_calls, "_watch MUST issue an os.kill(...) SIGKILL"
    assert "signal.SIGKILL" in src, (
        "the kill MUST be signal.SIGKILL (module-level import, not "
        "an inner import — no import lock on the last line of "
        "defense)"
    )
    # Defensive: no inner `import signal` on the kill path.
    for node in ast.walk(watch):
        if isinstance(node, ast.Import):
            assert not any(
                a.name == "signal" for a in node.names
            ), (
                "_watch MUST NOT `import signal` inside the closure "
                "— reference the module-level import; an inner "
                "import touches the import lock on the kill path"
            )


def test_ast_pin_watch_body_never_calls_atexit_fallback_write():
    """The watchdog path MUST NOT call ``_atexit_fallback_write``.
    That method acquires the logging lock (and does file I/O) — the
    old Layer 4 called it before ``os._exit`` and the postmortem
    proved it blocked. ``debug.log`` is the canonical session
    record per CLAUDE.md; a partial summary is not worth a
    poisonable blocker on the last line of defense."""
    watch = _watch_closure(_watchdog_fn())
    for node in ast.walk(watch):
        if isinstance(node, ast.Attribute):
            assert node.attr != "_atexit_fallback_write", (
                f"_watch MUST NOT call _atexit_fallback_write "
                f"(line {getattr(node, 'lineno', '?')}) — it is "
                f"logging-lock-poisonable; Phase D deletes the "
                f"Layer-4 summary-before-exit contract for the "
                f"watchdog path"
            )


def test_ast_pin_kill_uses_raw_os_write_kill_exit_triplet():
    """The resource-zero kill MUST use the raw-OS triplet:
    ``os.write`` (diagnostic) → ``os.kill(..., SIGKILL)`` →
    ``os._exit(137)``. All three are unblockable by a poisoned
    process; the ordering guarantees a diagnostic byte even if
    SIGKILL is somehow deferred, and ``os._exit`` is the absolute
    backstop."""
    watch = _watch_closure(_watchdog_fn())
    kill_fn = next(
        (
            n for n in ast.walk(watch)
            if isinstance(n, ast.FunctionDef)
            and n.name == "_resource_zero_kill"
        ),
        None,
    )
    assert kill_fn is not None, (
        "_resource_zero_kill closure must exist"
    )
    src = ast.get_source_segment(_HARNESS_SRC, kill_fn)
    assert src is not None
    assert "os.write(" in src, "kill MUST emit a raw os.write diag"
    assert "os.kill(os.getpid()" in src, (
        "kill MUST os.kill its own pid"
    )
    assert "signal.SIGKILL" in src, "kill MUST use SIGKILL"
    assert "os._exit(137)" in src, (
        "kill MUST os._exit(137) as the absolute backstop "
        "(128 + SIGKILL)"
    )
    # Ordering: write precedes kill precedes exit.
    assert (
        src.index("os.write(")
        < src.index("os.kill(")
        < src.index("os._exit(")
    ), "kill triplet MUST be ordered write → kill → exit"


def test_ast_pin_resource_zero_tripwire_runs_before_poisonable_calls():
    """The hard-kill tripwire MUST be the FIRST statement inside the
    ``while`` loop — before ``call_soon_threadsafe`` or any other
    call that could block on a poisoned resource. If a wedge lets
    the loop keep spinning past the hard-kill deadline, it must die
    on raw clocks, unblockably."""
    watch = _watch_closure(_watchdog_fn())
    while_node = next(
        (n for n in ast.walk(watch) if isinstance(n, ast.While)),
        None,
    )
    assert while_node is not None, (
        "_watch MUST contain the Layer-2 while loop"
    )
    first = while_node.body[0]
    src = ast.get_source_segment(_HARNESS_SRC, first)
    assert src is not None
    assert "hard_kill_monotonic" in src and "hard_kill_wall" in src, (
        "the FIRST statement in the watchdog while loop MUST be the "
        "dual-clock hard-kill tripwire (raw time.monotonic()/"
        "time.time() vs hard_kill_*), so a poisoned wedge cannot "
        "outlive it"
    )


def test_ast_pin_dead_escalation_knobs_retired():
    """Phase D collapsed Layers 3 & 4 — the env knobs that tuned
    them (``JARVIS_WALL_CLOCK_ESCALATION_SIGTERM_S`` /
    ``..._EXIT_S``) MUST be retired. A tunable that does nothing is
    operator-misleading dead config; keeping it would imply the
    poison path still exists."""
    dead = {
        "JARVIS_WALL_CLOCK_ESCALATION_SIGTERM_S",
        "JARVIS_WALL_CLOCK_ESCALATION_EXIT_S",
    }
    # AST-scoped: a string *Constant* (a live os.environ.get arg)
    # is the regression; an explanatory *comment* naming the
    # retired knob is desirable provenance. ast.walk never sees
    # comments, so this distinguishes the two correctly.
    fn = _watchdog_fn()
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(
            node.value, str
        ):
            assert node.value not in dead, (
                f"{node.value!r} appears as a live string literal "
                f"(line {getattr(node, 'lineno', '?')}) — it tuned "
                f"the deleted Layer 3/4 poison path; a no-op knob "
                f"misleads operators. Name it only in a comment."
            )


def test_ast_pin_hard_kill_margin_env_knob_present_and_bounded():
    """The resource-zero margin MUST stay operator-tunable (no
    hardcoding) and bounded (no pathological values)."""
    fn = _watchdog_fn()
    src = ast.get_source_segment(_HARNESS_SRC, fn)
    assert src is not None
    assert "JARVIS_WALL_CLOCK_HARD_KILL_MARGIN_S" in src, (
        "the resource-zero grace MUST be env-tunable via "
        "JARVIS_WALL_CLOCK_HARD_KILL_MARGIN_S — no hardcoding"
    )
    # Bounded: a max(floor, min(ceiling, ...)) clamp is present.
    assert "hard_kill_margin_s = max(" in src and "min(300.0" in src, (
        "the margin MUST be clamped (floor 5s / ceiling 300s) so a "
        "fat-fingered env value cannot disable or pathologically "
        "delay the last line of defense"
    )


# ---------------------------------------------------------------------------
# Behavioral proof — the raw-OS kill path actually fires and kills.
# Runs in a subprocess because the path terminates the interpreter
# (os.kill SIGKILL / os._exit). Drives the REAL shipped function
# bound to a minimal shim so no heavy harness boot is needed and the
# exact production code path is exercised.
# ---------------------------------------------------------------------------


_SUBPROC_DRIVER = textwrap.dedent(
    """
    import asyncio, os, sys, time
    from backend.core.ouroboros.battle_test.harness import (
        BattleTestHarness,
    )

    # An event loop exists but is NEVER run — call_soon_threadsafe
    # enqueues and never drains, exactly modelling the wedged-loop
    # scenario. The clean summary path therefore never wins, so the
    # bounded grace expires and the resource-zero SIGKILL must fire.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    class _Shim:
        # Only the attributes the shipped method reads / writes.
        _wd_panic_fd = 2
        _stop_reason = "unknown"
        _wall_clock_event = asyncio.Event()
        _wall_clock_hard_deadline_stop = None
        _wall_clock_hard_deadline_thread = None

    shim = _Shim()
    # Bind and invoke the REAL shipped function.
    bound = BattleTestHarness._start_wall_clock_hard_deadline_thread
    # cap_s=0.0 → deadline already in the past → Layer 2 fires at
    # once; floor-clamped grace + margin give a few seconds before
    # the resource-zero SIGKILL.
    bound(shim, 0.0)
    # Main thread parks. With no running loop the clean path can
    # never win, so the watchdog WILL SIGKILL this process.
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        time.sleep(0.25)
    # If we ever get here the watchdog failed to kill us.
    print("WATCHDOG-DID-NOT-FIRE", file=sys.stderr)
    sys.exit(99)
    """
)


def test_resource_zero_kill_actually_sigkills_the_process():
    """End-to-end: the shipped watchdog, driven with a zero cap and
    floor-clamped grace/margin, MUST terminate the process via
    SIGKILL (returncode -9) and MUST emit its resource-zero
    diagnostic on the pre-dup'd fd 2 — never through logging."""
    env = dict(os.environ)
    # Floor-clamp both windows so the kill fires fast but still
    # exercises the real Layer-2 → bounded-grace → SIGKILL sequence.
    env["JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S"] = "5"
    env["JARVIS_WALL_CLOCK_HARD_KILL_MARGIN_S"] = "5"

    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _SUBPROC_DRIVER],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    # SIGKILL on a process surfaces as returncode == -9 (killed by
    # signal 9). os._exit(137) is only reached if os.kill somehow
    # did not terminate — both are acceptable proofs the path fired.
    assert proc.returncode in (-9, 137), (
        f"expected SIGKILL (-9) or os._exit(137); got "
        f"returncode={proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "WATCHDOG-DID-NOT-FIRE" not in proc.stderr, (
        "watchdog thread failed to kill the wedged process"
    )
    # The diagnostic came out the raw fd-2 panic channel.
    assert "RESOURCE-ZERO HARD KILL" in proc.stderr, (
        "resource-zero kill MUST emit its os.write diagnostic on "
        f"the pre-dup'd fd 2; stderr={proc.stderr!r}"
    )
    # And it announced itself resource-zero at startup — also via
    # raw os.write, never logging.
    assert "resource-zero hard-deadline thread alive" in proc.stderr, (
        "startup announce MUST use raw os.write (severed from "
        f"logging); stderr={proc.stderr!r}"
    )


def test_panic_fd_is_a_real_dup_of_stderr_at_arm_time():
    """In-process: arming the watchdog MUST set ``self._wd_panic_fd``
    to a *distinct* fd (a real ``os.dup`` of 2), not literal 2 —
    proving the channel survives a later ``close``/reassignment of
    the original stderr. The thread is immediately stopped so the
    test process is never killed."""
    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        class _Shim:
            _wd_panic_fd = 2
            _stop_reason = "unknown"
            _wall_clock_event = asyncio.Event()
            _wall_clock_hard_deadline_stop = None
            _wall_clock_hard_deadline_thread = None

        shim = _Shim()
        # Large cap so neither Layer 2 nor the tripwire fires during
        # the test; we only assert the arm-time dup happened.
        BattleTestHarness._start_wall_clock_hard_deadline_thread(
            shim, 10_000.0,  # type: ignore[arg-type]  # duck-typed shim
        )
        try:
            assert isinstance(shim._wd_panic_fd, int)
            # os.dup() returns a NEW descriptor (> 2 in practice);
            # if dup failed the code falls back to literal 2 — assert
            # the happy path produced a distinct, writable fd.
            assert shim._wd_panic_fd != 2, (
                "panic fd should be a fresh os.dup(2), distinct from "
                "the original stderr fd"
            )
            os.write(shim._wd_panic_fd, b"")  # writable / valid fd
        finally:
            # Stop the watchdog thread so the test process survives.
            stop = shim._wall_clock_hard_deadline_stop
            assert isinstance(stop, threading.Event)
            stop.set()
            t = shim._wall_clock_hard_deadline_thread
            if isinstance(t, threading.Thread):
                t.join(timeout=10)
                assert not t.is_alive(), (
                    "watchdog thread MUST honour the stop event"
                )
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Provenance pin — postmortem discoverability
# ---------------------------------------------------------------------------


def test_phase_d_cites_root_cause_session_in_source():
    """The Phase D block MUST cite the diagnosing session
    bt-2026-05-17-024509 so a future reader can trace the poison
    that motivated the resource-zero collapse."""
    assert "bt-2026-05-17-024509" in _HARNESS_SRC, (
        "Phase D source MUST cite bt-2026-05-17-024509 — the "
        "session whose wedged logging lock + swallowed SIGTERM "
        "exposed the shared-resource kill path"
    )
    assert "resource-zero" in _HARNESS_SRC.lower(), (
        "Phase D source MUST name the 'resource-zero' contract for "
        "discoverability"
    )
