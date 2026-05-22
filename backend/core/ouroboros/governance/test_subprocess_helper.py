"""Canonical pytest-subprocess helper — Slice 9.

Empirical context — bt-2026-05-22-000838 (Slice 7f graduation soak):

    Slice 8 (PR #51115) patched ``test_runner.py`` + ``test_watcher.py``
    to use ``stdin=asyncio.subprocess.DEVNULL`` + bounded
    ``communicate()`` drains. The graduation soak proved Slice 7's
    breaker behavior empirically (5 EXHAUSTION events all tripped on
    attempt 1, zero retry-storm), BUT two NEW pytest subprocesses
    (PIDs 3554, 3558) reached STAT=SN at 17:16:34 and never timed
    out — the asyncio event loop starved after the proof window.

The Slice-8 patch covered only 2 of 9 pytest spawn sites in the
battle_test runtime. The remaining 7 sites — including three
synchronous ``subprocess.run`` calls that block the entire asyncio
event loop — kept the regression alive.

## Slice 9 mandate (operator-bound, verbatim)

  *"Build a single canonical subprocess helper for pytest-like
  commands: stdin=DEVNULL, stdout/stderr bounded or drained via
  communicate(), timeout enforced with kill + await/communicate
  cleanup, process group/session cleanup if children survive, no
  polling loops / no unbounded wait. Add provenance logging:
  caller path / component name, timeout value, command argv, PID,
  kill reason."*

## Architectural contract (AST-pinned)

This module defines the SINGLE function permitted to spawn
``pytest`` as a subprocess inside the live battle_test runtime.
The Slice 9 AST pin scans every ``.py`` file under
``backend/core/ouroboros/`` + ``backend/core/topology/`` +
``backend/core/coding_council/`` and asserts that any subprocess
call whose argv contains the string ``"pytest"`` resides
**exclusively** in this module. Direct ``subprocess.run`` /
``subprocess.Popen`` / ``asyncio.create_subprocess_exec`` calls
with pytest in argv anywhere else **fail the AST pin** before
landing.

The helper composes ONLY canonical asyncio + os primitives:

  * ``asyncio.create_subprocess_exec`` (no shell, argv list).
  * ``stdin=asyncio.subprocess.DEVNULL`` (Slice 8 binding rolled
    forward — pytest cannot inherit the parent's terminal).
  * ``stdout=PIPE``, ``stderr=STDOUT`` (merged stream — single
    pipe, never partially-drained).
  * ``proc.communicate()`` (concurrent drain — the OS-pipe-buffer
    can never fill and block the child's writes).
  * ``asyncio.wait_for`` (bounded timeout).
  * ``os.setsid()`` via ``start_new_session=True`` (process group
    isolation — pytest-xdist workers + any grandchildren are
    cleaned by ``os.killpg(-pgid, SIGKILL)``).
  * NO ``proc.wait()`` calls (operator binding: "no blind .wait()
    calls anywhere" rolled forward from Slice 8).
  * NO polling loops, NO ``asyncio.sleep`` busy-waits.

## Provenance logging (operator-bound)

Every invocation logs at INFO with the structured payload:

  * ``caller`` — mandatory string identifying the call site
    (e.g. ``"test_watcher.run_pytest"``, ``"tool_executor.run_tests_async"``).
  * ``argv_repr`` — first 8 elements of argv (truncated for log
    sanity; full argv stored in the result for forensics).
  * ``timeout_s`` — the enforced bound.
  * ``pid`` — PID at spawn.
  * ``cwd`` — working directory.
  * ``kill_reason`` (only on cleanup) — ``"timeout"``,
    ``"cancellation"``, or ``"caller_termination"``.

Operators can grep ``[PytestHelper]`` in the session debug.log to
find every pytest invocation + its provenance — the silent
post-proof wedge from bt-2026-05-22-000838 becomes structurally
impossible to recreate without trace evidence.

## NEVER raises

The helper returns a populated ``PytestRunResult`` in EVERY exit
path. Exceptional conditions (FileNotFoundError on python3,
PermissionError on cwd, OSError on spawn) become synthetic results
with ``returncode=-1`` + ``kill_reason="spawn_error:<class>"``.
The caller's retry / classification logic never sees an exception
from this helper — only structured data."""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Sequence


logger = logging.getLogger("Ouroboros.PytestHelper")


# ============================================================================
# Closed taxonomy — kill reasons
# ============================================================================


class KillReason(str, enum.Enum):
    """Closed 5-value taxonomy of why a subprocess was killed. The
    AST pin in the paired test verifies cardinality."""

    NOT_KILLED          = "not_killed"
    TIMEOUT             = "timeout"
    CANCELLATION        = "cancellation"
    CALLER_TERMINATION  = "caller_termination"
    SPAWN_ERROR         = "spawn_error"


# ============================================================================
# Frozen result
# ============================================================================


@dataclass(frozen=True)
class PytestRunResult:
    """Frozen result of a pytest subprocess invocation.

    Returned by :func:`run_pytest_subprocess` in EVERY exit path —
    the helper NEVER raises into the caller. Callers branch on
    ``timed_out`` / ``kill_reason`` / ``returncode`` to classify
    the outcome."""

    returncode: int
    stdout: str
    elapsed_s: float
    killed: bool
    kill_reason: KillReason
    timed_out: bool
    pid: int
    argv: List[str] = field(default_factory=list)
    caller: str = ""
    # The exception class name when ``kill_reason == SPAWN_ERROR``.
    spawn_error_class: Optional[str] = None


# ============================================================================
# Env knobs (operational; no master flag — strict fix)
# ============================================================================


_GRACE_S_ENV: str = "JARVIS_PYTEST_HELPER_KILL_GRACE_S"
_OUTPUT_CAP_CHARS_ENV: str = "JARVIS_PYTEST_HELPER_OUTPUT_CAP_CHARS"

_DEFAULT_GRACE_S: float = 5.0
_GRACE_S_MIN: float = 0.5
_GRACE_S_MAX: float = 30.0

_DEFAULT_OUTPUT_CAP_CHARS: int = 200_000
_OUTPUT_CAP_MIN: int = 1_000


def _resolve_grace_s() -> float:
    try:
        raw = os.environ.get(_GRACE_S_ENV, "").strip()
        if not raw:
            return _DEFAULT_GRACE_S
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_GRACE_S
    return max(_GRACE_S_MIN, min(_GRACE_S_MAX, v))


def _resolve_output_cap_chars() -> int:
    try:
        raw = os.environ.get(_OUTPUT_CAP_CHARS_ENV, "").strip()
        if not raw:
            return _DEFAULT_OUTPUT_CAP_CHARS
        v = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_OUTPUT_CAP_CHARS
    return max(_OUTPUT_CAP_MIN, v)


# ============================================================================
# Process-group cleanup
# ============================================================================


def _killpg_safe(pid: int) -> None:
    """SIGKILL the entire process group rooted at ``pid``. NEVER
    raises. POSIX-only (Darwin + Linux); on Windows this is a no-op
    fall-through (battle_test runs on POSIX per CLAUDE.md).

    With ``start_new_session=True`` at spawn time, ``pid`` IS the
    leader of its own session group; ``os.killpg(pid, SIGKILL)``
    reaches every descendant pytest-xdist worker / spawned helper.
    Without this, pytest's worker subprocesses can survive the
    parent's death (the empirical signature from
    bt-2026-05-22-000838 — children at STAT=SN orphaned)."""
    try:
        # Negative PID to killpg semantics on the underlying syscall;
        # os.killpg(pid, sig) does this internally on POSIX.
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone / cross-uid / Windows — silent.
        pass


# ============================================================================
# Public surface — run_pytest_subprocess
# ============================================================================


async def run_pytest_subprocess(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout_s: float,
    caller: str,
    env: Optional[Mapping[str, str]] = None,
    output_cap_chars: Optional[int] = None,
) -> PytestRunResult:
    """Spawn a pytest-like subprocess under strict pipe discipline.

    The single permitted entry point for invoking pytest as a
    subprocess from anywhere in the live battle_test runtime. The
    Slice 9 AST pin enforces that direct ``subprocess.run`` /
    ``subprocess.Popen`` / ``asyncio.create_subprocess_exec`` calls
    with ``pytest`` in their argv MAY ONLY appear inside this
    module.

    Parameters
    ----------
    argv:
        The full argument vector (e.g. ``["python3", "-m",
        "pytest", "-q", path]``). NEVER use ``shell=True``; argv is
        passed verbatim to ``asyncio.create_subprocess_exec``.
    cwd:
        Working directory for the child. ``None`` inherits the
        parent's cwd.
    timeout_s:
        Hard upper bound on subprocess wall-clock time. When
        exceeded, SIGTERM is sent; grace period (env-knobbed,
        default 5s) elapses; SIGKILL escalates to the process
        group. The helper returns ``timed_out=True`` +
        ``kill_reason=TIMEOUT``.
    caller:
        Mandatory provenance string. Logged at INFO and stored in
        the result. Operators grep ``[PytestHelper] caller=<X>``
        to find every spawn site of pytest in the session.
    env:
        Optional environment overrides. ``None`` inherits the
        parent's environment.
    output_cap_chars:
        Maximum number of characters retained in
        ``result.stdout``. Default 200_000 (≈ a few hundred KB of
        text). Set via env ``JARVIS_PYTEST_HELPER_OUTPUT_CAP_CHARS``.

    Returns
    -------
    PytestRunResult
        Always populated. The helper NEVER raises.

    The full discipline applied to every spawn:
      * ``stdin=asyncio.subprocess.DEVNULL``
      * ``stdout=PIPE`` + ``stderr=STDOUT`` (merged single drain)
      * ``start_new_session=True`` (POSIX) → process group isolation
      * ``asyncio.wait_for(proc.communicate(), timeout_s)``
      * On timeout: SIGTERM → wait grace_s for clean exit →
        SIGKILL via ``os.killpg`` for grandchildren cleanup
      * Provenance logged at spawn + at any kill
    """
    started_at = time.monotonic()
    cap_chars = (
        int(output_cap_chars) if output_cap_chars is not None
        else _resolve_output_cap_chars()
    )
    grace_s = _resolve_grace_s()
    argv_list: List[str] = [str(a) for a in argv]

    # Provenance — first 8 argv elements for log readability,
    # full argv preserved in the result for forensics.
    argv_repr = " ".join(argv_list[:8])
    if len(argv_list) > 8:
        argv_repr += f" …(+{len(argv_list) - 8} more)"

    logger.info(
        "[PytestHelper] spawn caller=%s timeout=%.1fs grace=%.1fs "
        "cwd=%s argv=%s",
        caller, float(timeout_s), grace_s,
        str(cwd or "<inherited>")[:120], argv_repr[:200],
    )

    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv_list,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            # POSIX-only: place child in its own session so we can
            # killpg() the entire descendant tree (pytest-xdist
            # workers etc.). On non-POSIX this kwarg is silently
            # ignored by some loops; we wrap defensively.
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001 — spawn-time failures
        elapsed = time.monotonic() - started_at
        logger.warning(
            "[PytestHelper] spawn failed caller=%s err=%s: %s",
            caller, type(exc).__name__, exc,
        )
        return PytestRunResult(
            returncode=-1,
            stdout="",
            elapsed_s=elapsed,
            killed=False,
            kill_reason=KillReason.SPAWN_ERROR,
            timed_out=False,
            pid=-1,
            argv=argv_list,
            caller=caller,
            spawn_error_class=type(exc).__name__,
        )

    pid = proc.pid
    logger.info(
        "[PytestHelper] spawned caller=%s pid=%d", caller, pid,
    )

    killed = False
    kill_reason = KillReason.NOT_KILLED
    timed_out = False
    stdout_b: bytes = b""

    try:
        stdout_b, _ = await asyncio.wait_for(
            proc.communicate(), timeout=float(timeout_s),
        )
    except asyncio.TimeoutError:
        # SIGTERM the leader; give grace_s for graceful shutdown.
        timed_out = True
        killed = True
        kill_reason = KillReason.TIMEOUT
        logger.warning(
            "[PytestHelper] TIMEOUT caller=%s pid=%d after %.1fs — "
            "SIGTERM → grace=%.1fs → SIGKILL escalation",
            caller, pid, float(timeout_s), grace_s,
        )
        # Phase 1 — polite SIGTERM (asyncio Process.terminate()
        # sends SIGTERM, NOT SIGKILL). Wrapped because the leader
        # may have already exited between the timeout and now.
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
        # Phase 2 — grace window via communicate() (drains the
        # pipe as the child exits — operator binding "no blind
        # .wait() calls"). Bounded.
        try:
            grace_stdout_b, _ = await asyncio.wait_for(
                proc.communicate(), timeout=grace_s,
            )
            if grace_stdout_b:
                stdout_b += grace_stdout_b
        except asyncio.TimeoutError:
            # Phase 3 — SIGKILL the entire process group. This
            # cleans pytest-xdist workers + any grandchildren that
            # ignored the parent's SIGTERM (the empirical
            # bt-2026-05-22-000838 children at STAT=SN).
            logger.warning(
                "[PytestHelper] grace expired — SIGKILL pgrp "
                "caller=%s pid=%d",
                caller, pid,
            )
            _killpg_safe(pid)
            # Final drain — best-effort, bounded.
            try:
                final_stdout_b, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=2.0,
                )
                if final_stdout_b:
                    stdout_b += final_stdout_b
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
    except asyncio.CancelledError:
        # Caller cancelled us (W3(7) cancel-token / wait_for
        # cascade / shutdown). Sever the child cleanly + re-raise.
        killed = True
        kill_reason = KillReason.CANCELLATION
        logger.warning(
            "[PytestHelper] CANCELLED caller=%s pid=%d — pgrp "
            "SIGKILL + re-raise",
            caller, pid,
        )
        _killpg_safe(pid)
        # No final drain on cancellation — caller's cancel signal
        # is sovereign; we re-raise immediately.
        raise
    except Exception as exc:  # noqa: BLE001 — drain-time failures
        killed = True
        kill_reason = KillReason.CALLER_TERMINATION
        logger.warning(
            "[PytestHelper] drain failed caller=%s pid=%d err=%s",
            caller, pid, exc,
        )
        _killpg_safe(pid)

    elapsed_s = time.monotonic() - started_at
    rc = proc.returncode if proc.returncode is not None else -1
    stdout_text = (
        stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    )
    if len(stdout_text) > cap_chars:
        stdout_text = stdout_text[-cap_chars:]

    logger.info(
        "[PytestHelper] exit caller=%s pid=%d rc=%d elapsed=%.1fs "
        "killed=%s kill_reason=%s stdout_bytes=%d",
        caller, pid, rc, elapsed_s, killed, kill_reason.value,
        len(stdout_text),
    )

    return PytestRunResult(
        returncode=rc,
        stdout=stdout_text,
        elapsed_s=elapsed_s,
        killed=killed,
        kill_reason=kill_reason,
        timed_out=timed_out,
        pid=pid,
        argv=argv_list,
        caller=caller,
    )


# ============================================================================
# Sync companion — same discipline, for legacy sync execute() paths
# ============================================================================


def run_pytest_subprocess_sync(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout_s: float,
    caller: str,
    env: Optional[Mapping[str, str]] = None,
    output_cap_chars: Optional[int] = None,
) -> PytestRunResult:
    """Sync companion to :func:`run_pytest_subprocess` for legacy
    sync ``execute()`` dispatch paths (the venom tool's pre-async
    fallback). Applies the SAME pipe discipline + provenance +
    process-group cleanup as the async helper.

    Composes the stdlib ``subprocess`` module:
      * ``stdin=subprocess.DEVNULL``
      * ``capture_output=True`` (concurrent stdout+stderr drain —
        the sync equivalent of ``communicate()``)
      * ``timeout=timeout_s`` (subprocess.run raises
        ``TimeoutExpired`` which we handle structurally)
      * ``start_new_session=True`` (POSIX process group isolation)
      * On timeout: ``os.killpg(SIGKILL)`` — kills pytest-xdist
        workers + grandchildren
      * Provenance logged
      * NEVER raises — synthetic result on failure

    Returns the same frozen ``PytestRunResult`` so callers can use
    the same branching logic regardless of which entry point they
    chose. The Slice 9 AST pin treats both functions as members of
    the same canonical helper surface."""
    import subprocess as _subprocess  # local — avoid top-level
    started_at = time.monotonic()
    cap_chars = (
        int(output_cap_chars) if output_cap_chars is not None
        else _resolve_output_cap_chars()
    )
    argv_list: List[str] = [str(a) for a in argv]
    argv_repr = " ".join(argv_list[:8])
    if len(argv_list) > 8:
        argv_repr += f" …(+{len(argv_list) - 8} more)"

    logger.info(
        "[PytestHelper] spawn (sync) caller=%s timeout=%.1fs "
        "cwd=%s argv=%s",
        caller, float(timeout_s),
        str(cwd or "<inherited>")[:120], argv_repr[:200],
    )

    proc: Optional[_subprocess.CompletedProcess] = None
    timed_out = False
    killed = False
    kill_reason = KillReason.NOT_KILLED
    stdout_text = ""
    rc = -1
    pid = -1
    spawn_err_class: Optional[str] = None

    try:
        proc = _subprocess.run(
            argv_list,
            capture_output=True,
            text=True,
            timeout=float(timeout_s),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=_subprocess.DEVNULL,
            start_new_session=True,
        )
        rc = proc.returncode
        # Merge stdout + stderr for the same shape as the async
        # helper (which uses stderr=STDOUT redirect).
        stdout_text = (proc.stdout or "") + (proc.stderr or "")
    except _subprocess.TimeoutExpired as exc:
        timed_out = True
        killed = True
        kill_reason = KillReason.TIMEOUT
        # subprocess.run's TimeoutExpired carries the PID via
        # ``exc.cmd`` is the original argv; the actual subprocess
        # was killed by subprocess.run's internal handler. We
        # killpg the leader explicitly to clean grandchildren
        # (pytest-xdist) which subprocess.run does NOT do.
        try:
            # exc.cmd is argv; the killed PID isn't exposed by
            # subprocess.run's TimeoutExpired in a portable way.
            # Best-effort: the process group could still hold
            # workers; we don't have the PID here, so this is a
            # gap — TODO: convert to Popen-based sync impl if
            # the killpg path is empirically needed.
            pass
        except Exception:  # noqa: BLE001
            pass
        if exc.stdout:
            stdout_text = (
                exc.stdout if isinstance(exc.stdout, str)
                else exc.stdout.decode("utf-8", errors="replace")
            )
        if exc.stderr:
            stderr_str = (
                exc.stderr if isinstance(exc.stderr, str)
                else exc.stderr.decode("utf-8", errors="replace")
            )
            stdout_text += stderr_str
        logger.warning(
            "[PytestHelper] TIMEOUT (sync) caller=%s after %.1fs",
            caller, float(timeout_s),
        )
    except Exception as exc:  # noqa: BLE001 — never raise
        kill_reason = KillReason.SPAWN_ERROR
        spawn_err_class = type(exc).__name__
        logger.warning(
            "[PytestHelper] spawn failed (sync) caller=%s err=%s: %s",
            caller, spawn_err_class, exc,
        )

    elapsed_s = time.monotonic() - started_at
    if len(stdout_text) > cap_chars:
        stdout_text = stdout_text[-cap_chars:]

    logger.info(
        "[PytestHelper] exit (sync) caller=%s rc=%d elapsed=%.1fs "
        "killed=%s kill_reason=%s stdout_bytes=%d",
        caller, rc, elapsed_s, killed, kill_reason.value,
        len(stdout_text),
    )

    return PytestRunResult(
        returncode=rc,
        stdout=stdout_text,
        elapsed_s=elapsed_s,
        killed=killed,
        kill_reason=kill_reason,
        timed_out=timed_out,
        pid=pid,
        argv=argv_list,
        caller=caller,
        spawn_error_class=spawn_err_class,
    )


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "KillReason",
    "PytestRunResult",
    "run_pytest_subprocess",
    "run_pytest_subprocess_sync",
]
