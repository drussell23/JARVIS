#!/usr/bin/env python3
"""Autonomous Wedge Diagnostic Harness — prove the Resource Governor end-to-end.

Single command, zero babysitting. Orchestrates the full two-run proof:

  Run 1  (THE WEDGE)  — Governor OFF, Death Rattle ON (capture-only).
                        Run the omni soak, let it wedge (or hit the wall),
                        harvest pre_oom_autopsy.log + the continuous resource
                        stream, extract peak pressure.
  Run 2  (THE FEAST)  — JARVIS_RESOURCE_GOVERNOR_ENABLED=1 (umbrella ON).
                        Re-run, watch the peaks flatten and the throttle engage.

Then print a Verdict Matrix comparing Run 1 (wedge) vs Run 2 (managed).

Each run spins up scripts/resource_blackbox_local.py as a background subprocess
(its stdout redirected to a per-run log) for clean per-run telemetry
segmentation. The soak's own stdout streams live to your console so you can
watch it happen.

Usage:
    python3 scripts/run_wedge_diagnostics.py [--wall 600] [--interval 1.0]
                                             [--keep-going]

Exit code: 0 if the proof is conclusive (Run 2 flattened the Run 1 peak),
1 if inconclusive (e.g. Run 1 didn't wedge locally), 2 on harness error.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO = Path(__file__).resolve().parent.parent
_SESSIONS_ROOT = _REPO / ".ouroboros" / "sessions"
_BLACKBOX = _REPO / "scripts" / "resource_blackbox_local.py"
_BATTLE = _REPO / "scripts" / "ouroboros_battle_test.py"
_DIAG_DIR = _REPO / "logs" / "wedge_diagnostics"

# ---------------------------------------------------------------------------
# Pure parsers / analysis (unit-tested) — no subprocess, no I/O side effects
# ---------------------------------------------------------------------------

_RSS_RE = re.compile(r"rss=([-\d.]+)MB")
_FREE_RE = re.compile(r"free=([-\d.]+)%")
_CPU_RE = re.compile(r"cpu=([-\d.]+)%")
_CTX_RE = re.compile(r"ctx=([-\d.]+)/s")
_SWAP_RE = re.compile(r"swap=([-\d.]+)MB")
_PAGEOUTS_RE = re.compile(r"pageouts=(\d+)")


def parse_blackbox_peaks(lines: List[str]) -> Dict[str, float]:
    """Extract peak pressure from a run's resource_blackbox_local stream.

    Peak = worst observed: max RSS, max cpu, max ctx-rate, max swap, max
    pageouts, MIN free%. Ignores the -1.0 probe-failure sentinels.
    """
    peak = {
        "peak_rss_mb": 0.0, "min_free_pct": 100.0, "peak_cpu_pct": 0.0,
        "peak_ctx_rate": 0.0, "peak_swap_mb": 0.0, "peak_pageouts": 0.0,
        "samples": 0.0,
    }
    for line in lines:
        m = _RSS_RE.search(line)
        if m:
            v = float(m.group(1))
            if v >= 0:
                peak["peak_rss_mb"] = max(peak["peak_rss_mb"], v)
        m = _FREE_RE.search(line)
        if m:
            v = float(m.group(1))
            if v >= 0:
                peak["min_free_pct"] = min(peak["min_free_pct"], v)
        m = _CPU_RE.search(line)
        if m:
            v = float(m.group(1))
            if v >= 0:
                peak["peak_cpu_pct"] = max(peak["peak_cpu_pct"], v)
        m = _CTX_RE.search(line)
        if m:
            v = float(m.group(1))
            if v >= 0:
                peak["peak_ctx_rate"] = max(peak["peak_ctx_rate"], v)
        m = _SWAP_RE.search(line)
        if m:
            v = float(m.group(1))
            if v >= 0:
                peak["peak_swap_mb"] = max(peak["peak_swap_mb"], v)
        m = _PAGEOUTS_RE.search(line)
        if m:
            peak["peak_pageouts"] = max(peak["peak_pageouts"], float(m.group(1)))
        if "rss=" in line:
            peak["samples"] += 1
    return peak


def summarize_autopsy(text: str) -> Dict[str, Any]:
    """Parse a pre_oom_autopsy.log body. Returns whether a death rattle
    fired, the peak per-process RSS in the snapshot table, and the count of
    thread-stack frames captured."""
    fired = "PRE-OOM DEATH RATTLE" in text and "END DEATH RATTLE" in text
    # RSS table lines look like:  "12345 812MB python3.11"
    rss_rows = re.findall(r"^\s*(\d+)\s+(\d+)MB\s+(.+)$", text, re.MULTILINE)
    peak_proc_rss = max((int(r[1]) for r in rss_rows), default=0)
    n_procs = len(rss_rows)
    n_frames = text.count("File \"") + text.count("File '")
    return {
        "rattle_fired": fired,
        "peak_proc_rss_mb": peak_proc_rss,
        "n_procs_in_snapshot": n_procs,
        "n_stack_frames": n_frames,
    }


def detect_throttle(debug_log: str) -> Dict[str, Any]:
    """Scan a run's debug.log for evidence the Governor engaged: redline
    fires, memory-pressure cap clamps, and can_fanout throttle markers."""
    return {
        "redline_fired": "REDLINE" in debug_log
        or "resource_governor_redline" in debug_log,
        "fanout_capped": "memory_pressure_gate.capped_to" in debug_log,
        "pressure_cap_fired": "ProcessMemoryWatchdog] CAP" in debug_log,
        "stagger_held": "stagger hold timeout" in debug_log
        or "[ResourceGovernor]" in debug_log,
    }


def compute_verdict(
    run1: Dict[str, Any], run2: Dict[str, Any],
) -> Dict[str, Any]:
    """Decide whether Run 2 flattened Run 1's pressure. Conclusive only if
    Run 1 actually showed elevated pressure (a real wedge/spike to beat)."""
    p1 = run1["peaks"]  # type: ignore[index]
    p2 = run2["peaks"]  # type: ignore[index]
    r1_wedged = bool(run1.get("autopsy", {}).get("rattle_fired")) or \
        run1.get("stop_reason") in ("process_memory_cap", "resource_governor_redline")
    # Did the managed run reduce the worst pressures?
    rss_drop = p1["peak_rss_mb"] - p2["peak_rss_mb"]
    ctx_drop = p1["peak_ctx_rate"] - p2["peak_ctx_rate"]
    free_gain = p2["min_free_pct"] - p1["min_free_pct"]
    flattened = (rss_drop > 0 or ctx_drop > 0 or free_gain > 0)
    throttle_engaged = any(run2.get("throttle", {}).values())
    if not r1_wedged and p1["peak_rss_mb"] < 1.0:
        verdict = "INCONCLUSIVE: Run 1 produced no telemetry/wedge"
        conclusive = False
    elif not r1_wedged:
        verdict = ("INCONCLUSIVE: Run 1 did not wedge locally "
                   "(no autopsy / no cap) — wedge may be cloud-specific")
        conclusive = False
    elif flattened or throttle_engaged:
        verdict = "PROVEN: Run 2 flattened the spike / throttle engaged"
        conclusive = True
    else:
        verdict = "FAILED: Run 2 did not reduce pressure vs Run 1"
        conclusive = False
    return {
        "verdict": verdict, "conclusive": conclusive,
        "run1_wedged": r1_wedged, "throttle_engaged": throttle_engaged,
        "rss_drop_mb": rss_drop, "ctx_drop": ctx_drop,
        "free_gain_pct": free_gain,
    }


def render_matrix(run1: Dict[str, Any], run2: Dict[str, Any],
                  verdict: Dict[str, Any]) -> str:
    p1 = run1["peaks"]  # type: ignore[index]
    p2 = run2["peaks"]  # type: ignore[index]

    def row(label: str, a, b, fmt="{:.1f}") -> str:
        sa = fmt.format(a) if isinstance(a, (int, float)) else str(a)
        sb = fmt.format(b) if isinstance(b, (int, float)) else str(b)
        return f"  {label:<22} {sa:>16} {sb:>16}"

    sep = "  " + "-" * 56
    lines = [
        "",
        "  " + "=" * 56,
        "  WEDGE DIAGNOSTIC — VERDICT MATRIX",
        "  " + "=" * 56,
        f"  {'METRIC':<22} {'RUN 1 (WEDGE)':>16} {'RUN 2 (FEAST)':>16}",
        sep,
        row("peak RSS (MB)", p1["peak_rss_mb"], p2["peak_rss_mb"]),
        row("peak ctx-rate (/s)", p1["peak_ctx_rate"], p2["peak_ctx_rate"]),
        row("min free (%)", p1["min_free_pct"], p2["min_free_pct"]),
        row("peak cpu (%)", p1["peak_cpu_pct"], p2["peak_cpu_pct"]),
        row("peak swap (MB)", p1["peak_swap_mb"], p2["peak_swap_mb"]),
        row("stop_reason", run1.get("stop_reason", "?"),
            run2.get("stop_reason", "?"), "{}"),
        row("rattle fired", run1.get("autopsy", {}).get("rattle_fired", False),
            run2.get("autopsy", {}).get("rattle_fired", False), "{}"),
        row("throttle engaged", "-", verdict["throttle_engaged"], "{}"),
        sep,
        f"  Δ peak RSS reduced : {verdict['rss_drop_mb']:.1f} MB",
        f"  Δ peak ctx reduced : {verdict['ctx_drop']:.1f} /s",
        f"  Δ free%   gained   : {verdict['free_gain_pct']:.1f} %",
        sep,
        f"  VERDICT: {verdict['verdict']}",
        "  " + "=" * 56,
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration (subprocess side effects)
# ---------------------------------------------------------------------------

def _snapshot_sessions() -> set:
    try:
        return {p.name for p in _SESSIONS_ROOT.iterdir() if p.is_dir()}
    except FileNotFoundError:
        return set()


def _find_new_session(before: set) -> Optional[Path]:
    after = _snapshot_sessions()
    new = sorted(
        (_SESSIONS_ROOT / n for n in (after - before)),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if new:
        return new[0]
    # Fallback: newest session dir by mtime (run may have reused a dir).
    try:
        dirs = sorted(
            (p for p in _SESSIONS_ROOT.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        return dirs[0] if dirs else None
    except FileNotFoundError:
        return None


def _start_blackbox(out_path: Path, interval: float) -> subprocess.Popen:
    fh = open(out_path, "w")
    proc = subprocess.Popen(
        [sys.executable, str(_BLACKBOX), "--interval", str(interval),
         "--log", str(out_path.with_suffix(".tee.log"))],
        stdout=fh, stderr=subprocess.STDOUT, cwd=str(_REPO),
    )
    proc._diag_fh = fh  # type: ignore[attr-defined]
    return proc


def _stop(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        if proc is not None:
            fh = getattr(proc, "_diag_fh", None)
            if fh:
                fh.close()
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass
    fh = getattr(proc, "_diag_fh", None)
    if fh:
        try:
            fh.close()
        except Exception:
            pass


def run_phase(name: str, extra_env: Dict[str, str], wall_s: float,
              interval: float) -> Dict[str, Any]:
    _DIAG_DIR.mkdir(parents=True, exist_ok=True)
    bb_out = _DIAG_DIR / f"{name}_blackbox.log"
    print(f"\n[WedgeDiag] === {name} === starting resource streamer + soak "
          f"(wall={wall_s:.0f}s)")
    bb = _start_blackbox(bb_out, interval)
    before = _snapshot_sessions()
    env = dict(os.environ)
    env.update(extra_env)
    start = time.monotonic()
    rc: Optional[int] = None
    try:
        soak = subprocess.Popen(
            [sys.executable, str(_BATTLE), "--headless",
             "--max-wall-seconds", str(int(wall_s)), "-v"],
            cwd=str(_REPO), env=env,
        )
        # Generous margin over the wall cap for boot + teardown.
        try:
            rc = soak.wait(timeout=wall_s + 240)
        except subprocess.TimeoutExpired:
            print(f"[WedgeDiag] {name}: soak overran wall+240s — terminating")
            soak.terminate()
            try:
                rc = soak.wait(timeout=15)
            except subprocess.TimeoutExpired:
                soak.kill()
                rc = soak.wait()
    finally:
        _stop(bb)
    elapsed = time.monotonic() - start
    session = _find_new_session(before)
    print(f"[WedgeDiag] {name}: soak exited rc={rc} after {elapsed:.0f}s; "
          f"session={session.name if session else '<none>'}")

    # Harvest telemetry + artifacts.
    try:
        bb_lines = bb_out.read_text(errors="replace").splitlines()
    except Exception:
        bb_lines = []
    peaks = parse_blackbox_peaks(bb_lines)
    autopsy: Dict[str, Any] = {"rattle_fired": False}
    throttle: Dict[str, Any] = {}
    stop_reason = "?"
    if session is not None:
        ap = session / "pre_oom_autopsy.log"
        if ap.exists():
            autopsy = summarize_autopsy(ap.read_text(errors="replace"))
        dl = session / "debug.log"
        if dl.exists():
            throttle = detect_throttle(dl.read_text(errors="replace"))
        sm = session / "summary.json"
        if sm.exists():
            try:
                import json
                stop_reason = json.loads(sm.read_text()).get(
                    "stop_reason", "?")
            except Exception:
                pass
    return {
        "name": name, "rc": rc, "elapsed_s": elapsed,
        "session": str(session) if session else None,
        "peaks": peaks, "autopsy": autopsy, "throttle": throttle,
        "stop_reason": stop_reason,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wall", type=float, default=600.0,
                    help="max-wall-seconds per run (default 600)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="resource streamer sample interval (default 1.0s)")
    ap.add_argument("--keep-going", action="store_true",
                    help="run Run 2 even if Run 1 didn't wedge")
    args = ap.parse_args()

    if not _BLACKBOX.exists() or not _BATTLE.exists():
        print(f"[WedgeDiag] FATAL: missing {_BLACKBOX} or {_BATTLE}",
              file=sys.stderr)
        return 2

    # Run 1 — THE WEDGE: Governor OFF, Death Rattle ON (capture-only).
    run1 = run_phase(
        "run1_baseline",
        {"JARVIS_RESOURCE_GOVERNOR_ENABLED": "",
         "JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED": "1"},
        args.wall, args.interval,
    )
    r1_wedged = bool(run1["autopsy"].get("rattle_fired")) or \
        run1["stop_reason"] in ("process_memory_cap", "resource_governor_redline")
    if not r1_wedged and not args.keep_going:
        print("\n[WedgeDiag] Run 1 did not wedge locally (no autopsy/cap). "
              "The wedge may be cloud-specific (disk/IAP/spot). "
              "Re-run with --keep-going to force Run 2 anyway.")
        # Still print a one-sided matrix for the record.
        run2 = {"name": "run2_skipped", "peaks": parse_blackbox_peaks([]),
                "autopsy": {"rattle_fired": False}, "throttle": {},
                "stop_reason": "skipped"}
        verdict = compute_verdict(run1, run2)
        print(render_matrix(run1, run2, verdict))
        return 1

    # Run 2 — THE FEAST: umbrella flag ON (all pieces engage).
    run2 = run_phase(
        "run2_governed",
        {"JARVIS_RESOURCE_GOVERNOR_ENABLED": "1"},
        args.wall, args.interval,
    )

    verdict = compute_verdict(run1, run2)
    print(render_matrix(run1, run2, verdict))
    return 0 if verdict["conclusive"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[WedgeDiag] interrupted", file=sys.stderr)
        raise SystemExit(130)
