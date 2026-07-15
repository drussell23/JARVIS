"""Child-process reaper — deterministic cascade teardown for spawned children.

Closes the orphan gap the 2026-07-15 verification soak exposed: the Aegis
daemon is a plain ``subprocess.Popen`` child with NO WorkerLifeline armed, so
when the organism dies (SIGKILL by the external watchdog, or LoopDeadman's
``os._exit(75)``) it survives forever polling DoubleWord — one instance ran
9.6 h, and a graveyard of 14 orphaned aegis daemons + mp workers accumulated
over two days of soaks. ``WorkerLifeline`` covers POOL workers (ppid-drift
self-exit) but never the aegis daemon, and nothing cascades on the parent side.

This module is a MINIMAL, signal-safe PID registry + cascade — NOT a threading
supervisor and NOT a second lifecycle owner. The contract:

  * A component that spawns a long-lived child (the Aegis daemon today)
    registers its PID via :func:`register_child`.
  * Every teardown seam — the harness ``_shutdown_components`` (graceful),
    ``LoopDeadman._fire_wedge`` just before ``os._exit(75)`` (hard, in-process),
    and an ``atexit`` fallback — calls :func:`cascade_terminate`, which
    SIGTERM→(grace)→SIGKILLs every still-live registered child.

Defense-in-depth composes with, does not replace, the per-child WorkerLifeline
(Slice 22 also arms it inside the aegis daemon): the reaper is the fast,
parent-driven path; the lifeline is the backstop for the pure-SIGKILL-of-parent
case where no parent code runs at all.

DISCIPLINE — this must be callable from LoopDeadman's daemon thread microseconds
before ``os._exit``: it uses only ``os.kill`` + a bounded ``time.sleep`` grace,
holds its lock only to snapshot the registry (never across a kill), and NEVER
raises. No logging lock is taken on the hard path (best-effort stderr only).
"""
from __future__ import annotations

import os
import signal
import threading
import time
from typing import Dict, Tuple

# role string per pid — purely for observability in the teardown log line.
_children: Dict[int, str] = {}
_lock = threading.Lock()

# Env: seconds to wait between the SIGTERM sweep and the SIGKILL sweep on the
# GRACEFUL path. The hard path (LoopDeadman pre-exit) passes grace_s=0 — we are
# already dying, so escalate immediately. Bounded so a wedged teardown can't
# hang here. NEVER hardcoded into the kill logic; resolved at call time.
_ENV_GRACE_S = "JARVIS_CHILD_REAPER_GRACE_S"
_DEFAULT_GRACE_S = 2.0


def _resolve_grace_s() -> float:
    try:
        raw = os.environ.get(_ENV_GRACE_S, "").strip()
        if not raw:
            return _DEFAULT_GRACE_S
        return max(0.0, min(30.0, float(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_GRACE_S


def register_child(pid: int, role: str = "child") -> None:
    """Register a spawned child PID for cascade teardown. Idempotent.
    NEVER raises."""
    try:
        p = int(pid)
        if p <= 0:
            return
        with _lock:
            _children[p] = str(role)[:40]
    except Exception:  # noqa: BLE001 — registration must never break a spawn
        pass


def unregister_child(pid: int) -> None:
    """Drop a PID (child reaped/adopted elsewhere). Idempotent. NEVER raises."""
    try:
        with _lock:
            _children.pop(int(pid), None)
    except Exception:  # noqa: BLE001
        pass


def registered_children() -> Tuple[Tuple[int, str], ...]:
    """Immutable snapshot of (pid, role). Test/observability. NEVER raises."""
    try:
        with _lock:
            return tuple(_children.items())
    except Exception:  # noqa: BLE001
        return ()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass  # already gone / not ours


def cascade_terminate(
    *,
    grace_s: float | None = None,
    hard: bool = False,
) -> int:
    """SIGTERM→(grace)→SIGKILL every registered child that is still alive.

    Returns the number of PIDs that received at least one signal. Reaped PIDs
    are unregistered. Signal-safe + NEVER raises — safe to call from the
    LoopDeadman daemon thread immediately before ``os._exit``.

    ``hard=True`` (LoopDeadman path): skip the grace window and SIGKILL after a
    single SIGTERM pass with no sleep — we are already exiting, correctness is
    "no survivor", not "clean shutdown". ``grace_s`` overrides the env default
    on the graceful path.
    """
    try:
        with _lock:
            targets = list(_children.items())
    except Exception:  # noqa: BLE001
        targets = []
    if not targets:
        return 0

    # Pass 1 — polite SIGTERM to everything alive.
    signalled = 0
    still: list = []
    for pid, role in targets:
        if _alive(pid):
            _signal(pid, signal.SIGTERM)
            signalled += 1
            still.append((pid, role))
    try:
        _stderr(
            f"[ChildReaper] cascade: SIGTERM {signalled} child(ren) "
            f"{[f'{p}:{r}' for p, r in still]}"
        )
    except Exception:  # noqa: BLE001
        pass

    # Grace — bounded; hard path skips it entirely.
    g = 0.0 if hard else (grace_s if grace_s is not None else _resolve_grace_s())
    if g > 0.0 and still:
        try:
            time.sleep(g)
        except Exception:  # noqa: BLE001
            pass

    # Pass 2 — SIGKILL survivors, then unregister everything we handled.
    for pid, _role in still:
        if _alive(pid):
            _signal(pid, signal.SIGKILL)
    for pid, _role in targets:
        unregister_child(pid)
    return signalled


def _stderr(msg: str) -> None:
    """Best-effort stderr write — no logging framework (its lock could deadlock
    on the hard path). NEVER raises."""
    try:
        import sys
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


_atexit_armed = False


def arm_atexit_cascade() -> None:
    """Register :func:`cascade_terminate` as an atexit fallback. Idempotent.
    Covers ordinary interpreter shutdown; the harness signal handlers +
    LoopDeadman pre-exit hook cover the paths atexit cannot. NEVER raises."""
    global _atexit_armed
    try:
        with _lock:
            if _atexit_armed:
                return
            _atexit_armed = True
        import atexit
        atexit.register(lambda: cascade_terminate())
    except Exception:  # noqa: BLE001
        pass


def reset_for_tests() -> None:
    try:
        with _lock:
            _children.clear()
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "arm_atexit_cascade",
    "cascade_terminate",
    "register_child",
    "registered_children",
    "reset_for_tests",
    "unregister_child",
]
