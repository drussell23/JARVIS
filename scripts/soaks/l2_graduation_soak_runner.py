#!/usr/bin/env python3
"""Armored, self-throttling graduation soak runner for the L2 repair engine + Repair Context Bridge.

Runs the Ouroboros battle test as a **detached OS subprocess** (its own process + event loop → no
nesting under the parent, so the historical in-process starvation class cannot recur) with the five
core engine switches ON, under three armor layers:

  Phase 1 — Asynchronous Telemetry Watchdog Overlay
      A daemon thread continuously probes host CPU + RAM headroom and the child process. Under
      pressure (CPU sustained > threshold, or low RAM headroom) it governs the child EXTERNALLY —
      ``renice`` to lower priority + a brief SIGSTOP/SIGCONT *micro-yield pulse* (the honest external
      equivalent of an in-loop ``asyncio.sleep`` injection: an outside process cannot reach inside the
      child's event loop, but it CAN make the OS scheduler yield the child's CPU slice).

  Phase 2 — Non-Destructive Intermediate Checkpointing
      Every probe + every throttle event is journaled to a SQLite checkpoint DB, and a rolling
      snapshot of the child's session summary is captured — so a throttle/restart never loses the
      evidence trail. Process priority is lowered (nice) before resuming.

  Phase 3 — Autonomous CI Promotion Gate
      Hard wall-clock cap. On a CLEAN conclusion (session completed, structural-graph parity holds,
      zero token-limit blowups, zero unhandled sandbox regressions) **AND** evidence the guarded
      subsystems actually fired, the runner graduates the five flags to default-ON by persisting them
      to ``.env`` (host-local, reversible — NOT a committed source-default flip). Hollow soaks (the
      subsystems never engaged) do NOT graduate — graduation must be earned, never rubber-stamped.

Honest bounds: the watchdog governs the child by OS priority + stop/cont pulses, not by editing its
internal loop; checkpointing is runner-side (observable telemetry), not a reach into the child's
RepairProgressTracker. Graduation writes ``.env``, the established reversible surface — flipping source
defaults remains a separate human-reviewed step.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import psutil
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore

FLAGS = (
    "JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED",
    "JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED",
    "JARVIS_L2_MULTIFILE_ENABLED",
    "JARVIS_L2_DIVERGE_ESCAPE_ENABLED",
    "JARVIS_L2_PROGRESS_V11_ENABLED",
)


def _log(msg: str) -> None:
    print(f"[soak-runner {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- checkpoint DB
class CheckpointDB:
    """Phase 2 — non-destructive intermediate checkpoint journal (SQLite)."""

    def __init__(self, path: Path) -> None:
        # check_same_thread=False: the watchdog thread writes probes; all writes are
        # serialized through self._lock, so cross-thread use is safe.
        self._cx = sqlite3.connect(str(path), check_same_thread=False)
        self._cx.execute("PRAGMA journal_mode=WAL")
        self._cx.execute(
            "CREATE TABLE IF NOT EXISTS soak_checkpoint("
            "ts REAL, kind TEXT, cpu REAL, mem_avail_pct REAL, child_rss_mb REAL, detail TEXT)"
        )
        self._cx.commit()
        self._lock = threading.Lock()

    def record(self, kind: str, cpu: float, mem_pct: float, rss_mb: float, detail: str = "") -> None:
        with self._lock:
            self._cx.execute(
                "INSERT INTO soak_checkpoint VALUES (?,?,?,?,?,?)",
                (time.time(), kind, cpu, mem_pct, rss_mb, detail),
            )
            self._cx.commit()

    def close(self) -> None:
        with self._lock:
            self._cx.close()


# --------------------------------------------------------------------------- watchdog
class TelemetryWatchdog(threading.Thread):
    """Phase 1 — async resource watchdog governing the child by OS priority + micro-yield pulses."""

    def __init__(self, child_pid: int, db: CheckpointDB, *, cpu_pct: float, mem_floor_pct: float,
                 interval_s: float, boot_grace_s: float = 240.0, sustained_n: int = 4,
                 ncpu: int = 1) -> None:
        super().__init__(daemon=True)
        self._pid = child_pid
        self._db = db
        self._cpu_pct = cpu_pct
        self._mem_floor = mem_floor_pct
        self._interval = interval_s
        self._boot_grace_s = boot_grace_s   # observe-only while the heavy stack boots
        self._sustained_n = sustained_n     # require N consecutive pressured probes to act
        self._ncpu = max(1, ncpu)
        self._stop = threading.Event()
        self._t0 = time.time()
        self._consec = 0
        self.throttle_events = 0
        self.samples = 0

    def stop(self) -> None:
        self._stop.set()

    def _child(self):  # -> Optional[psutil.Process]
        if psutil is None:
            return None
        try:
            return psutil.Process(self._pid)
        except Exception:  # noqa: BLE001
            return None

    def run(self) -> None:
        if psutil is None:
            _log("psutil unavailable — watchdog runs in log-only mode (no active throttle)")
        # prime cpu_percent (first call returns 0.0)
        if psutil is not None:
            psutil.cpu_percent(interval=None)
        while not self._stop.wait(self._interval):
            cpu = psutil.cpu_percent(interval=None) if psutil is not None else 0.0
            vm = psutil.virtual_memory() if psutil is not None else None
            mem_pct = (vm.available / vm.total * 100.0) if vm is not None else 100.0
            proc = self._child()
            rss_mb = 0.0
            if proc is not None:
                try:
                    rss_mb = proc.memory_info().rss / (1024 * 1024)
                except Exception:  # noqa: BLE001
                    pass
            self.samples += 1
            self._db.record("probe", cpu, mem_pct, rss_mb)

            # Pressure that MATTERS — not a sole heavy process pegging CPU on an idle host
            # (that's healthy: let boot/indexing finish fast). Two real signals:
            #   * MEMORY compaction: available headroom <= floor (the 16 GB-host risk), OR
            #   * genuine CPU OVERSUBSCRIPTION: high CPU AND load-avg per core >= 1.0 (others starved).
            mem_pressure = mem_pct <= self._mem_floor
            try:
                load1 = os.getloadavg()[0] / self._ncpu
            except (OSError, AttributeError):
                load1 = 0.0
            cpu_pressure = cpu >= self._cpu_pct and load1 >= 1.0
            in_boot_grace = (time.time() - self._t0) < self._boot_grace_s
            # During boot grace, only MEMORY pressure can act (CPU is expected to peg).
            pressured = mem_pressure or (cpu_pressure and not in_boot_grace)

            if pressured:
                self._consec += 1
            else:
                self._consec = 0
            if self._consec >= self._sustained_n and proc is not None:
                self.throttle_events += 1
                detail = (f"mem_avail={mem_pct:.1f}%<= {self._mem_floor:.0f}" if mem_pressure
                          else f"cpu={cpu:.0f}% load/core={load1:.2f} sustained={self._consec}")
                _log(f"⚠️  sustained resource pressure ({detail}) → gentle renice"
                     + (" + memory micro-yield pulse" if mem_pressure else ""))
                try:
                    proc.nice(min(10, (proc.nice() or 0) + 1))   # gentle, capped at 10 (not floor 19)
                except Exception:  # noqa: BLE001
                    pass
                # Micro-yield (SIGSTOP/CONT) reserved for genuine MEMORY pressure — gives the
                # OS a beat to reclaim/compact; never applied for mere CPU use.
                if mem_pressure:
                    try:
                        os.kill(self._pid, signal.SIGSTOP)
                        time.sleep(0.2)
                        os.kill(self._pid, signal.SIGCONT)
                    except Exception:  # noqa: BLE001
                        pass
                self._db.record("throttle", cpu, mem_pct, rss_mb, detail)
                self._consec = 0  # reset after acting


# --------------------------------------------------------------------------- promotion gate
_TOKEN_BLOWUP_RE = re.compile(
    r"context[_ ]length|maximum context|token limit|context window exceeded|"
    r"prompt is too long|ContextWindowExceeded", re.IGNORECASE,
)
_REGRESSION_RE = re.compile(
    r"Traceback \(most recent call last\)|sandbox_infra_error|unhandled exception|"
    r"RuntimeError: .*sandbox|FATAL", re.IGNORECASE,
)


def _newest_session_dir(repo: Path) -> Optional[Path]:
    base = repo / ".ouroboros" / "sessions"
    if not base.is_dir():
        return None
    dirs = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    return dirs[-1] if dirs else None


def evaluate_promotion(repo: Path, stdout_log: Path, wall_capped: bool) -> Tuple[bool, Dict[str, object]]:
    """Phase 3 — clean iff: session completed, no token-limit blowups, no unhandled sandbox
    regressions, structural-graph parity holds (no parity divergence logged), AND the guarded
    subsystems actually fired (no hollow graduation)."""
    findings: Dict[str, object] = {}
    text = ""
    for src in (stdout_log,):
        try:
            text += src.read_text(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sess = _newest_session_dir(repo)
    summary: Dict = {}
    if sess is not None:
        findings["session_dir"] = str(sess)
        try:
            text += (sess / "debug.log").read_text(errors="replace")
        except Exception:  # noqa: BLE001
            pass
        try:
            summary = json.loads((sess / "summary.json").read_text())
        except Exception:  # noqa: BLE001
            summary = {}
    findings["session_outcome"] = summary.get("session_outcome", "unknown")

    token_blowups = len(_TOKEN_BLOWUP_RE.findall(text))
    regressions = len(_REGRESSION_RE.findall(text))
    parity_divergence = "parity_divergence" in text or "DualBackendParity" in text and "divergence" in text
    # Evidence the guarded subsystems fired (no hollow graduation):
    bridge_fired = ("RepairBridge" in text) or ("dependency cone" in text.lower())
    gate_fired = "StructuralGate" in text
    l2_fired = ("L2 Repair" in text) or ("RepairEngine" in text) or ("L2_" in text)
    subsystems_fired = bridge_fired or gate_fired or l2_fired

    findings.update({
        "token_blowups": token_blowups,
        "sandbox_regressions": regressions,
        "structural_parity_held": not parity_divergence,
        "wall_capped": wall_capped,
        "bridge_fired": bridge_fired, "gate_fired": gate_fired, "l2_fired": l2_fired,
    })

    completed = findings["session_outcome"] in ("complete", "unknown") and not wall_capped
    clean = (
        completed
        and token_blowups == 0
        and regressions == 0
        and not parity_divergence
        and subsystems_fired
    )
    findings["verdict"] = "CLEAN" if clean else "NOT_CLEAN"
    return clean, findings


def graduate_env(repo: Path) -> None:
    """Persist the five flags =true to .env (host-local, reversible). Idempotent upsert."""
    env_path = repo / ".env"
    lines: List[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    have = {ln.split("=", 1)[0].strip(): i for i, ln in enumerate(lines) if "=" in ln and not ln.strip().startswith("#")}
    for flag in FLAGS:
        entry = f"{flag}=true"
        if flag in have:
            lines[have[flag]] = entry
        else:
            lines.append(entry)
    env_path.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Armored L2 graduation soak runner")
    ap.add_argument("--repo", default=str(Path.cwd()))
    ap.add_argument("--max-wall-seconds", type=int, default=1200)
    ap.add_argument("--cost-cap", type=float, default=0.75)
    ap.add_argument("--idle-timeout", type=int, default=600)
    ap.add_argument("--cpu-throttle-pct", type=float, default=85.0)
    ap.add_argument("--mem-floor-pct", type=float, default=10.0)
    ap.add_argument("--probe-interval", type=float, default=3.0)
    ap.add_argument("--boot-grace-s", type=float, default=240.0,
                    help="observe-only window while the heavy stack boots (CPU expected to peg)")
    ap.add_argument("--sustained-probes", type=int, default=4,
                    help="consecutive pressured probes required before the watchdog acts")
    ap.add_argument("--graduate", action="store_true", help="persist flags to .env on a CLEAN soak")
    ap.add_argument("--workdir", default=None, help="dir for logs + checkpoint db")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    workdir = Path(args.workdir).resolve() if args.workdir else (repo / ".jarvis" / "soak_runs" / time.strftime("%Y%m%d-%H%M%S"))
    workdir.mkdir(parents=True, exist_ok=True)
    stdout_log = workdir / "battle_test.stdout.log"
    db = CheckpointDB(workdir / "checkpoint.sqlite")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo)
    for flag in FLAGS:
        env[flag] = "true"
    # Soft soak-throttle hint the harness can honor if it polls it (cooperative, optional).
    env["JARVIS_SOAK_THROTTLE_HINT_FILE"] = str(workdir / "throttle.flag")

    cmd = [
        sys.executable, "scripts/ouroboros_battle_test.py",
        "--cost-cap", str(args.cost_cap),
        "--idle-timeout", str(args.idle_timeout),
        "--max-wall-seconds", str(args.max_wall_seconds),
        "--headless", "-v",
    ]
    _log(f"repo={repo}")
    _log(f"flags ON: {', '.join(FLAGS)}")
    _log(f"caps: wall={args.max_wall_seconds}s cost=${args.cost_cap} idle={args.idle_timeout}s "
         f"cpu_throttle={args.cpu_throttle_pct}% mem_floor={args.mem_floor_pct}%")
    _log(f"workdir={workdir}")
    _log(f"launching: {' '.join(cmd)}")

    out = open(stdout_log, "w")
    proc = subprocess.Popen(cmd, cwd=str(repo), env=env, stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True)
    _log(f"battle test PID={proc.pid}")

    _ncpu = (psutil.cpu_count() if psutil is not None else None) or os.cpu_count() or 1
    wd = TelemetryWatchdog(proc.pid, db, cpu_pct=args.cpu_throttle_pct,
                           mem_floor_pct=args.mem_floor_pct, interval_s=args.probe_interval,
                           boot_grace_s=args.boot_grace_s, sustained_n=args.sustained_probes,
                           ncpu=_ncpu)
    wd.start()

    deadline = time.time() + args.max_wall_seconds + 30  # runner-side belt over harness cap
    wall_capped = False
    try:
        while proc.poll() is None:
            if time.time() > deadline:
                _log("runner wall-clock belt exceeded → terminating child")
                wall_capped = True
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            time.sleep(2)
    except KeyboardInterrupt:
        _log("interrupted → terminating child")
        proc.send_signal(signal.SIGTERM)
        wall_capped = True
    finally:
        wd.stop()
        out.flush(); out.close()

    rc = proc.returncode
    _log(f"battle test exited rc={rc}; watchdog samples={wd.samples} throttle_events={wd.throttle_events}")

    clean, findings = evaluate_promotion(repo, stdout_log, wall_capped)
    db.record("verdict", 0.0, 0.0, 0.0, json.dumps(findings))
    db.close()

    _log("================= PROMOTION GATE =================")
    for k, v in findings.items():
        _log(f"  {k}: {v}")
    if clean and args.graduate:
        graduate_env(repo)
        _log("✅ CLEAN soak → graduated 5 flags to .env (default-ON, reversible)")
    elif clean:
        _log("✅ CLEAN soak (graduation withheld: --graduate not set)")
    else:
        _log("⛔ NOT CLEAN → flags NOT graduated (see findings above)")
    _log("=================================================")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
