"""Worker Lifeline — in-worker survival sentinel for spawn workers.

THE INCIDENT (2026-07-11 OOM RCA): an Oracle IPC spawn worker outlived
its organism by 29 hours at a 33.9 GB phys_footprint (7 MB rss — the
compressor had its pages, so every rss-scoped monitor read it as
healthy). Three such orphans totalled ~63 GB at the jetsam moment and
took the host down mid-soak. The worker's two intended exits both have
structural holes:

  * ``daemon=True`` — multiprocessing only terminates daemon children
    on a GRACEFUL parent exit; SIGKILL / jetsam / wedged teardown skip
    it entirely.
  * pipe-EOF — only observed when the loop is back at ``recv()``; a
    worker busy inside a long index/crawl call never sees it.

The lifeline closes the class from INSIDE the worker with a daemon
sentinel thread that consults kernel truths only (Watchdog Isolation
Invariant — it must never share state with the loop it guards):

  1. **Parent-death**: ``os.getppid()`` drift from the value captured
     at arm time (covers reparenting to launchd/pid-1 AND to any
     subreaper) → immediate ``os._exit(EXIT_CODE_ORPHANED)``.
  2. **Footprint self-cap**: own ``phys_footprint`` (compression-aware
     probe shared with ``process_tree_probe`` — zero duplication; rss
     fallback off-darwin) over an adaptive budget → immediate
     ``os._exit(EXIT_CODE_FOOTPRINT_CAP)``. The parent's proxy
     machinery (``AsyncOracleProxy`` crash/respawn, pool respawn)
     already treats worker death as a crash and rebuilds — worker
     state is a cache by design, so self-sacrifice IS the checkpoint.

Budgets are host-relative, never hardcoded:
``JARVIS_WORKER_FOOTPRINT_CAP_MB`` absolute override, else
``JARVIS_WORKER_FOOTPRINT_CAP_FRACTION`` (default 0.25) of total RAM;
no override + unknowable RAM → the footprint check stays disarmed
(parent-death check always arms). Master switch
``JARVIS_WORKER_LIFELINE_ENABLED`` (default true);
``JARVIS_WORKER_LIFELINE_INTERVAL_S`` cadence (default 10s, clamped
[1, 120]).
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Callable, Optional, Tuple

# Distinct from EXIT_CODE_HARNESS_WEDGED=75 (shutdown_watchdog) so a
# reaped worker's cause is readable straight off the exit status.
EXIT_CODE_ORPHANED = 86
EXIT_CODE_FOOTPRINT_CAP = 87

_ARM_LOCK = threading.Lock()
_ARMED_THREAD: Optional[threading.Thread] = None


def _env_float(name: str) -> Optional[float]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def lifeline_enabled() -> bool:
    return os.environ.get(
        "JARVIS_WORKER_LIFELINE_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def _interval_s() -> float:
    raw = _env_float("JARVIS_WORKER_LIFELINE_INTERVAL_S") or 10.0
    return max(1.0, min(120.0, raw))


def resolve_footprint_cap_mb() -> Optional[float]:
    """Adaptive worker memory budget in MB, or None = check disarmed.

    Mirrors the ProcessMemoryWatchdog threshold discipline
    (harness ``_resolve_process_memory_thresholds``): absolute env
    override wins; otherwise a fraction of total system RAM so the
    same code protects a 16 GB laptop and a 256 GB box; if total RAM
    is unknowable and no override exists, stay disarmed rather than
    invent a number.
    """
    cap = _env_float("JARVIS_WORKER_FOOTPRINT_CAP_MB")
    if cap is not None:
        return cap
    frac = _env_float("JARVIS_WORKER_FOOTPRINT_CAP_FRACTION")
    frac = frac if frac is not None else 0.25
    frac = max(0.05, min(0.90, frac))
    try:
        import psutil
        total_mb = psutil.virtual_memory().total / (1024.0 * 1024.0)
        return total_mb * frac
    except Exception:  # noqa: BLE001 — psutil missing / probe failed
        return None


def _probe_self_memory_mb() -> Optional[float]:
    """Own footprint (darwin, compression-aware) with rss fallback.

    Reuses ``process_tree_probe``'s per-pid primitive — the single
    source of truth for the footprint syscall — self-scoped because a
    lifeline guards exactly one worker (Oracle workers force their AST
    helper in-process; FS pool workers spawn nothing).
    """
    try:
        from backend.core.ouroboros.governance.process_tree_probe import (
            _probe_pid_footprint_mb,
        )
        mb = _probe_pid_footprint_mb(os.getpid())
        if mb is not None:
            return mb
    except Exception:  # noqa: BLE001 — fall through to rss
        pass
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024.0 * 1024.0)
    except Exception:  # noqa: BLE001
        return None


def _tick_decision(
    armed_ppid: int,
    current_ppid: int,
    footprint_mb: Optional[float],
    cap_mb: Optional[float],
) -> Optional[Tuple[int, str]]:
    """Pure decision core — (exit_code, reason) or None = healthy.

    Parent-death outranks the footprint check: an orphan must die even
    when its memory is modest (it only gets bigger — proven 29h live).
    """
    if current_ppid != armed_ppid:
        return (
            EXIT_CODE_ORPHANED,
            f"orphaned: ppid drifted {armed_ppid} -> {current_ppid}",
        )
    if (
        cap_mb is not None
        and footprint_mb is not None
        and footprint_mb > cap_mb
    ):
        return (
            EXIT_CODE_FOOTPRINT_CAP,
            f"footprint_cap: {footprint_mb:.0f}MB > {cap_mb:.0f}MB",
        )
    return None


def _lifeline_loop(
    role: str,
    armed_ppid: int,
    cap_mb: Optional[float],
    interval_s: float,
    exit_fn: Callable[[int], None],
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(interval_s):
        try:
            verdict = _tick_decision(
                armed_ppid,
                os.getppid(),
                _probe_self_memory_mb(),
                cap_mb,
            )
        except Exception:  # noqa: BLE001 — a broken probe must not kill
            continue
        if verdict is None:
            continue
        code, reason = verdict
        try:
            sys.stderr.write(
                f"[WorkerLifeline] role={role} pid={os.getpid()} "
                f"{reason} -> os._exit({code})\n"
            )
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 — the exit MUST still fire
            pass
        exit_fn(code)
        return  # only reachable with an injected (test) exit_fn


def arm_worker_lifeline(
    role: str = "worker",
    *,
    expected_parent_pid: Optional[int] = None,
    exit_fn: Callable[[int], None] = os._exit,
    interval_s: Optional[float] = None,
) -> Optional[threading.Thread]:
    """Arm the sentinel in THIS process. Idempotent; returns the
    daemon thread, or None when disabled via env.

    ``expected_parent_pid`` — the parent captures ``os.getpid()`` at
    spawn time and ships it into the worker (pool ``initargs``, spawn
    ``args``). This closes the arm-after-parent-death race: a spawn
    worker that boots AFTER its parent died would otherwise capture
    the REPARENTED ppid as its baseline and never detect orphanhood.
    (A raw ``ppid == 1`` heuristic is deliberately avoided — in the
    IsomorphicEnv container mode the organism legitimately runs as
    PID 1.) When omitted, falls back to ``os.getppid()`` at arm time.
    ``exit_fn`` / ``interval_s`` are injection seams for tests only."""
    global _ARMED_THREAD
    if not lifeline_enabled():
        return None
    with _ARM_LOCK:
        if _ARMED_THREAD is not None and _ARMED_THREAD.is_alive():
            return _ARMED_THREAD
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_lifeline_loop,
            args=(
                role,
                expected_parent_pid
                if expected_parent_pid is not None
                else os.getppid(),
                resolve_footprint_cap_mb(),
                interval_s if interval_s is not None else _interval_s(),
                exit_fn,
                stop_event,
            ),
            name=f"worker-lifeline-{role}",
            daemon=True,
        )
        thread._lifeline_stop = stop_event  # type: ignore[attr-defined]
        thread.start()
        _ARMED_THREAD = thread
        return thread


def pool_worker_initializer(
    role: str = "fs_process_pool",
    expected_parent_pid: Optional[int] = None,
) -> None:
    """Module-level (spawn-picklable) ``ProcessPoolExecutor``
    initializer — arms the lifeline in every pool worker. The pool
    owner passes ``initargs=(role, os.getpid())`` so the baseline
    parent pid is authoritative even if a worker boots late. Fail-
    soft: an initializer exception would poison the whole pool, so it
    never raises."""
    try:
        arm_worker_lifeline(role, expected_parent_pid=expected_parent_pid)
    except Exception:  # noqa: BLE001
        pass
