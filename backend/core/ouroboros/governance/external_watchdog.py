"""Slice 49 — External Subprocess Watchdog (GIL-immune terminal kill path).

v44 (bt-2026-05-31-002950) ran 73 min past a 40-min ``--max-wall-seconds``
cap because the in-process resource-zero watchdog — a Python thread — never
fired under FS-scan GIL contention. A watchdog that shares the interpreter
with the system it guards can be starved out. The fix is a SEPARATE OS
process: it cannot be GIL-starved by the parent by construction.

Architecture (aligned with the Slice 47 Watchdog Isolation Invariant — this
sentinel is even MORE isolated than the resource-zero thread, since it shares
no interpreter, GIL, logging lock, or signal queue with the parent):

  * The parent ``beat()``s a WALL timestamp into a heartbeat file each loop
    tick. (monotonic clocks are NOT comparable across processes, so the
    cross-process liveness signal must be wall-clock.)
  * The child sentinel polls the file with its OWN clocks and decides:
      - BUDGET kill — wall-authoritative: once total session wall exceeds the
        budget it SIGKILLs the parent regardless of GIL state. This is the
        backstop that should have killed v44.
      - STALENESS kill — suspend-aware: a stale heartbeat means a wedge ONLY
        if real time elapsed. A host sleep (wall jumps while the child's
        monotonic barely moves) must NOT be mistaken for a wedge (Slice 46).

This module is STDLIB-ONLY on purpose: the sentinel subprocess must start
fast and never depend on the (possibly wedged or un-importable) backend
package. It is both the importable/testable module AND the subprocess entry
point (``python external_watchdog.py --watch <pid> <hb> <budget> <stale>``).
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional, Tuple


def _suspend_detected(
    mono_delta: float, wall_delta: float, threshold: float,
) -> bool:
    """True if this poll interval looks like a host suspend.

    During suspend the wall clock keeps advancing (or jumps on resume) while
    the monotonic clock pauses, so ``wall_delta`` greatly exceeds
    ``mono_delta``. Genuine elapsed time advances both ~equally.
    """
    return (wall_delta - mono_delta) > threshold


def evaluate_kill(
    *,
    now_wall: float,
    armed_wall: float,
    last_beat_wall: float,
    budget_s: float,
    stale_window_s: float,
    suspended: bool,
) -> Tuple[bool, str]:
    """Pure kill decision for one poll.

    Budget is wall-authoritative (fires across suspend, matching
    ``--max-wall-seconds`` semantics). Staleness fires only when NOT a suspend
    interval, so a host sleep never forges a wedge.
    """
    if (now_wall - armed_wall) >= budget_s:
        return True, "wall_budget_exceeded"
    if not suspended and (now_wall - last_beat_wall) >= stale_window_s:
        return True, "heartbeat_stale"
    return False, ""


def _read_beat(heartbeat_path: str) -> Optional[float]:
    """Read the parent's last wall timestamp; None on any error."""
    try:
        with open(heartbeat_path, "r") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def run_watchdog(
    target_pid: int,
    heartbeat_path: str,
    budget_s: float,
    stale_window_s: float,
    poll_s: float = 1.0,
    suspend_threshold_s: float = 5.0,
) -> str:
    """Sentinel loop (runs in the child process). Returns the kill reason.

    SIGKILLs ``target_pid`` when the budget or a real (non-suspend) staleness
    window is exceeded, then returns. Exits early if the parent already died.
    """
    armed_wall = time.time()
    armed_mono = time.monotonic()
    prev_wall = armed_wall
    prev_mono = armed_mono
    # If the parent never writes a beat, fall back to arm time so the budget
    # path still governs.
    while True:
        time.sleep(poll_s)
        now_wall = time.time()
        now_mono = time.monotonic()
        suspended = _suspend_detected(
            now_mono - prev_mono, now_wall - prev_wall, suspend_threshold_s,
        )
        prev_wall, prev_mono = now_wall, now_mono

        # Parent already gone? nothing to guard.
        try:
            os.kill(target_pid, 0)
        except ProcessLookupError:
            return "parent_exited"
        except PermissionError:
            pass  # alive, different ownership — keep guarding

        last_beat = _read_beat(heartbeat_path)
        if last_beat is None:
            last_beat = armed_wall  # no beat yet → budget still applies

        kill, reason = evaluate_kill(
            now_wall=now_wall, armed_wall=armed_wall, last_beat_wall=last_beat,
            budget_s=budget_s, stale_window_s=stale_window_s, suspended=suspended,
        )
        if kill:
            try:
                os.write(
                    2,
                    (
                        "\n[ExternalWatchdog] SIGKILL parent pid=%d "
                        "reason=%s (out-of-process, GIL-immune)\n"
                        % (target_pid, reason)
                    ).encode("ascii", "replace"),
                )
            except Exception:
                pass
            try:
                os.kill(target_pid, signal.SIGKILL)
            except OSError:
                pass
            return reason


class ExternalProcessWatchdog:
    """Manager wiring the parent to its out-of-process sentinel.

    Lifecycle: ``arm()`` once at boot, ``beat()`` each main-loop tick,
    ``disarm()`` on clean shutdown. The sentinel is a detached child process
    (``start_new_session=True``) so it survives parent thread wedges.
    """

    def __init__(
        self,
        target_pid: int,
        heartbeat_path: "Path",
        budget_s: float,
        stale_window_s: float,
        poll_s: float = 1.0,
    ) -> None:
        self.target_pid = int(target_pid)
        self.heartbeat_path = Path(heartbeat_path)
        self.budget_s = float(budget_s)
        self.stale_window_s = float(stale_window_s)
        self.poll_s = float(poll_s)
        self._proc: Optional["object"] = None

    def beat(self) -> None:
        """Atomically write the current wall timestamp to the heartbeat file."""
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.heartbeat_path.with_suffix(
                self.heartbeat_path.suffix + ".tmp"
            )
            tmp.write_text(repr(time.time()))
            os.replace(tmp, self.heartbeat_path)
        except OSError:
            pass  # best-effort — a missed beat just shortens the stale margin

    def arm(self) -> None:
        """Spawn the detached sentinel subprocess."""
        import subprocess

        self.beat()  # seed the heartbeat before the child starts polling
        self._proc = subprocess.Popen(
            [
                sys.executable, os.path.abspath(__file__), "--watch",
                str(self.target_pid), str(self.heartbeat_path),
                str(self.budget_s), str(self.stale_window_s), str(self.poll_s),
            ],
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach — survives parent thread wedges
        )

    def disarm(self) -> None:
        """Terminate the sentinel on clean shutdown."""
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()  # type: ignore[attr-defined]
            proc.wait(timeout=3)  # type: ignore[attr-defined]
        except Exception:
            try:
                proc.kill()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._proc = None


def _main(argv: list) -> int:
    # argv: --watch <pid> <heartbeat_path> <budget_s> <stale_window_s> [poll_s]
    if not argv or argv[0] != "--watch" or len(argv) < 5:
        return 2
    pid = int(argv[1])
    hb = argv[2]
    budget_s = float(argv[3])
    stale_window_s = float(argv[4])
    poll_s = float(argv[5]) if len(argv) >= 6 else 1.0
    run_watchdog(pid, hb, budget_s, stale_window_s, poll_s=poll_s)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
