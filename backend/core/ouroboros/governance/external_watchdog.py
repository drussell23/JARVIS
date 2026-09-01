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

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

# Parent-process logger only — the sentinel CHILD stays logging-free by
# design (standalone subprocess, no logging config; it speaks via
# os.write(2, ...) so its lines land on the harness's inherited stderr).
logger = logging.getLogger("Ouroboros.ExternalWatchdog")


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


#: How often to re-log a run of consecutive beat-read failures. First
#: failure always logs; then every Nth, so a persistently unreadable path
#: is visible without flooding a 1Hz poller's stderr.
_READ_WARN_EVERY_N = 30


def _read_beat_ex(
    heartbeat_path: str,
) -> Tuple[Optional[float], Optional[BaseException]]:
    """Read the parent's last wall timestamp, and WHY if it could not be read.

    The exception is returned rather than swallowed because a read failure
    and a stale process are indistinguishable from the value alone -- and
    only one of them warrants killing a healthy organism. Post-mortem on
    bt-2026-09-01-173444 could not say whether the sentinel saw an OSError
    from the 9p mount or a malformed payload, because the reason was
    discarded at this line.
    """
    try:
        with open(heartbeat_path, "r") as f:
            return float(f.read().strip()), None
    except (OSError, ValueError) as exc:
        return None, exc


def _read_beat(heartbeat_path: str) -> Optional[float]:
    """Read the parent's last wall timestamp; None on any error.

    Thin wrapper over :func:`_read_beat_ex`, kept because callers and tests
    depend on the Optional[float] shape.
    """
    return _read_beat_ex(heartbeat_path)[0]


def format_poll_forensics(
    *,
    tag: str,
    now_wall: float,
    armed_wall: float,
    last_beat_wall: Optional[float],
    stale_window_s: float,
    budget_s: float,
    suspended: bool,
    wall_delta: float,
    mono_delta: float,
    poll_s: float,
    suppressed_polls: int,
    max_poll_drift_s: float,
) -> str:
    """Slice 29 — one dynamically-computed forensic line for the sentinel.

    Pure formatter (unit-testable): every value in the line is a live delta
    calculation, never a canned narrative. This is the instrumentation the
    bt-2026-07-15-233357 post-mortem lacked — the kill fired 915s after the
    beat froze instead of the 120s window and NOTHING recorded why (child
    scheduling drift? suspend-suppression? unreadable beat file?). Each
    emitted line answers that mathematically for its poll.
    """
    beat_age = (now_wall - last_beat_wall) if last_beat_wall is not None else -1.0
    return (
        "[ExternalWatchdog:%s] now_wall=%.3f beat_age_s=%.1f "
        "stale_window_s=%.0f budget_elapsed_s=%.1f/%.0f suspended=%s "
        "poll_wall_delta_s=%.2f poll_mono_delta_s=%.2f "
        "poll_drift_s=%.2f (expected=%.2f) "
        "suppressed_polls=%d max_poll_drift_s=%.2f"
        % (
            tag, now_wall, beat_age, stale_window_s,
            now_wall - armed_wall, budget_s, suspended,
            wall_delta, mono_delta,
            wall_delta - poll_s, poll_s,
            suppressed_polls, max_poll_drift_s,
        )
    )


def run_watchdog(
    target_pid: int,
    heartbeat_path: str,
    budget_s: float,
    stale_window_s: float,
    poll_s: float = 1.0,
    suspend_threshold_s: float = 5.0,
    status_every_s: float = 60.0,
) -> str:
    """Sentinel loop (runs in the child process). Returns the kill reason.

    SIGKILLs ``target_pid`` when the budget or a real (non-suspend) staleness
    window is exceeded, then returns. Exits early if the parent already died.

    Slice 29 forensics — the loop now EMITS its delta math (stderr, inherited
    from the harness so lines land in the session output) at exactly the
    moments that matter, all values computed live:
      * a poll whose staleness would fire but is SUPPRESSED (suspended=True);
      * a poll whose own scheduling drifted (slept far past ``poll_s`` — the
        App-Nap/timer-coalescing class that can delay a kill for minutes);
      * a periodic status line every ``status_every_s`` while the beat is
        older than half the stale window (quiet when healthy);
      * the kill itself, with the full computation that justified it.
    """
    armed_wall = time.time()
    armed_mono = time.monotonic()
    prev_wall = armed_wall
    prev_mono = armed_mono
    suppressed_polls = 0
    max_poll_drift_s = 0.0
    last_status_wall = armed_wall

    def _emit(line: str) -> None:
        try:
            os.write(2, ("\n" + line + "\n").encode("ascii", "replace"))
        except Exception:
            pass

    # If the parent never writes a beat, fall back to arm time so the budget
    # path still governs.
    #: Last beat we actually READ. Distinct from `armed_wall`: liveness once
    #: proven is not un-proven by a filesystem hiccup.
    last_known_good_beat: Optional[float] = None
    read_failures = 0

    while True:
        time.sleep(poll_s)
        now_wall = time.time()
        now_mono = time.monotonic()
        wall_delta = now_wall - prev_wall
        mono_delta = now_mono - prev_mono
        suspended = _suspend_detected(
            mono_delta, wall_delta, suspend_threshold_s,
        )
        prev_wall, prev_mono = now_wall, now_mono
        poll_drift = wall_delta - poll_s
        if poll_drift > max_poll_drift_s:
            max_poll_drift_s = poll_drift

        # Parent already gone? nothing to guard.
        try:
            os.kill(target_pid, 0)
        except ProcessLookupError:
            return "parent_exited"
        except PermissionError:
            pass  # alive, different ownership — keep guarding

        last_beat, beat_read_exc = _read_beat_ex(heartbeat_path)
        if last_beat is not None:
            if read_failures:
                _emit(
                    "[ExternalWatchdog:beat-read-recovered] after %d "
                    "consecutive failure(s)" % read_failures
                )
            last_known_good_beat = last_beat
            read_failures = 0
        else:
            read_failures += 1
            # Name the actual I/O fault BEFORE staleness is evaluated. A
            # sentinel that kills without saying what it could not read
            # leaves a post-mortem unable to separate "the organism hung"
            # from "one read glitched".
            if (
                read_failures == 1
                or read_failures % _READ_WARN_EVERY_N == 0
            ):
                _emit(
                    "[ExternalWatchdog:beat-read-failed] consecutive=%d "
                    "path=%s %s: %s"
                    % (
                        read_failures, heartbeat_path,
                        type(beat_read_exc).__name__, beat_read_exc,
                    )
                )

        # A transient read failure must NOT erase known liveness. The old
        # fallback went straight to `armed_wall`, so ONE unreadable poll at
        # any point past `stale_window_s` computed an age equal to the whole
        # session and killed instantly: soak bt-2026-09-01-173444 died at
        # 2645s on its FIRST failed read, 4ms after a successful beat, with
        # zero write failures, on a 9p mount where `os.replace` is not
        # reliably atomic no matter how correct the writer is.
        #
        # `armed_wall` remains the fallback for a parent that has never
        # beaten AT ALL, which is what that fallback was actually for --
        # the budget path still governs a parent that never starts beating.
        if last_beat is not None:
            effective_beat = last_beat
        elif last_known_good_beat is not None:
            effective_beat = last_known_good_beat
        else:
            effective_beat = armed_wall

        kill, reason = evaluate_kill(
            now_wall=now_wall, armed_wall=armed_wall,
            last_beat_wall=effective_beat,
            budget_s=budget_s, stale_window_s=stale_window_s,
            suspended=suspended,
        )

        def _forensics(tag: str) -> str:
            return format_poll_forensics(
                tag=tag, now_wall=now_wall, armed_wall=armed_wall,
                last_beat_wall=last_beat, stale_window_s=stale_window_s,
                budget_s=budget_s, suspended=suspended,
                wall_delta=wall_delta, mono_delta=mono_delta, poll_s=poll_s,
                suppressed_polls=suppressed_polls,
                max_poll_drift_s=max_poll_drift_s,
            )

        beat_age = now_wall - effective_beat
        stale_condition = beat_age >= stale_window_s
        if stale_condition and suspended and not kill:
            # The exact blind spot of bt-2026-07-15-233357: staleness true
            # but suppressed. Every suppression is now on the record.
            suppressed_polls += 1
            _emit(_forensics("stale-suppressed"))
        elif poll_drift >= max(poll_s * 5.0, suspend_threshold_s):
            # This poll's own sleep overshot badly — child scheduling drift
            # (App Nap / timer coalescing) delays kill decisions; record it.
            _emit(_forensics("poll-drift"))
        elif (
            beat_age >= stale_window_s / 2.0
            and (now_wall - last_status_wall) >= status_every_s
        ):
            last_status_wall = now_wall
            _emit(_forensics("beat-aging"))

        if kill:
            _emit(_forensics("kill:" + reason))
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
        # Slice 29 — beat-write visibility counters (see beat()).
        self.beat_failures: int = 0
        self.beat_successes: int = 0
        self._beat_consecutive_failures: int = 0
        try:
            self._BEAT_WARN_EVERY_N = max(1, int(os.environ.get(
                "JARVIS_WATCHDOG_BEAT_WARN_EVERY_N", "10",
            )))
        except (TypeError, ValueError):
            self._BEAT_WARN_EVERY_N = 10
        self._proc: Optional["object"] = None

    def beat(self) -> None:
        """Atomically write the current wall timestamp to the heartbeat file.

        Slice 29 — best-effort BUT never silent (bt-2026-07-15-233357: the
        tick froze 15min before a false-positive heartbeat_stale SIGKILL and
        post-mortem could not distinguish task death from persistently
        swallowed write faults). A failed write still never raises into the
        beat caller, but it now logs a rate-limited WARNING with the actual
        exception (first failure + every Nth thereafter), and the first
        success after a failure run logs the recovery. ``beat_failures`` /
        ``beat_successes`` counters are public for tests/diagnostics.
        """
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.heartbeat_path.with_suffix(
                self.heartbeat_path.suffix + ".tmp"
            )
            tmp.write_text(repr(time.time()))
            os.replace(tmp, self.heartbeat_path)
        except OSError as exc:
            self.beat_failures += 1
            self._beat_consecutive_failures += 1
            # Rate limit: always the 1st of a failure run, then every Nth.
            if (
                self._beat_consecutive_failures == 1
                or self._beat_consecutive_failures % self._BEAT_WARN_EVERY_N == 0
            ):
                logger.warning(
                    "[ExternalWatchdog] heartbeat WRITE FAILED "
                    "(consecutive=%d total=%d path=%s): %s: %s — beats are "
                    "the sentinel's ONLY liveness signal; sustained failure "
                    "ends in a heartbeat_stale SIGKILL of a healthy process",
                    self._beat_consecutive_failures, self.beat_failures,
                    self.heartbeat_path, type(exc).__name__, exc,
                )
        else:
            if self._beat_consecutive_failures > 0:
                logger.warning(
                    "[ExternalWatchdog] heartbeat write RECOVERED after "
                    "%d consecutive failure(s)",
                    self._beat_consecutive_failures,
                )
            self._beat_consecutive_failures = 0
            self.beat_successes += 1

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
