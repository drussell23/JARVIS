"""Keep one test from taking down the runner, or poisoning the tests after it.

Measured on 2026-09-26, all on unchanged main:

* a harness watchdog armed by a failed test fired ``os._exit(75)`` minutes
  later and ended the whole pytest process with no summary;
* two runs launched from one shell died at the same instant — a process-group
  kill reached the runner's own group;
* a timed-out test left ``.git/index.lock`` behind, and later git-using tests
  failed on it;
* leftover children (spawned without their own session) outlived their test.

Three mechanisms, each acting ONLY in the runner's own process. A
``multiprocessing`` fork child inherits these patches and must still be able to
``os._exit``, so every check first asks "am I the runner?".

**Why not a per-test SIGALRM.** pytest-timeout (``--timeout-method=signal``)
already owns SIGALRM per test, and a process has one handler per signal: a
second handler would silently disable the timeout. The harness watchdog's
``os._exit`` is a deliberate PRODUCTION safety net (a wedged shutdown must
die); it is fenced here only while tests run, never removed.
"""
from __future__ import annotations

import os
import signal
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

_ENV_MODE = "JARVIS_TEST_ISOLATION_MODE"          # report (default) | strict
_ENV_REAP_GRACE_S = "JARVIS_TEST_REAP_GRACE_S"


def isolation_mode() -> str:
    raw = (os.environ.get(_ENV_MODE, "") or "").strip().lower()
    return raw if raw in ("report", "strict") else "report"


def _reap_grace_s() -> float:
    try:
        return max(0.0, float(os.environ.get(_ENV_REAP_GRACE_S, "") or 3.0))
    except ValueError:
        return 3.0


def _caller(skip: int = 2) -> str:
    frames = traceback.extract_stack()[:-skip]
    mine = [f for f in frames
            if "/lib/python3" not in f.filename and "site-packages" not in f.filename]
    pick = (mine or frames)[-4:]
    return " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}"
                       for f in reversed(pick))


# ---------------------------------------------------------------------------
# 1. The runner fence
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    what: str
    detail: str
    thread: str
    caller: str

    def render(self) -> str:
        return f"{self.what}({self.detail}) in thread {self.thread!r}: {self.caller}"


class RunnerFence:
    """Refuses, inside the runner process, the calls that end the runner:
    ``os._exit``, a signal aimed at the runner's own process group, and a
    process-ending signal aimed at the runner's own pid.

    A refused ``os._exit`` raises ``SystemExit`` in the calling thread — the
    thread ends (a watchdog thread simply stops) and the test that armed it is
    reported, instead of the process vanishing mid-suite. A refused group kill
    returns without signalling. Both are recorded against the current test.
    """

    def __init__(self) -> None:
        self.runner_pid = os.getpid()
        self.runner_pgrp = os.getpgrp()
        self.violations: List[Violation] = []
        self._lock = threading.Lock()
        self._installed = False
        self._real: Dict[str, Callable] = {}

    def _is_runner(self) -> bool:
        return os.getpid() == self.runner_pid

    def _record(self, what: str, detail: str) -> None:
        with self._lock:
            self.violations.append(Violation(
                what, detail, threading.current_thread().name, _caller(3)))

    def _hits_runner_group(self, pid: int) -> bool:
        return pid == 0 or (pid < 0 and -pid == self.runner_pgrp)

    @staticmethod
    def _would_end_the_runner(sig: int) -> bool:
        """A self-directed signal that would kill or freeze the runner.

        SIGKILL / SIGSTOP always (uncatchable). Any other signal only while it
        is at its DEFAULT disposition and that default ends the process — a
        test that installs a handler and then signals itself to exercise it
        is legitimate and passes through. The harness's shutdown-deadline
        reaper sends ``SIGKILL`` to its own pid 25 s after a test arms it and
        never disarms it; that ended a whole run with rc=137 (2026-09-26).
        """
        if sig in (signal.SIGKILL, signal.SIGSTOP):
            return True
        harmless = {signal.SIGCHLD, signal.SIGURG, signal.SIGWINCH, signal.SIGCONT}
        try:
            return sig not in harmless and signal.getsignal(sig) in (signal.SIG_DFL, None)
        except (ValueError, OSError):
            return False

    def install(self) -> None:
        if self._installed:
            return
        self._real = {"_exit": os._exit, "killpg": os.killpg, "kill": os.kill}
        real = self._real

        def _exit(code: int = 0) -> None:
            if not self._is_runner():
                real["_exit"](code)
            self._record("os._exit", str(code))
            raise SystemExit(code)

        def killpg(pgid: int, sig: int) -> None:
            if self._is_runner() and pgid == self.runner_pgrp:
                self._record("os.killpg", f"runner group {pgid}, {signal.Signals(sig).name}")
                return None
            return real["killpg"](pgid, sig)

        def kill(pid: int, sig: int) -> None:
            if self._is_runner():
                if self._hits_runner_group(pid):
                    self._record("os.kill", f"{pid} (runner group), {signal.Signals(sig).name}")
                    return None
                if pid == self.runner_pid and self._would_end_the_runner(sig):
                    self._record("os.kill", f"{pid} (the runner), {signal.Signals(sig).name}")
                    return None
            return real["kill"](pid, sig)

        os._exit, os.killpg, os.kill = _exit, killpg, kill
        self._installed = True

    def uninstall(self) -> None:
        if self._installed:
            os._exit = self._real["_exit"]
            os.killpg = self._real["killpg"]
            os.kill = self._real["kill"]
            self._installed = False

    def real_killpg(self) -> Callable:
        return self._real.get("killpg", os.killpg)

    def drain(self) -> List[Violation]:
        with self._lock:
            out, self.violations = self.violations, []
        return out


# ---------------------------------------------------------------------------
# 2. Leaked children
# ---------------------------------------------------------------------------


def _multiprocessing_owned() -> Set[int]:
    """Children multiprocessing owns and manages itself — pool workers and
    the resource tracker are process-wide singletons that outlive any test by
    design; reaping them breaks every later test that uses the pool."""
    pids: Set[int] = set()
    try:
        import multiprocessing
        pids.update(p.pid for p in multiprocessing.active_children() if p.pid)
    except Exception:  # noqa: BLE001
        pass
    try:
        from multiprocessing import resource_tracker
        pid = getattr(resource_tracker._resource_tracker, "_pid", None)
        if pid:
            pids.add(pid)
    except Exception:  # noqa: BLE001
        pass
    return pids


def child_pids() -> Set[int]:
    try:
        import psutil
        return {c.pid for c in psutil.Process().children(recursive=True)}
    except Exception:  # noqa: BLE001
        return set()


@dataclass
class ReapReport:
    reaped: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.reaped)


def reap_new_children(before: Set[int], fence: Optional[RunnerFence] = None) -> ReapReport:
    """Terminate children that appeared during a test and are still alive.

    Each leak is signalled through ITS OWN process group when it leads one
    that is not the runner's, else as a single process — never the runner's
    group. SIGTERM, a grace (``JARVIS_TEST_REAP_GRACE_S``), then SIGKILL.
    NEVER raises.
    """
    report = ReapReport()
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return report
    runner_pgrp = os.getpgrp()
    killpg = fence.real_killpg() if fence is not None else os.killpg
    exempt = _multiprocessing_owned()
    try:
        leaked = [p for p in psutil.Process().children(recursive=True)
                  if p.pid not in before and p.pid not in exempt]
    except Exception:  # noqa: BLE001
        return report
    if not leaked:
        return report

    def _signal(p: "psutil.Process", sig: int) -> None:
        try:
            pgid = os.getpgid(p.pid)
            if pgid == p.pid and pgid != runner_pgrp:
                killpg(pgid, sig)
            else:
                p.send_signal(sig)
        except (ProcessLookupError, psutil.NoSuchProcess, PermissionError):
            pass

    for p in leaked:
        try:
            report.reaped.append(f"{p.pid} {' '.join(p.cmdline())[:120]}")
        except Exception:  # noqa: BLE001
            report.reaped.append(str(p.pid))
        _signal(p, signal.SIGTERM)
    _, alive = psutil.wait_procs(leaked, timeout=_reap_grace_s())
    for p in alive:
        _signal(p, signal.SIGKILL)
    psutil.wait_procs(alive, timeout=_reap_grace_s())
    return report


# ---------------------------------------------------------------------------
# 3. Stale git locks
# ---------------------------------------------------------------------------

#: Lock files git leaves when a writer dies mid-update. Refs locks live under
#: refs/ and are per-ref; these are the repository-wide ones every command
#: that touches the index or HEAD must acquire.
_GIT_LOCK_NAMES: Tuple[str, ...] = ("index.lock", "HEAD.lock", "config.lock",
                                    "shallow.lock", "packed-refs.lock")


def git_admin_dirs(repo_root: Path) -> List[Path]:
    """The repository's git dir plus every linked worktree's admin dir.
    Read once per session; the worktree set does not change under a test."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
        )
        common = Path(out.stdout.strip())
        if out.returncode != 0 or not str(common):
            return []
        if not common.is_absolute():
            common = (repo_root / common).resolve()
    except Exception:  # noqa: BLE001
        return []
    dirs = [common]
    wt = common / "worktrees"
    if wt.is_dir():
        dirs.extend(d for d in wt.iterdir() if d.is_dir())
    return dirs


def lock_holders(lock: Path) -> Optional[List[int]]:
    """PIDs holding ``lock`` open, via ``/proc/*/fd``. ``None`` when this
    platform has no ``/proc`` — the caller must then NOT sweep, since
    absence of evidence is not evidence of a dead holder.

    Git writes a lock through the file descriptor it opened with O_EXCL and
    keeps it open until it renames the lock into place, so "some live
    process has it open" is exactly "the lock is in use". The lock file
    itself carries no PID.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    target = str(lock)
    holders: List[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    if os.readlink(fd) == target:
                        holders.append(int(entry.name))
                        break
                except OSError:
                    continue
        except OSError:
            continue           # exited, or not ours to inspect
    return holders


@dataclass
class SweepReport:
    removed: List[str] = field(default_factory=list)
    held: List[str] = field(default_factory=list)
    unverifiable: List[str] = field(default_factory=list)


def sweep_git_locks(admin_dirs: Iterable[Path]) -> SweepReport:
    """Unlink every repository-wide git lock no live process holds.

    Cheap when clean — one ``stat`` per candidate; ``/proc`` is only scanned
    when a lock actually exists. A lock with a live holder is left alone and
    reported; on a platform without ``/proc`` nothing is removed. NEVER
    raises.
    """
    report = SweepReport()
    for d in admin_dirs:
        for name in _GIT_LOCK_NAMES:
            lock = d / name
            try:
                if not lock.exists():
                    continue
            except OSError:
                continue
            holders = lock_holders(lock)
            if holders is None:
                report.unverifiable.append(str(lock))
            elif holders:
                report.held.append(f"{lock} (pid {', '.join(map(str, holders))})")
            else:
                try:
                    lock.unlink()
                    report.removed.append(str(lock))
                except FileNotFoundError:
                    pass                # its writer finished between checks
                except OSError:
                    report.held.append(f"{lock} (unlink refused)")
    return report


__all__ = [
    "ReapReport", "RunnerFence", "SweepReport", "Violation",
    "child_pids", "git_admin_dirs", "isolation_mode", "lock_holders",
    "reap_new_children", "sweep_git_locks",
]
