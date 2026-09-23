"""Terminal-state supervisor: every soak ends with a recorded reason.

The harness already records its own ending on every path it can see:
SIGTERM/SIGINT/SIGHUP handlers and an ``atexit`` fallback stamp
``summary.json``, and the kernel releases every ``flock`` a dead process
held. What no in-process hook can see is the death that never runs Python
again:

* **SIGKILL / the OOM killer** -- uncatchable by design.
* **The VM stopping under it** -- soak bt-2026-09-23-005910 was held alive by
  a ``wsl.exe`` belonging to an interactive session. When that session closed
  at 19:06 the whole WSL VM stopped (its journal ends with no shutdown
  sequence), and the session was left ``in_flight`` / ``stop_reason:
  unknown`` with nothing saying why.

So the evidence has to come from outside the daemon:

1. ``supervise(argv)`` runs the daemon as its CHILD. A parent gets the real
   wait status -- the signal number of a SIGKILL included -- forwards
   SIGTERM/SIGINT/SIGHUP so the harness's graceful path still runs, and
   afterwards writes ``terminal.json`` and stamps a still-unrecorded summary.
2. ``reconcile_orphans()`` runs before every supervised launch (and on
   demand). When the VM itself stopped, the supervisor died too; the next boot
   finds the session whose heartbeat went silent and places that heartbeat in
   the journal's boot list. A boot that ended right after it means the machine
   went away under the session (``host_vm_stopped``); a boot that ran on --
   including the current one -- means the process vanished uncatchably
   (``process_vanished``).

A RECORDED outcome always wins: nothing here overwrites a session that wrote
its own ending. Stdlib only, so it can supervise a daemon whose imports are
broken. NEVER raises out of the public helpers.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.TerminalSupervisor")

#: Mirrors ``session_archive._NON_TERMINAL_OUTCOMES``: what a summary says
#: while the session is (believed to be) running.
NON_TERMINAL_OUTCOMES = frozenset({"", "in_flight", "running", "unknown"})

_FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def stale_after_s() -> float:
    """Heartbeat silence after which no live daemon can own a session.

    Defaults to twice the harness's own out-of-process watchdog window
    (``JARVIS_EXTERNAL_WATCHDOG_STALE_S``, 120 s), which kills a daemon whose
    heartbeat is stale that long -- so a session silent for twice that has
    no living owner. Override: ``JARVIS_TERMINAL_SUPERVISOR_STALE_S``.
    """
    for name, scale in (("JARVIS_TERMINAL_SUPERVISOR_STALE_S", 1.0),
                        ("JARVIS_EXTERNAL_WATCHDOG_STALE_S", 2.0)):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                return max(30.0, float(raw) * scale)
            except ValueError:
                pass
    return 240.0


def boot_time() -> Optional[float]:
    """Epoch seconds at which this kernel booted (``/proc/stat`` btime)."""
    try:
        with open("/proc/stat", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _boot_last_entry(boot_id: str, timeout_s: float) -> Optional[float]:
    """The realtime stamp of a boot's LAST journal entry, read from the entries.

    ``--list-boots`` reports ``last_entry`` from the journal file header, which
    is never finalized when the machine stops uncleanly -- exactly the case
    this exists for. Measured: it put bt-2026-09-23-005910's boot at an 18:07
    end; the boot's own entries run to 19:06.
    """
    try:
        got = subprocess.run(
            ["journalctl", "-b", boot_id, "-n", "1", "-o", "json", "--no-pager"],
            capture_output=True, text=True, timeout=timeout_s,
        )
        line = (got.stdout or "").strip().splitlines()[-1]
        return int(json.loads(line)["__REALTIME_TIMESTAMP"]) / 1e6
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
        return None


def boot_windows(timeout_s: float = 10.0) -> Optional[List[Tuple[float, float]]]:
    """``(first_entry, last_entry)`` epoch seconds of every PAST boot the
    journal remembers, or None when the journal is unreadable.

    The CURRENT boot time alone cannot tell a stopped VM from a killed process:
    every session from any earlier boot has a heartbeat older than it. What
    settles it is when the boot the session ran in ENDED.
    """
    try:
        got = subprocess.run(
            ["journalctl", "--list-boots", "-o", "json", "--no-pager"],
            capture_output=True, text=True, timeout=timeout_s,
        )
        rows = json.loads(got.stdout or "null")
        if not isinstance(rows, list):
            return None
        out: List[Tuple[float, float]] = []
        for r in rows:
            if int(r.get("index", 0)) >= 0:
                continue                   # the current boot has not ended
            last = _boot_last_entry(str(r["boot_id"]), timeout_s)
            out.append((r["first_entry"] / 1e6, last if last is not None else r["last_entry"] / 1e6))
        return out
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def last_heartbeat(session_dir: Path, summary: Optional[Dict[str, Any]]) -> float:
    """The last instant the session proved it was alive (0.0 if never)."""
    try:
        beat = float((session_dir / "heartbeat.tick").read_text(encoding="utf-8").strip() or 0)
        if beat > 0:
            return beat
    except (OSError, ValueError):
        pass
    try:
        return float((summary or {}).get("last_activity_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def stamp_summary(session_dir: Path, fields: Dict[str, Any]) -> bool:
    """Merge *fields* into a summary that never recorded its own ending.

    False (and no write) when the summary is missing or already terminal.
    """
    path = session_dir / "summary.json"
    summary = _read_json(path)
    if summary is None:
        return False
    if str(summary.get("session_outcome") or "") not in NON_TERMINAL_OUTCOMES:
        return False
    summary.update(fields)
    try:
        _write_json_atomic(path, summary)
        return True
    except OSError:
        logger.warning("[TerminalSupervisor] could not stamp %s", path, exc_info=True)
        return False


def classify_orphan(
    session_dir: Path,
    summary: Dict[str, Any],
    *,
    now: float,
    btime: Optional[float],
    stale_s: float,
    boots: Optional[List[Tuple[float, float]]] = None,
) -> Optional[Dict[str, Any]]:
    """The recorded ending for an unrecorded session, or None if undecidable.

    None when the heartbeat is recent enough that a daemon may still own it,
    or when there is no heartbeat at all to reason from.

    * heartbeat inside the current boot -> ``process_vanished`` (the machine
      is still up; the process died without running Python again);
    * heartbeat in an earlier boot that ENDED within ``stale_s`` of it ->
      ``host_vm_stopped`` (the machine went away under the session);
    * earlier boot that outlived it -> ``process_vanished``;
    * no journal record of that boot -> ``vanished_cause_unknown``.
    """
    if str(summary.get("session_outcome") or "") not in NON_TERMINAL_OUTCOMES:
        return None
    beat = last_heartbeat(session_dir, summary)
    if beat <= 0 or now - beat < stale_s:
        return None
    evidence: Dict[str, Any] = {
        "last_heartbeat": beat,
        "silent_s": round(now - beat, 1),
        "boot_time": btime,
    }
    if btime is not None and beat >= btime:
        reason = "process_vanished"
        evidence["detail"] = (
            "heartbeat stopped inside the current boot with no recorded ending: "
            "an uncatchable kill (SIGKILL / OOM killer)"
        )
    else:
        # The boot it ran in: the latest one that had started by the heartbeat.
        home = max(
            (b for b in (boots or ()) if b[0] <= beat), key=lambda b: b[0], default=None,
        )
        if home is not None and beat - home[1] > stale_s:
            home = None                    # the journal lost that boot's tail
        if home is None:
            reason = "vanished_cause_unknown"
            evidence["detail"] = (
                "no readable journal record of the boot this session ran in "
                "(past retention, or this user cannot read system entries: "
                "add it to the systemd-journal group)"
            )
        else:
            evidence["boot_last_entry"] = home[1]
            outlived = home[1] - beat
            if outlived <= stale_s:
                reason = "host_vm_stopped"
                evidence["detail"] = (
                    f"its boot ended {max(outlived, 0):.0f}s after the last heartbeat: "
                    f"the machine went away under the session"
                )
            else:
                reason = "process_vanished"
                evidence["detail"] = (
                    f"its boot ran on {outlived:.0f}s after the last heartbeat: "
                    f"the process died, not the machine"
                )
    return {
        "session_outcome": "incomplete_kill",
        "stop_reason": reason,
        "reconciled_by": "terminal_supervisor",
        "reconciled_at": now,
        "terminal_evidence": evidence,
    }


def reconcile_orphans(
    sessions_root: Path,
    *,
    now: Optional[float] = None,
    btime: Optional[float] = None,
    stale_s: Optional[float] = None,
    boots: Optional[List[Tuple[float, float]]] = None,
    dry_run: bool = False,
) -> List[Tuple[str, str]]:
    """Stamp every session that stopped without recording why.

    Returns ``(session_id, stop_reason)`` per session stamped (or, with
    ``dry_run``, that would be).
    """
    out: List[Tuple[str, str]] = []
    try:
        dirs = sorted(p for p in Path(sessions_root).iterdir() if p.is_dir())
    except OSError:
        return out
    now = time.time() if now is None else now
    btime = boot_time() if btime is None else btime
    stale_s = stale_after_s() if stale_s is None else stale_s
    if boots is None:
        boots = boot_windows()
    for d in dirs:
        summary = _read_json(d / "summary.json")
        if summary is None:
            continue
        verdict = classify_orphan(
            d, summary, now=now, btime=btime, stale_s=stale_s, boots=boots,
        )
        if verdict is None:
            continue
        if dry_run or stamp_summary(d, verdict):
            out.append((d.name, verdict["stop_reason"]))
            logger.warning(
                "[TerminalSupervisor] reconciled %s stop_reason=%s (%s)",
                d.name, verdict["stop_reason"], verdict["terminal_evidence"]["detail"],
            )
    return out


def describe_exit(returncode: int) -> Tuple[str, Optional[str]]:
    """``(stop_reason, signal_name)`` for a child's Popen return code."""
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"SIG{-returncode}"
        return f"killed:{name}", name
    return f"exit:{returncode}", None


def oom_evidence(pid: int, timeout_s: float = 5.0) -> Optional[str]:
    """The kernel's OOM-kill line for *pid*, if the log is readable."""
    needle = f"Killed process {pid} "
    for argv in (["dmesg"], ["journalctl", "-k", "-o", "cat", "--no-pager", "-n", "2000"]):
        try:
            got = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in reversed(got.stdout.splitlines()):
            if needle in line:
                return line.strip()[:400]
    return None


def _session_started_by(sessions_root: Path, launched_at: float) -> Optional[Path]:
    """The session the child started: the latest start it recorded at or
    after launch. Directory times are no guide -- a reconcile rewrite bumps an
    old session's mtime too.

    The start comes from the in-flight summary's ``started_at`` or, failing
    that, from ``wall_deadline.json``'s mtime (written once at session start,
    never rewritten). The FINAL summary schema carries no ``started_at``:
    measured on bt-2026-09-23-172111, a lookup made after a clean exit found
    nothing and no terminal.json was written."""
    best: Tuple[float, Optional[Path]] = (0.0, None)
    try:
        dirs = [p for p in Path(sessions_root).iterdir() if p.is_dir()]
    except OSError:
        return None
    for d in dirs:
        try:
            started = float((_read_json(d / "summary.json") or {}).get("started_at") or 0)
        except (TypeError, ValueError):
            started = 0.0
        if started <= 0:
            try:
                started = (d / "wall_deadline.json").stat().st_mtime
            except OSError:
                continue
        if started >= launched_at - 1 and started > best[0]:
            best = (started, d)
    return best[1]


def _env_gb(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.lstat(os.path.join(root, fn)).st_size
            except OSError:
                pass
    return total


class DiskGuard:
    """Keeps a detached soak from running a disk out of space.

    Watches FREE BYTES, not percent used, on every volume the soak writes
    through: the guest root, and the host drive that backs WSL's ext4.vhdx.
    The guest's own ``df`` reports the virtual disk's capacity (1007 GB,
    788 GB free on 2026-09-23) while the Windows drive holding it had 483 GB
    free -- the host is the real limit, the same shape as the host-commit
    memory lesson.

    * free below the SOFT floor, or the log dir over its quota -> a forced
      ``ArtifactJanitor`` sweep (gzip aging logs, prune ancient ones) over the
      log dir and the session dirs, never touching the live session;
    * free below the HARD floor -> ``on_trip(reason)`` once: the supervisor
      SIGTERMs the daemon, whose own handler writes a graceful summary.

    Knobs (GB): ``JARVIS_DISK_GUARD_SOFT_FREE_GB`` (20),
    ``JARVIS_DISK_GUARD_HARD_FREE_GB`` (5), ``JARVIS_SOAK_LOG_QUOTA_GB`` (2);
    ``JARVIS_DISK_GUARD_INTERVAL_S`` (60); extra volumes via
    ``JARVIS_DISK_GUARD_VOLUMES`` (comma-separated, default ``/mnt/c``).
    """

    def __init__(
        self, *,
        volumes: Sequence[Path],
        sweep_dirs: Sequence[Path],
        log_dir: Optional[Path],
        on_trip,
        protect=lambda: (),
        free_probe=None,
        janitor_factory=None,
    ) -> None:
        self._volumes = [Path(v) for v in volumes if Path(v).exists()]
        self._sweep_dirs = [Path(d) for d in sweep_dirs]
        self._log_dir = log_dir
        self._on_trip = on_trip
        self._protect = protect
        self._free = free_probe or (lambda p: shutil.disk_usage(p).free)
        self._janitor_factory = janitor_factory
        self._tripped: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        gb = 1024 ** 3
        self.soft = _env_gb("JARVIS_DISK_GUARD_SOFT_FREE_GB", 20.0) * gb
        self.hard = _env_gb("JARVIS_DISK_GUARD_HARD_FREE_GB", 5.0) * gb
        self.quota = _env_gb("JARVIS_SOAK_LOG_QUOTA_GB", 2.0) * gb
        self.interval = _env_gb("JARVIS_DISK_GUARD_INTERVAL_S", 60.0) or 60.0
        self.sweeps: List[Dict[str, Any]] = []

    @property
    def tripped(self) -> Optional[str]:
        return self._tripped

    def _sweep(self, why: str) -> None:
        factory = self._janitor_factory
        if factory is None:
            try:  # stdlib-only module, imported lazily so the supervisor stays importable
                from backend.core.ouroboros.governance.artifact_janitor import ArtifactJanitor
            except Exception:  # noqa: BLE001
                logger.warning("[DiskGuard] janitor unavailable; cannot rotate (%s)", why)
                return
            factory = ArtifactJanitor
        janitor = factory(
            scan_dirs=[str(d) for d in self._sweep_dirs],
            compress_age_days=_env_gb("JARVIS_DISK_GUARD_COMPRESS_AGE_DAYS", 1.0),
            protect_paths=[str(p) for p in self._protect()],
        )
        report = janitor.sweep(force=True)
        report["why"] = why
        self.sweeps.append(report)
        logger.warning("[DiskGuard] swept (%s): compressed=%s deleted=%s freed=%.1f MB",
                       why, report.get("compressed"), report.get("deleted"),
                       report.get("freed_bytes", 0) / 1e6)

    def check_once(self) -> Optional[str]:
        """One evaluation; returns the action taken (None, 'sweep', 'trip')."""
        if self._tripped:
            return None
        try:
            frees = {str(v): self._free(v) for v in self._volumes}
        except OSError:
            return None
        low = min(frees.items(), key=lambda kv: kv[1], default=None)
        if low is not None and low[1] < self.hard:
            self._tripped = f"disk_guard:{low[0]}:{low[1] / 1024 ** 3:.1f}GB_free"
            logger.error("[DiskGuard] HARD floor breached (%s); stopping the soak", self._tripped)
            self._on_trip(self._tripped)
            return "trip"
        why = None
        if low is not None and low[1] < self.soft:
            why = f"soft_floor:{low[0]}"
        elif self._log_dir is not None and self._log_dir.is_dir() and _dir_bytes(self._log_dir) > self.quota:
            why = f"log_quota:{self._log_dir}"
        if why:
            self._sweep(why)
            return "sweep"
        return None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 — a guard must never take the supervisor down
                logger.warning("[DiskGuard] check failed", exc_info=True)

    def start(self) -> "DiskGuard":
        self._thread = threading.Thread(target=self._run, name="disk-guard", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


def _guard_volumes(sessions_root: Path) -> List[Path]:
    raw = os.environ.get("JARVIS_DISK_GUARD_VOLUMES", "/mnt/c")
    return [Path(sessions_root)] + [Path(v) for v in raw.split(",") if v.strip()]


def supervise(
    argv: Sequence[str], *, sessions_root: Path, cwd: Optional[str] = None,
    log_dir: Optional[Path] = None,
) -> int:
    """Run *argv* as a supervised child; returns its exit code (128+N for a signal)."""
    reconcile_orphans(sessions_root)
    launched_at = time.time()
    child = subprocess.Popen(list(argv), cwd=cwd)

    def _trip(_reason: str) -> None:
        try:
            child.send_signal(signal.SIGTERM)
        except OSError:
            pass

    seen: Dict[str, Path] = {}

    def _live_session() -> Optional[Path]:
        # Remembered once found: the end-of-run summary is a different schema.
        if "dir" not in seen:
            found = _session_started_by(sessions_root, launched_at)
            if found is not None:
                seen["dir"] = found
        return seen.get("dir")

    def _live_paths():
        live = _live_session()
        return [live] if live is not None else []

    guard = DiskGuard(
        volumes=_guard_volumes(sessions_root),
        sweep_dirs=[d for d in (log_dir, sessions_root) if d is not None],
        log_dir=log_dir, on_trip=_trip, protect=_live_paths,
    ).start()

    def _forward(signum, _frame):
        try:
            child.send_signal(signum)
        except OSError:
            pass

    previous = {s: signal.signal(s, _forward) for s in _FORWARDED}
    try:
        rc = child.wait()  # PEP 475: resumes after a forwarded signal
    finally:
        guard.stop()
        for s, h in previous.items():
            signal.signal(s, h)

    stop_reason, signame = describe_exit(rc)
    oom = oom_evidence(child.pid) if signame == "SIGKILL" else None
    if oom:
        stop_reason = "killed:oom"
    if guard.tripped:
        stop_reason = guard.tripped
    record: Dict[str, Any] = {
        "pid": child.pid,
        "argv": list(argv),
        "launched_at": launched_at,
        "ended_at": time.time(),
        "returncode": rc,
        "signal": signame,
        "stop_reason": stop_reason,
        "oom_evidence": oom,
        "disk_guard": {"tripped": guard.tripped, "sweeps": guard.sweeps},
    }
    session_dir = _live_session()
    if session_dir is not None:
        record["session_id"] = session_dir.name
        record["summary_stamped"] = stamp_summary(session_dir, {
            "session_outcome": "incomplete_kill",
            "stop_reason": stop_reason,
            "reconciled_by": "terminal_supervisor",
            "reconciled_at": record["ended_at"],
            "terminal_evidence": {k: record[k] for k in ("pid", "returncode", "signal", "oom_evidence")},
        })
        try:
            _write_json_atomic(session_dir / "terminal.json", record)
        except OSError:
            logger.warning("[TerminalSupervisor] could not write terminal.json", exc_info=True)
    logger.warning("[TerminalSupervisor] child pid=%d ended: %s", child.pid, stop_reason)
    return 128 - rc if rc < 0 else rc


def _default_sessions_root() -> Path:
    return Path.cwd() / ".ouroboros" / "sessions"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sessions-root", type=Path, default=None)
    ap.add_argument("--reconcile-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-dir", type=Path, default=None)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    root = args.sessions_root or _default_sessions_root()
    if args.reconcile_only:
        for sid, reason in reconcile_orphans(root, dry_run=args.dry_run):
            print(f"{sid} {reason}")
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("no command to supervise")
    return supervise(command, sessions_root=root, log_dir=args.log_dir)


if __name__ == "__main__":
    sys.exit(main())
